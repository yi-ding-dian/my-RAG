"""文本切块（无 langchain，纯标准库实现）

- Chunk: 切块结果（text + 相对输入文本的字符偏移 char_start/char_end），
  所有切块器的 chunk() 统一返回 List[Chunk]（偏移支持详情展示/子父块归属计算）
- RecursiveChunker: 递归字符切（分隔符 ["\\n\\n","\\n","。","；","，"," ",""]，
  size=800 字符 / overlap=100，env 可配；可选自定义 delimiter 优先切）
- MarkdownSplitter: 按标题切（默认 #/##/### + 纯文本常见标题样式：setext
  下划线式/单行包裹式/前导符号式，可用 split_level 限层级，标题保留块首），
  超长段再递归切
- RegexChunker: 按正则匹配位置切（匹配片段与片段间文本都成块，文本不丢），
  超长段再递归切（参考 KnowFlow regex.py 思路，finditer 避免 re.split 空匹配）
- ParentChildChunker: 父子分块（对齐 KnowFlow parent_child 语义）：
  父块按标题聚合完整章节（无大小上限，超长单节按 parent_chunk_size 兜底），
  子块按标题边界断章后段内递归字符切（不跨章节、overlap 不跨章）；
  入库只存子块（metadata 带父块全文），检索按 retrieval_mode 决定返回
  子块或附带父块上下文
- 切块质量增强（title/parent_child，对齐 KnowFlow AST 语义）：
  find_protected_ranges 识别表格（连续 | 行/HTML <table>）与 ``` 围栏
  代码块，切分边界避开保护区间（整体归块、超长不切开）；标题智能回退
  （title 按 split_level 只切 1 个超长块时放宽一级重切）；连续标题不切
  （直接相邻标题行并入后续内容块）；add_heading_paths 标题链注入
- QaChunker: QA 问答切块（问/答标记识别，问答对整块，答案跨多段保留，
  文档头杂项兜底普通块；analyze_qa_format/is_qa_format_valid 为规范性
  检测纯函数，入库前按占比 >=50% 判定合格，与切块器统计口径一致）
- get_chunker: 切块器工厂（naive/title/regex/parent_child/qa 五种同步方式，
  入库参数驱动；agentic 为 LLM 异步切块，不走本工厂——ingestion_service
  特殊分支调用 backend/services/agentic_chunker.py，失败回退 title）
- Chunker 协议: 预留语义切块等扩展
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Protocol, Tuple

from backend.config import get_active_config

# 支持的切块方式（ingest 请求 method / 文档 parser_id 取值范围；
# agentic=Agentic 智能分块（LLM 读全文切逻辑段落+标签），异步实现见
# backend/services/agentic_chunker.py，get_chunker 不支持（ingestion 特殊分支））
VALID_METHODS = ("naive", "title", "regex", "parent_child", "qa", "agentic")

_TITLE_RE = re.compile(r"^#{1,3}\s+\S.*$", re.MULTILINE)

# 标题路径正则：识别 1~6 级 Markdown 标题行（标题树，供父标题前缀拼接）
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
# 块文本是否已含标题行（含则不再拼接父标题）
_CHUNK_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)

# ---- 纯文本标题样式识别（title 切块增强，MarkdownSplitter 标题边界使用）----
# 标题内容行最大字符数：超长行（如整段正文）不是标题
_HEADING_MAX_LEN = 50
# setext 下划线行：整行（去首尾空白）只含 = 或 - 且 >=4 个（markdown Setext 标题）
_SETEXT_EQ_RE = re.compile(r"^[ \t]*={4,}[ \t]*$")
_SETEXT_DASH_RE = re.compile(r"^[ \t]*-{4,}[ \t]*$")
# 单行包裹式首尾符号：半角 = - *（各 >=3 个）与全角装饰线 ━ ─（各 >=2 个）
_WRAP_SYMBOLS = "=-*━─"
# 前导符号式行首标记（■◆●※▶▍，后跟空格或直接接文字）
_LEADING_MARK_RE = re.compile(r"^[ \t]*([■◆●※▶▍])\s*(.+?)\s*$")
# 纯符号字符集：仅含这些字符的行是装饰线/分隔线（如 ========、------），
# 不是标题内容（防误判：单独一行 = 装饰线不当作 setext 内容行）
_PURE_SYMBOL_CHARS = set("-=*~_#|+━─—·•●○■◆※▶▍")


def _is_pure_symbol_line(line: str) -> bool:
    """纯符号/纯空白行（如单独一行 ========、------、━━）→ True

    装饰线、分隔线不是标题内容（防误判：'====\\n====' 的上一行不是标题）。
    """
    s = line.strip()
    return not s or all(ch in _PURE_SYMBOL_CHARS for ch in s)


def _is_heading_text_candidate(line: str) -> bool:
    """标题内容候选：非空、非纯符号、长度 <= 50（setext 内容行/包裹内容共用）"""
    s = line.strip()
    return bool(s) and len(s) <= _HEADING_MAX_LEN and not _is_pure_symbol_line(s)


def _unwrap_wrapped_heading(line: str) -> str | None:
    """单行包裹式标题解析：命中返回标题内容（去掉首尾包裹符号），否则 None

    - 符号包裹：首尾同一符号（= - * 或全角装饰 ━ ─），前缀/后缀各 >=3 个
      （━ ─ 为 >=2 个），左右可不等长；中间内容非空、<=50 字符、非纯符号；
      示例：'===== 6.3.2 变电站模型 ====='、'--- 备注 ---'、'*** 说明 ***'、
      '━━ 标题 ━━'；
    - 全角方括号包裹：整行就是【内容】（如 '【设备容器模型】'）；
    - 纯符号行（如单独一行 '=========='）无内容 → 不命中（装饰线不是标题）
    """
    s = line.strip()
    if not s:
        return None
    if len(s) >= 3 and s[0] == "【" and s[-1] == "】":
        inner = s[1:-1].strip()
        return inner if _is_heading_text_candidate(inner) else None
    first, last = s[0], s[-1]
    if first != last or first not in _WRAP_SYMBOLS:
        return None
    head = 0
    while head < len(s) and s[head] == first:
        head += 1
    tail = 0
    while tail < len(s) and s[len(s) - 1 - tail] == first:
        tail += 1
    min_wrap = 2 if first in "━─" else 3
    if head < min_wrap or tail < min_wrap or head + tail >= len(s):
        return None  # 包裹符号不足 或 整行都是符号（无内容）
    inner = s[head:len(s) - tail].strip()
    return inner if _is_heading_text_candidate(inner) else None


def _iter_headings(text: str,
                   protected: List[Tuple[int, int]] | None = None) -> List[Tuple[int, int, str]]:
    """统一标题识别（title 切块）：ATX # 标题 + 纯文本常见标题样式

    返回 [(标题行起始偏移, 级别, 标题文本)]，按位置升序。级别映射
    （供 split_level 过滤，'识别 <=N 级标题' 语义与 # 标题统一）：
    - ATX '# 标题'（# 后空白 + 非空内容）→ 级别 = # 数量（1~6）
    - Setext '标题\\n========'（下划线整行 >=4 个 =）→ 级别 1
             '标题\\n--------'（下划线整行 >=4 个 -）→ 级别 2
      （setext 标题边界在标题文字行起点，下划线行并入该标题块）
    - 单行包裹式 '===== 标题 =====' / '--- 备注 ---' / '*** 说明 ***' /
      '【标题】' / '━━ 标题 ━━' → 级别 2
    - 前导符号式 '■ 第一章 概述'（■◆●※▶▍ 开头，后跟空格或直接接文字，
      整行 <=50 字符）→ 级别 2

    防误判：
    - 纯符号行（单独一行 ======== / ------ 装饰线）不是标题内容；
    - 表格/代码块保护区间内的行不是标题（protected 过滤）；
    - setext 内容行不以 < 开头（XML/HTML 标签行，如 '<Breaker::湖北>'）、
      不以 | 开头（表格行）、不以 :/：结尾（字段定义/程序输出标签行，
      如 'E文件导出实例：'）、非纯符号、<=50 字符；
    - 一行已按 ATX/包裹式/前导符号式识别为标题时，不再重复识别为 setext
      内容行（避免 '【标题】\\n====' 双重识别）；
    - 连续标题（直接相邻标题行不各自成块）由调用方 _filter_continuous_headings
      负责，本函数不处理。
    """
    lines = text.split("\n")
    result: List[Tuple[int, int, str]] = []
    pos = 0
    for i, line in enumerate(lines):
        start = pos
        pos += len(line) + 1  # 换行符偏移（末行多余 +1 无害，不参与产出）
        s = line.strip()
        if not s:
            continue
        title, level = "", 0
        m = _HEADING_RE.match(line)
        if m:  # ATX：'# 标题'
            title, level = m.group(2).strip(), len(m.group(1))
        else:
            inner = _unwrap_wrapped_heading(line)
            if inner is not None:
                title, level = inner, 2  # 单行包裹式
            else:
                lm = _LEADING_MARK_RE.match(line)
                if lm is not None and len(s) <= _HEADING_MAX_LEN:
                    title, level = lm.group(2).strip(), 2  # 前导符号式
                elif (i + 1 < len(lines)
                        and _is_heading_text_candidate(line)
                        and not s.startswith(("<", "|"))
                        and not s.endswith((":", "："))):
                    # setext：内容行 + 下一行是整行下划线
                    if _SETEXT_EQ_RE.match(lines[i + 1]):
                        title, level = s, 1
                    elif _SETEXT_DASH_RE.match(lines[i + 1]):
                        title, level = s, 2
        if title and (not protected
                      or not any(ps < start < pe for ps, pe in protected)):
            result.append((start, level, title))
    return result

# ---- 表格/代码块保护区间（切分边界不得落在区间内部，保证块级完整性）----
# markdown 表格行：以 | 开头且以 | 结尾（仅允许空格/制表缩进，不含换行）
_TABLE_LINE_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)
# HTML 表格起止标签
_HTML_TABLE_OPEN_RE = re.compile(r"<table\b", re.IGNORECASE)
_HTML_TABLE_CLOSE_RE = re.compile(r"</table\s*>", re.IGNORECASE)
# 围栏代码块开/闭行（``` 起，行内仅反引号 + 可选语言名）
_FENCE_LINE_RE = re.compile(r"^(`{3,})[^\n]*$", re.MULTILINE)


def _is_table_separator_line(line: str) -> bool:
    """判断是否为 markdown 表格分隔行（如 | --- | :---: |，每列仅由 - 与 : 组成）"""
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return False
    inner = s[1:-1]
    if not inner.strip():
        return False
    for cell in inner.split("|"):
        cell = cell.strip()
        if cell and not set(cell) <= set("-:"):
            return False
    return True


def _find_table_ranges(text: str) -> List[Tuple[int, int]]:
    """定位 markdown 表格块：| 行组成的块（允许空行间隔）且其中含分隔符行

    - 块 = 连续出现的 | 行（行间只允许空白/空行，出现正文行即断开）；
      块内至少 2 行且含分隔符行（如 | --- |）才构成表格——普通文本中
      形如 | x | 的零散行（无分隔行）不误判为表格；
    - 允许空行间隔是兼容 OCR 解析产物（MinerU 表格行间常插空行）；
    - 返回 [(start, end)] 半开区间（含分隔行，不含表后空行）
    """
    rows = [(m.start(), m.end(), _is_table_separator_line(m.group(0)))
            for m in _TABLE_LINE_RE.finditer(text)]
    ranges: List[Tuple[int, int]] = []
    i = 0
    while i < len(rows):
        # 连续 | 行块（行间只允许空白/空行）
        j = i + 1
        while j < len(rows) and text[rows[j - 1][1]:rows[j][0]].strip() == "":
            j += 1
        block = rows[i:j]
        if len(block) >= 2 and any(sep for _, _, sep in block):
            ranges.append((block[0][0], block[-1][1]))
        i = j
    return ranges


def _find_html_table_ranges(text: str) -> List[Tuple[int, int]]:
    """定位 HTML 表格块：<table> 到 </table>（MinerU 产物中常见）"""
    ranges: List[Tuple[int, int]] = []
    for op in _HTML_TABLE_OPEN_RE.finditer(text):
        close = _HTML_TABLE_CLOSE_RE.search(text, op.end())
        if close:
            ranges.append((op.start(), close.end()))
        else:
            # 未闭合的 <table>：保护到文末（不切分内部）
            ranges.append((op.start(), len(text)))
    return ranges


def _find_fence_ranges(text: str) -> List[Tuple[int, int]]:
    """定位 ``` 围栏代码块：开 fence 行到配对的闭 fence 行（含 fence 行）

    未闭合的 fence（全文仅一个 ```）保护到文末，保证代码块不被动刀。
    """
    fences = list(_FENCE_LINE_RE.finditer(text))
    ranges: List[Tuple[int, int]] = []
    i = 0
    while i < len(fences):
        start_m = fences[i]
        # 找配对闭 fence：行内反引号后仅空白
        j = i + 1
        while j < len(fences):
            body = fences[j].group(0)[len(fences[j].group(1)):]
            if body.strip() == "":
                break
            j += 1
        if j < len(fences):
            ranges.append((start_m.start(), fences[j].end()))
            i = j + 1
        else:
            ranges.append((start_m.start(), len(text)))
            break
    return ranges


def find_protected_ranges(text: str) -> List[Tuple[int, int]]:
    """返回不可切分的区间列表（markdown 表格 / HTML 表格 / 围栏代码块）

    - 升序且互不重叠（重叠部分合并——如代码块内恰好有 | 分隔行）；
    - 切分边界不得落在区间内部：区间作为整体归入某块，超长也可整体成块。
    """
    ranges = (_find_table_ranges(text) + _find_html_table_ranges(text)
              + _find_fence_ranges(text))
    ranges.sort()
    merged: List[Tuple[int, int]] = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _bounds_outside_protected(bounds: List[int],
                              protected: List[Tuple[int, int]]) -> List[int]:
    """过滤落在保护区间内部的边界（表格单元格/代码块内的 # 行不是标题）"""
    if not protected:
        return bounds
    return [b for b in bounds
            if not any(ps < b < pe for ps, pe in protected)]


