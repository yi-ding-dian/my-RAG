"""切块公共逻辑：标题识别（ATX + 纯文本样式）、表格/代码块/图片保护区间、
连续标题过滤、标题链注入（add_heading_paths）

被 title/parent_child 等方法共同依赖——独立成文件，一处修改全方法生效。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from backend.chunking.base import Chunk
from backend.chunking.heading_presets import match_system_position as _match_system_position

# 支持的切块方式（ingest 请求 method / 文档 parser_id 取值范围；
# agentic=Agentic 智能分块（LLM 读全文切逻辑段落+标签），异步实现见
# backend/services/agentic_chunker.py，get_chunker 不支持（ingestion 特殊分支）；
# hierarchical=规范文档层级聚合切块，实现在独立包 backend/normative（自带标题链，
# 入库流程不应再叠加 add_heading_paths）
VALID_METHODS = ("naive", "title", "regex", "parent_child", "qa", "agentic",
                 "hierarchical")

# 标题路径正则：识别 1~6 级 Markdown 标题行（标题树，供父标题前缀拼接）
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
# 块文本是否已含标题行（含则不再拼接父标题）；必须整行捕获标题文本——
# 只匹配到首个非空白字符（\S）会让后续"按标题文本在标题链中定位"永远失配，
# 块首自带标题的块补不出祖先链
_CHUNK_HEADING_RE = re.compile(r"^#{1,6}\s+.+", re.MULTILINE)

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


# 整行加粗标题：**标题**（首尾 ** 配对、中间不含 **）
_BOLD_HEADING_RE = re.compile(r"^\*\*(.+)\*\*$")


def _unwrap_bold_heading(line: str) -> str | None:
    """整行加粗标题解析：命中返回标题内容，否则 None

    整行就是 `**文字**` 的行，语义上通常是标题。常见来源：
    - MinerU **未经 PDF 转换直接解析 Office 文档**（如 docx/ppt）时的产物：
      版面分析没参与，标题样式没被还原，只留下加粗；
    - Word 里手动加粗排版标题（未使用 Heading 样式）。

    这类行既无 `#`，也不属于既有五种纯文本标题样式（setext / 包裹式 /
    前导符号式 / 编号式），加这一支把它们接住。

    判据从严（宁可漏认，也不能把正文里的加粗强调行当标题）：
    - 整行就是 `**...**`：首尾配对且**中间不再含 `**`**（多个加粗块并列
      的行不是标题）；
    - 内容非空、<= 50 字符、非纯符号行（复用 _is_heading_text_candidate）；
    - 不含句末标点（。！？；）与逗号——标题几乎不含，含之的多半是正文句。
    """
    s = line.strip()
    m = _BOLD_HEADING_RE.match(s)
    if not m:
        return None
    inner = m.group(1).strip()
    if "**" in inner:
        return None
    if not _is_heading_text_candidate(inner):
        return None
    if any(ch in inner for ch in _SENTENCE_PUNCT) or "，" in inner or "," in inner:
        return None
    return inner


# 纯文本编号标题识别：只认体系里的**高层编号**（拼接长表位置 <= 本值）。
# legal 体系下即 编(0)/章(1)/节(2)，条(3)/款(4)/项(5) 不认——正文里
# "第十六条　……"这类条文正文同样匹配编号正则，认了会把每一条都变成标题
# 边界，切块碎成一片、标题链也烂掉。
_PLAIN_HEADING_MAX_POS = 2
# 句末标点：标题几乎不含；含之的多半是"第三章规定了…。"这类引用章节号的正文句
_SENTENCE_PUNCT = "。！？；"

# ---- 标题末尾标点白名单（配置档案可改，见 config.ChunkingConfig）----
# 管辖范围：只有**末尾落在这些字符里**才受白名单裁决（"句末标点"）。
#   - 中文 。？！；… + 半角 .?!;
#   - **不含冒号/顿号/逗号**：它们是标题的正常组成部分（实测真实文档里
#     "注："、"第二步：" 都是真标题，"一、总则" 的顿号在行内），纳入管辖会大面积误杀。
_END_PUNCT_CHARS = "。？！；…" + ".?!;"
# 默认白名单：GB/T 15834—2011《标点符号用法》——"文章标题的末尾通常不用标点
# 符号，但有时根据需要**可用问号、叹号或省略号**"（含半角形式）。
_DEFAULT_END_PUNCT_WHITELIST = ("？", "！", "…", "?", "!")


def resolve_end_punct_whitelist(whitelist=None) -> frozenset:
    """标题末尾标点白名单：显式传入优先；否则读当前激活配置，回退默认值

    空列表**回退默认值**，而非"什么都不认"——后者会让所有问句标题（"什么是 X？"）
    一夜之间全部消失。运行时读取，改配置后重新入库即生效。
    """
    if whitelist is not None:
        return (frozenset(whitelist) if whitelist
                else frozenset(_DEFAULT_END_PUNCT_WHITELIST))
    try:
        from backend.config import get_active_config
        configured = get_active_config().chunking.heading_end_punct_whitelist
        if configured:
            return frozenset(configured)
    except Exception:  # 配置不可用（导入期/单测）→ 用默认
        pass
    return frozenset(_DEFAULT_END_PUNCT_WHITELIST)


def passes_end_punct(title: str, whitelist=None) -> bool:
    """标题末尾标点判定：末尾不是句末标点 → 通过；是 → 须在白名单里

    **只看最后一个字符**：多标点连用（"…生效！！"、"真的吗？？"）取末尾那个。
    非句末标点（普通字、右括号、冒号、顿号…）一律不受管辖。
    """
    t = (title or "").rstrip()
    if not t:
        return True
    if t[-1] not in _END_PUNCT_CHARS:
        return True  # 非句末标点：白名单不管
    return t[-1] in resolve_end_punct_whitelist(whitelist)


def _prev_nonblank(lines: List[str], i: int) -> Optional[int]:
    """从下标 i 起向上找第一个非空行（无则 None）"""
    while i >= 0:
        if lines[i].strip():
            return i
        i -= 1
    return None


def _is_plain_numbered_heading(lines: List[str], i: int, s: str,
                               systems: List[str]) -> bool:
    """无任何标记的编号标题行（MinerU 漏标 `##`，行内只剩编号 + 标题文本）

    仅在调用方提供编号体系时启用——体系已检测出该文档用"第X章"这类编号做
    标题（其余标题带 `#`），那么裸编号行也该认。实测同一份文档里多数标题带
    `##`、少数漏标，漏标处即导致目录树缺章、切块少边界。

    约束从严（宁可漏认，不能把正文或目录当标题）：
    - 编号属体系高层（见 _PLAIN_HEADING_MAX_POS）；
    - 短、非表格/HTML 行、不以冒号结尾（字段/程序输出行特征）；
    - 不含句末标点（引用章节号的正文句常含）；
    - **前后都须是空行**：标题独立成段；顺带挡掉目录里"标题行紧挨标题行"
      的连续排列（只有首行前面是空行）；
    - **目录区判据**：越过空行往上，连续两行也是编号行 → 判为目录（正文里
      章节标题之间必有正文，不会编号行挨着编号行）。目录区里
      "…第十一章\n\n第十二章\n\n第十三章"这类被空行隔开的行靠这条挡下
      （实测否则会切出几个字符的碎片块）。
    """
    if len(s) > _HEADING_MAX_LEN or s.startswith(("<", "|")):
        return False
    if s.endswith((":", "：")):
        return False
    if any(ch in s for ch in _SENTENCE_PUNCT):
        return False
    if i > 0 and lines[i - 1].strip():
        return False
    if i + 1 < len(lines) and lines[i + 1].strip():
        return False
    p1 = _prev_nonblank(lines, i - 1)
    if p1 is not None and _match_system_position(lines[p1].strip(), systems):
        p2 = _prev_nonblank(lines, p1 - 1)
        if (p2 is not None
                and _match_system_position(lines[p2].strip(), systems)):
            return False  # 上面连着两行编号 → 目录区，不是标题
    pos = _match_system_position(s, systems)
    return pos is not None and pos <= _PLAIN_HEADING_MAX_POS


def _iter_headings(text: str,
                   protected: List[Tuple[int, int]] | None = None,
                   heading_systems: List[str] | None = None,
                   end_punct_whitelist: List[str] | None = None,
                   ) -> List[Tuple[int, int, str]]:
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
    - 整行加粗式 '**标题**'（首尾 ** 配对、中间不含 **、<=50 字符、不含
      句末标点/逗号）→ 级别 2。常见于 MinerU 未经 PDF 转换直接解析 Office
      文档的产物、以及 Word 里手动加粗排版的标题；提供编号体系时级别按
      编号推断（见 _unwrap_bold_heading）

    heading_systems（可选，编号体系名列表如 ["chapter_body","numeric_multi"]）：
    提供时，ATX 标题的级别**优先按标题文本的编号推断**（并列拼接长表位置 + 1，
    见 heading_presets）——MinerU 把 PDF 所有标题输出为 `##`（层级丢失），
    真实层级藏在编号里（如"一、"=章、"1.1"=节、"1.1.1"=小节）；编号未命中
    或未提供体系时回退 # 数量（既有行为）。

    end_punct_whitelist（可选，标题末尾标点白名单）：
    标题末尾的句末标点须落在此列表内；None = 读当前激活配置（配置为空回退
    默认值，见 resolve_end_punct_whitelist）。

    防误判：
    - 纯符号行（单独一行 ======== / ------ 装饰线）不是标题内容；
    - 表格/代码块保护区间内的行不是标题（protected 过滤）；
    - 标题末尾若是**句末标点**（。？！；… 及半角 .?!;），须落在**末尾标点白名单**
      内才认（默认取国标列举的问号/叹号/省略号，配置档案可改，见 passes_end_punct）——
      挡的是解析器把正文句、表格单元格文本或列表项标成标题的误判（实测某检测方案里
      表格单元格"电气设备如有异常则在图元标注黄色背景。"被标成 `##`，进目录树后把
      真实章节链整段弹空，后续章节全部挂错父级）。非句末标点（右括号、冒号、顿号…）
      **不受白名单管辖**；
    - setext 内容行不以 < 开头（XML/HTML 标签行，如 '<Breaker::湖北>'）、
      不以 | 开头（表格行）、不以 :/：结尾（字段定义/程序输出标签行，
      以冒号结尾的行）、非纯符号、<=50 字符；
    - 一行已按 ATX/包裹式/前导符号式识别为标题时，不再重复识别为 setext
      内容行（避免 '【标题】\\n====' 双重识别）；
    - 连续标题（直接相邻标题行不各自成块）由调用方 _filter_continuous_headings
      负责，本函数不处理。
    """
    lines = text.split("\n")
    # 第一遍：识别所有标题 + 编号位置（ATX 行带体系时记位置，其余记 None）
    raw: List[Tuple[int, int, str, Optional[int]]] = []  # (偏移, #级别, 标题, 编号位置)
    pos = 0
    for i, line in enumerate(lines):
        start = pos
        pos += len(line) + 1  # 换行符偏移（末行多余 +1 无害，不参与产出）
        s = line.strip()
        if not s:
            continue
        title, level, sys_pos = "", 0, None
        m = _HEADING_RE.match(line)
        if m:  # ATX：'# 标题'
            title = m.group(2).strip()
            level = len(m.group(1))
            if heading_systems:
                sys_pos = _match_system_position(title, heading_systems)
        else:
            inner = _unwrap_wrapped_heading(line)
            if inner is not None:
                title, level = inner, 2  # 单行包裹式
            else:
                lm = _LEADING_MARK_RE.match(line)
                if lm is not None and len(s) <= _HEADING_MAX_LEN:
                    title, level = lm.group(2).strip(), 2  # 前导符号式
                elif (bold := _unwrap_bold_heading(line)) is not None:
                    # 整行加粗式（见 _unwrap_bold_heading）：级别默认 2，
                    # 提供编号体系时按编号推断（加粗本身不含层级信息）
                    title, level = bold, 2
                    if heading_systems:
                        sys_pos = _match_system_position(bold, heading_systems)
                elif (heading_systems
                      and _is_plain_numbered_heading(lines, i, s,
                                                     heading_systems)):
                    # 纯文本编号标题（MinerU 漏标 `#` 时兜底）；级别由第二遍
                    # 按编号位置归一化覆盖
                    title, level = s, 2
                    sys_pos = _match_system_position(s, heading_systems)
                elif (i + 1 < len(lines)
                        and _is_heading_text_candidate(line)
                        and not s.startswith(("<", "|"))
                        and not s.endswith((":", "："))):
                    # setext：内容行 + 下一行是整行下划线
                    if _SETEXT_EQ_RE.match(lines[i + 1]):
                        title, level = s, 1
                    elif _SETEXT_DASH_RE.match(lines[i + 1]):
                        title, level = s, 2
        # 统一出口判定：所有识别路径共用同一把尺子——标题末尾的句末标点
        # 必须在白名单内（非句末标点不受管辖，见 passes_end_punct）
        if (title and passes_end_punct(title, end_punct_whitelist)
                and (not protected
                     or not any(ps < start < pe for ps, pe in protected))):
            raw.append((start, level, title, sys_pos))

    # 第二遍：编号位置归一化（文档内实际用到的位置 → 1..N，保持相对顺序）
    # 设计：体系拼接表的绝对位置（如 chapter_body+numeric_multi 下 2/5/6/8）
    # 直接当级别会与 parent_split_level（1~6 markdown 层级语义）尺度不符
    # （级别 5 的"13.1"会让 parent_split_level=2 全部过滤掉）→ 映射到文档
    # 实际层级序列：最小位置 → 1（章）、次小 → 2（节）……
    norm: Dict[int, int] = {}
    if heading_systems:
        used = sorted({p for _o, _l, _t, p in raw if p is not None})
        norm = {p: i + 1 for i, p in enumerate(used)}
    result: List[Tuple[int, int, str]] = []
    for start, level, title, sys_pos in raw:
        if sys_pos is not None and norm:
            level = norm[sys_pos]  # 编号推断级别（归一化，替代 # 数量）
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


