"""CSV 读取器（标准库 csv，零依赖）

- 编码探测：BOM（utf-8-sig）→ utf-8；否则 utf-8 尝试，失败回退 gb18030
  （GBK 超集，覆盖内网中文 Excel 导出"中文乱码"最常见场景）；极端情况
  gb18030 仍失败 → 按 utf-8 带错误替换读取（不阻断入库）；
- 分隔符探测：csv.Sniffer 嗅探（逗号/分号/制表/竖线），失败默认逗号；
- 文件名（去扩展名）作为唯一 sheet 名（csv 无多表概念；"Sheet: 文件名"
  在检索命中时提供定位信息）；全空行跳过；无分隔符的纯文本行按单列。
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import List

from backend.services.spreadsheet_reader import Sheet, pad_rows


def _detect_encoding(raw: bytes) -> str:
    """BOM/尝试解码探测编码（utf-8 失败 → gb18030）"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gb18030"


def _detect_delimiter(head: str) -> str:
    """csv.Sniffer 嗅探分隔符（候选含逗号/分号/制表/竖线），默认逗号"""
    sample = head[:4096]
    for cand in (",", ";", "\t", "|"):
        try:
            if len(sample.split(cand)) > 1:
                dialect = csv.Sniffer().sniff(sample, delimiters=cand)
                return dialect.delimiter
        except csv.Error:
            continue
    return ","


def read_csv(path: Path) -> List[Sheet]:
    """读取 CSV 单 sheet（编码/分隔符自动探测，空行跳过）"""
    raw = path.read_bytes()
    text = raw.decode(_detect_encoding(raw), errors="replace")
    # 解码后去除 BOM 残留（utf-8-sig 已去；gb18030 分支兜底）
    text = text.lstrip("﻿")
    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: List[List[str]] = []
    n_cols = 0
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        rows.append([c.strip() for c in row])
        n_cols = max(n_cols, len(row))
    if not rows:
        return []
    # 空列宽补齐（csv 行尾逗号多空单元格）
    for r in rows:
        if len(r) < n_cols:
            r.extend([""] * (n_cols - len(r)))
    name = path.stem or "CSV"
    return [Sheet(name=name, rows=pad_rows(rows))]
