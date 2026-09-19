"""xls 读取器（老 BIFF 格式，xlrd）

- xlrd.open_workbook(path)：默认格式化信息关闭（性能好），
  cell_value 对公式单元格返回**存的缓存值**（老格式保存即含）；
- 日期：xlrd 以 CELL_DATE 类型 + float 存储（Excel 序列日），
  经 xldate_as_datetime(wb.datemode) 转 datetime → ISO 文本；
- 合并单元格：sheet.merged_cells 为 (row_low, row_high, col_low, col_high)
  0 基**开**区间 → 转 0 基闭区间交 spreadsheet.reader.fill_merged（与
  xlsx 共用）；index_last_row/col 在合并单元格存在时可能被填充到有效
  数据终点，rows 等宽化时缺列补空；
- 空 sheet/全空行跳过；行数上限为 xls 格式本身限制（65535）。
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import List

from backend.services.spreadsheet.reader import (Sheet, build_merged_ranges,
                                                 fill_merged, pad_rows)

logger = logging.getLogger(__name__)


def _cell_text(xls, wb, value, ctype) -> str:
    """xlrd 单元格 → 文本（日期经 xldate_as_datetime 转 ISO）"""
    from xlrd import XL_CELL_DATE, xldate_as_datetime

    if value is None or value == "":
        return ""
    if ctype == XL_CELL_DATE:
        try:
            dt = xldate_as_datetime(value, wb.datemode)
            return dt.isoformat(sep=" ")
        except (ValueError, TypeError, OverflowError):
            return str(value)
    if isinstance(value, float) and value == int(value):
        return str(int(value))  # 整数浮点去小数点（行号/计数类可读性）
    return str(value)


def read_xls(path: Path) -> List[Sheet]:
    """读取工作簿全部工作表中非空 sheet"""
    import xlrd

    # formatting_info=True 才带出 merged_cells（xlrd 默认不解析格式化信息，
    # 实测同一文件默认读为 []、开启后为 [(1, 6, 0, 1)]）——不开的话合并单元格
    # 永远不展开，与 xlsx 路径行为不一致；个别老文件解析格式化信息会失败，
    # 退回默认读取（代价仅是不展开合并，不影响单元格取值）
    try:
        wb = xlrd.open_workbook(str(path), formatting_info=True)
    except Exception as e:
        logger.warning("xls 格式化信息读取失败，合并单元格不展开: %s", e)
        wb = xlrd.open_workbook(str(path))
    sheets: List[Sheet] = []
    for sh in wb.sheets():
        rows: List[List[str]] = []
        row_numbers: List[int] = []  # rows[i] 对应的原始行号（0 基，与 xlrd 一致）
        for ri in range(sh.nrows):
            cells = [_cell_text(sh, wb, sh.cell_value(ri, ci),
                                sh.cell_type(ri, ci))
                     for ci in range(sh.ncols)]
            if not any(c for c in cells):
                continue
            rows.append(cells)
            row_numbers.append(ri)
        if not rows:
            continue
        # xlrd merged_cells: (row_low, row_high, col_low, col_high)，
        # row_high/col_high 为开区间上界 → 减 1 转闭区间；行号交
        # build_merged_ranges 按"跳空行映射"折成 rows 索引（与 xlsx 同因同修）
        raw = [(r0, c0, r1 - 1, c1 - 1) for r0, r1, c0, c1 in sh.merged_cells]
        fill_merged(rows, build_merged_ranges(row_numbers, raw))
        sheets.append(Sheet(name=sh.name, rows=pad_rows(rows),
                            stats={"merged_cells": len(sh.merged_cells)}))
    return sheets