def _filter_continuous_headings(text: str, bounds: List[int],
                                start: int = 0) -> List[int]:
    """连续标题不切：直接相邻的标题行（中间无空行/无正文）不各自成块

    - 直接相邻链 = 标题行两两直接相邻（中间无空行/无正文行）的连续序列；
    - 链中非链尾成员不作为切分边界（并入前一块的内容）；
    - 链尾成员保留为边界当且仅当链首之前有内容（文档头直接是链首时整链
      并入首个正文块，避免产生纯标题块）；
    - 空行间隔的标题（如 "### 三级\\n\\n### 三级之二"）不是相邻链，
      保持独立成块语义；bounds 为全局偏移，start 为子区间起点
    """
    if not bounds:
        return bounds
    result: List[int] = []
    i, n = 0, len(bounds)
    while i < n:
        b = bounds[i]
        # 向后找直接相邻链 [i, j)：相邻标题行之间只有换行符
        j = i + 1
        while j < n:
            line_end = text.find("\n", bounds[j - 1])
            if line_end != -1 and text[line_end + 1:bounds[j]] == "":
                j += 1
            else:
                break
        if j == i + 1:
            result.append(b)  # 单标题（无直接相邻）→ 正常切分边界
        elif text[start:b].strip():
            result.append(bounds[j - 1])  # 链首前有内容 → 链尾保留为边界
        # 链首前为空：整链并入后续正文块（不产生边界，避免纯标题块）
        i = j
    return result


