"""智能解析引导：文档画像分析接口（独立模块，不碰现有手动解析）

- GET /api/kbs/{kb_id}/documents/{doc_id}/analyze（can_manage_kb）：
  对文档做规则画像分析——未解析文档也能分析（轻量本地文本提取：
  txt/md 直读、pdf 用 pypdf、docx 用 python-docx、doc 经 LibreOffice 转
  docx（见 _extract_docx_like），不调 MinerU/DeepDoc 外部服务），返回画像
  （格式/引擎建议/标题结构/篇幅/QA/指代密集度） + 切块方式/上下文检索推荐，
  供前端 4 步引导向导（SmartParseWizard）展示。确定后由向导生成
  parser_config 调现有 POST /{doc_id}/ingest 解析（ingest 接口零改动）。
- docx/doc 附加结构探测（_probe_docx_structure）：样式标题覆盖率（outlineLvl/
  Heading 样式/numPr）+ 编号体系命中 → 规范性判定（is_normative），
  规范文档建议本地结构化解析（docx_struct，标题层级/自动编号保留）。
- 阈值（contextual_retrieval.max_full_doc_chars，默认 20000）：每次
  调用实时读 get_active_config()，超管改配置即时生效（与
  contextual_retriever.enrich_chunks 同源，口径一致）。
- 容错：文本提取失败 / 画像任一步失败均不影响整体（返回部分画像 +
  warning，接口恒 200）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.chunking import (analyze_qa_format, find_protected_ranges,
                              is_qa_format_valid, detect_heading_systems,
                              order_systems)
from backend.chunking.common import _iter_headings, _is_pure_symbol_line
from backend.config import get_active_config
from backend.db import get_db
from backend.deps import get_current_user, kb_or_404
from backend.models.user_models import UserPublic
from backend.services.document_service import get_document_service
from backend.services.parsers.client import get_parser_client
from backend.services.parsers.probe import probe_parsers

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/kbs/{kb_id}/documents", tags=["智能解析"])

# 指代词表（多字词优先，re alternation 按顺序最长匹配，"该方案"不会重复计
# 入"该"；用于"指代密集度"画像——指代密集的文档上下文检索/父子块收益更高）
_REFERENCE_RE = re.compile(
    r"该方案|前者|后者|上述|如上|其|该|它|此")

# 编号式标题扫描（_iter_headings 未覆盖的行首编号样式）：
# - "第X章/第X节/第X条"（中文数字或阿拉伯数字）
# - "1.1 / 1.1.2" 编号 + 空格 + 内容
# - "一、/ 二、" 中文编号顿号
_NUMBERED_HEADING_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百千万0-9]+[章节条款]|"
    r"\d{1,3}(?:\.\d{1,3}){1,4}\s+\S|"
    r"[一二三四五六七八九十]{1,3}、\s*\S)")

# 标题内容行最大字符数（与 splitter._HEADING_MAX_LEN 对齐，防正文长行误判）
_HEADING_MAX_LEN = 50

_TEXT_FILE_TYPES = {"txt", "md"}
_PARSER_FILE_TYPES = {"pdf", "docx"}
_SPREADSHEET_FILE_TYPES = {"xlsx", "xls", "csv"}
# docx/doc：结构化读取（doc 先经 LibreOffice 转 docx，见 _extract_docx_like）——
# doc 不能走 _extract_plain（python-docx 读不了老二进制格式），单列一类
_DOC_LIKE_FILE_TYPES = {"docx", "doc"}

# 规范性判定阈值（样式标题数量 + 非空段落占比）：达到即认为文档有规范写作
# 格式（标题层级明确），建议本地结构化解析（docx_struct）保留层级
_NORMATIVE_MIN_STYLE_HEADINGS = 3
_NORMATIVE_MIN_STYLE_RATIO = 0.02

_METHOD_LABELS = {
    "naive": "通用切块",
    "title": "按标题切块",
    "regex": "正则切块",
    "parent_child": "父子分块",
    "qa": "QA 问答",
    "agentic": "Agentic 智能分块",
}


def _format_chars(n: int) -> str:
    """字符数万字格式化：<1 万显示数字 + 字，>=1 万显示 X.X 万字"""
    if n >= 10000:
        return f"{n / 10000:.1f} 万字"
    return f"{n} 字"


# ---------------- 文本提取（轻量本地，不调外部解析服务） ----------------

def _extract_text(path: Path, file_type: str):
    """轻量本地文本提取：txt/md 直读（utf-8，GBK 兼容回退）；pdf/docx 复用
    parsers.client._extract_plain（pypdf / python-docx）。不调 MinerU/DeepDoc。
    返回 (text, extracted, warning)：extracted=False 时 text 为空串。
    """
    try:
        if file_type in _TEXT_FILE_TYPES:
            if not path.exists():
                return "", False, "源文件缺失，无法提取文本"
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("gbk", errors="replace")
        elif file_type in _PARSER_FILE_TYPES:
            if not path.exists():
                return "", False, "源文件缺失，无法提取文本"
            text = get_parser_client()._extract_plain(path, file_type)
        else:
            return "", False, f"暂不支持该类型（{file_type}）画像分析"
    except Exception as e:
        logger.warning("智能解析文本提取失败 %s: %s", path.name, e)
        return "", False, f"文本提取失败: {e}"
    if not text.strip():
        return "", False, "未提取到文本内容（文件为空或为扫描件）"
    return text.strip(), True, None


# ---------------- docx/doc 结构化读取 + 规范性探测（纯本地，不调外部服务） ----------------

def _outline_level(paragraph) -> int | None:
    """段落大纲层级：pPr/outlineLvl 显式层级 或 样式名 Heading N/标题 N；
    非标题段落返回 None（访问异常防御式返回 None，不影响整体探测）"""
    try:
        p_pr = paragraph._p.pPr
        if p_pr is not None and p_pr.outlineLvl is not None:
            return int(p_pr.outlineLvl.val)
        name = (paragraph.style.name if paragraph.style is not None
                else "") or ""
    except Exception:
        return None
    m = re.match(r"^(?:heading|标题)\s*([1-9])$", name.strip(), re.I)
    return int(m.group(1)) - 1 if m else None


def _has_num_pr(paragraph) -> bool:
    """段落是否挂自动编号/多级列表（pPr.numPr）：规范文档的标题常由多级
    列表编号（如"一、"/"1.1"），没有 Heading 样式也可视为结构化标题"""
    try:
        p_pr = paragraph._p.pPr
        return p_pr is not None and p_pr.numPr is not None
    except Exception:
        return False


def _probe_docx_structure(document) -> dict:
    """docx 结构探测（规范性判定；python-docx 轻量读取，不调 MinerU/DeepDoc）

    - style_headings：样式标题段落数（outlineLvl 或 Heading 样式——Word 里
      "这是标题"的显式标记）；
    - numbered_headings：挂自动编号（numPr）的段落数，仅作画像参考、**不参与**
      规范性判据：正文枚举列表（"1）…"）同样带 numPr，计入会把列表密集的
      普通文档误判为规范文档；
    - style_ratio：样式标题 / 非空段落数（样式标题覆盖率）；
    - style_heading_systems：样式标题的编号体系命中（复用 heading_presets
      .detect_heading_systems，与切块层级推断同口径）；
    - is_normative：样式标题 >= 3 个且覆盖率 >= 2% → 有规范写作格式
      （适合结构化解析保留标题层级）。
    """
    total = 0
    style_headings = 0
    numbered_headings = 0
    titles: list = []
    for p in document.paragraphs:
        text = (p.text or "").strip()
        if not text:
            continue
        total += 1
        if _outline_level(p) is not None:
            style_headings += 1
            if len(titles) < 200:  # 编号体系命中统计的样本上限
                titles.append(text)
        elif _has_num_pr(p):
            numbered_headings += 1
    ratio = style_headings / total if total else 0.0
    return {
        "style_headings": style_headings,
        "numbered_headings": numbered_headings,
        "total_paragraphs": total,
        "style_ratio": round(ratio, 4),
        "style_heading_systems": detect_heading_systems(titles),
        "is_normative": (style_headings >= _NORMATIVE_MIN_STYLE_HEADINGS
                         and ratio >= _NORMATIVE_MIN_STYLE_RATIO),
    }


def _extract_docx_like(path: Path, file_type: str):
    """docx/doc 结构化读取（画像用）：一次 OOXML 解析出「纯文本 + 结构探测」

    - docx 直接读；doc 先经 LibreOffice 转 docx（仅转一次，临时目录即用即清，
      见 parsers.client.convert_doc_to_docx）；
    - 文本口径与 parsers.client._extract_plain 一致（段落 + 表格行拼接）；
    - 结构探测见 _probe_docx_structure（规范性判定，建议解析引擎用）。
    返回 (text, probe, warning)：warning 非空 = 提取失败（text 为空串）。
    """
    tmp_dir = None
    try:
        if file_type == "doc":
            from backend.services.parsers.client import convert_doc_to_docx
            path, tmp_dir = convert_doc_to_docx(path)
        import docx  # python-docx
        d = docx.Document(str(path))
        paras = [p.text for p in d.paragraphs if p.text.strip()]
        for table in d.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    paras.append(" | ".join(cells))
        text = "\n\n".join(paras).strip()
        probe = _probe_docx_structure(d)
    except Exception as e:
        logger.warning("智能解析结构化读取失败 %s: %s", path.name, e)
        return "", {}, f"文本提取失败: {e}"
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    if not text:
        return "", probe, "未提取到文本内容（文件为空或为扫描件）"
    return text, probe, None


# ---------------- 画像分析（纯规则，每步独立容错） ----------------

def _analyze_structure(text: str) -> dict:
    """标题结构画像：_iter_headings（# 标题/setext 下划线/包裹式/前导符号式）
    + 编号式标题补充扫描（第X章 / X.X / 一、），返回有/无 + 计数 + 示例
    + 标题编号体系检测（heading_systems：中文公文/数字多级/章节体/法律/字母
    罗马——切块层级推断用，检测结果自动启用，界面展示详情）"""
    if not text:
        return {"has_headings": False, "heading_count": 0,
                "numbered_headings": 0, "examples": [], "heading_systems": []}
    protected = find_protected_ranges(text)
    headings = _iter_headings(text, protected)
    numbered: list = []
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) > _HEADING_MAX_LEN or _is_pure_symbol_line(s):
            continue
        if _NUMBERED_HEADING_RE.match(s):
            numbered.append(s)
    # 标题编号体系检测（标题文本候选 = 统一识别的标题 + 编号式标题行）
    candidates = [t for _, _, t in headings]
    candidates += [s for s in numbered if s not in candidates]
    systems = detect_heading_systems(candidates)
    return {
        "has_headings": bool(headings) or bool(numbered),
        "heading_count": len(headings),
        "numbered_headings": len(numbered),
        "examples": [t for _, _, t in headings[:5]],
        # 检测到的编号体系（按命中数降序）：[{system, label, hits, examples}]
        "heading_systems": systems,
        # 建议启用的体系（按语义优先级排序，切块层级推断用）
        "suggested_systems": order_systems([r["system"] for r in systems]),
    }


def _analyze_qa(text: str) -> dict:
    """QA 格式画像：复用 splitter.analyze_qa_format（同口径），占比 >=50% 判定"""
    if not text:
        return {"qa_pairs": 0, "total_paragraphs": 0, "ratio": 0.0,
                "is_qa": False}
    stats = analyze_qa_format(text)
    ratio = (stats.qa_pairs / stats.total_paragraphs
             if stats.total_paragraphs > 0 else 0.0)
    return {
        "qa_pairs": stats.qa_pairs,
        "total_paragraphs": stats.total_paragraphs,
        "ratio": round(ratio, 4),
        "is_qa": is_qa_format_valid(stats),
    }


def _analyze_reference_density(text: str) -> dict:
    """指代密集度画像：规则统计指代词出现频率，按字数归一化（次/千字）
    level: <2=low / 2~5=mid / >=5=high"""
    if not text:
        return {"count": 0, "per_1000_chars": 0.0, "level": "low",
                "level_label": "低"}
    count = len(_REFERENCE_RE.findall(text))
    per = count / len(text) * 1000
    if per >= 5:
        level, label = "high", "高"
    elif per >= 2:
        level, label = "mid", "中"
    else:
        level, label = "low", "低"
    return {"count": count, "per_1000_chars": round(per, 2),
            "level": level, "level_label": label}


def _count_paragraphs(text: str) -> int:
    """段落数（空行/连续换行切分，与 splitter._split_paragraphs 同口径）"""
    if not text:
        return 0
    return sum(1 for seg in re.split(r"\n\s*\n", text) if seg.strip())


def _analyze_length(text: str) -> dict:
    """篇幅画像：总字符数 + 2 万阈值（实时读活跃配置，改配置即时生效）"""
    doc_chars = len(text)
    threshold = int(get_active_config().contextual_retrieval
                    .max_full_doc_chars)
    return {
        "doc_chars": doc_chars,
        "threshold_chars": threshold,
        "over_threshold": doc_chars > threshold,
        "doc_label": _format_chars(doc_chars),
        "threshold_label": _format_chars(threshold),
        "paragraphs": _count_paragraphs(text),
    }


def _safe_analyze(fn, text: str, warnings: list, name: str):
    """画像单步容错：失败不中断整体（部分画像 + warning）"""
    try:
        return fn(text)
    except Exception as e:
        logger.warning("智能解析 %s 分析失败: %s", name, e)
        warnings.append(f"{name}分析失败: {e}")
        return {}


# ---------------- 表格画像（轻量本地读取，无 LLM） ----------------

def _analyze_spreadsheet(path: Path) -> dict:
    """Excel/CSV 轻量画像：Sheet 数量/行列/合计行数/合并单元格数量。

    复用 spreadsheet.reader（结构化直读），秒出、不烧 token——
    比 LLM 文本分析对表格文档更有用（Sheet 结构即文档骨架）。
    """
    from backend.services.spreadsheet.reader import read_spreadsheet
    sheets = read_spreadsheet(path)
    if not sheets:
        return {}
    infos = []
    total_rows = 0
    merged = 0
    for s in sheets:
        nrows = len(s.rows)
        ncols = max((len(r) for r in s.rows), default=0)
        total_rows += nrows
        merged += int(s.stats.get("merged_cells", 0))
        infos.append({"name": s.name, "rows": nrows, "cols": ncols})
    return {
        "sheet_count": len(infos),
        "sheets": infos,
        "total_rows": total_rows,
        "merged_cells": merged,
    }


# ---------------- 引擎建议（probe 探测 mineru/deepdoc 可用性） ----------------

def _suggest_engine(file_type: str, probe: dict,
                    docx_probe: dict | None = None) -> dict:
    """基于文件类型 + 解析器可用性探测的引擎建议（纯规则）

    docx_probe：docx/doc 结构探测结果（可选，见 _probe_docx_structure）；
    判定为规范文档（is_normative）时建议本地结构化解析 docx_struct
    （标题层级/自动编号保留）；非 docx/doc 或未探测时建议逻辑不变
    """
    mineru = probe.get("mineru") or {}
    deepdoc = probe.get("deepdoc") or {}
    if file_type in _SPREADSHEET_FILE_TYPES:
        return {"suggested": "spreadsheet",
                "reason": "Excel/CSV 本地结构化直读（表格→管道），无需解析引擎"}
    if file_type in _TEXT_FILE_TYPES:
        return {"suggested": "plain",
                "reason": "纯文本直读，无需外部解析器"}
    if file_type == "pdf":
        if mineru.get("available"):
            return {"suggested": "mineru",
                    "reason": "PDF 混排文档，MinerU 高精度解析（服务可用，推荐）"}
        if deepdoc.get("available"):
            return {"suggested": "deepdoc",
                    "reason": "MinerU 不可用；DeepDoc 可用，表格输出可检索 HTML（仅 PDF）"}
        return {"suggested": "plain",
                "reason": "MinerU/DeepDoc 均不可用，降级纯文本提取（pypdf）"}
    if file_type in _DOC_LIKE_FILE_TYPES and (docx_probe or {}).get("is_normative"):
        return {"suggested": "docx_struct",
                "reason": "检测到规范标题样式，可用结构解析保留层级"}
    if file_type == "doc":
        return {"suggested": "docx_struct",
                "reason": "老版 Word（doc）经 LibreOffice 转换后结构化解析"
                          "（本地，无服务依赖）"}
    if file_type == "docx":
        if mineru.get("available"):
            return {"suggested": "mineru",
                    "reason": "docx 由 MinerU 解析（服务可用，推荐）"}
        return {"suggested": "plain",
                "reason": "MinerU 不可用，python-docx 纯文本提取"}
    return {"suggested": "auto",
            "reason": "该类型文档使用默认引擎"}


# ---------------- 推荐（规则版决策矩阵） ----------------

def _recommend(length: dict, structure: dict, qa: dict) -> dict:
    """切块方式/上下文检索推荐（规则版决策矩阵）：
    - QA 格式 → qa（问答对整块）；有标题结构 → parent_child（章节前缀推荐）
    - 无标题 且 ≤ 阈值 → naive + 上下文检索推荐
    - 无标题 > 阈值 → naive + 提示不建议上下文检索
    - agentic 恒为可选（LLM 语义切分，与上下文检索互斥）
    """
    has_headings = bool(structure.get("has_headings"))
    heading_total = (structure.get("heading_count", 0)
                     + structure.get("numbered_headings", 0))
    is_qa = bool(qa.get("is_qa"))
    over = bool(length.get("over_threshold"))
    qa_ratio = qa.get("ratio", 0.0)
    qa_pairs = qa.get("qa_pairs", 0)
    doc_chars = length.get("doc_chars", 0)
    threshold_label = length.get("threshold_label", "2 万字")

    alternatives = []
    contextual = {"recommended": False, "reason": ""}
    enable_heading_in_content = False

    if is_qa:
        main = {"method": "qa", "label": _METHOD_LABELS["qa"],
                "recommended": True,
                "reason": (f"检测到 QA 问答格式（问答对 {qa_pairs} 组，"
                           f"占比 {qa_ratio * 100:.0f}%），问答对整块入库最合适")}
        if has_headings:
            alternatives.append(
                {"method": "parent_child", "label": _METHOD_LABELS["parent_child"],
                 "recommended": False,
                 "reason": "备选：文档同时有标题结构，可按章节父子分块"})
        contextual = {"recommended": False,
                      "reason": "QA 问答对自带上下文，无需上下文检索增强"}
    elif has_headings:
        main = {"method": "parent_child",
                "label": _METHOD_LABELS["parent_child"],
                "recommended": True,
                "reason": (f"检测到 {heading_total} 个标题，父子分块父块聚合"
                           "章节、子块精细切分，检索返回父块完整上下文")}
        alternatives.append(
            {"method": "title", "label": _METHOD_LABELS["title"],
             "recommended": False,
             "reason": "备选：按标题直接切块，结构简单时更轻量"})
        enable_heading_in_content = True
        contextual = {"recommended": False,
                      "reason": "有标题结构，建议优先利用标题/父块上下文而非 LLM 摘要"}
    else:
        main = {"method": "naive", "label": _METHOD_LABELS["naive"],
                "recommended": True,
                "reason": "未检测到标题结构，通用递归字符切块最稳妥"}
        if not over:
            contextual = {
                "recommended": True,
                "reason": (f"无标题且文档 {_format_chars(doc_chars)} ≤ 阈值"
                           f" {threshold_label}，上下文检索增强可为孤立块补全局背景")}
        else:
            contextual = {
                "recommended": False,
                "reason": (f"文档超过上下文检索完整文档阈值"
                           f"（{threshold_label}），效果不佳不建议开启")}

    alternatives.append(
        {"method": "agentic", "label": _METHOD_LABELS["agentic"],
         "recommended": False,
         "reason": ("可选：LLM 语义切分逻辑段落（1 万~5 万字需确认，"
                    "超 5 万字不支持；与上下文检索增强互斥）")})
    return {
        "chunk_method": main,
        "alternatives": alternatives,
        "contextual_retrieval": contextual,
        "enable_heading_in_content": enable_heading_in_content,
    }


# ---------------- 接口 ----------------

def _get_doc_or_404(kb_id: str, doc_id: str):
    doc = get_document_service().get_by_kb(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


@router.get("/{doc_id}/analyze")
async def analyze_document(request: Request, kb_id: str, doc_id: str,
                           db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(get_current_user)):
    """文档画像分析（can_manage_kb；未解析文档也能分析）

    轻量本地文本提取（不调 MinerU/DeepDoc），规则画像 + 切块方式/
    上下文检索推荐；任何一步失败不影响整体（部分画像 + warnings）。
    """
    await kb_or_404(db, kb_id, user, manage=True)
    doc = _get_doc_or_404(kb_id, doc_id)
    file_type = (doc.file_type or "").lower().lstrip(".")
    warnings: list = []

    # 0) 表格文档：跳过文本结构画像与解析器探测（秒出，无 LLM 成本）。
    # 轻量表格画像（Sheet/行列/合并单元格）+ 固定推荐（按 Sheet 切块）。
    if file_type in _SPREADSHEET_FILE_TYPES:
        spreadsheet: dict = {}
        try:
            spreadsheet = _analyze_spreadsheet(
                get_document_service().get_upload_path(doc))
        except Exception as e:
            logger.warning("表格画像失败 %s: %s", doc.original_name, e)
            warnings.append(f"表格画像失败: {e}")
        engine = _suggest_engine(file_type, {})
        recommendations = {
            "chunk_method": {
                "method": "title", "label": _METHOD_LABELS["title"],
                "recommended": True,
                "reason": "表格文档按 Sheet 分节（## Sheet: 名称）切块，"
                          "每张表独立成块，检索语义最精准"},
            "alternatives": [
                {"method": "naive", "label": _METHOD_LABELS["naive"],
                 "recommended": False,
                 "reason": "备选：通用字符切块（管道表格块仍原子保护）"},
                {"method": "parent_child", "label": _METHOD_LABELS["parent_child"],
                 "recommended": False,
                 "reason": "备选：父子分块，父块含完整表格"},
            ],
            "contextual_retrieval": {
                "recommended": False,
                "reason": "表格自带结构上下文，无需 LLM 摘要增强"},
            "enable_heading_in_content": False,
        }
        return {
            "doc_id": doc_id,
            "file_type": file_type,
            "extracted": True,
            "extract_warning": None,
            "engine_suggestion": engine,
            "spreadsheet": spreadsheet,
            "length": {}, "structure": {}, "qa": {},
            "reference_density": {},
            "recommendations": recommendations,
            "warnings": warnings,
        }

    # 1) 文本提取（容错：失败返回部分画像）；docx/doc 走结构化读取
    # （doc 经 LibreOffice 转换，文本与结构探测共用同一次转换产物）
    upload_path = get_document_service().get_upload_path(doc)
    docx_probe: dict = {}
    if file_type in _DOC_LIKE_FILE_TYPES:
        text, docx_probe, extract_warning = await asyncio.to_thread(
            _extract_docx_like, upload_path, file_type)
        extracted = bool(text.strip())
    else:
        text, extracted, extract_warning = _extract_text(upload_path, file_type)
    if extract_warning:
        warnings.append(extract_warning)

    # 2) 引擎建议（probe_parsers 自身不抛异常：失败=不可用+原因；再兜一层）
    engine = {"suggested": "auto", "reason": "解析器探测失败，使用默认引擎",
              "probe": None}
    try:
        probe = await probe_parsers()
        engine = _suggest_engine(file_type, probe, docx_probe)
        engine["probe"] = probe
    except Exception as e:
        logger.warning("智能解析解析器探测失败: %s", e)
        warnings.append(f"解析器探测失败: {e}")

    # 3) 画像分析（每步独立容错）
    length = _safe_analyze(_analyze_length, text, warnings, "篇幅")
    structure = _safe_analyze(_analyze_structure, text, warnings, "标题结构")
    if docx_probe:
        # docx 结构探测（规范性判定，引擎建议用）并入标题结构画像（新字段）
        structure["docx_structure"] = docx_probe
    qa = _safe_analyze(_analyze_qa, text, warnings, "QA 格式")
    density = _safe_analyze(
        _analyze_reference_density, text, warnings, "指代密集度")

    # 4) 推荐（决策矩阵，基于画像）
    recommendations = {}
    try:
        recommendations = _recommend(length, structure, qa)
    except Exception as e:
        logger.warning("智能解析推荐生成失败: %s", e)
        warnings.append(f"推荐生成失败: {e}")

    logger.info("智能解析画像: %s %s 提取=%s 字数=%s 标题=%s QA=%s",
                doc.original_name, file_type, extracted,
                length.get("doc_chars"), structure.get("has_headings"),
                qa.get("is_qa"))
    return {
        "doc_id": doc_id,
        "file_type": file_type,
        "extracted": extracted,
        "extract_warning": extract_warning,
        "engine_suggestion": engine,
        "length": length,
        "structure": structure,
        "qa": qa,
        "reference_density": density,
        "recommendations": recommendations,
        "warnings": warnings,
    }
