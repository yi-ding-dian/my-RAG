"""Excel 公式计算引擎（formulas 封装 + 磁盘缓存）

背景：WPS 保存的 xlsx 常见"仅存公式、无计算缓存"（data_only=True 读不出值），
类 Excel 预览/入库会空白。本模块用纯 Python formulas 引擎重算公式结果，
并做内容 hash 缓存（data/spreadsheet_cache/{hash}.json），命中秒读。

- 触发：调用前需先经 openpyxl data_only=False 判定存在公式（本模块不主动扫描，
  由调用方双读后按需触发 recalc_cells）；
- 结果：{(sheet, row, col): float|str}，公式输出 NaN/None/错误 → 不收录
  （调用方回退"公式文本"显示，绝不空白/崩溃）；
- 缓存：按文件字节 md5（内容变化自动失效）；计算异常 → 返回 {}（兜底原语义）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Dict, Tuple

from backend.config import DATA_DIR

logger = logging.getLogger(__name__)

_CACHE_DIR = DATA_DIR / "spreadsheet_cache"
# 公式值 key: (sheet, row, col)
CellKey = Tuple[str, int, int]


def _cache_path(path: Path) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.md5(path.read_bytes()).hexdigest()[:16]
    return _CACHE_DIR / f"{digest}.json"


def _load_cache(path: Path) -> Dict[str, Dict]:  # {sheet: {row: {col: v}}}
    try:
        cp = _cache_path(path)
        if cp.exists():
            return json.loads(cp.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("公式缓存读取失败: %s", e)
    return {}


def _save_cache(path: Path, data: Dict[str, Dict]) -> None:
    try:
        _cache_path(path).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("公式缓存写入失败: %s", e)


def _to_py(value):
    """numpy 标量 → python 标量；NaN/空 → None"""
    try:
        import numpy as np
        if isinstance(value, np.generic):
            value = value.item()
    except Exception:
        pass
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    return value


def recalc_cells(path: Path) -> Dict[str, Dict[str, Dict]]:
    """重算全表公式 → {sheet: {row(str): {col(str): value}}}

    - 缓存命中返回缓存；无公式/计算失败返回 {}
    - 结果结构形如 {"旅游消费-桂林": {"17": {"B": 879.2, ...}, ...}}
      （行列用 Excel 坐标，与 openpyxl 坐标系统一由调用方换算）
    """
    cached = _load_cache(path)
    if cached:
        return cached
    try:
        import formulas
        model = formulas.ExcelModel().loads(str(path)).finish()
        solution = model.calculate()
        out: Dict[str, Dict[str, Dict]] = {}
        for key, ranges in solution.items():
            if key == "self" or not isinstance(key, str):
                continue
            # key: "'[file.xlsx]Sheet名'!B17"  → 仅取单格结果(标量)
            if "!" not in key:
                continue
            sheet = key.rsplit("]", 1)[-1].rsplit("'", 1)[0]
            ref = key.rsplit("!", 1)[1]
            if not ref or len(ref) > 6:
                continue
            try:
                value = _to_py(ranges.values[key][1][0][0])
            except Exception:
                continue
            if value is None:
                continue
            col = "".join(ch for ch in ref if ch.isalpha())
            row = "".join(ch for ch in ref if ch.isdigit())
            out.setdefault(sheet, {}).setdefault(row, {})[col] = value
        if out:
            _save_cache(path, out)
        return out
    except Exception as e:
        logger.warning("公式重算失败(回退公式文本): %s %s", path.name, str(e)[:150])
        return {}
