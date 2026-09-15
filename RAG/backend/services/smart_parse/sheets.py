"""表格文档画像（轻量本地读取，无 LLM）

复用 services/spreadsheet.reader 结构化直读：秒出、不烧 token——比 LLM 文本
分析对表格文档更有用（Sheet 结构本身就是文档骨架）。

文件名不叫 spreadsheet.py：与外层 backend/services/spreadsheet/ 同名会让人
在 grep 时多停一秒。
"""
from __future__ import annotations

from pathlib import Path


def analyze_spreadsheet(path: Path) -> dict:
    """Excel/CSV 轻量画像：Sheet 数量/行列/合计行数/合并单元格数量

    读取失败或空表返回空 dict（调用方按"画像缺失"处理，不中断整体）。
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