def add_heading_paths(chunks: List["Chunk"], text: str) -> List["Chunk"]:
    """切块后处理：为不含标题行的块拼接其前最近的标题链（enable_heading_in_content）

    - 从原文标题树（正则提取所有标题行 + 位置）为每个块找 char_start 之前
      最近的标题链（如 "第一章 > 1.1"，用 " > " 连接各级标题文本）；
    - 块文本自身已以标题行开头（^#{1,6}）或未找到标题链 → 不拼接；
    - 拼接格式："标题链\\n块文本"（\n 分隔）；
    - 仅修改块 text，char_start/char_end 保持原文偏移不变（定位/归属不受影响）
    """
    if not text or not chunks:
        return chunks
    # 1) 提取全部标题（按位置升序），并维护"当前位置的标题链"：
    #    新标题入栈前弹出所有 level >= 自身 的标题（保证链按层级递增）；
    #    表格/代码块保护区间内的 # 行是内容不是标题，不参与标题链
    protected = find_protected_ranges(text)
    chains: List[Tuple[int, List[str]]] = []  # (标题行偏移, 链标题列表)
    stack: List[Tuple[int, str]] = []         # (level, 标题文本)
    for m in _HEADING_RE.finditer(text):
        if any(ps < m.start() < pe for ps, pe in protected):
            continue
        level, title = len(m.group(1)), m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        chains.append((m.start(), [t for _, t in stack]))
    if not chains:
        return chunks
    # 2) 对每个块：找 char_start 之前最近的标题链，拼接（块已含标题行则跳过）
    chain_titles: List[str] = [c[0] for c in chains]
    result: List["Chunk"] = []
    for c in chunks:
        if _CHUNK_HEADING_RE.search(c.text):
            result.append(c)
            continue
        # 线性找最后一个标题偏移 <= 块起始（标题已按位置排序，倒序首命中即可）
        titles = []
        for off, t in reversed(chains):
            if off <= c.char_start:
                titles = t
                break
        if not titles:
            result.append(c)
            continue
        prefix = " > ".join(titles)
        result.append(Chunk(text=f"{prefix}\n{c.text}",
                            char_start=c.char_start, char_end=c.char_end))
    return result


@dataclass
class Chunk:
    """切块结果：text 为该块文本，char_start/char_end 为相对输入全文的字符区间
    （半开区间 [char_start, char_end)，overlap 时区间为块文本实际覆盖范围）"""

    text: str
    char_start: int
    char_end: int


class Chunker(Protocol):
    """切块协议：输入全文，输出带偏移的切块列表（阶段2 可扩展语义切块实现）"""

    def chunk(self, text: str) -> List[Chunk]:
        ...


