"""编排入口：build_analyze()（analyze 接口的唯一业务入口）

流程：文本提取 → 解析器探测（由调用方注入，见下）→ 引擎建议 → 画像 → 决策矩阵

**probe_fn 为什么是回调**：解析器探测是网络 IO，且路由层测试通过 monkeypatch
`routers.documents.smart_parse.probe_parsers` 打桩。把"调用哪个探测函数"交给
路由传进来，测试的补丁路径就不用改（`build_analyze(..., probe_fn=probe_parsers)`
在调用时才查路由模块的全局名）。

容错：任一步失败只丢那一步（部分画像 + warning，接口恒 200）。画像与推荐
分开兜底——推荐失败时 plan 为 None，前端按"无推荐"处理。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from backend.chunking import order_systems
from backend.config import get_active_config
from backend.services.smart_parse.engine import suggest_engine
from backend.services.smart_parse.extract import extract_for_profile
from backend.services.smart_parse.plan import PlanInput, build_plan
from backend.services.smart_parse.profiling import (analyze_length,
                                                    analyze_qa,
                                                    analyze_reference_density,
                                                    analyze_structure,
                                                    safe_analyze)
from backend.services.smart_parse.sheets import analyze_spreadsheet
from backend.services.smart_parse.types import (AnalyzeReport, is_spreadsheet)

logger = logging.getLogger(__name__)

# 无探测结果时的引擎建议（探测失败/未注入）
_ENGINE_FALLBACK = {"suggested": "auto",
                    "reason": "解析器探测失败，使用默认引擎", "probe": None}


async def build_analyze(*, doc_id: str, file_type: str, path: Path,
                        doc_name: str = "",
                        probe_fn: Optional[Callable] = None) -> AnalyzeReport:
    """画像 + 入库方案（analyze 接口的唯一编排入口）

    - probe_fn：解析器探测协程（通常是 parsers.probe.probe_parsers）。
      表格文档不需要（跳过探测），传了也不会调。
    """
    warnings: list[str] = []

    # ---- 表格文档：跳过文本结构画像与解析器探测（秒出，无 LLM 成本）----
    if is_spreadsheet(file_type):
        profile: dict[str, dict] = {"length": {}, "structure": {}, "qa": {},
                                    "reference_density": {}}
        try:
            profile["spreadsheet"] = analyze_spreadsheet(path)
        except Exception as e:
            logger.warning("表格画像失败 %s: %s", doc_name, e)
            warnings.append(f"表格画像失败: {e}")
        engine = suggest_engine(file_type, {})
        inp = PlanInput.from_profile(file_type=file_type,
                                     engine=engine["suggested"],
                                     profile=profile, kind="spreadsheet")
        return AnalyzeReport(
            doc_id=doc_id, file_type=file_type, extracted=True,
            extract_warning=None, engine_suggestion=engine, profile=profile,
            plan=_safe_plan(inp, warnings), warnings=warnings)

    # ---- 1) 文本提取（失败返回部分画像，不中断）----
    text, extracted, docx_probe, extract_warning = await extract_for_profile(
        path, file_type)
    if extract_warning:
        warnings.append(extract_warning)

    # ---- 2) 引擎建议（探测由调用方注入；失败降级为 auto，不影响整体）----
    probe = None
    if probe_fn is not None:
        try:
            probe = await probe_fn()
        except Exception as e:
            logger.warning("智能解析解析器探测失败: %s", e)
            warnings.append(f"解析器探测失败: {e}")
    if probe is None:
        engine = dict(_ENGINE_FALLBACK)
    else:
        engine = suggest_engine(
            file_type, probe, docx_probe,
            # 转换服务是否已配：老版 .doc 无样式时靠它转 PDF 交 MinerU
            gotenberg_ready=bool(
                (get_active_config().gotenberg.base_url or "").strip()))
        engine["probe"] = probe

    # ---- 3) 画像分析（每步独立容错）----
    threshold = int(get_active_config().contextual_retrieval.max_full_doc_chars)
    length = safe_analyze(lambda t: analyze_length(t, threshold), text,
                          warnings, "篇幅")
    structure = safe_analyze(analyze_structure, text, warnings, "标题结构")
    if docx_probe:
        # docx 结构探测（规范性判定，引擎建议用）并入标题结构画像
        structure["docx_structure"] = docx_probe
        _merge_docx_headings(structure, docx_probe)
    qa = safe_analyze(analyze_qa, text, warnings, "QA 格式")
    density = safe_analyze(analyze_reference_density, text, warnings, "指代密集度")

    profile = {"length": length, "structure": structure, "qa": qa,
               "reference_density": density}

    logger.info("智能解析画像: %s %s 提取=%s 字数=%s 标题=%s QA=%s",
                doc_name or path.name, file_type, extracted,
                length.get("doc_chars"), structure.get("has_headings"),
                qa.get("is_qa"))

    # ---- 4) 决策矩阵（纯计算；失败只丢推荐，画像照常返回）----
    inp = PlanInput.from_profile(file_type=file_type,
                                 engine=engine["suggested"], profile=profile)
    return AnalyzeReport(
        doc_id=doc_id, file_type=file_type, extracted=extracted,
        extract_warning=extract_warning, engine_suggestion=engine,
        profile=profile, plan=_safe_plan(inp, warnings), warnings=warnings)


def _merge_docx_headings(structure: dict, docx_probe: dict) -> None:
    """把 Word 样式标题并进标题结构画像（就地更新）

    **为什么必须并**：文本正则看不见 OOXML 的样式/大纲层级——docx 提取出的纯
    文本里标题就是一行普通字（没有 #、没有编号），于是同一份文档会出现「引擎
    建议说检测到规范标题样式、标题结构却说无结构」的自相矛盾，并连锁推错切块
    方式（naive 而非层级聚合）、关掉父标题拼接、误推花钱的上下文检索。

    判据与引擎建议统一取 is_normative（见 engine.suggest_engine），两处口径
    一致才不会再次打架。

    计数口径：heading_count / numbered_headings 保持文本正则的原语义不动，
    样式标题另记 style_headings（总数由 PlanInput.heading_total 汇总）——样式
    标题与编号式标题可能指同一批标题，混进 heading_count 会重复计数。
    """
    if not docx_probe.get("is_normative"):
        return
    structure["has_headings"] = True
    structure["style_headings"] = int(docx_probe.get("style_headings") or 0)
    # examples 用样式标题打头：它们在纯文本里看不出是标题，探测结果才是权威
    examples = list(docx_probe.get("style_heading_examples") or [])
    for t in structure.get("examples") or []:
        if t not in examples:
            examples.append(t)
    structure["examples"] = examples[:5]
    systems = _merge_systems(structure.get("heading_systems") or [],
                             docx_probe.get("style_heading_systems") or [])
    structure["heading_systems"] = systems
    structure["suggested_systems"] = order_systems([r["system"] for r in systems])


def _merge_systems(base: list, extra: list) -> list:
    """编号体系命中归并（按 system 合并，按命中数降序）

    hits 取两条通道的**较大值**而非相加：样式标题与文本正则扫的是同一批标题，
    相加会把同一批命中算两遍；examples 互补，各留最多 3 个（与检测函数同口径）。
    """
    merged: dict[str, dict] = {}
    for r in list(base) + list(extra):
        name = r.get("system")
        if not name:
            continue
        cur = merged.get(name)
        if cur is None:
            merged[name] = {
                "system": name, "label": r.get("label", name),
                "hits": int(r.get("hits") or 0),
                "examples": list(r.get("examples") or [])[:3]}
            continue
        cur["hits"] = max(cur["hits"], int(r.get("hits") or 0))
        for e in r.get("examples") or []:
            if len(cur["examples"]) < 3 and e not in cur["examples"]:
                cur["examples"].append(e)
    return sorted(merged.values(), key=lambda r: -r["hits"])


def _safe_plan(inp: PlanInput, warnings: list[str]):
    """决策矩阵容错：失败记 warning 并返回 None（前端按"无推荐"处理）"""
    try:
        return build_plan(inp)
    except Exception as e:
        logger.exception("智能解析推荐生成失败")
        warnings.append(f"推荐生成失败: {e}")
        return None
