"""Excel / CSV 读取器（统一入口 + 公共基础）

- read_spreadsheet(path) -> list[Sheet]：按扩展名分发到专用读取器
  （xlsx→spreadsheet_xlsx / xls→spreadsheet_xls / csv→spreadsheet_csv），
  每个 Sheet 为 (名称, 等宽二维行表)，空白行/空表已跳过；
- Sheet | render_sheet_pipe(sheet)：行表 → markdown 管道表格，
  "## Sheet: 名称" 标题 + 管道表（表头行+分隔行+数据行），与
  table_normalizer 的 GFM 语义一致——入库后切块原子保护/检索/
  前端表格渲染全链路直接复用，无需任何 HTML 转换；
- fill_merged(rows, ranges)：合并单元格左上值填充（xlsx/xls 共用，
  坐标系统一为 0 基闭区间 (row1, col1, row2, col2)）；
- 读取器契约：read_xxx(path: Path) -> list[Sheet]，行内单元格为 str
  （日期转 ISO 文本、数字转普通文本、None → ""）。

不处理：单元格内图片/批注（读取器取不到其内容，仅文本值入库）；
超大文件（xlsx 建议 <10 万行，更大请转 csv 用标准库流式读取）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from backend.services.table_normalizer import pipe_escape

logger = logging.getLogger(__name__)


@dataclass
class Sheet:
    """单个工作表：名称 + 等宽二维行表（每个单元格已是字符串）

    stats: 读取器补充的表级统计（智能画像用，nonspecific）：
    "merged_cells"=合并单元格数量（xlsx/xls 读取器填写，csv 无此概念）
    """

    name: str
    rows: List[List[str]] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# 合并单元格 0 基闭区间: (row1, col1, row2, col2)
MergedRange = Tuple[int, int, int, int]

# 各扩展名支持的读取器（key 统一无点，与 SUPPORTED_EXTS 风格一致）
_SPREADSHEET_EXTS = {"xlsx", "xls", "csv"}


def is_spreadsheet_ext(ext: str) -> bool:
    return (ext or "").lower().lstrip(".") in _SPREADSHEET_EXTS


def read_spreadsheet(path: Path) -> List[Sheet]:
    """按扩展名分发读取（文件名扩展名决定读取器）。

    非法扩展名抛 ValueError（与 parser_client 语义一致）；读取异常向上抛，
    由解析链路标记 failed。
    """
    ext = path.suffix.lower().lstrip(".")
    if ext not in _SPREADSHEET_EXTS:
        raise ValueError(f"不支持的表格文件类型: .{ext}")
    if ext == "xlsx":
        from backend.services.spreadsheet_xlsx import read_xlsx
        return read_xlsx(path)
    if ext == "xls":
        from backend.services.spreadsheet_xls import read_xls
        return read_xls(path)
    from backend.services.spreadsheet_csv import read_csv
    return read_csv(path)


def fill_merged(rows: List[List[str]], ranges: List[MergedRange]) -> None:
    """合并单元格填充：区域左上值覆盖区域内全部单元格（就地修改 rows）

    - xlsx（openpyxl ranges）与 xls（xlrd merged_cells）坐标系不同，
      读取器负责统一转换为 0 基闭区间后调用本函数（公共逻辑复用）；
    - 保真策略：与 HTML rowspan/colspan 的"重复填充"语义一致——
      合并处的值在每个被跨单元格重复，检索/LLM/渲染均不丢信息且
      管道表格保持等宽（渲染端无需感知合并结构）。
    """
    if not ranges:
        return
    for r1, c1, r2, c2 in ranges:
        if not (0 <= r1 <= r2 and 0 <= c1 <= c2):
            continue
        if r1 >= len(rows):
            continue
        r1_row = rows[r1]
        if c1 >= len(r1_row):
            continue
        value = r1_row[c1]
        for ri in range(r1, min(r2, len(rows) - 1) + 1):
            row = rows[ri]
            for ci in range(c1, min(c2, len(row) - 1) + 1):
                row[ci] = value


def pad_rows(rows: List[List[str]]) -> List[List[str]]:
    """行等宽化：按最宽行补空单元格（Excel 行数据可能宽度不一致）"""
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    if width == 0:
        return rows
    for r in rows:
        if len(r) < width:
            r.extend([""] * (width - len(r)))
    return rows


def render_sheet_pipe(sheet: Sheet) -> str:
    """单 sheet → markdown 管道表格文本（"## Sheet: 名称" + 表头/分隔/数据）

    - 首行作为表头行（Excel 工作区第一行通常即表头；无数据返回空串）；
    - 分隔行 "| --- |"（与 table_normalizer 输出格式一致）；
    - 表格前后补空行、与相邻 sheet 分隔；单元格内换行/| 经 pipe_escape。
    """
    if not sheet.rows:
        return ""
    rows = pad_rows([list(r) for r in sheet.rows])
    width = len(rows[0])
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    lines = ["| " + " | ".join(pipe_escape(c) for c in rows[0]) + " |", sep]
    for r in rows[1:]:
        lines.append("| " + " | ".join(pipe_escape(c) for c in r) + " |")
    title = f"## Sheet: {sheet.name}"
    return f"{title}\n\n" + "\n".join(lines) + "\n"


def render_sheets_text(sheets: List[Sheet]) -> str:
    """多个 sheet 拼接为入库文本（每表前空行分隔）"""
    blocks = [render_sheet_pipe(s) for s in sheets if s.rows]
    return "\n\n".join(f"\n{b}" for b in blocks).lstrip("\n") if blocks else ""
