"""纯规则画像（文本 → dict），零 IO、零 LLM

每一步都是独立纯函数：输入文本、输出画像 dict。调用方（build.py）逐个包在
容错里跑，任一步失败只丢那一步的画像（部分画像 + warning，接口恒 200）。
"""
from __future__ import annotations

import logging
import re

from backend.chunking import (analyze_qa_format, detect_heading_systems,
                              find_protected_ranges, is_qa_format_valid,
                              order_systems)
from backend.chunking.common import _is_pure_symbol_line, _iter_headings
from backend.services.parsers.images import extract_image_refs

logger = logging.getLogger(__name__)

# 指代词表（多字词优先，re alternation 按顺序最长匹配，"该方案"不会重复计
# 入"该"；用于"指代密集度"画像——指代密集的文档上下文检索/父子块收益更高）
_REFERENCE_RE = re.compile(r"该方案|前者|后者|上述|如上|其|该|它|此")

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

# 指代密集度分级阈值（次/千字）
_DENSITY_MID = 2.0
_DENSITY_HIGH = 5.0


def format_chars(n: int) -> str:
    """字符数万字格式化：<1 万显示数字 + 字，>=1 万显示 X.X 万字"""
    if n >= 10000:
        return f"{n / 10000:.1f} 万字"
    return f"{n} 字"


def count_paragraphs(text: str) -> int:
    """段落数（空行/连续换行切分，与 splitter._split_paragraphs 同口径）"""
    if not text:
        return 0
    return sum(1 for seg in re.split(r"\n\s*\n", text) if seg.strip())


def analyze_length(text: str, threshold: int) -> dict:
    """篇幅画像：总字符数 + 阈值对照（阈值由调用方从活跃配置读入，保持本函数为纯函数）

    image_refs：markdown 图片引用数，成本预估用。注意本地预览提取的文本里，
    PDF/docx 的图片通常尚未出现（要解析后才有引用），所以它往往是下限而非真值。
    """
    doc_chars = len(text)
    return {
        "doc_chars": doc_chars,
        "threshold_chars": threshold,
        "over_threshold": doc_chars > threshold,
        "doc_label": format_chars(doc_chars),
        "threshold_label": format_chars(threshold),
        "paragraphs": count_paragraphs(text),
        "image_refs": len(extract_image_refs(text)),
    }


def analyze_structure(text: str) -> dict:
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


def analyze_qa(text: str) -> dict:
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


def analyze_reference_density(text: str) -> dict:
    """指代密集度画像：规则统计指代词出现频率，按字数归一化（次/千字）
    level: <2=low / 2~5=mid / >=5=high"""
    if not text:
        return {"count": 0, "per_1000_chars": 0.0, "level": "low",
                "level_label": "低"}
    count = len(_REFERENCE_RE.findall(text))
    per = count / len(text) * 1000
    if per >= _DENSITY_HIGH:
        level, label = "high", "高"
    elif per >= _DENSITY_MID:
        level, label = "mid", "中"
    else:
        level, label = "low", "低"
    return {"count": count, "per_1000_chars": round(per, 2),
            "level": level, "level_label": label}


def safe_analyze(fn, text: str, warnings: list, name: str) -> dict:
    """画像单步容错：失败不中断整体（部分画像 + warning，返回空 dict）"""
    try:
        return fn(text)
    except Exception as e:
        logger.warning("智能解析 %s 分析失败: %s", name, e)
        warnings.append(f"{name}分析失败: {e}")
        return {}