class RecursiveChunker:
    """递归字符切块（仿 langchain RecursiveCharacterTextSplitter 逻辑）

    内部算法基于 (文本, 起始偏移) 元组操作，保证每块都能回溯原文位置；
    overlap 续接时新块 = 当前块尾部 overlap 字符 + 分隔符 + 新段，
    起始偏移 = 原起始 + 原长度 - overlap。

    protected_ranges: 表格/代码块等不可切分区间（全局偏移，默认空）；
    传入时保护区间整体成块（超长也保留完整，不切开），区间外文本照常
    递归切分；naive/regex 不传 → 行为与历史完全一致。
    """

    DEFAULT_SEPARATORS = ["\n\n", "\n", "。", "；", " ", ""]

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None,
                 separators: List[str] | None = None,
                 delimiter: str | None = None,
                 protected_ranges: List[Tuple[int, int]] | None = None,
                 sentence_aware: bool = True):
        cfg = get_active_config().chunking
        self.chunk_size = chunk_size if chunk_size is not None else cfg.chunk_size
        self.overlap = overlap if overlap is not None else cfg.chunk_overlap
        if separators is not None:
            self.separators = separators
        elif delimiter:
            # 自定义分隔符（naive 模式参数，str 或 list）：
            # - str：自定义优先，超长段递归退到更细的默认分隔符（兼容旧行为）
            # - list：用户完整自定义分隔符集（删除默认项=该项不参与）；
            #   空列表/全无效时回退默认，防切分退化
            if isinstance(delimiter, list):
                self.separators = [d for d in delimiter
                                   if isinstance(d, str) and d != ""]
                if not self.separators:
                    self.separators = self.DEFAULT_SEPARATORS
            else:
                self.separators = [delimiter, *self.DEFAULT_SEPARATORS]
        else:
            self.separators = self.DEFAULT_SEPARATORS
        self.protected_ranges = protected_ranges or []
        # 句子感知切分：块边界优先落在句子（。！？）之间，单句超长才句内切
        self._sentence_aware = sentence_aware
        # 自定义分隔符列表（naive 完整替代默认集）：句子感知时也作为强边界
        # （分隔符不进块），实现"删默认项/加自定义项"语义
        self._custom_delimiter_list = delimiter if isinstance(delimiter, list) else None

    def chunk(self, text: str) -> List[Chunk]:
        chunks = self._split_sentence_aware(text) if self._sentence_aware \
            else self._split_text(text)
        return [c for c in chunks if c.text and c.text.strip()]

    # 句子边界标点（中英文句号/问号/感叹号——强句界；分号/逗号属句内
    # 分隔符，留给句内递归按分隔符优先级切，避免列举文本过度切碎）
    _SENTENCE_BOUNDARY_RE = re.compile(
        r"[^。！？.!?]+[。！？.!?]?")

    def _split_sentence_aware(self, text: str, start: int = 0) -> List[Chunk]:
        """句子感知切分（方案 B）：先按句子边界（。！？；.!?;）切句子单元，
        再按 chunk_size 贪心合并连续句子成块——**块边界永远在句子之间**；
        单句超长（> chunk_size，如无标点长串）才走句内递归切（方案 A 兜底：
        分隔符优先级 + 硬切回退）。表格/代码块保护区间保持整体成块。
        """
        if not text:
            return []
        # 表格/代码块保护区间预处理（与 _split_text 同逻辑）
        if self.protected_ranges and any(
                ps < start + len(text) and pe > start
                for ps, pe in self.protected_ranges):
            segs: List[Tuple[str, int, bool]] = []
            cursor = start
            for ps, pe in self.protected_ranges:
                if pe <= start or ps >= start + len(text):
                    continue
                lo, hi = max(ps, start), min(pe, start + len(text))
                if lo > cursor:
                    segs.append((text[cursor - start:lo - start], cursor, False))
                segs.append((text[lo - start:hi - start], lo, True))
                cursor = hi
            if cursor < start + len(text):
                segs.append((text[cursor - start:], cursor, False))
            final_chunks: List[Chunk] = []
            for seg_text, seg_start, is_protected in segs:
                if is_protected:
                    stripped = seg_text.strip()
                    if not stripped:
                        continue
                    s = seg_start + (len(seg_text) - len(seg_text.lstrip()))
                    final_chunks.append(Chunk(stripped, s, s + len(stripped)))
                else:
                    final_chunks.extend(
                        self._split_sentence_aware(seg_text, seg_start))
            return final_chunks

        # 按 \n\n 分段（段落强边界：块不跨段落，\n\n 不保留在块内——
        # 与 _split_text 的分隔符优先级语义一致），段内再按句子聚合
        chunks: List[Chunk] = []
        paras = text.split("\n\n")
        offset = start
        for i, para in enumerate(paras):
            para_start = offset
            para_len = len(para)
            offset = para_start + para_len + (2 if i < len(paras) - 1 else 0)
            if not para.strip():
                continue
            # 自定义分隔符列表：先按分隔符（交替正则）切成子段，分隔符不进块
            # （完整替代默认集的"删/加"语义）；无自定义列表则整段一个子段
            if self._custom_delimiter_list:
                pattern = "|".join(
                    re.escape(d) for d in self._custom_delimiter_list if d)
                sub_segs: List[Tuple[str, int]] = []
                if pattern:
                    cursor = para_start
                    for m in re.finditer(pattern, para):
                        sub_segs.append((para[cursor - para_start:m.start()],
                                         cursor))
                        cursor = m.end()
                    sub_segs.append((para[cursor - para_start:], cursor))
                else:
                    sub_segs = [(para, para_start)]
            else:
                sub_segs = [(para, para_start)]
            for seg_text, seg_start in sub_segs:
                if not seg_text.strip():
                    continue
                # 句子单元切分（含边界标点；无标点连续文本整体算一个长句子单元）
                sentences = [
                    (m.group(), seg_start + m.start())
                    for m in self._SENTENCE_BOUNDARY_RE.finditer(seg_text)
                    if m.group().strip()]
                buf: str = ""
                buf_start: int = seg_start
                for s, s_start in sentences:
                    if len(s) > self.chunk_size:
                        # 超长单句（无标点长串）：先封缓冲，再句内递归切（方案 A）
                        if buf:
                            chunks.append(
                                Chunk(buf, buf_start, buf_start + len(buf)))
                            buf = ""
                        chunks.extend(self._split_text(s, s_start))
                        continue
                    if buf and len(buf) + len(s) > self.chunk_size:
                        # 加下句会超大小 → 封块（块边界在句子之间，不拆句）
                        chunks.append(
                            Chunk(buf, buf_start, buf_start + len(buf)))
                        if self.overlap > 0:
                            # overlap 续接：取当前块尾部 overlap 字符（同 _split_text）
                            tail = buf[-self.overlap:]
                            buf_start = buf_start + len(buf) - self.overlap
                            buf = tail + s
                        else:
                            buf, buf_start = s, s_start
                    else:
                        buf += s
                if buf:
                    chunks.append(Chunk(buf, buf_start, buf_start + len(buf)))
        return chunks

    def _split_text(self, text: str, start: int = 0) -> List[Chunk]:
        """递归切分，返回 List[Chunk]（偏移相对整个输入文本；start 为全局偏移）"""
        if not text:
            return []
        # 表格/代码块保护区间预处理：仅当当前段与保护区相交时，相交部分
        # 整体成块（不按 chunk_size 切开），区间外文本递归继续切分
        if self.protected_ranges and any(
                ps < start + len(text) and pe > start
                for ps, pe in self.protected_ranges):
            segs: List[Tuple[str, int, bool]] = []
            cursor = start  # 全局偏移游标（段起始可能 > 0，段文本索引用 cursor-start）
            for ps, pe in self.protected_ranges:
                if pe <= start or ps >= start + len(text):
                    continue  # 与当前段不相交
                lo, hi = max(ps, start), min(pe, start + len(text))
                if lo > cursor:
                    segs.append((text[cursor - start:lo - start], cursor, False))
                segs.append((text[lo - start:hi - start], lo, True))
                cursor = hi
            if cursor < start + len(text):
                segs.append((text[cursor - start:], cursor, False))
            final_chunks: List[Chunk] = []
            for seg_text, seg_start, is_protected in segs:
                if is_protected:
                    # 表格/代码块整体成块（strip 后重算偏移，保持切片一致性）
                    stripped = seg_text.strip()
                    if not stripped:
                        continue
                    s = seg_start + (len(seg_text) - len(seg_text.lstrip()))
                    final_chunks.append(Chunk(stripped, s, s + len(stripped)))
                else:
                    final_chunks.extend(self._split_text(seg_text, seg_start))
            return final_chunks
        final_chunks: List[Chunk] = []

        # 选择第一个能切出多段的（或空）分隔符；new_splits 为 (片段文本, 全局起始)
        separator = self.separators[-1]
        new_splits: List[Tuple[str, int]] = []
        for sep in self.separators:
            if sep == "":
                new_splits = [(ch, start + i) for i, ch in enumerate(text)]
            else:
                # finditer + re.escape 等价于 str.split(sep)（含连续分隔符的空段），
                # 同时拿到每段的全局起始偏移
                new_splits = []
                cursor = 0
                for m in re.finditer(re.escape(sep), text):
                    new_splits.append((text[cursor:m.start()], start + cursor))
                    cursor = m.end()
                new_splits.append((text[cursor:], start + cursor))
            if len(new_splits) > 1 or sep == "":
                separator = sep
                break

        if separator == "":
            # 无任何可用分隔符：按 chunk_size 硬切
            return [Chunk(text[i:i + self.chunk_size], start + i,
                          start + min(i + self.chunk_size, len(text)))
                    for i in range(0, len(text), self.chunk_size)]

        good_splits: List[Tuple[str, int]] = []
        bad_splits: List[Tuple[str, int]] = []
        _current_text = ""
        _current_start = 0
        for split_text, split_start in new_splits:
            if len(split_text) < self.chunk_size:
                if _current_text:
                    candidate = _current_text + separator + split_text
                    if len(candidate) <= self.chunk_size:
                        _current_text = candidate
                    else:
                        good_splits.append((_current_text, _current_start))
                        # 带 overlap 续接：新块 = 当前块尾部 overlap 字符 + 分隔符 + 本段
                        # （tail 在原块中的位置为 [原长-overlap, 原长)，故新起始随之前移）
                        if self.overlap > 0:
                            tail = _current_text[-self.overlap:]
                            _current_start = _current_start + len(_current_text) - self.overlap
                            _current_text = tail + separator + split_text
                        else:
                            _current_text = split_text
                            _current_start = split_start
                else:
                    _current_text = split_text
                    _current_start = split_start
            else:
                if _current_text:
                    good_splits.append((_current_text, _current_start))
                    _current_text = ""
                bad_splits.append((split_text, split_start))
        if _current_text:
            good_splits.append((_current_text, _current_start))

        # 超长段递归（会用更细的分隔符再切）
        for bad_text, bad_start in bad_splits:
            if len(bad_text) > self.chunk_size:
                final_chunks.extend(self._split_text(bad_text, bad_start))
            else:
                final_chunks.append(Chunk(bad_text, bad_start,
                                          bad_start + len(bad_text)))

        good_chunks = [Chunk(t, s, s + len(t)) for t, s in good_splits]
        return good_chunks + final_chunks


