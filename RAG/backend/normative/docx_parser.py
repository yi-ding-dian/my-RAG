"""DOCX 结构化解析（OOXML 直读 → 与 MinerU 同构的 markdown）

背景：docx 走 MinerU 解析（底层是 docx→PDF→OCR）会丢掉 word 里的结构信息
——标题层级被压平、自动编号消失、表格位置错乱、图片位置漂移。本模块不走
OCR，直接读 OOXML 还原出结构：

- 按 body XML 顺序遍历 w:p / w:tbl（分开取 paragraphs + tables 会让表格
  全部堆到文末，位置信息丢失）；
- 标题层级三级优先：段落 outlineLvl > 样式链 outlineLvl > 样式名
  （Heading N / 标题 N；中文版 Office 的样式名是本地化的，所以样式链判定
  必须优先于名称匹配）；
- 自动编号还原：段落 numPr 优先，无则沿样式链取样式定义里的 numPr（编号
  定义挂在样式上而非段落上是常见形态，段落上反而看不到），按 (numId, ilvl)
  计数、用 numbering.xml 的 numFmt/lvlText 渲染编号文本；
- 图片按段落位置提取（drawing 的 a:blip / VML 的 v:imagedata），命名
  imageN.ext，引用写成 images/{name}（下游按 basename 匹配替换为鉴权 URL，
  src 形式不对就替换不上，图片等于丢了）；
- 表格 → markdown 管道表格，合并单元格按矩形网格重复填充（与 HTML 表格
  转换的 rowspan 语义一致）。

返回值 (markdown, images, parse_method) 与 MinerU 产物同构：images 为
[{name, data: bytes}]，parse_method 为 "normative"（结构化解析标识，供
上层记录实际解析方式），下游切块/检索/前端预览零改动。
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import docx
from docx.table import Table

logger = logging.getLogger(__name__)

# ---- 命名空间标签（python-docx 的 qn() 不含 VML 映射，用字面 URI）----
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_V_NS = "urn:schemas-microsoft-com:vml"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_TAG_P = f"{{{_W_NS}}}p"
_TAG_TBL = f"{{{_W_NS}}}tbl"
_TAG_R = f"{{{_W_NS}}}r"
_TAG_T = f"{{{_W_NS}}}t"
_TAG_TAB = f"{{{_W_NS}}}tab"
_TAG_BR = f"{{{_W_NS}}}br"
_TAG_CR = f"{{{_W_NS}}}cr"
_TAG_PPR = f"{{{_W_NS}}}pPr"
_TAG_RPR = f"{{{_W_NS}}}rPr"
_TAG_STYLE = f"{{{_W_NS}}}style"
_TAG_PSTYLE = f"{{{_W_NS}}}pStyle"
_TAG_B = f"{{{_W_NS}}}b"
_TAG_I = f"{{{_W_NS}}}i"
_TAG_NUM_PR = f"{{{_W_NS}}}numPr"
_TAG_NUM_ID = f"{{{_W_NS}}}numId"
_TAG_ILVL = f"{{{_W_NS}}}ilvl"
_TAG_OUTLINE_LVL = f"{{{_W_NS}}}outlineLvl"
_TAG_INSTR_TEXT = f"{{{_W_NS}}}instrText"
_TAG_FLD_SIMPLE = f"{{{_W_NS}}}fldSimple"
_TAG_SDT = f"{{{_W_NS}}}sdt"
_TAG_SDT_CONTENT = f"{{{_W_NS}}}sdtContent"
_TAG_BLIP = f"{{{_A_NS}}}blip"
_TAG_IMAGEDATA = f"{{{_V_NS}}}imagedata"
_TAG_VAL = f"{{{_W_NS}}}val"
_TAG_NAME = f"{{{_W_NS}}}name"
_TAG_BASED_ON = f"{{{_W_NS}}}basedOn"
_TAG_STYLE_ID = f"{{{_W_NS}}}styleId"
_TAG_ABSTRACT_NUM = f"{{{_W_NS}}}abstractNum"
_TAG_ABSTRACT_NUM_ID = f"{{{_W_NS}}}abstractNumId"
_TAG_NUM = f"{{{_W_NS}}}num"
_TAG_LVL = f"{{{_W_NS}}}lvl"
_TAG_LVL_OVERRIDE = f"{{{_W_NS}}}lvlOverride"
_TAG_START = f"{{{_W_NS}}}start"
_TAG_START_OVERRIDE = f"{{{_W_NS}}}startOverride"
_TAG_NUM_FMT = f"{{{_W_NS}}}numFmt"
_TAG_LVL_TEXT = f"{{{_W_NS}}}lvlText"
_TAG_IS_LGL = f"{{{_W_NS}}}isLgl"
_TAG_NUM_STYLE_LINK = f"{{{_W_NS}}}numStyleLink"
_TAG_STYLE_LINK = f"{{{_W_NS}}}styleLink"

_R_EMBED = f"{{{_R_NS}}}embed"
_R_ID = f"{{{_R_NS}}}id"
_W_INSTR = f"{{{_W_NS}}}instr"

# 图片引用目录：与 MinerU 产物一致（下游按 basename 匹配替换为鉴权 URL）
_IMG_DIR = "images"

# 段落内需要递归下钻的行内容器（w:del 等删除修订不在其中：修订删除的内容
# 按"最终稿"语义不输出，w:delText 不是 w:t 也不会被采到）
_RUN_CONTAINERS = frozenset({
    f"{{{_W_NS}}}{name}" for name in (
        "hyperlink", "ins", "smartTag", "sdt", "sdtContent", "dir",
        "bdo", "moveTo", "customXml", "fldSimple",
    )
})

# 标题样式名匹配：英文版 "Heading 1"、中文版 "标题 1"（本地化名，仅作兜底）
_HEADING_NAME_RE = re.compile(r"^\s*(?:heading|标题)\s*(\d+)\s*$", re.IGNORECASE)

# 目录判定：域代码含 TOC（如 ' TOC \o "1-3" \h \z \u '）或样式名含 TOC/目录
_TOC_FIELD_RE = re.compile(r"\bTOC\b", re.IGNORECASE)
_TOC_STYLE_RE = re.compile(r"toc|目录", re.IGNORECASE)

# numbering 的占位符 %1~%9（各级计数器渲染值）
_PLACEHOLDER_RE = re.compile(r"%(\d)")

# 空白折叠（单元格/标题单行化）
_WS_RE = re.compile(r"[ \t\r\n　]+")

# 编号与正文之间不加空格的收尾字符（中文顿号/括号等：编号自带分隔效果，
# 再补空格会变成 "一、 系统安装"）
_TIGHT_TAIL_CHARS = "、。，；：）)】」》＞>.,;:"

# ---- 自动编号的"正规形式编号"（legal numbering）语义开关 ----
# ECMA-376 字面规则：lvlText 里 %N 用**第 N 级自己**的 numFmt 渲染（一级
# 中文数字 + 二级阿拉伯数字的文档里，"1.1" 会被算成 "一.1"）；而 Word/WPS
# 实际渲染时上层级编号跟随**当前级**的 numFmt（显示 "1.1"），该语义由
# <w:isLgl/> 显式声明、但转换工具常把它丢掉。实测原文档显示为 "1.1"，故
# 默认按实际渲染语义（跟随当前级 numFmt），lvl 显式含 <w:isLgl/> 时同理。
_LEGAL_NUMBERING = True

# 中文数字（chineseCounting / chineseCountingThousand）
_CN_DIGITS = "零一二三四五六七八九"
_CN_UNITS = ("", "十", "百", "千")

# 图片扩展名：优先 partname 后缀，取不到再按 content_type 映射
_EXT_BY_CONTENT_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/x-emf": "emf",
    "image/emf": "emf",
    "image/x-wmf": "wmf",
    "image/wmf": "wmf",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}


def _int_attr(element, attr: str = _TAG_VAL) -> Optional[int]:
    """取元素属性的整数（元素缺失/非数字 → None，编号属性异常不影响整篇）"""
    if element is None:
        return None
    raw = element.get(attr)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _format_chinese(n: int) -> str:
    """中文数字（1 → 一、11 → 十一、21 → 二十一、105 → 一百零五）

    chineseCounting 与 chineseCountingThousand 在千位以下的常见场景
    （章节号、"第X条"）渲染一致，统一按带零的十进制位读法；超出千位
    回落阿拉伯数字（Word 对该量级也不会有稳定读法）。
    """
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n - 10] if n > 10 else "")
    digits = str(n)
    if len(digits) > 4:
        return digits
    out: List[str] = []
    length = len(digits)
    for i, ch in enumerate(digits):
        d = int(ch)
        if d == 0:
            # 中间的零只补一个（如 1005 → 一千零五），末尾零不补
            if out and out[-1] != "零" and any(int(x) for x in digits[i + 1:]):
                out.append("零")
        else:
            out.append(_CN_DIGITS[d] + _CN_UNITS[length - i - 1])
    return "".join(out)


def _format_alpha(n: int, upper: bool = False) -> str:
    """字母编号（1 → a、26 → z、27 → aa，Excel 列名式进位）"""
    if n <= 0:
        return str(n)
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("a") + rem) + out
    return out.upper() if upper else out


def _format_roman(n: int, upper: bool = False) -> str:
    """罗马数字（超范围回落阿拉伯数字，保证编号不丢）"""
    if n <= 0 or n > 3999:
        return str(n)
    table = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
             (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
             (5, "v"), (4, "iv"), (1, "i"))
    out: List[str] = []
    for value, symbol in table:
        while n >= value:
            out.append(symbol)
            n -= value
    text = "".join(out)
    return text.upper() if upper else text


def _format_number(value: int, num_fmt: str) -> str:
    """按 numFmt 渲染单个计数器值（未知格式回落十进制，编号不丢）"""
    if num_fmt in ("decimal", "decimalZero"):
        return f"{value:02d}" if num_fmt == "decimalZero" else str(value)
    if num_fmt in ("chineseCounting", "chineseCountingThousand"):
        return _format_chinese(value)
    if num_fmt == "chineseLegalSimplified":
        return _format_chinese(value)
    if num_fmt == "lowerLetter":
        return _format_alpha(value)
    if num_fmt == "upperLetter":
        return _format_alpha(value, upper=True)
    if num_fmt == "lowerRoman":
        return _format_roman(value)
    if num_fmt == "upperRoman":
        return _format_roman(value, upper=True)
    if num_fmt in ("none", "bullet"):
        return ""
    return str(value)


def _join_number(prefix: str, text: str) -> str:
    """编号文本 + 正文（编号自带中文收尾标点时紧贴，否则补一个空格）

    编号的 suffix 语义（w:suff：tab/space/nothing）在纯文本里无法还原，
    按收尾字符判断：'一、系统安装' / '1.1 DEB包安装'。
    """
    if not prefix:
        return text
    if not text:
        return prefix
    if prefix[-1] in _TIGHT_TAIL_CHARS:
        return prefix + text
    return f"{prefix} {text}"


def _wrap_emphasis(text: str, bold: bool, italic: bool) -> str:
    """run 级加粗/斜体 → markdown 强调标记（首尾空白留在标记外）

    trim 后再包裹：`** 文本 **` 在多数渲染器里不生效，跨 run 的空白本就
    不属于强调内容。
    """
    if not (bold or italic) or not text.strip():
        return text
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    core = text.strip()
    if bold:
        core = f"**{core}**"
    if italic:
        core = f"*{core}*"
    return f"{lead}{core}{trail}"


def _escape_cell(text: str) -> str:
    """管道单元格文本：空白折叠 + 字面 | 转义（与 HTML 表格转换同语义）"""
    return _WS_RE.sub(" ", text or "").strip().replace("|", "\\|")


def _fold_ws(text: str) -> str:
    """行内空白折叠为单空格（标题必须单行，否则 # 行会被截断）"""
    return _WS_RE.sub(" ", text or "").strip()


