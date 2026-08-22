"""解析产物 HTML 表格 → markdown 管道表格规范化。

背景：MinerU 解析 PDF/docx 时表格输出为 HTML <table>（table_enable 开启），
这类标签文本进入知识库后：① token 冗余大（<table><tr><td> 标签占一半）；
② 召回进上下文中 LLM 理解/复述困难，输出时容易照抄 HTML 标签（用户看到
"<table><tr><td>…" 根本看不懂）；③ 回答截断时半截标签可读性极差。

本模块将 md 全文中的 <table>…</table> 块重写为 markdown 管道表格
（| 列 | 列 |），其余文本原样不动。转换在入库链路"解析后 → 切块前"调用，
切块层对管道表格已有原子保护（splitter._find_table_ranges），
表头+分隔行齐全才符合 GFM 表格语义，切块/检索/LLM/前端渲染都更友好。

转换规则（以 MinerU 实际产物的常见形态为准）：
- <th>/<td> → 管道单元格，单元格内 | 转义为 \\|（HTML 实体 &amp; 等由
  HTMLParser 自动还原，转义后的字面 | 需转义）
- rowspan="n"：合并列后续 n-1 行重复填充该值（保信息不丢，如"上游层"跨多行）
- colspan="n"：展开为 n 列重复值（管道语法无合并单元格）
- 单元格内 <p> 多段 / <br> → 空格合并（单元格单行化，前端按行切分安全）
- 单元格内 <img src alt> → 保留为 ![alt](src)（含图片表格可视化不受损；
  调用时机在 rewrite_image_refs 之后，src 已是鉴权代理 URL 原样保留）
- 第一个含 <th> 的行作为表头行；无 <th>（纯 td 表）时首行作表头
  （GFM 硬性要求表头+分隔行，纯 td 场景语义偏差可接受）
- 未闭合 <table>：从起始转换到文末，不崩溃；无 <tr> 的残留（如空表）丢弃
- 表格前后补空行，与正文分段
- 列数按最宽行对齐，缺列补空格，多余截断

不处理：<formula> 等公式（保留原样）；表内嵌套 <table>（MinerU 不产，
嵌套时按首个 </table> 截断，与 splitter._find_html_table_ranges 同口径）。
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

# HTML 表格起止标签（与 chunking/splitter.py 口径一致，忽略大小写）
_HTML_TABLE_OPEN_RE = re.compile(r"<table\b", re.IGNORECASE)
_HTML_TABLE_CLOSE_RE = re.compile(r"</table\s*>", re.IGNORECASE)

# 单元格内空白折叠（跨 <p>/<br> 的换行缩进合并成单个空格）
_WS_RE = re.compile(r"\s+")


def _is_header_cell(tag: str) -> bool:
    return tag.lower() == "th"


def pipe_escape(text: str) -> str:
    """管道单元格文本规范化：空白折叠 + 字面 | 转义（全链路公共）

    - HTML 表格转换（本模块）与 spreadsheet 读取器（Excel/CSV 直接转管道）
      共用，保证两个入口产出语义一致的 GFM 管道表格；
    - 折叠 `\\s+` → 单空格：单元格单行化（前端按行渲染安全）；
    - 替换 `|` → `\\|`：GFM 单元格分隔符转义，检索/渲染后字形还原。
    """
    return _WS_RE.sub(" ", text or "").strip().replace("|", "\\|")


class _Cell:
    """单个单元格结构：文本 + 合并跨度 + 是否表头"""

    __slots__ = ("text", "rowspan", "colspan", "is_header")

    def __init__(self, text: str, rowspan: int, colspan: int,
                 is_header: bool):
        self.text = text
        self.rowspan = rowspan
        self.colspan = colspan
        self.is_header = is_header


class _TableParser(HTMLParser):
    """流式解析单个 <table> 区间，收集 (rows, cells) 结构。

    容错策略（HTMLParser 默认宽容）：
    - 标签错配/未闭合 → handle_endtag 找不到对应开始，静默忽略
    - 未闭合 <td> 在下一 <tr> 开始时自动收尾
    """
    ROW_HEADERS: dict = None  # type: ignore[assignment]  # 占位，实际用行内

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[_Cell]] = []
        self._cur_row: Optional[List[_Cell]] = None
        self._cur_text: List[str] = None  # type: ignore[assignment]
        self._cur_cell: Optional[_Cell] = None
        self._in_table = False

    # ---- 生命周期 ----
    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag_l = tag.lower()
        if tag_l == "table":
            self._in_table = True
            return
        if not self._in_table:
            return
        if tag_l == "tr":
            self._close_row()
            self._cur_row = []
            return
        if tag_l in ("td", "th"):
            self._close_cell()
            a = dict(attrs)
            try:
                rs = int(a.get("rowspan", 1) or 1)
                cs = int(a.get("colspan", 1) or 1)
            except (TypeError, ValueError):
                rs, cs = 1, 1
            self._cur_cell = _Cell([], max(rs, 1), max(cs, 1),
                                   _is_header_cell(tag_l))
            self._cur_text = []
            return
        if tag_l == "img":
            # 单元格内图片：保留图片引用（src 此时已是代理 URL 或原值）
            a = dict(attrs)
            src = a.get("src") or ""
            alt = a.get("alt") or ""
            self._push_text(f"![{alt}]({src})")
            return
        if tag_l == "p" or tag_l == "br":
            self._push_text(" ")
            return
        # 其他行内标签（strong/em 等）：仅作文本分隔，不渲染结构

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        # <img/> 形态（HTML5 布尔式空配置少见，但图片常写成 <img ... />）
        if tag.lower() == "img":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l == "table":
            self._close_cell()
            self._close_row()
            self._in_table = False
            return
        if tag_l in ("td", "th"):
            self._close_cell()
            return
        if tag_l == "tr":
            self._close_cell()
            self._close_row()
            return
        if tag_l == "p":
            self._push_text(" ")

    def handle_data(self, data: str) -> None:
        self._push_text(data)

    def handle_entityref(self, name: str) -> None:
        self._push_text(html.unescape(f"&{name};"))

    # ---- 内部 ----
    def _push_text(self, chunk: str) -> None:
        if self._cur_cell is not None:
            self._cur_text.append(chunk)

    def _close_cell(self) -> None:
        if self._cur_cell is not None:
            raw = "".join(self._cur_text)
            text = pipe_escape(raw)
            self._cur_cell.text = text
            if self._cur_row is None:
                self._cur_row = []
            self._cur_row.append(self._cur_cell)
        self._cur_cell = None
        self._cur_text = []  # type: ignore[assignment]

    def _close_row(self) -> None:
        if self._cur_row is not None:
            self.rows.append(self._cur_row)
        self._cur_row = None


def _expand_layout(rows: List[List[_Cell]]) -> Tuple[List[List[str]], int]:
    """rowspan/colspan 展开为矩形文本网格（缺列补空、多余截断）。

    算法：逐行扫描，维护 pending（col → (剩余行数, 文本)）记录尚在生效的
    rowspan。每个单元格放入"第一个空闲列"：
    - 该列的列宽推进前，先填掉所有 pending 占用的列（重复填充 rowspan 文本，
      信息保真：跨 6 行的"上游层"在 6 行里都可见）；
    - colspan 在行内展开为 n 个重复单元格；
    - 返回 (grid, 表头行索引)：第一个含 <th> 的行（无 th 则为 0，
      见 _render_pipe 说明）。
    """
    grid: List[List[str]] = []
    pending: dict = {}  # col -> (剩余行数, 文本)
    header_idx: Optional[int] = None
    for i, row in enumerate(rows):
        out: List[str] = []
        col = 0
        for cell in row:
            # 跳过/填充 pending rowspan 占用的列（重复上次的文本）
            while col in pending:
                rem, txt = pending[col]
                out.append(txt)
                if rem <= 1:
                    del pending[col]
                else:
                    pending[col] = (rem - 1, txt)
                col += 1
            span_w = max(cell.colspan, 1)
            # start_col 为单元格跨列起点（col 随后才推进，勿用 col-span_w：
            # span_w=1 时 col-span_w=col-1 会错位一列）
            start_col = col
            for _ in range(span_w):
                out.append(cell.text)
            if cell.rowspan > 1:
                for k in range(span_w):
                    pending[start_col + k] = (cell.rowspan - 1, cell.text)
            # 第一个含 <th> 的行作为表头（MinerU 表头行基本都带 th）
            if header_idx is None and any(c.is_header for c in row):
                header_idx = i
            col += span_w
        # 全空行丢弃（空 <tr></tr> / 空表头行残留，防止渲染出 "|  |" 行）
        if not out or all(not c for c in out):
            continue
        grid.append(out)
    # 行宽对齐：按最宽行补充空单元格（缺失列按 HTML 语义是空单元格）
    if grid:
        width = max(len(r) for r in grid)
        for r in grid:
            if len(r) < width:
                r.extend([""] * (width - len(r)))
    if header_idx is None:
        header_idx = 0
    return grid, header_idx


def _render_pipe(grid: List[List[str]], header_idx: int = 0) -> str:
    """渲染为 GFM 管道表格：表头行（header_idx）+ 分隔行 + 数据行。

    无 <th> 时取第 0 行为表头（GFM 硬性要求表头+分隔行才成表格；
    MinerU 产物基本都有 <th>，少数纯 <td> 表格以此兜底（首行数据兼表头，
    语义偏差可接受））。分隔行用 "---"（对齐信息由前端按 th 渲染）。
    """
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    lines = []
    for i, r in enumerate(grid):
        vals = list(r) + [""] * (width - len(r))
        lines.append("| " + " | ".join(vals) + " |")
    header = lines[header_idx]
    rest = [l for j, l in enumerate(lines) if j != header_idx]
    return "\n".join([header, sep] + rest)


def html_tables_to_pipe(text: str) -> str:
    """转换 md 全文中的 HTML 表格为 markdown 管道表格。

    - `<table>…</table>` 完整块：转换（无 <tr> 的空表丢弃不计）
    - 未闭合 `<table>`：从起始转换到文末（与 splitter 同口径）
    - 其余文本（含 <Breaker::湖北> 等 XML 标签、公式）原样不动
    """
    if not text or "<table" not in text.lower():
        return text
    out: List[str] = []
    last = 0
    pos = 0
    while True:
        m = _HTML_TABLE_OPEN_RE.search(text, pos)
        if not m:
            out.append(text[last:])
            break
        # 区间外原文原样输出
        out.append(text[last:m.start()])
        close = _HTML_TABLE_CLOSE_RE.search(text, m.end())
        if close:
            seg = text[m.start():close.end()]
            end = close.end()
        else:
            seg = text[m.start():]
            end = len(text)
        parser = _TableParser()
        parser.feed(seg)
        # 未闭合残留解析：HTMLParser 对缺 </td>/</tr> 的容错在 feed 结束时需收尾
        if close is None:
            parser.handle_endtag("table")
        if parser.rows:
            grid, header_idx = _expand_layout(parser.rows)
            pipe = _render_pipe(grid, header_idx)
            if pipe.strip():
                out.append("\n" + pipe + "\n")
        # 丢弃的表格（空表）直接跳过，不输出任何内容
        last = end
        pos = end
    result = "".join(out)
    # 干净化：表格块与上下文之间只留一个空行间隔（转换时补的换行+原文换行
    # 可能叠加成 3 个以上，统一塌缩相邻表格前后多余空行）
    result = re.sub(r"\n{4,}", "\n\n\n", result)
    return result