class MarkdownSplitter:
    """按标题切块（Markdown # 标题 + 纯文本常见标题样式），标题保留在块首；
    超长段递归 RecursiveChunker

    split_level: 参与切分的最大标题层级 1-6（默认 3）。ATX '# 标题' 按 #
    数量计级；纯文本样式级别映射（见 _iter_headings）：setext = 下划线 → 1 级、
    - 下划线 → 2 级；单行包裹式（===== 标题 ===== 等）与前导符号式（■ 标题 等）
    → 2 级。如传 2 则仅 1~2 级标题切分，### 归入上层块；6 级为父子分块父块
    聚合预留。

    增强（对齐 KnowFlow title 方式）：
    - 表格/代码块完整性：切分边界避开表格（连续 | 行）与 ``` 围栏代码块
      内部，保护区间作为整体归入某块（超长可整体成块）；
    - 标题智能回退：按 split_level 切只切出 1 个超长块时，放宽一级标题
      重切（直至多块/6 级/无更低级标题），避免"章节太长只出一个块"；
    - 连续标题不切：直接相邻的标题行（中间无空行/无正文）不各自成块，
      并入后续内容块（不产生纯标题空块）。
    """

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None,
                 split_level: int | None = None):
        self._recursive = RecursiveChunker(chunk_size=chunk_size, overlap=overlap)
        level = split_level if split_level is not None else 3
        if not 1 <= level <= 6:
            raise ValueError(f"split_level 超出范围: {level}（需 1~6）")
        self.split_level = level

    @staticmethod
    def _heading_bounds(text: str, level: int,
                        protected: List[Tuple[int, int]]) -> List[int]:
        """level 级内标题切分边界（升序）：ATX # 与纯文本标题样式统一识别
        （setext 下划线式/单行包裹式/前导符号式，级别映射见 _iter_headings），
        过滤保护区内标题 + 连续标题不切"""
        bounds = [start for start, lvl, _ in _iter_headings(text, protected)
                  if lvl <= level]
        bounds = _bounds_outside_protected(bounds, protected)
        return _filter_continuous_headings(text, bounds)

    @staticmethod
    def _sections(text: str, bounds: List[int]) -> List[Tuple[str, int]]:
        """按边界切章节段（前瞻式：边界标题保留在段首），返回 (段文本, 全局起始)"""
        parts: List[Tuple[str, int]] = []
        cursor = 0
        for b in bounds:
            parts.append((text[cursor:b], cursor))
            cursor = b
        parts.append((text[cursor:], cursor))
        return parts

    @staticmethod
    def _clamp_chunk(chunk: Chunk, lo: int, hi: int, full: str) -> Chunk | None:
        """递归切块区间夹回章节段边界 [lo, hi)

        段首块长度 < overlap 时，RecursiveChunker 的 overlap 续接会把起点
        回移到段起点之前（吞掉段前空白甚至上一节内容），且该块文本与
        偏移不一致——按原文切片重建，保证块完整落在段内且
        text == full[char_start:char_end]；夹空（段前内容整块被吞）返回 None
        """
        cs, ce = chunk.char_start, chunk.char_end
        if cs >= lo and ce <= hi:
            return chunk
        cs2, ce2 = max(cs, lo), min(ce, hi)
        if cs2 >= ce2:
            return None
        return Chunk(full[cs2:ce2], cs2, ce2)

    def chunk(self, text: str) -> List[Chunk]:
        if not text or not text.strip():
            return []
        protected = find_protected_ranges(text)
        # 超长段递归切分器（带保护区：表格/代码块作为整体不切开）
        recursive = RecursiveChunker(
            chunk_size=self._recursive.chunk_size,
            overlap=self._recursive.overlap,
            separators=self._recursive.separators,
            protected_ranges=protected)
        level = self.split_level
        bounds = self._heading_bounds(text, level, protected)
        parts = self._sections(text, bounds)
        # 标题智能回退：非空段仅 1 个且超长 → 放宽一级标题重切（直至多段
        # /6 级/无更低级标题——避免无标题长文的无谓循环）
        nonempty = [(t, s) for t, s in parts if t.strip()]
        while (len(nonempty) == 1 and level < 6
               and len(nonempty[0][0]) > self._recursive.chunk_size):
            level += 1
            retry = self._heading_bounds(text, level, protected)
            if len(retry) == len(bounds):
                break  # 无更低级标题可切（或新增标题全部被连续标题过滤）
            bounds = retry
            parts = self._sections(text, bounds)
            nonempty = [(t, s) for t, s in parts if t.strip()]
        # 逐段处理：超长段段内递归字符切（表格/代码块不切开）
        chunks: List[Chunk] = []
        for part_text, part_start in parts:
            stripped = part_text.strip()
            if not stripped:
                continue
            # strip 后重算起始（去掉前导空白）
            start = part_start + (len(part_text) - len(part_text.lstrip()))
            if len(stripped) <= self._recursive.chunk_size:
                chunks.append(Chunk(stripped, start, start + len(stripped)))
            else:
                # clamp：段首块过短时 overlap 续接起点会越过段起点（甚至
                # 吞掉上一节内容），夹回段内保证块不跨标题边界
                for c in recursive._split_text(stripped, start):
                    clamped = self._clamp_chunk(c, start, start + len(stripped), text)
                    if clamped and clamped.text and clamped.text.strip():
                        chunks.append(clamped)
        return chunks