def _iter_blocks(element) -> Iterator:
    """按 XML 顺序产出块级元素（w:p / w:tbl）；w:sdt 展开到其内容

    body 子元素还有 w:sectPr（节属性）等，非内容一律跳过。
    """
    for child in element.iterchildren():
        tag = child.tag
        if tag in (_TAG_P, _TAG_TBL):
            yield child
        elif tag == _TAG_SDT:
            content = child.find(_TAG_SDT_CONTENT)
            if content is not None:
                yield from _iter_blocks(content)


def _iter_runs(element) -> Iterator:
    """按文档顺序产出段落内的 run（下钻超链接/域/内联 sdt 等容器）"""
    for child in element.iterchildren():
        tag = child.tag
        if tag == _TAG_R:
            yield child
        elif tag in _RUN_CONTAINERS:
            yield from _iter_runs(child)


class _StyleNode:
    """样式定义（只保留结构解析用得到的字段）"""

    __slots__ = ("style_id", "name", "based_on", "outline_lvl", "num_id",
                 "ilvl", "has_num_pr")

    def __init__(self, style_id: str, name: str, based_on: Optional[str],
                 outline_lvl: Optional[int], num_id: Optional[int],
                 ilvl: Optional[int], has_num_pr: bool):
        self.style_id = style_id
        self.name = name
        self.based_on = based_on
        self.outline_lvl = outline_lvl
        self.num_id = num_id
        self.ilvl = ilvl
        self.has_num_pr = has_num_pr