# 图片引用：markdown ![alt](src) / HTML <img>（作为原子保护，见 _find_image_ranges）
_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_IMAGE_HTML_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# 图片摘要回填的图注块（与 services/image_summary.py 的 SUMMARY_PREFIX 对应）：
# 首行 `> 图片说明：`，其后是连续的 `> ` 引用行
_IMAGE_NOTE_RE = re.compile(r"\n[ \t]*>[ \t]*图片说明[：:][^\n]*(?:\n[ \t]*>[^\n]*)*")


def _find_image_ranges(text: str) -> List[Tuple[int, int]]:
    """定位图片引用区间（markdown ![alt](src) / HTML <img>）+ 紧随的图注块

    背景（修复前实测 bug）：图片引用含英文感叹号 `!`，被句子感知切分
    （句界集合含 `!`）当作句子分隔符拆碎——块文本变成残缺的 `[](url)`，
    前端不渲染、显示为链接文本。引用作整体保护后与表格同等待遇：
    原子并入某块，不被切分毁坏（alt/src 保持完整）。

    图片摘要（services/image_summary.py）会把模型读出的图内文字回填到图片
    **下方**的 `> 图片说明：` 引用块。那块文字就是这张图的"可检索化身"，
    必须与图一起作为原子区间——否则切块边界落在两者之间时图与文字分家，
    摘要的检索价值就丢了。故此处把「图片引用 + 紧随的图注块」扩成一个区间。
    """
    spans = [(m.start(), m.end()) for m in _IMAGE_MD_RE.finditer(text)]
    spans += [(m.start(), m.end()) for m in _IMAGE_HTML_RE.finditer(text)]
    for m in _IMAGE_MD_RE.finditer(text):
        note = _IMAGE_NOTE_RE.match(text, m.end())
        if note:
            spans.append((m.start(), note.end()))
    return spans