class RegexChunker:
    """按正则匹配位置切块（参考 KnowFlow regex.py 的"按匹配位置切块"思路）

    - 用 re.finditer 定位匹配：匹配片段本身与片段之间的文本都作为块，
      保证文本不丢；跳过空匹配（避免 re.split 空匹配导致死循环/丢文本）
    - 超长块再递归 RecursiveChunker（chunk_size/overlap）
    - pattern 为空或编译失败 → ValueError（由调用方决定 400 或写回 failed）
    """

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None,
                 pattern: str | None = None):
        self._recursive = RecursiveChunker(chunk_size=chunk_size, overlap=overlap)
        self.pattern = pattern or ""

    def chunk(self, text: str) -> List[Chunk]:
        if not text or not text.strip():
            return []
        if not self.pattern.strip():
            raise ValueError("正则切块需提供 regex_pattern")
        try:
            compiled = re.compile(self.pattern)
        except re.error as e:
            raise ValueError(f"正则表达式无效: {e}") from e
        # 按匹配位置分段：匹配片段之前文本 / 匹配片段本身 / 尾部剩余文本
        parts: List[Tuple[str, int]] = []
        last_end = 0
        for m in compiled.finditer(text):
            if m.start() > last_end:
                parts.append((text[last_end:m.start()], last_end))
            if m.end() > m.start():
                parts.append((text[m.start():m.end()], m.start()))
            last_end = m.end()
        if last_end < len(text):
            parts.append((text[last_end:], last_end))
        chunks: List[Chunk] = []
        for part_text, part_start in parts:
            stripped = part_text.strip()
            if not stripped:
                continue
            start = part_start + (len(part_text) - len(part_text.lstrip()))
            if len(stripped) <= self._recursive.chunk_size:
                chunks.append(Chunk(stripped, start, start + len(stripped)))
            else:
                chunks.extend(self._recursive._split_text(stripped, start))
        return chunks


@dataclass
class ParentChildChunkResult:
    """父子分块结果：子块（入向量库）、父块（上下文）、子块→父块归属映射
    （child_parent_map 值为父块索引，-1 表示无父块）"""

    children: List[Chunk]
    parents: List[Chunk]
    child_parent_map: Dict[int, int]


# 父块超长兜底阈值：单节超过该字符数视为极端情况（如无标题长文），
# 按 parent_chunk_size 二次切分兜底（正常章节不受 parent_chunk_size 限制）
_PARENT_SECTION_FALLBACK_CHARS = 50_000
# 子块标题边界最大层级（KnowFlow 用 H1~H3；与父块层级取 min，见类 docstring）
_CHILD_HEADING_MAX_LEVEL = 3


