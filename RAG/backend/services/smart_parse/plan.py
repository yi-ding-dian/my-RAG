"""决策矩阵：画像 + 引擎建议 → ParsePlan（完整入库方案）

**规则引擎只自动定"免费"的**（切块方式 / 解析引擎 / 切块参数 / 不花钱的开关）：
这些零成本、确定、可解释，自动选没问题。花钱的 LLM 环节（上下文检索、
知识图谱、图片摘要、Agentic）只给推荐值或备选位，是否开启由用户按预算决定
——规则引擎替用户决定花钱，这事不该干。

**方案 A（本次修复的核心）**：矩阵吃进"引擎建议"。此前 `_recommend` 的入参
只有画像，永远推不出 `hierarchical`，于是前端自己叠了层"引擎是 docx_struct
就强制选层级聚合"的特判，导致 Step1 面板/Step2 徽标/默认选中三处打架。现在
引擎建议是矩阵的正式输入，三处都从这一个 plan 派生。

config 的每个键都经 `_emit` 写入并断言在 IngestRequest 的字段集内——契约违反
在构造期就炸，而不是变成前端的 422。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.config import get_active_config
from backend.models.rag_models import IngestRequest
from backend.services.ingestion.params import (_MAX_AGENTIC_TEXT_CHARS,
                                               _MAX_AGENTIC_TEXT_CHARS_HARD,
                                               method_defaults)
from backend.services.smart_parse import cost as cost_mod
from backend.services.smart_parse.profiling import format_chars
from backend.services.smart_parse.types import (Decision, MethodRecommendation,
                                                ParsePlan, is_parsed,
                                                label_for)

logger = logging.getLogger(__name__)

# 切块参数的中文理由（键与 method_defaults 返回的键对应；未登记的键回退键名）。
# 参数值本身由 ingestion.params.method_defaults 供给——那里才是唯一来源
_PARAM_REASONS = {
    "chunk_size": "块大小预算（取系统配置的当前值）",
    "overlap": "块间重叠（取系统配置的当前值）",
    "split_level": "按 2 级标题（#/##）切块",
    "parent_chunk_size": "超长单节兜底切分用（父块本身无大小上限，章节完整性优先）",
    "parent_chunk_overlap": "超长单节兜底切分的重叠",
    "parent_split_level": "父块聚合到 2 级标题（#/## 为章节边界）",
    "retrieval_mode": "检索命中子块后返回父块全文作上下文（父子的意义所在）",
}

# 表格文档的理由与文本不同（同样取值，但含义是"单张表"而非"整篇"）
_SHEET_PARAM_REASONS = {
    "split_level": "按 Sheet 分节标题切块",
    "chunk_size": "单张表超长时的兜底切分大小",
    "overlap": "兜底切分的块间重叠",
}


@dataclass(frozen=True)
class PlanInput:
    """决策矩阵的输入（文本文档与表格文档归一后同构）"""
    kind: str = "text"                 # "text" | "spreadsheet"
    file_type: str = ""
    engine: str = "auto"               # engine_suggestion.suggested
    doc_chars: int = 0
    paragraphs: int = 0
    threshold_chars: int = 0
    over_threshold: bool = False
    image_refs: int = 0
    has_headings: bool = False
    heading_count: int = 0
    numbered_headings: int = 0
    is_normative: bool = False
    is_qa: bool = False
    qa_pairs: int = 0
    qa_ratio: float = 0.0
    sheet_count: int = 0
    total_rows: int = 0
    merged_cells: int = 0

    @property
    def heading_total(self) -> int:
        return self.heading_count + self.numbered_headings

    @classmethod
    def from_profile(cls, *, file_type: str, engine: str,
                     profile: dict, kind: str = "text") -> "PlanInput":
        """从画像 dict 组装（build 编排用；画像缺失的段按空处理）"""
        length = profile.get("length") or {}
        structure = profile.get("structure") or {}
        qa = profile.get("qa") or {}
        sheet = profile.get("spreadsheet") or {}
        docx_probe = structure.get("docx_structure") or {}
        return cls(
            kind=kind, file_type=file_type, engine=engine,
            doc_chars=int(length.get("doc_chars") or 0),
            paragraphs=int(length.get("paragraphs") or 0),
            threshold_chars=int(length.get("threshold_chars") or 0),
            over_threshold=bool(length.get("over_threshold")),
            image_refs=int(length.get("image_refs") or 0),
            has_headings=bool(structure.get("has_headings")),
            heading_count=int(structure.get("heading_count") or 0),
            numbered_headings=int(structure.get("numbered_headings") or 0),
            is_normative=bool(docx_probe.get("is_normative")),
            is_qa=bool(qa.get("is_qa")),
            qa_pairs=int(qa.get("qa_pairs") or 0),
            qa_ratio=float(qa.get("ratio") or 0.0),
            sheet_count=int(sheet.get("sheet_count") or 0),
            total_rows=int(sheet.get("total_rows") or 0),
            merged_cells=int(sheet.get("merged_cells") or 0),
        )


class _Builder:
    """config 累加器：每写一个键就记一条带理由的决策，并校验字段合法"""

    def __init__(self) -> None:
        self.config: dict = {}
        self.decisions: list[Decision] = []

    def emit(self, key: str, value, reason: str, source: str = "rule",
             billable: bool = False) -> None:
        if key not in IngestRequest.model_fields:
            raise ValueError(f"入库配置不存在该字段: {key}（见 IngestRequest）")
        self.config[key] = value
        self.decisions.append(Decision(key=key, value=value, reason=reason,
                                       source=source, billable=billable))

    def note(self, key: str, value, reason: str, billable: bool = True) -> None:
        """只记决策、不写 config（花钱的开关：给推荐值，由用户决定是否落到请求里）"""
        self.decisions.append(Decision(key=key, value=value, reason=reason,
                                       source="rule", billable=billable))


def _ctx_reason(inp: PlanInput) -> tuple[bool, str]:
    """上下文检索开关：推荐值 + 理由（超阈值时不可开启，不只是不推荐）"""
    if inp.threshold_chars and inp.doc_chars > inp.threshold_chars:
        return False, (
            f"文档 {format_chars(inp.doc_chars)} 超过完整文档阈值 "
            f"{format_chars(inp.threshold_chars)}，开启会导致入库失败，不可开启")
    if inp.has_headings:
        return False, "有标题结构，建议优先利用标题/父块上下文而非 LLM 摘要"
    return True, (f"无标题且文档 {format_chars(inp.doc_chars)} ≤ 阈值 "
                  f"{format_chars(inp.threshold_chars)}，"
                  "上下文检索增强可为孤立块补全局背景")


def _agentic_rec(inp: PlanInput) -> MethodRecommendation:
    """Agentic 备选项：文案里的字数上限由 params 常量拼出，不再手抄字符串"""
    return MethodRecommendation(
        method="agentic", label=label_for("agentic"), recommended=False,
        badge="可选",
        reason=(f"可选：LLM 语义切分逻辑段落（"
                f"{format_chars(_MAX_AGENTIC_TEXT_CHARS)}~"
                f"{format_chars(_MAX_AGENTIC_TEXT_CHARS_HARD)} 需确认，超 "
                f"{format_chars(_MAX_AGENTIC_TEXT_CHARS_HARD)} 不支持；"
                f"与上下文检索增强互斥）"))


def _plan_text(inp: PlanInput, b: _Builder, *, defaults: dict) -> tuple[
        MethodRecommendation, list[MethodRecommendation]]:
    """文本文档分支：QA / 层级聚合 / 父子分块 / 通用切块"""
    alternatives: list[MethodRecommendation] = []

    if inp.is_qa:
        method = "qa"
        main = MethodRecommendation(
            method=method, label=label_for(method), recommended=True,
            badge="推荐",
            reason=(f"检测到 QA 问答格式（问答对 {inp.qa_pairs} 组，"
                    f"占比 {inp.qa_ratio * 100:.0f}%），问答对整块入库最合适"))
        b.emit("method", method, main.reason)
        if inp.has_headings:
            alternatives.append(MethodRecommendation(
                method="parent_child", label=label_for("parent_child"),
                recommended=False,
                reason="备选：文档同时有标题结构，可按章节父子分块"))
        ctx_on, ctx_reason = False, "QA 问答对自带上下文，无需上下文检索增强"
    elif inp.has_headings and inp.engine == "docx_struct":
        # 方案 A：规范标题样式 + 结构解析（层级由 OOXML 直读、精确）→ 层级聚合。
        # 此前这条规则只活在前端特判里，后端矩阵不知道，于是推荐与默认选中打架
        method = "hierarchical"
        main = MethodRecommendation(
            method=method, label=label_for(method), recommended=True,
            badge="推荐",
            reason=(f"检测到 {inp.heading_total} 个标题且解析引擎为结构解析"
                    "（标题层级由 OOXML 直读、精确）——层级聚合切块按章节"
                    "自底向上聚合，块边界落在标题之间，块首自带祖先标题链"))
        b.emit("method", method, main.reason)
        alternatives.append(MethodRecommendation(
            method="parent_child", label=label_for("parent_child"),
            recommended=False,
            reason="备选：父块聚合章节、子块精细切分，检索返回父块完整上下文"))
        alternatives.append(MethodRecommendation(
            method="title", label=label_for("title"), recommended=False,
            reason="备选：按标题直接切块，结构简单时更轻量"))
        ctx_on, ctx_reason = _ctx_reason(inp)
    elif inp.has_headings:
        method = "parent_child"
        main = MethodRecommendation(
            method=method, label=label_for(method), recommended=True,
            badge="推荐",
            reason=(f"检测到 {inp.heading_total} 个标题，父子分块父块聚合"
                    "章节、子块精细切分，检索返回父块完整上下文"))
        b.emit("method", method, main.reason)
        alternatives.append(MethodRecommendation(
            method="title", label=label_for("title"), recommended=False,
            reason="备选：按标题直接切块，结构简单时更轻量"))
        ctx_on, ctx_reason = _ctx_reason(inp)
    else:
        method = "naive"
        main = MethodRecommendation(
            method=method, label=label_for(method), recommended=True,
            badge="推荐", reason="未检测到标题结构，通用递归字符切块最稳妥")
        b.emit("method", method, main.reason)
        ctx_on, ctx_reason = _ctx_reason(inp)

    # 切块参数：由 ingestion.params.method_defaults 统一供给（数值只有一个来源）
    for key, value in defaults.get(method, {}).items():
        b.emit(key, value, _PARAM_REASONS.get(key, key), "default")

    # 包含父标题：层级聚合的块自带祖先标题链，再叠一遍会重复（service 侧也会
    # 显式跳过），所以此处推荐 False——否则等于推荐一个不生效的开关
    heading_on = inp.has_headings and method != "hierarchical"
    h_reason = ("有标题结构，块首拼上父标题路径，检索命中后可知章节归属"
                if heading_on else
                ("层级聚合的块首已自带祖先标题链，无需再拼" if method == "hierarchical"
                 else "无标题结构，无父标题可拼"))
    b.emit("enable_heading_in_content", heading_on, h_reason)
    b.emit("contextual_retrieval", ctx_on, ctx_reason, source="profile")
    return main, alternatives


def _plan_spreadsheet(inp: PlanInput, b: _Builder, *, defaults: dict) -> tuple[
        MethodRecommendation, list[MethodRecommendation]]:
    """表格文档分支：按 Sheet 分节切块（每张表独立成块）"""
    alternatives: list[MethodRecommendation] = []
    sheets = inp.sheet_count or 1
    rows_note = f"、合计 {inp.total_rows} 行" if inp.total_rows else ""
    main = MethodRecommendation(
        method="title", label=label_for("title"), recommended=True, badge="推荐",
        reason=(f"表格文档共 {sheets} 个 Sheet{rows_note}，按 Sheet 分节"
                "（## Sheet: 名称）切块，每张表独立成块，检索语义最精准"))
    b.emit("method", "title", main.reason)
    # 取值同文本 title 分支，但理由按"单张表"表述
    for key, value in defaults.get("title", {}).items():
        b.emit(key, value, _SHEET_PARAM_REASONS.get(key, key), "default")
    b.emit("enable_heading_in_content", False, "表格按 Sheet 分节，无需拼标题路径")
    b.emit("contextual_retrieval", False, "表格自带结构上下文，无需 LLM 摘要增强",
           source="profile")
    alternatives.append(MethodRecommendation(
        method="naive", label=label_for("naive"), recommended=False,
        reason="备选：通用字符切块（管道表格块仍原子保护）"))
    alternatives.append(MethodRecommendation(
        method="parent_child", label=label_for("parent_child"),
        recommended=False, reason="备选：父子分块，父块含完整表格"))
    return main, alternatives


def build_plan(inp: PlanInput, *, cfg=None) -> ParsePlan:
    """决策矩阵出口：画像 + 引擎建议 → 完整入库方案"""
    if cfg is None:
        cfg = get_active_config()
    defaults = method_defaults(cfg)

    b = _Builder()
    if inp.kind == "spreadsheet":
        main, alternatives = _plan_spreadsheet(inp, b, defaults=defaults)
    else:
        # 解析引擎：仅对走解析器的类型写入（pdf/docx/doc）；auto 等于没建议
        if is_parsed(inp.file_type) and inp.engine not in ("", "auto"):
            b.emit("parser_engine", inp.engine,
                   f"按文件类型与解析器可用性建议「{inp.engine}」",
                   source="engine")
        main, alternatives = _plan_text(inp, b, defaults=defaults)

    alternatives.append(_agentic_rec(inp))

    switches = {
        "contextual_retrieval": bool(b.config.get("contextual_retrieval")),
        "enable_heading_in_content": bool(
            b.config.get("enable_heading_in_content")),
        # 花钱的开关：规则引擎一律不推荐（默认关），由用户在预算档位里决定
        "knowledge_graph": False,
        "image_summary": False,
        "agentic": True,
    }
    b.note("knowledge_graph", False,
           "知识图谱需为每个块调用 LLM 抽取实体关系，产生额外费用——按需开启")
    b.note("image_summary", False,
           "图片摘要需为每张图调用视觉模型，产生额外费用——按需开启")

    cost = cost_mod.estimate(
        kind=inp.kind, profile=_profile_of(inp), config=b.config,
        switches=switches, engine=inp.engine, cfg=cfg)

    return ParsePlan(
        config=b.config, decisions=b.decisions, cost=cost,
        alternatives=[main] + alternatives, switches=switches,
        engine=inp.engine)


def _profile_of(inp: PlanInput) -> dict:
    """成本预估需要的画像切片（复用 PlanInput 的字段，不重新扫描文本）"""
    return {
        "length": {
            "doc_chars": inp.doc_chars,
            "threshold_chars": inp.threshold_chars,
            "image_refs": inp.image_refs,
        },
        "structure": {"heading_count": inp.heading_count},
        "qa": {"is_qa": inp.is_qa, "qa_pairs": inp.qa_pairs},
        "spreadsheet": {"sheet_count": inp.sheet_count},
    }