class _Styles:
    """styles.xml 解析与样式链（basedOn 上溯）查询

    样式链上的属性继承各自独立（outlineLvl 与 numPr 分别找链上第一个声明），
    与 ECMA-376 的样式继承语义一致。
    """

    def __init__(self, styles_element):
        self._nodes: Dict[str, _StyleNode] = {}
        if styles_element is None:
            return
        for style in styles_element.iter(_TAG_STYLE):
            style_id = style.get(_TAG_STYLE_ID)
            if not style_id:
                continue
            based_on_el = style.find(_TAG_BASED_ON)
            self._nodes[style_id] = _StyleNode(
                style_id=style_id,
                name=self._style_name(style),
                based_on=(based_on_el.get(_TAG_VAL) if based_on_el is not None
                          else None),
                outline_lvl=None,
                num_id=None,
                ilvl=None,
                has_num_pr=False,
            )
            self._apply_style_pPr(self._nodes[style_id], style)

    @staticmethod
    def _style_name(style) -> str:
        name_el = style.find(_TAG_NAME)
        return (name_el.get(_TAG_VAL) or "") if name_el is not None else ""

    @staticmethod
    def _apply_style_pPr(node: _StyleNode, style) -> None:
        """样式定义里的 pPr：outlineLvl 与 numPr（标题编号常在这里）"""
        pPr = style.find(_TAG_PPR)
        if pPr is None:
            return
        node.outline_lvl = _int_attr(pPr.find(_TAG_OUTLINE_LVL))
        numPr = pPr.find(_TAG_NUM_PR)
        if numPr is not None:
            node.has_num_pr = True
            node.num_id = _int_attr(numPr.find(_TAG_NUM_ID))
            node.ilvl = _int_attr(numPr.find(_TAG_ILVL))

    def chain(self, style_id: Optional[str]) -> List[_StyleNode]:
        """样式链（自身 → basedOn 祖先），带环防护（损坏文件可能成环）"""
        nodes: List[_StyleNode] = []
        seen: set = set()
        while style_id and style_id not in seen:
            seen.add(style_id)
            node = self._nodes.get(style_id)
            if node is None:
                break
            nodes.append(node)
            style_id = node.based_on
        return nodes

    def has_toc_name(self, chain: Sequence[_StyleNode]) -> bool:
        """样式链上样式名含 TOC/目录（目录项样式，本地化名兜底）"""
        return any(_TOC_STYLE_RE.search(node.name or "") for node in chain)


