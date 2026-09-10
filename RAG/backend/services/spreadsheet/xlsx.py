"""xlsx 读取器（openpyxl，值直读）

- load_workbook(data_only=True)：公式取缓存值（Excel/WPS 保存均写缓存；
  从未被应用打开的公式单元格返回 None → ""，可接受，见模块 docstring）；
- 合并单元格：openpyxl ranges（1 基，含端点）→ 统一 0 基闭区间交
  spreadsheet.reader.fill_merged（与 xls 共用填充逻辑）；
- 单元格格式：None → ""，datetime/date/time → ISO 文本（可读、可检索），
  数字/字符串原样转文本；布尔 → "TRUE"/"FALSE"；
- 空 sheet（所有行全空）跳过；多 sheet 全部导入（"## Sheet: 名称" 分节）。
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import List

from backend.services.spreadsheet.reader import (Sheet, fill_merged,
                                                 pad_rows)


def _cell_text(value) -> str:
    """单元格值 → 文本（日期转 ISO、布尔转文本、None → 空）"""
    if value is None:
        return ""
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime.datetime) \
            else value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _formula_text(value) -> str:
    """公式文本兜底：WPS/未重算文件无缓存值时显示 =公式（不显示为空）"""
    s = str(value or "").strip()
    if s.startswith("=="):
        s = s[1:]  # WPS 双等号怪癖归一
    return s if s else "=公式"


def read_xlsx(path: Path) -> List[Sheet]:
    """读取工作簿全部工作表中非空 sheet（空 sheet/全空行跳过）"""
    import openpyxl
    from openpyxl.utils import get_column_letter

    wb = openpyxl.load_workbook(path, data_only=True)
    # 公式兜底：data_only 无缓存值时从公式视图回读 =公式 文本
    try:
        wb_formula = openpyxl.load_workbook(path, data_only=False)
    except Exception:
        wb_formula = None
    # 公式计算引擎（有公式时按需重算，缓存命中秒读；失败回退公式文本）
    _recalc: Dict = {}
    if wb_formula is not None:
        try:
            has_formula = any(
                isinstance(c.value, str) and c.value.startswith("=")
                for sheet in wb_formula.worksheets for row in sheet.iter_rows()
                for c in row
            )
            if has_formula:
                from backend.services.spreadsheet.formula import recalc_cells
                _recalc = recalc_cells(path)
        except Exception:
            _recalc = {}
    sheets: List[Sheet] = []
    for ws in wb.worksheets:
        rows: List[List[str]] = []
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row,
                                min_col=1, max_col=ws.max_column):
            cells = []
            for cell in row:
                text = _cell_text(cell.value)
                # 无缓存值(公式未计算,如 WPS 保存) → 计算引擎值优先,公式文本兜底
                if text == "" and wb_formula is not None:
                    fv = wb_formula[ws.title].cell(
                        row=cell.row, column=cell.column).value
                    if isinstance(fv, str) and fv.startswith("="):
                        calc = _recalc.get(ws.title, {}).get(
                            str(cell.row), {}).get(get_column_letter(cell.column))
                        if isinstance(calc, float):
                            from backend.services.spreadsheet.reader import humanize_number
                            calc = humanize_number(calc)
                        text = str(calc) if calc is not None else _formula_text(fv)
                cells.append(text)
            # 全空行跳过（Excel 尾部空行/格式化残留行）
            if not any(c for c in cells):
                continue
            rows.append(cells)
        if not rows:
            continue
        # 合并单元格：openpyxl 1 基含端点 → 0 基闭区间（min_row-1, min_col-1…）
        ranges = [
            (rng.min_row - 1, rng.min_col - 1,
             rng.max_row - 1, rng.max_col - 1)
            for rng in ws.merged_cells.ranges
        ]
        fill_merged(rows, ranges)
        sheets.append(Sheet(name=ws.title, rows=pad_rows(rows),
                            stats={"merged_cells": len(ranges)}))
    return sheets
