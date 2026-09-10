"""标题编号体系预设与检测（切块层级推断）

背景：MinerU 解析 PDF 时把所有标题行统一输出为 `##`（2 级），markdown 层级
信息丢失——真实层级藏在标题文本的**编号**里（如"一、"是章、"1.1"是节、
"1.1.1"是小节）。本模块提供：
- PRESETS：常见中文/混排文档的标题编号体系（每体系 = 有序模式列表，
  列表顺序 = 层级从高到低；「并列拼接」语义：多体系选中时按顺序首尾相接
  成一张长层级表，标题行从上往下第一个命中的位置即推断层级）
- detect_heading_systems：扫描全文标题行，统计各体系命中数（智能解析画像
  展示 + 入库自动检测自动启用）
- match_system_position：标题文本 → 在拼接长表中的位置（未命中 None）

同一符号在不同体系层级不同（如 "1." 在中文公文里是第 3 层、在数字多级里
是第 1 层），因此不能全自动判体系，需按文档实际体系配置（检测结果自动启用，
界面可覆盖）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# "单数字点"模式前缀（1. / 1、——单级数字 + 点/顿号 的模式字符串特征）：
# 层级语义最弱，拼接时统一降级到长表末尾（见 system_level_table）
_SINGLE_NUM_DOT_PREFIX = r"^\d+\s*[、.]"

# ---- 预设体系：有序模式列表（顺序 = 层级从高到低）----
# 正则均带"不接更细编号"的负向断言（如 `(?!\d)`），保证 "13.1" 命中
# "1.1" 位置而非 "1" 位置（从上往下首个命中语义下防前缀误匹配）
_PRESETS: Dict[str, List[str]] = {
    # 1) 中文公文/行政文件：一、→（一）→ 1. →（1）→ ①
    "chinese_official": [
        r"^[一二三四五六七八九十百千]+[、.]",
        r"^[（(][一二三四五六七八九十百千]+[）)]",
        r"^\d+\s*[、.](?!\d)",          # "1." / "1、"（点后不接数字防 13.1 误命中）
        r"^[（(]\d+[）)]",
        r"^[①-⑳]",
    ],
    # 2) 数字多级编号（学术论文/技术文档）：1 → 1.1 → 1.1.1 → 1.1.1.1
    "numeric_multi": [
        r"^\d+(?![.\d])",               # 裸数字 + 非点非数字
        r"^\d+\.\d+(?![.\d])",
        r"^\d+\.\d+\.\d+(?![.\d])",
        r"^\d+(?:\.\d+){3,}(?![.\d])",
    ],
    # 3) 章节体（书籍/教材/论文）：第X篇/章 → 第X节 → 一、→（一）→ 1.
    "chapter_body": [
        r"^第[一二三四五六七八九十百千\d]+[篇章]",
        r"^第[一二三四五六七八九十百千\d]+节",
        r"^[一二三四五六七八九十百千]+[、.]",
        r"^[（(][一二三四五六七八九十百千]+[）)]",
        r"^\d+\s*[、.](?!\d)",
    ],
    # 4) 法律/合同/标准条文：第X编 → 第X章 → 第X节 → 第X条 → 第X款 → 第X项
    "legal": [
        r"^第[一二三四五六七八九十百千\d]+编",
        r"^第[一二三四五六七八九十百千\d]+章",
        r"^第[一二三四五六七八九十百千\d]+节",
        r"^第[一二三四五六七八九十百千\d]+条",
        r"^第[一二三四五六七八九十百千\d]+款",
        r"^第[一二三四五六七八九十百千\d]+项",
    ],
    # 5) 字母/罗马数字/混合编号（英文文档/附录/标准/PPT）：
    #    Appendix A → A.1 → A.1.1；I. II.；a)；i. ii.
    "alpha_roman": [
        r"^(?:Appendix|附录)\s*[A-Z]\b",
        r"^[A-Z]\.\d+(?![.\d])",        # A.1（点后不接数字防 A.1.1 误命中）
        r"^[A-Z](?:\.\d+){2,}(?![.\d])",  # A.1.1+
        r"^[IVXLC]+[.、]\s",             # I. II.（罗马数字 + 点/顿号 + 空白）
        r"^[a-z][）)]|^[a-z]\.\s",       # a) / a.
        r"^[ivxlc]+[.、]\s",             # i. ii.（小写罗马）
    ],
}

# 体系显示名（画像/配置界面展示用）
SYSTEM_LABELS: Dict[str, str] = {
    "chinese_official": "中文公文（一、/（一）/1./（1）/①）",
    "numeric_multi": "数字多级（1/1.1/1.1.1）",
    "chapter_body": "章节体（第X章/第X节/一、/（一）/1.）",
    "legal": "法律条文（第X编/章/节/条/款/项）",
    "alpha_roman": "字母罗马（Appendix A/A.1/I./a)/i.）",
    "markdown": "Markdown（#/##/###）",
}

ALL_SYSTEMS: Tuple[str, ...] = tuple(_PRESETS.keys()) + ("markdown",)

# 体系语义优先级（并列拼接的排序依据）：数值越小越靠前（层级越高）
# 依据各体系"最高层标题"的语义：第X编/章、一、是章级（文档顶层）；
# 数字多级 1/1.1/1.1.1 从顶层到细分（配合负向断言不与"1."误配）；
# 字母罗马多为附录/次级编号。检测到多体系时按此优先级排序拼接，
# 而非命中数——避免"命中多的体系抢占高层"导致层级颠倒
_SYSTEM_ORDER: Dict[str, int] = {
    "legal": 0,            # 第X编/章（法律条文顶层）
    "chapter_body": 1,     # 第X章/节（书籍顶层）
    "chinese_official": 2,  # 一、（中文公文顶层）
    "numeric_multi": 3,    # 1/1.1/1.1.1
    "alpha_roman": 4,      # Appendix A / A.1 / I.
}


def order_systems(systems: List[str]) -> List[str]:
    """按体系语义优先级排序（检测结果 → 拼接顺序；未知体系排最后保持原序）"""
    return sorted(systems, key=lambda s: _SYSTEM_ORDER.get(s, 99))


def system_level_table(systems: List[str]) -> List[Tuple[str, str]]:
    """「并列拼接」展开：多体系按顺序首尾相接为一张长层级表

    返回 [(体系名, 正则), ...]——列表下标即层级位置（0 最高）。
    例：["chinese_official", "numeric_multi"] → 公文的 5 个模式在前、
    数字多级的 4 个模式在后，共 9 档。
    markdown 体系不参与（# 数量本身即层级，见 _iter_headings 既有逻辑）。

    "单数字点模式"（如 `^\\d+\\s*[、.](?!\\d)`，即 "1." / "2、"）统一**降到
    整张表末尾**：这类写法在文档中既可能是顶层编号（"1. 绪论"）也可能是
    更深层级下的要点（"1.2 节里的 2. 某要点"）；置于末尾保证它不会压过
    "1.1"（点分节号）等有明确层级语义的编号——与
    「章级编号 → 节级点分编号 → 节下要点」的实际文档语义一致。
    """
    tail: List[Tuple[str, str]] = []   # 单数字点模式（降级到末尾）
    table: List[Tuple[str, str]] = []
    for name in systems:
        for pattern in _PRESETS.get(name, []):
            if pattern.startswith(_SINGLE_NUM_DOT_PREFIX):
                tail.append((name, pattern))
            else:
                table.append((name, pattern))
    return table + tail


# 解析产物标题里的上下标标签（MinerU 把标题中的数字/字母包成 <sub>/<sup>，
# 如 "## <sub>1.1</sub> 节标题"）：推断前剥离，否则编号正则匹配失败
_SUBSUP_RE = re.compile(r"</?(?:sub|sup)>", re.IGNORECASE)


def _strip_subsup(text: str) -> str:
    """剥离 <sub>/<sup> 标签（仅用于编号匹配，不改动原文）"""
    return _SUBSUP_RE.sub("", text)


def match_system_position(title: str, systems: List[str]) -> Optional[int]:
    """标题文本 → 在拼接长表中的位置（0 起；未命中/无体系 → None）

    从上往下第一个命中的模式位置即推断层级（位置越小层级越高）。
    匹配前剥离 <sub>/<sup> 标签（MinerU 把标题里的编号包成上下标，
    如 "## <sub>1.1</sub> 节标题" → 按 "1.1 节标题" 匹配）。
    """
    if not systems or not title:
        return None
    text = _strip_subsup(title.strip())
    if not text:
        return None
    for pos, (_name, pattern) in enumerate(system_level_table(systems)):
        if re.search(pattern, text):
            return pos
    return None


def detect_heading_systems(candidates: List[str]) -> List[dict]:
    """扫描标题行文本候选，统计各体系命中数

    candidates: 标题文本列表（不含 markdown # 前缀，调用方剥壳后传入）。
    返回 [{system, label, hits, examples}] 按命中数降序（hits>0 才返回）：
    - hits：命中该体系任一模式的标题行数；
    - examples：最多 3 个命中示例（画像展示"命中了哪些标题"用）。

    重叠体系去重：chapter_body（第X章/节 + 一、/（一）/1.）已涵盖
    chinese_official（一、/（一）/1./（1）/①）的编号模式——两者同时启用会
    让同一符号（如 "1."）在拼接表里出现两次产生层级歧义。检测到
    chapter_body 时**不重复列出** chinese_official（保留更专有的整体体系），
    仅当 chapter_body 未命中时才单列 chinese_official。
    """
    results: List[dict] = []
    for name, patterns in _PRESETS.items():
        hits = 0
        examples: List[str] = []
        for title in candidates:
            text = _strip_subsup(title.strip())
            if not text:
                continue
            if any(re.search(p, text) for p in patterns):
                hits += 1
                if len(examples) < 3:
                    examples.append(text[:50])
        if hits > 0:
            results.append({
                "system": name,
                "label": SYSTEM_LABELS.get(name, name),
                "hits": hits,
                "examples": examples,
            })
    # 重叠去重：chapter_body 命中 → 移除 chinese_official（避免同符号双归属）
    names = {r["system"] for r in results}
    if "chapter_body" in names and "chinese_official" in names:
        results = [r for r in results if r["system"] != "chinese_official"]
    results.sort(key=lambda r: r["hits"], reverse=True)
    return results
