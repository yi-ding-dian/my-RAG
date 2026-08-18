"""配置字段校验（update_profile 范围校验专用）

从 backend/services/settings_service.py 按职责拆分而来（行为零变化）：
- _FIELD_LABELS：range 范围校验字段的中文标签（错误文案用）
- validate_range：整数范围校验（越界 → ValueError，错误文案与历史一致）

依赖 settings_schema.FieldSpec（仅类型注解）；不依赖 SettingsService 单例。
"""
from __future__ import annotations

from typing import Tuple

from backend.services.settings_schema import FieldSpec

# range 范围校验字段的中文标签（错误文案用，如"入库并发数需为 1~10"）；
# 未登记的字段回退用字段名（history_rounds 保持原文案"历史轮数"不变）
_FIELD_LABELS = {
    "history_rounds": "历史轮数",
    "concurrency": "入库并发数",
}


def validate_range(fspec: FieldSpec, key: str, value) -> int:
    """整数范围校验（越界 → ValueError，文案带字段中文标签；
    如 chat.history_rounds 1~20 / ingestion.concurrency 1~10）"""
    lo, hi = fspec.range
    label = _FIELD_LABELS.get(key, key)
    try:
        val = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}需为 {lo}~{hi} 的整数")
    if not lo <= val <= hi:
        raise ValueError(f"{label}需为 {lo}~{hi}")
    return val