class _Numbering:
    """numbering.xml 解析与计数器（自动编号还原）

    - numId → abstractNumId（w:num/w:abstractNumId），支持 numStyleLink
      （abstractNum 把编号定义委托给编号样式，实际定义在带 styleLink 的
      abstractNum 上）；
    - 各级定义取 w:num/w:lvlOverride/w:lvl（完整覆盖）或 abstractNum 下
      w:lvl[@w:ilvl]（w:lvlOverride/w:startOverride 覆盖起始值）；
    - 计数器按 numId 隔离、按 ilvl 计数：进入第 k 级时 k 级 +1 并重置所有
      更深级（同一序列里 "1.1 → 1.2 → 2.1" 的语义），更浅级保持不动；
    - %N 占位符渲染：default 跟随当前级 numFmt（见 _LEGAL_NUMBERING 说明）。
    """

    def __init__(self, numbering_element):
        # numId → {abstract_id: 抽象编号 id, overrides: {ilvl: 覆盖元素}}
        self._nums: Dict[int, dict] = {}
        self._abstracts: Dict[int, object] = {}
        self._style_links: Dict[str, int] = {}  # styleLink 值 → abstractNumId
        self._lvl_cache: Dict[Tuple[int, int], object] = {}
        self._counters: Dict[int, Dict[int, int]] = {}
        if numbering_element is None:
            return
        for abstract in numbering_element.iter(_TAG_ABSTRACT_NUM):
            abs_id = _int_attr(abstract, f"{{{_W_NS}}}abstractNumId")
            if abs_id is None:
                continue
            self._abstracts[abs_id] = abstract
            link = abstract.find(_TAG_STYLE_LINK)
            if link is not None and link.get(_TAG_VAL):
                self._style_links[link.get(_TAG_VAL)] = abs_id
        for num in numbering_element.iter(_TAG_NUM):
            num_id = _int_attr(num, f"{{{_W_NS}}}numId")
            if num_id is None:
                continue
            overrides: Dict[int, object] = {}
            for override in num.findall(_TAG_LVL_OVERRIDE):
                ilvl = _int_attr(override, f"{{{_W_NS}}}ilvl")
                if ilvl is None:
                    ilvl = _int_attr(override.find(_TAG_ILVL), _TAG_VAL)
                if ilvl is not None:
                    overrides[ilvl] = override
            self._nums[num_id] = {
                "abstract_id": _int_attr(num.find(_TAG_ABSTRACT_NUM_ID)),
                "overrides": overrides,
            }

    # ---- 定义查询 ----

    def defined(self, num_id: int, ilvl: int) -> bool:
        """该级是否有编号定义（未定义的 numId 不产出编号文本）"""
        return self._lvl(num_id, ilvl) is not None

    def num_fmt(self, num_id: int, ilvl: int) -> str:
        lvl = self._lvl(num_id, ilvl)
        if lvl is None:
            return ""
        fmt_el = lvl.find(_TAG_NUM_FMT)
        if fmt_el is None:
            return "decimal"
        return fmt_el.get(_TAG_VAL) or "decimal"

    def _lvl(self, num_id: int, ilvl: int):
        key = (num_id, ilvl)
        if key in self._lvl_cache:
            return self._lvl_cache[key]
        lvl = None
        num = self._nums.get(num_id)
        if num is not None:
            override = num["overrides"].get(ilvl)
            if override is not None:
                lvl = override.find(_TAG_LVL)
            if lvl is None:
                abstract = self._resolve_abstract(num["abstract_id"])
                if abstract is not None:
                    for candidate in abstract.findall(_TAG_LVL):
                        if _int_attr(candidate, f"{{{_W_NS}}}ilvl") == ilvl:
                            lvl = candidate
                            break
        self._lvl_cache[key] = lvl
        return lvl

    def _resolve_abstract(self, abstract_id: Optional[int]):
        """abstractNum 经 numStyleLink 重定向到实际的编号定义"""
        seen: set = set()
        while abstract_id is not None and abstract_id not in seen:
            seen.add(abstract_id)
            abstract = self._abstracts.get(abstract_id)
            if abstract is None:
                return None
            link = abstract.find(_TAG_NUM_STYLE_LINK)
            if link is None or not link.get(_TAG_VAL):
                return abstract
            abstract_id = self._style_links.get(link.get(_TAG_VAL))
        return None

    def _start(self, num_id: int, ilvl: int) -> int:
        num = self._nums.get(num_id) or {}
        override = (num.get("overrides") or {}).get(ilvl)
        if override is not None:
            start = _int_attr(override.find(_TAG_START_OVERRIDE))
            if start is not None:
                return start
        lvl = self._lvl(num_id, ilvl)
        if lvl is not None:
            start = _int_attr(lvl.find(_TAG_START))
            if start is not None:
                return start
        return 1

    # ---- 编号渲染 ----

    def next_text(self, num_id: int, ilvl: int) -> Tuple[str, str]:
        """产出该段落编号文本并推进计数器，返回 (文本, numFmt)

        - bullet：返回 ("", "bullet")，调用方输出 "- " 前缀，不计数
        - numFmt=none 或该级无定义：返回 ("", fmt)，不计数（无编号语义）
        """
        lvl = self._lvl(num_id, ilvl)
        fmt = self.num_fmt(num_id, ilvl)
        if lvl is None or fmt in ("bullet", "none", ""):
            return "", fmt
        self._advance(num_id, ilvl)
        legal = _LEGAL_NUMBERING or lvl.find(_TAG_IS_LGL) is not None
        values: Dict[int, str] = {}
        for placeholder in range(1, 10):
            lvl_index = placeholder - 1
            # 正规形式编号：%N 用当前级的 numFmt 渲染（字面规则则各用各级
            # 自己的 numFmt，见模块顶部 _LEGAL_NUMBERING 说明）
            value_fmt = fmt
            if not legal:
                value_fmt = self.num_fmt(num_id, lvl_index) or "decimal"
            values[placeholder] = _format_number(
                self._value(num_id, lvl_index), value_fmt)
        lvl_text_el = lvl.find(_TAG_LVL_TEXT)
        lvl_text = ""
        if lvl_text_el is not None:
            lvl_text = lvl_text_el.get(_TAG_VAL) or ""
        if not lvl_text:
            return "", fmt
        text = _PLACEHOLDER_RE.sub(
            lambda m: values.get(int(m.group(1)), ""), lvl_text)
        return text, fmt

    def _advance(self, num_id: int, ilvl: int) -> None:
        """进入第 ilvl 级：该级 +1，所有更深级重置（下次从 start 重新开始）"""
        levels = self._counters.setdefault(num_id, {})
        for deeper in [k for k in levels if k > ilvl]:
            del levels[deeper]
        if ilvl in levels:
            levels[ilvl] += 1
        else:
            levels[ilvl] = self._start(num_id, ilvl)

    def _value(self, num_id: int, ilvl: int) -> int:
        """第 ilvl 级当前值（未出现过用 start：%1 引用未启动的上层不空窗）"""
        levels = self._counters.get(num_id) or {}
        if ilvl in levels:
            return levels[ilvl]
        return self._start(num_id, ilvl)