class ParentChildChunker:
    """父子分块（对齐 KnowFlow parent_child 语义）

    1. 父块：按 markdown 标题（1~parent_split_level 级）聚合完整章节，
       无大小上限（章节完整，标题行包含在块内）；首个标题前的无标题
       文本（文档头）作为独立父块；防极端情况：单节超过 50_000 字符
       （如无标题长文）按 parent_chunk_size 二次切分兜底——
       parent_chunk_size 是"兜底上限"而非目标大小，正常章节不受其限制
    2. 子块：标题边界层级 = min(3, parent_split_level)（KnowFlow 用
       H1~H3；父块层级 <3 时随父块收紧，保证章节段粒度不粗于父块）。
       全文先按该层级标题切成章节段，段内 RecursiveChunker(chunk_size,
       overlap) 递归切：任何子块不跨章节边界，overlap 只在章节段内部
       生效；章节段以标题行开头 → 段内首个子块自带标题行（便于识别
       章节归属，与 KnowFlow"标题注入"思路一致）
    3. 归属：子块按章节段切分、父块按章节聚合 → 子块 char 区间完整
       落在父块区间内；仅超长单节兜底的字符级父块（无章节语义）边界
       可能被子块 overlap 尾巴跨过 → 回退"起始偏移归属"
    4. 增强（对齐 KnowFlow AST 语义）：
       - 表格/代码块完整性：标题边界避开表格（连续 | 行）与 ``` 围栏
         代码块内部（块内 # 行不是标题）；段内递归字符切分同样避开，
         表格/代码块作为整体归入某块（超长可整体成块）；
       - 连续标题不切：直接相邻的标题行（中间无空行/无正文）不各自成
         段，并入后续内容段（不产生纯标题空父块/空子块）
    """

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None,
                 parent_chunk_size: int = 1024, parent_chunk_overlap: int = 100,
                 parent_split_level: int = 2):
        self._child_splitter = RecursiveChunker(chunk_size=chunk_size,
                                                overlap=overlap)
        # 兜底切分器：仅超长单节（>50_000 字符）按 parent_chunk_size 二次切分时用
        self._fallback_splitter = RecursiveChunker(
            chunk_size=parent_chunk_size, overlap=parent_chunk_overlap)
        if not 1 <= parent_split_level <= 6:
            raise ValueError(f"parent_split_level 超出范围: {parent_split_level}（需 1~6）")
        self.parent_split_level = parent_split_level
        self.child_split_level = min(_CHILD_HEADING_MAX_LEVEL, parent_split_level)
        self._result: ParentChildChunkResult | None = None

    def chunk(self, text: str) -> List[Chunk]:
        """协议兼容入口：返回子块（同时计算并缓存父块与映射）"""
        result = self.chunk_parent_child(text)
        return result.children

    def chunk_parent_child(self, text: str) -> ParentChildChunkResult:
        """执行父子分块，返回完整结果（子块/父块/映射）"""
        if not text or not text.strip():
            self._result = ParentChildChunkResult(children=[], parents=[],
                                                  child_parent_map={})
            return self._result
        # 表格/代码块保护区间：标题边界避开（代码块内的 # 行不是标题），
        # 段内递归字符切分避开（表格/代码块作为整体归入某块，不切开）
        protected = find_protected_ranges(text)
        headings = [m for m in _HEADING_RE.finditer(text)
                    if not any(ps < m.start() < pe for ps, pe in protected)]
        self._protected_ranges = protected
        self._child_splitter.protected_ranges = protected
        self._fallback_splitter.protected_ranges = protected
        parents = self._build_parents(text, headings)
        children = self._build_children(text, headings)
        self._result = ParentChildChunkResult(
            children=children, parents=parents,
            child_parent_map=self._map_children_to_parents(children, parents),
        )
        return self._result

    def get_parent_chunks(self) -> List[Chunk]:
        """父块列表（须先调用 chunk/chunk_parent_child，否则抛错）"""
        if self._result is None:
            raise RuntimeError("尚未执行切块，无法获取父块（请先调用 chunk/chunk_parent_child）")
        return self._result.parents

    def get_mapping(self) -> Dict[int, int]:
        """子块索引 → 父块索引 映射（须先调用 chunk/chunk_parent_child，否则抛错）"""
        if self._result is None:
            raise RuntimeError("尚未执行切块，无法获取映射（请先调用 chunk/chunk_parent_child）")
        return self._result.child_parent_map

    @staticmethod
    def _split_sections(text: str, headings: List[re.Match], level: int,
                        start: int = 0, end: int | None = None) -> List[Tuple[int, int]]:
        """按标题层级切章节段：返回 [(start, end)] 半开区间（段含标题行）

        - 边界 = level 级以内标题行的起点（连续标题不切：直接相邻的标题
          行不各自成段，并入后续内容段）；段 = [边界标题起点, 下一边界起点)
        - 首个边界前的文本（文档头/节内前置正文，strip 后非空）为独立段
        - 区间 [start, end) 内无边界 → 整段为唯一章节段
        """
        end = len(text) if end is None else end
        bounds = [m.start() for m in headings
                  if start <= m.start() < end and len(m.group(1)) <= level]
        bounds = _filter_continuous_headings(text, bounds, start=start)
        if not bounds:
            return [(start, end)]
        sections: List[Tuple[int, int]] = []
        if bounds[0] > start and text[start:bounds[0]].strip():
            sections.append((start, bounds[0]))
        for i, b in enumerate(bounds):
            b_end = bounds[i + 1] if i + 1 < len(bounds) else end
            sections.append((b, b_end))
        return sections

    @staticmethod
    def _make_chunk(text: str, s: int, e: int) -> Chunk:
        """原文区间 [s, e) 切块：strip 前后空白并重算偏移
        （保证 text == full[char_start:char_end] 切片一致）"""
        raw = text[s:e]
        stripped = raw.strip()
        start = s + (len(raw) - len(raw.lstrip()))
        return Chunk(stripped, start, start + len(stripped))

    @staticmethod
    def _clamp_chunk(chunk: Chunk, lo: int, hi: int, full: str) -> Chunk | None:
        """子块区间夹回章节段边界 [lo, hi)

        段首块长度 < overlap 时，RecursiveChunker 的 overlap 续接会把起点
        回移到段起点之前（吞掉段前空白甚至上一段内容），且该块文本与
        偏移不一致——按原文切片重建，保证子块完整落在段内且
        text == full[char_start:char_end]；夹空（段前内容整块被吞）返回 None
        """
        cs, ce = chunk.char_start, chunk.char_end
        if cs >= lo and ce <= hi:
            return chunk
        cs2, ce2 = max(cs, lo), min(ce, hi)
        if cs2 >= ce2:
            return None
        return Chunk(full[cs2:ce2], cs2, ce2)

    def _build_parents(self, text: str, headings: List[re.Match]) -> List[Chunk]:
        """父块：完整章节（无大小上限）；超长单节按 parent_chunk_size 兜底"""
        parents: List[Chunk] = []
        for s, e in self._split_sections(text, headings, self.parent_split_level):
            if not text[s:e].strip():
                continue
            if e - s > _PARENT_SECTION_FALLBACK_CHARS:
                parents.extend(self._fallback_split_section(text, headings, s, e))
            else:
                parents.append(self._make_chunk(text, s, e))
        return parents

    def _fallback_split_section(self, text: str, headings: List[re.Match],
                                s: int, e: int) -> List[Chunk]:
        """超长单节兜底：节内子段（子块章节边界）按 parent_chunk_size 贪心聚合

        - 子段 = 节内 child_split_level 级标题边界切出的完整章节段，
          聚合父块由完整子段构成 → 子块仍完整落在父块内；
        - 单个子段本身超 parent_chunk_size（如无标题长文的唯一段）：
          交给 _fallback_splitter 字符级切分（其边界无章节语义，
          归属回退起始偏移）
        """
        sub_sections = self._split_sections(text, headings, self.child_split_level,
                                            start=s, end=e)
        parents: List[Chunk] = []
        bad: List[Tuple[int, int]] = []
        cur_start, cur_len = -1, 0
        for ss, se in sub_sections:
            seg_len = se - ss
            if seg_len > self._fallback_splitter.chunk_size:
                # 单子段超长：封当前聚合块，该子段走字符级切分
                if cur_start >= 0:
                    parents.append(self._make_chunk(text, cur_start, ss))
                    cur_start, cur_len = -1, 0
                bad.append((ss, se))
            elif cur_start < 0 or cur_len + seg_len <= self._fallback_splitter.chunk_size:
                if cur_start < 0:
                    cur_start, cur_len = ss, seg_len
                else:
                    cur_len += seg_len
            else:
                parents.append(self._make_chunk(text, cur_start, ss))
                cur_start, cur_len = ss, seg_len
        if cur_start >= 0:
            parents.append(self._make_chunk(text, cur_start, e))
        for bs, be in bad:
            seg = text[bs:be]
            stripped = seg.strip()
            start = bs + (len(seg) - len(seg.lstrip()))
            end = start + len(stripped)
            for c in self._fallback_splitter._split_text(stripped, start):
                clamped = self._clamp_chunk(c, start, end, text)
                if clamped and clamped.text and clamped.text.strip():
                    parents.append(clamped)
        return parents

    def _build_children(self, text: str, headings: List[re.Match]) -> List[Chunk]:
        """子块：章节段内递归字符切（overlap 只在段内生效，不跨章节）"""
        children: List[Chunk] = []
        for s, e in self._split_sections(text, headings, self.child_split_level):
            seg = text[s:e]
            stripped = seg.strip()
            if not stripped:
                continue
            start = s + (len(seg) - len(seg.lstrip()))
            end = start + len(stripped)
            for c in self._child_splitter._split_text(stripped, start):
                # clamp：段首块过短时 overlap 续接起点会越过段起点，夹回段内
                clamped = self._clamp_chunk(c, start, end, text)
                if clamped and clamped.text and clamped.text.strip():
                    children.append(clamped)
        return children

    @staticmethod
    def _map_children_to_parents(children: List[Chunk],
                                 parents: List[Chunk]) -> Dict[int, int]:
        """子块归属父块：区间完整包含优先（子块按章节切分，天然完整落在
        其章节聚合的父块内）；兜底字符级父块（无章节语义）边界可能被
        子块 overlap 尾巴跨过 → 回退"起始偏移归属"；再回退前最近父块"""
        mapping: Dict[int, int] = {}
        for idx, child in enumerate(children):
            p_idx = -1
            for pi, parent in enumerate(parents):
                if parent.char_start <= child.char_start and child.char_end <= parent.char_end:
                    p_idx = pi
                    break
            if p_idx == -1:
                # 回退：起始偏移落在父块区间（兜底字符级父块边界无章节语义）
                for pi, parent in enumerate(parents):
                    if parent.char_start <= child.char_start < parent.char_end:
                        p_idx = pi
                        break
            if p_idx == -1 and parents:
                # 子块起始落在父块间空白空隙：归其前最近父块；无则归首个父块
                for pi in range(len(parents) - 1, -1, -1):
                    if parents[pi].char_start <= child.char_start:
                        p_idx = pi
                        break
                if p_idx == -1:
                    p_idx = 0
            mapping[idx] = p_idx
        return mapping


# QA 问答切块：问题标记 = 块内行首（允许前导空白）"问/Q/q + 冒号"（全角：/半角: 均可）
_QA_QUESTION_RE = re.compile(r"(?:问|Q|q)\s*[:：]\s*")
# 答案标记（格式识别定义：段首"答/A/a + 冒号"；问答对聚合不依赖它——
# 答案可无标记、可跨多段，直到下一个问题标记为止）
_QA_ANSWER_RE = re.compile(r"(?:答|A|a)\s*[:：]\s*")


@dataclass
class QaStats:
    """QA 规范性统计：问答对数量 / 聚合段数（与 QaChunker 切块共用同一
    段落切分与问题块判定，口径一致）"""

    qa_pairs: int
    total_paragraphs: int


def _split_paragraphs(text: str) -> List[Tuple[str, int]]:
    """按空行/连续换行（\\n\\s*\\n）切分段落，返回 [(段文本, 段起始偏移)]

    - 空行 = 两个换行之间只含空白（含 \\n\\n / \\n \\n / 连续多个空行）；
    - 空白段（连续空行产生的空段）跳过，不计数、不参与切块。
    """
    parts: List[Tuple[str, int]] = []
    cursor = 0
    for m in re.finditer(r"\n\s*\n", text):
        if text[cursor:m.start()].strip():
            parts.append((text[cursor:m.start()], cursor))
        cursor = m.end()
    if text[cursor:].strip():
        parts.append((text[cursor:], cursor))
    return parts