def _extend_to_preceding_heading(text: str,
                                 ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """保护区间前扩展：紧邻表格（中间至多 1 空行）的标题行并入区间。

    背景：超长表格触发 title 切块智能回退（忽略标题边界重切）时，
    "标题行+空行"会被字符窗口切走成孤儿块（几十字、无任何数据、
    检索无意义——如 Excel 分段表 "## Sheet: xx (第 1-10 行)"）。
    标题行并入保护区间后，回退切分不会把标题与表格拆开，
    块内标题定位信息 + 表头 + 数据自解释。
    仅表格前紧邻标题行时生效（前有说明文字/段落时不动，保持常规边界语义）。
    """
    out: List[Tuple[int, int]] = []
    for s, e in ranges:
        pre = text[:s]
        m = re.search(
            r"(?:^|\n)([ \t]*#{1,6}[ \t]+[^\n]*)[ \t]*\n[ \t]*\n?$",
            pre,
        )
        if m:
            s = m.start(1)
        out.append((s, e))
    return out


def find_protected_ranges(text: str) -> List[Tuple[int, int]]:
    """返回不可切分的区间列表（markdown 表格 / HTML 表格 / 围栏代码块）

    - 升序且互不重叠（重叠部分合并——如代码块内恰好有 | 分隔行）；
    - 切分边界不得落在区间内部：区间作为整体归入某块，超长也可整体成块；
    - 紧邻表格的标题行并入区间（见 _extend_to_preceding_heading：
      防超长回退时标题被切飞成孤儿块）。
    """
    ranges = (_find_table_ranges(text) + _find_html_table_ranges(text)
              + _find_fence_ranges(text) + _find_image_ranges(text))
    ranges.sort()
    merged: List[Tuple[int, int]] = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return _extend_to_preceding_heading(text, merged)


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


def add_heading_paths(chunks: List["Chunk"], text: str,
                      heading_systems: List[str] | None = None,
                      end_punct_whitelist: List[str] | None = None
                      ) -> List["Chunk"]:
    """切块后处理：为块拼接其标题链（enable_heading_in_content）

    - 从原文标题树（_iter_headings 识别 + 位置）为每个块找 char_start 之前
      最近的标题链（如 "第一章 > 1.1"，用 " > " 连接各级标题文本）；
    - heading_systems 提供时按**编号推断级别**建链（MinerU 全 ## 扁平输出
      时 "一、 > 1.1 > 1.1.1" 能正确分层；见 heading_presets）；
    - **块首自带标题行**时：若该标题有更上级的祖先标题未含块内，把祖先链
      拼到块首（如子块 "## 1.1 节标题\n…" 补出 "一、章标题" 作前缀）
      ——块首即链首（无更上级祖先）则不再拼接；
    - 拼接格式："标题链\\n块文本"（\n 分隔）；
    - 仅修改块 text，char_start/char_end 保持原文偏移不变（定位/归属不受影响）
    """
    if not text or not chunks:
        return chunks
    # 1) 提取全部标题（按位置升序），并维护"当前位置的标题链"：
    #    新标题入栈前弹出所有 level >= 自身 的标题（保证链按层级递增）；
    #    表格/代码块保护区间内的 # 行是内容不是标题，不参与标题链
    protected = find_protected_ranges(text)
    headings = _iter_headings(text, protected, heading_systems,
                              end_punct_whitelist)
    chains: List[Tuple[int, List[str]]] = []  # (标题行偏移, 链标题列表)
    stack: List[Tuple[int, str]] = []         # (level, 标题文本)
    for off, level, title in headings:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        chains.append((off, [t for _, t in stack]))
    if not chains:
        return chunks

    def _chain_before(offset: int) -> List[str]:
        """offset 之前最近的完整标题链（倒序首命中）"""
        for off, t in reversed(chains):
            if off <= offset:
                return t
        return []

    result: List["Chunk"] = []
    for c in chunks:
        # 块首标题（^#{1,6} 行）：查它的祖先链是否缺链首，缺则补祖先前缀
        m = _CHUNK_HEADING_RE.match(c.text)
        if m:
            own_title = re.sub(r"^#{1,6}\s+", "", m.group(0)).strip()
            chain = _chain_before(c.char_start)
            # 该块首标题在链中的位置（按标题文本匹配，取最后出现）
            idx = -1
            for k in range(len(chain) - 1, -1, -1):
                if chain[k] == own_title:
                    idx = k
                    break
            if idx <= 0:
                result.append(c)  # 链首或未找到 → 无上级祖先，不拼
                continue
            prefix = " > ".join(chain[:idx])
            result.append(Chunk(text=f"{prefix}\n{c.text}",
                                char_start=c.char_start, char_end=c.char_end))
            continue
        # 无标题行：整链拼到块首（未找到标题链 → 不拼接）
        titles = _chain_before(c.char_start)
        if not titles:
            result.append(c)
            continue
        prefix = " > ".join(titles)
        result.append(Chunk(text=f"{prefix}\n{c.text}",
                            char_start=c.char_start, char_end=c.char_end))
    return result