class _DocxParser:
    """单篇解析上下文（图片命名/去重、单元格图片去重等跨段落状态）"""

    def __init__(self, path: str):
        self.doc = docx.Document(str(path))
        self.styles = _Styles(self._styles_element())
        self.numbering = _Numbering(self._numbering_element())
        self.images: List[dict] = []
        self._image_name_by_rid: Dict[str, str] = {}
        # 合并单元格（rowspan）会按矩形网格重复渲染同一个 tc，图片只在其
        # 首次出现的位置输出一次（避免一张图被引用多次，引用数与图片数对齐）
        self._imaged_tcs: set = set()

    # ---- 包内部件 ----

    def _styles_element(self):
        try:
            return self.doc.styles.element
        except Exception as exc:  # 样式部件缺失（损坏文件）不影响正文解析
            logger.warning("styles.xml 读取失败: %s", exc)
            return None

    def _numbering_element(self):
        try:
            return self.doc.part.numbering_part.element
        except Exception:
            return None  # 无 numbering.xml：文档本就没有自动编号

    # ---- 图片 ----

    def _image_name(self, rid: str) -> Optional[str]:
        """rid → 包内图片名（imageN.ext，按出现顺序编号；同一 rid 复用）

        外链图片（r:link）在 related_parts 里没有 blob，返回 None（不产出
        引用，避免产出下游替换不上的死链）。
        """
        if rid in self._image_name_by_rid:
            return self._image_name_by_rid[rid]
        part = self.doc.part.related_parts.get(rid)
        blob = getattr(part, "blob", None)
        if not blob:
            return None
        ext = self._image_ext(part)
        name = f"image{len(self.images) + 1}.{ext}"
        self.images.append({"name": name, "data": blob})
        self._image_name_by_rid[rid] = name
        return name

    @staticmethod
    def _image_ext(part) -> str:
        partname = getattr(part, "partname", None)
        suffix = getattr(partname, "ext", "") if partname is not None else ""
        if suffix:
            return str(suffix).lstrip(".").lower()
        content_type = getattr(part, "content_type", "") or ""
        return _EXT_BY_CONTENT_TYPE.get(content_type.lower(), "png")

    def _image_refs(self, container, allow_images: bool) -> Iterator[str]:
        """容器（run）内全部图片引用（按 XML 顺序：drawing 的 blip + VML）"""
        if not allow_images:
            return
        for element in container.iter():
            if element.tag == _TAG_BLIP:
                rid = element.get(_R_EMBED)
            elif element.tag == _TAG_IMAGEDATA:
                rid = element.get(_R_ID)
            else:
                continue
            if not rid:
                continue
            name = self._image_name(rid)
            if name:
                yield f"![]({_IMG_DIR}/{name})"

    # ---- 段落 ----

    def _style_chain(self, p_element) -> List[_StyleNode]:
        """段落有效样式链；pStyle 引用失效（p.style 为 None 的底层形态）
        时返回空链，标题/编号退化为段落自身属性"""
        pPr = p_element.find(_TAG_PPR)
        style_id = None
        if pPr is not None:
            pStyle = pPr.find(_TAG_PSTYLE)
            if pStyle is not None:
                style_id = pStyle.get(_TAG_VAL)
        return self.styles.chain(style_id)

    def _is_toc(self, p_element, chain: Sequence[_StyleNode]) -> bool:
        """目录段落判定：域代码含 TOC，或样式名含 TOC/目录

        目录是"导航快照"不是正文内容，混进正文会变成一大块重复垃圾。
        """
        if self.styles.has_toc_name(chain):
            return True
        instr = "".join(
            (element.text or "")
            for element in p_element.iter(_TAG_INSTR_TEXT))
        if not instr:
            for element in p_element.iter(_TAG_FLD_SIMPLE):
                instr += element.get(_W_INSTR) or ""
        return bool(_TOC_FIELD_RE.search(instr))

    def _heading_level(self, p_element,
                       chain: Sequence[_StyleNode]) -> Optional[int]:
        """标题层级三级优先：段落 outlineLvl → 样式链 outlineLvl → 样式名

        outlineLvl=9 是"正文级"（ECMA-376 明确定义的 body text），不生成
        标题；中文版 Office 的样式名是本地化的（"标题 1"），名称匹配只能
        兜底，故排在样式链判定之后。
        """
        pPr = p_element.find(_TAG_PPR)
        if pPr is not None:
            level = _int_attr(pPr.find(_TAG_OUTLINE_LVL))
            if level is not None:
                return level if 0 <= level <= 8 else None
        for node in chain:
            if node.outline_lvl is not None:
                return node.outline_lvl if 0 <= node.outline_lvl <= 8 else None
        for node in chain:
            match = _HEADING_NAME_RE.match(node.name or "")
            if match:
                return int(match.group(1)) - 1
        return None

    def _effective_num_pr(
            self, p_element,
            chain: Sequence[_StyleNode]) -> Optional[Tuple[int, int]]:
        """段落有效编号 (numId, ilvl)：段落 numPr 优先，缺失部分沿样式链补

        - 段落 numPr 的 numId=0 是"显式取消编号"，不再继承样式（否则会把
          样式里的标题编号错误地贴到该段）；
        - ilvl/numId 可分别继承（ECMA-376：numPr 子元素各自可选）；
        - 段落与样式链都无 numPr → 无编号。
        """
        num_id: Optional[int] = None
        ilvl: Optional[int] = None
        pPr = p_element.find(_TAG_PPR)
        if pPr is not None:
            numPr = pPr.find(_TAG_NUM_PR)
            if numPr is not None:
                num_id = _int_attr(numPr.find(_TAG_NUM_ID))
                ilvl = _int_attr(numPr.find(_TAG_ILVL))
        for node in chain:
            if node.has_num_pr:
                if num_id is None:
                    num_id = node.num_id
                if ilvl is None:
                    ilvl = node.ilvl
                break
        if num_id is None or num_id == 0:
            return None
        return num_id, (ilvl or 0)

    def _render_runs(self, p_element, allow_emphasis: bool = True,
                     allow_images: bool = True) -> str:
        """段落内 run → markdown 片段（文本 + 图片引用）

        - 相邻同格式（粗/斜）run 合并，避免产出 `**a****b**` 这类碎标记；
        - 标题行不包裹强调标记（标题本身已是 # 级结构，再叠 ** 是噪音）；
        - 上下标（w:vertAlign）按纯文本输出：落盘前会被 strip_subsup_tags
          洗掉，产出 <sub>/<sup> 标签是白费功夫。
        """
        fragments: List[list] = []  # [kind, 文本, bold, italic]
        for run in _iter_runs(p_element):
            bold, italic = self._run_style(run)
            if not allow_emphasis:
                bold = italic = False
            for text in self._run_texts(run):
                if (fragments and fragments[-1][0] == "text"
                        and fragments[-1][2] == bold
                        and fragments[-1][3] == italic):
                    fragments[-1][1] += text
                else:
                    fragments.append(["text", text, bold, italic])
            for ref in self._image_refs(run, allow_images):
                fragments.append(["image", ref, False, False])
        return "".join(
            fragment[1] if fragment[0] == "image"
            else _wrap_emphasis(fragment[1], fragment[2], fragment[3])
            for fragment in fragments)

    @staticmethod
    def _run_texts(run) -> Iterator[str]:
        """run 内文本片段（制表/换行按空格单行化；图片走 _image_refs 通道）"""
        for child in run.iterchildren():
            tag = child.tag
            if tag == _TAG_T:
                yield child.text or ""
            elif tag in (_TAG_TAB, _TAG_BR, _TAG_CR):
                yield " "

    @staticmethod
    def _run_style(run) -> Tuple[bool, bool]:
        """run 级加粗/斜体（w:val="0"/"false" 是显式关闭）"""
        bold = italic = False
        rPr = run.find(_TAG_RPR)
        if rPr is None:
            return False, False
        for tag, flag in ((_TAG_B, "b"), (_TAG_I, "i")):
            element = rPr.find(tag)
            if element is None:
                continue
            value = (element.get(_TAG_VAL) or "").strip().lower()
            enabled = value not in ("0", "false", "off")
            if flag == "b":
                bold = enabled
            else:
                italic = enabled
        return bold, italic

    def _number_prefix(self, p_element,
                       chain: Sequence[_StyleNode]) -> Tuple[str, str]:
        """段落编号前缀：(前缀文本, numFmt)；bullet 返回 ("-", "bullet")"""
        num_pr = self._effective_num_pr(p_element, chain)
        if num_pr is None:
            return "", ""
        num_id, ilvl = num_pr
        if not self.numbering.defined(num_id, ilvl):
            return "", ""
        text, fmt = self.numbering.next_text(num_id, ilvl)
        if fmt == "bullet":
            return "-", fmt
        return text, fmt

    def _render_paragraph(self, p_element,
                          allow_images: bool = True) -> Optional[str]:
        """段落 → markdown 块（标题带 # 前缀；None 表示空段/目录段不输出）"""
        chain = self._style_chain(p_element)
        if self._is_toc(p_element, chain):
            return None
        level = self._heading_level(p_element, chain)
        if level is not None:
            text = _fold_ws(self._render_runs(
                p_element, allow_emphasis=False, allow_images=allow_images))
            if not text:
                return None
            prefix, _fmt = self._number_prefix(p_element, chain)
            heading = _join_number(prefix, text)
            return f"{'#' * (min(level, 5) + 1)} {heading}"
        text = self._render_runs(p_element, allow_images=allow_images).strip()
        prefix, fmt = self._number_prefix(p_element, chain)
        if fmt == "bullet":
            return f"{prefix} {text}" if text else None
        if not text and not prefix:
            return None  # 空段落不输出（空块由 join 前的过滤丢弃，这里省一步）
        # 有编号无正文（如表格里的"编号"列）保留编号本身：编号就是该格内容
        return _join_number(prefix, text)

    # ---- 表格 ----

    def _render_table(self, tbl_element) -> Optional[str]:
        """w:tbl → markdown 管道表格（首行为表头 + 分隔行）

        合并单元格按矩形网格重复填充（python-docx 的 row.cells 对 vMerge
        续格返回上一行的同一格对象、对 gridSpan 重复返回该格，天然就是
        "重复填充"语义，与 HTML 表格 rowspan 的处理口径一致）。
        """
        try:
            table = Table(tbl_element, self.doc)
            rows = [[self._render_cell(cell) for cell in row.cells]
                    for row in table.rows]
        except Exception as exc:  # 畸形表格的网格展开异常不拖垮整篇解析
            logger.warning("表格网格展开失败，退化为逐行文本: %s", exc)
            rows = self._fallback_rows(tbl_element)
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows:
            return None
        width = max(len(row) for row in rows)
        lines = []
        for index, row in enumerate(rows):
            cells = list(row) + [""] * (width - len(row))
            lines.append("| " + " | ".join(cells) + " |")
            if index == 0:  # GFM 表格必须有表头 + 分隔行
                separator = " | ".join("---" for _ in range(width))
                lines.append(f"| {separator} |")
        return "\n".join(lines)

    def _render_cell(self, cell) -> str:
        """单元格文本：段落（含编号/图片）→ 单行化 + 管道转义

        合并单元格（rowspan）会被按矩形网格重复访问到同一个 tc：文本按
        "重复填充"语义重复输出，图片只在其首次出现的位置输出一次（否则
        一张图被引用多次，引用数与图片数对不上）。
        """
        tc = getattr(cell, "_tc", None)
        allow_images = True
        if tc is not None:
            allow_images = id(tc) not in self._imaged_tcs
            self._imaged_tcs.add(id(tc))
        parts: List[str] = []
        for paragraph in cell.paragraphs:
            text = self._render_paragraph(paragraph._p,
                                          allow_images=allow_images)
            if text:
                parts.append(text)
        return _escape_cell(" ".join(parts))

    @staticmethod
    def _fallback_rows(tbl_element) -> List[List[str]]:
        """表格网格兜底：逐 tr/tc 取纯文本（不处理合并，保住内容）"""
        rows: List[List[str]] = []
        for tr in tbl_element.findall(f"{{{_W_NS}}}tr"):
            cells = []
            for tc in tr.findall(f"{{{_W_NS}}}tc"):
                texts = [(t.text or "") for t in tc.iter(_TAG_T)]
                cells.append(_escape_cell("".join(texts)))
            rows.append(cells)
        return rows

    # ---- 主流程 ----

    def run(self) -> Tuple[str, List[dict], str]:
        blocks: List[str] = []
        for element in _iter_blocks(self.doc.element.body):
            if element.tag == _TAG_P:
                block = self._render_paragraph(element)
            else:
                block = self._render_table(element)
            if block:
                blocks.append(block)
        text = "\n\n".join(blocks)  # 连续空段天然被丢弃（空块不入列）
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        logger.info("DOCX 结构化解析: %d 块 / %d 张图片", len(blocks),
                    len(self.images))
        return text, self.images, "normative"