def _block_has_question(seg: str) -> bool:
    """块内是否含问题标记行（逐行行首匹配 (?:问|Q|q)\\s*[:：]）

    - 问题标记允许不在块首：如"标题行\\n问：…？\\n答：…。"同块时，
      标题行不阻断问答对识别（标题并入该块，不单独成段）；
    - 同块多个问标记行按 1 个问题块计（与 QaChunker 块级切块一致）。
    """
    return any(_QA_QUESTION_RE.match(line.lstrip())
               for line in seg.splitlines())


def _count_qa_segments(paragraphs: List[str]) -> int:
    """问答对聚合段数（analyze_qa_format 专用统计口径）：

    - 问题块（块内含问题标记行）与其后所有非问题块合并为 1 个问答对段，
      答案可跨多个原始块（如示例3 答案 5 行连续段落）直至下一问题块；
    - 开头杂项（第一个问题块之前）：
      - 恰 1 块（如标题）→ 并入第一个问答对段，不拉低占比；
      - 多块（叙述文场景）→ 每块独立成段，拉低占比（保证"叙述文夹
        1 个问答对"类文档仍可能不达标）；
    - 全文无问题块 → 每个原始块 1 段（0 对，占比必为 0 不合格）。
    """
    q_idx = [i for i, seg in enumerate(paragraphs) if _block_has_question(seg)]
    if not q_idx:
        return len(paragraphs)
    first = q_idx[0]
    return len(q_idx) if first <= 1 else len(q_idx) + first


def analyze_qa_format(text: str) -> QaStats:
    """QA 规范性统计纯函数（与 QaChunker 共用 _split_paragraphs 段落切分
    与 _block_has_question 问题块判定，口径天然一致）：
    - 总段落数 = 按空行/连续换行切分后，再按问答对聚合的段数（问块+其后
      所有答案块=1 段；开头单块标题并入第一问答对；多块杂项独立成段）；
    - 问答对数量 = 含问题标记行（全角/半角冒号、大小写 Q/q 均可，允许
      块内非首行）的块数；答案跨多段不增加对数。
    """
    paragraphs = [seg for seg, _ in _split_paragraphs(text)]
    qa_pairs = sum(1 for seg in paragraphs if _block_has_question(seg))
    return QaStats(qa_pairs=qa_pairs,
                   total_paragraphs=_count_qa_segments(paragraphs))


def is_qa_format_valid(stats: QaStats, min_ratio: float = 0.5) -> bool:
    """QA 规范性判定：问答对占比（问答对 / 总段落）>= min_ratio（默认 50%）合格；
    空文档（无段落）视为不合格（无问答对可入库）"""
    if stats.total_paragraphs <= 0:
        return False
    return stats.qa_pairs / stats.total_paragraphs >= min_ratio


class QaChunker:
    """QA 问答切块：问答对整块（问题段起，答案跨多段保留，含原文问/答标记）

    - 段落 = 按空行/连续换行（\\n\\s*\\n）切分（与 analyze_qa_format 同口径，
      复用 _split_paragraphs 与 _block_has_question，判定一致）；
    - 问题块 = 块内任意行行首（允许前导空白）匹配问题标记 (?:问|Q|q)\\s*[:：]
      （全角/半角冒号、大小写；允许标题与问题行同块）；问题块之后到下一个
      问题块之前的所有内容（答案段，可带"答："标记也可无标记、可跨多段）
      归入该问答对；
    - 问答对整体成一块：text 保留原文（含"问：/答："标记，不破坏偏移契约），
      char_start/char_end 按原文本偏移（text == full[char_start:char_end]）；
    - 第一个问题段之前的杂项内容合并为独立普通块；全文无问题标记 → 整文
      一个普通块（内容不丢失，QA 对优先、其余兜底）；
    - 超长问答对整体成块，不按 chunk_size 二次切分（问答对完整性优先；
      chunk_size/overlap 为兼容参数，暂不生效）
    """

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None):
        self.chunk_size = chunk_size
        self.overlap = overlap

    @staticmethod
    def _make_chunk(text: str, s: int, e: int) -> Chunk:
        """原文区间 [s, e) 成块：strip 前后空白并重算偏移（切片一致性）"""
        raw = text[s:e]
        stripped = raw.strip()
        start = s + (len(raw) - len(raw.lstrip()))
        return Chunk(stripped, start, start + len(stripped))

    def chunk(self, text: str) -> List[Chunk]:
        if not text or not text.strip():
            return []
        paragraphs = _split_paragraphs(text)
        q_indices = [i for i, (seg, _) in enumerate(paragraphs)
                     if _block_has_question(seg)]
        if not q_indices:
            # 全文无问题标记：整文一个普通块（内容不丢失）
            return [self._make_chunk(text, 0, len(text))]
        chunks: List[Chunk] = []
        # 文档头杂项（第一个问题段之前）：合并为独立普通块
        first_q = q_indices[0]
        if first_q > 0:
            chunks.append(self._make_chunk(
                text, paragraphs[0][1], paragraphs[first_q][1]))
        # 问答对：问题段起 → 下一个问题段前（答案跨段内容完整保留）
        for i, idx in enumerate(q_indices):
            start = paragraphs[idx][1]
            end = (paragraphs[q_indices[i + 1]][1]
                   if i + 1 < len(q_indices) else len(text))
            chunks.append(self._make_chunk(text, start, end))
        return chunks


def get_chunker(method: str, config: dict) -> Chunker:
    """切块器工厂：按 method 构造，config 为入库参数

    - naive → RecursiveChunker（delimiter 可选，为空用默认分隔符列表）
    - title → MarkdownSplitter（split_level 标题层级，默认 3）
    - regex → RegexChunker（regex_pattern 必填，为空 chunk 时抛 ValueError）
    - parent_child → ParentChildChunker（父块参数 parent_chunk_size/
      parent_chunk_overlap/parent_split_level，子块参数 chunk_size/overlap）
    - qa → QaChunker（问答对整块，chunk_size/overlap 兼容参数暂不生效）
    """
    if method == "naive":
        return RecursiveChunker(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
            delimiter=config.get("delimiter") or None,
        )
    if method == "title":
        return MarkdownSplitter(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
            split_level=config.get("split_level"),
        )
    if method == "regex":
        return RegexChunker(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
            pattern=config.get("regex_pattern"),
        )
    if method == "parent_child":
        return ParentChildChunker(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
            parent_chunk_size=config.get("parent_chunk_size"),
            parent_chunk_overlap=config.get("parent_chunk_overlap"),
            parent_split_level=config.get("parent_split_level"),
        )
    if method == "qa":
        return QaChunker(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
        )
    raise ValueError(f"未知切块方式: {method}（支持: {'/'.join(VALID_METHODS)}）")


# 默认切块器（通用切块 + 超长递归），env 可配
def default_chunker() -> Chunker:
    return RecursiveChunker()