def parse_docx(path) -> Tuple[str, List[dict], str]:
    """解析 docx，返回 (markdown, images, parse_method)

    - markdown：标题层级用 # 数量表示（outlineLvl 0 → #，最多 6 个），标题
      文本已还原自动编号；表格为管道表格；图片引用为 images/{name}；
    - images：[{"name": "image1.png", "data": bytes}]，与 MinerU 产物同构
      （按文档出现顺序命名，同一图片资源只出现一次）；
    - parse_method：固定 "normative"（结构化解析标识）。
    """
    return _DocxParser(str(path)).run()


# 标题块的行形态（_render_paragraph 产出）："## 1.1 DEB包安装"
_OUTLINE_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def extract_outline(path) -> List[dict]:
    """提取文档标题结构，返回 [{"level": 井号个数, "title": 标题文本}]

    供「查看文档结构」在解析前预览这份文档会被解析成什么样的标题层级树：

    - 复用 _render_paragraph 逐段渲染并只保留 # 标题行——层级判定
      （outlineLvl > 样式链 > 样式名）、自动编号还原、目录(TOC)跳过全部与
      真实解析同源，不另写一套判定逻辑（否则预览与入库结果会不一致）；
    - 只取 body 级段落：表格内段落与真实解析一致地不算标题（表格内容按
      单元格渲染，不参与标题层级）；
    - allow_images=False：结构预览不需要图片，省去整篇图片资源的解码
      （含图文档可达数十 MB，纯浪费）；
    - 返回按文档顺序排列。
    """
    parser = _DocxParser(str(path))
    items: List[dict] = []
    for element in _iter_blocks(parser.doc.element.body):
        if element.tag != _TAG_P:
            continue
        block = parser._render_paragraph(element, allow_images=False)
        if not block:
            continue
        match = _OUTLINE_RE.match(block)
        if match:
            items.append({"level": len(match.group(1)),
                          "title": match.group(2)})
    logger.info("DOCX 结构提取: %d 个标题", len(items))
    return items
