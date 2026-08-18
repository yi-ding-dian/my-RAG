"""聊天/LLM 配置的字段级合并与提取（纯函数，无单例依赖）

从 backend/services/settings_service.py 按职责拆分而来（行为零变化）：
- chat_payload：从档案提取聊天设置字段（GET/POST /api/settings/chat 共用响应结构）
- merge_chat_config / merge_department_llm：部门级配置与全局配置的字段级合并
- active_llm_item：从 llm 段（{models, active}）取激活模型条目

依赖 settings_schema 的白名单常量（CHAT_FIELD_NAMES / CHAT_RETRIEVAL_FIELD_NAMES /
LLM_FIELD_NAMES）；不依赖 SettingsService 单例（避免循环依赖）。
"""
from __future__ import annotations

from typing import Optional

from backend.services.settings_schema import (CHAT_FIELD_NAMES,
                                              CHAT_RETRIEVAL_FIELD_NAMES,
                                              LLM_FIELD_NAMES)


def chat_payload(profile: dict) -> dict:
    """提取活跃档案的聊天设置字段（GET/POST /api/settings/chat 共用响应结构）"""
    chat = profile.get("chat") or {}
    retrieval = profile.get("retrieval") or {}
    return {
        "retrieval": {
            "top_k": retrieval.get("top_k"),
            "similarity_threshold": retrieval.get("similarity_threshold"),
        },
        "chat": {
            "enable_multi_turn": chat.get("enable_multi_turn"),
            "history_rounds": chat.get("history_rounds"),
            "temperature": chat.get("temperature"),
            "top_p": chat.get("top_p"),
            "max_tokens": chat.get("max_tokens"),
            "system_prompt": chat.get("system_prompt", ""),
            "kg_enhance": chat.get("kg_enhance", True),
        },
    }


def merge_chat_config(global_profile: dict, dept_config: dict) -> dict:
    """聊天配置字段级合并（部门只覆盖它设置的字段，其余用全局）

    - global_profile：活跃配置档案（含 chat/retrieval 段）
    - dept_config：部门级配置（{"chat": {...}, "retrieval": {...}}，
      与 chat_payload 结构同构；空 dict = 未设置，返回纯全局）
    - 合并规则：部门字段值非 None 且非空串（system_prompt 空串视为
      未设置=跟随全局）→ 覆盖全局；其余保持全局值
    - 返回 chat_payload 同构结果（仅白名单字段）
    """
    base = chat_payload(global_profile)
    dept = dept_config or {}
    # 段内仅接受 dict（脏数据容错：非 dict 视为未设置）
    dchat = dept.get("chat") if isinstance(dept.get("chat"), dict) else {}
    dretr = dept.get("retrieval") if isinstance(dept.get("retrieval"), dict) else {}
    for k in CHAT_FIELD_NAMES:
        v = dchat.get(k)
        if v is None or v == "":
            continue  # 部门未设置该字段 → 用全局
        base["chat"][k] = v
    for k in CHAT_RETRIEVAL_FIELD_NAMES:
        v = dretr.get(k)
        if v is None:
            continue
        base["retrieval"][k] = v
    return base


def active_llm_item(llm: dict) -> dict:
    """从 llm 段（{models, active}）取激活模型条目；异常数据/无条目 → {}"""
    if not isinstance(llm, dict):
        return {}
    models = llm.get("models")
    if not isinstance(models, list) or not models:
        return {}
    try:
        idx = int(llm.get("active") or 0)
    except (TypeError, ValueError):
        idx = 0
    if not 0 <= idx < len(models):
        return {}
    item = models[idx]
    return item if isinstance(item, dict) else {}


def merge_department_llm(global_llm: dict, dept_llm: dict) -> dict:
    """LLM 配置字段级合并（部门只覆盖它显式设置的字段，其余用全局）

    - global_llm：全局 LLM 配置（dict 或 model_dump 结果，含
      base_url/api_key/model/temperature/max_tokens/timeout）
    - dept_llm：部门级 LLM 配置（空 dict/None = 未设置，返回纯全局）
    - 合并规则：部门字段非 None 且非空串 → 覆盖全局；None/空串 =
      跟随全局（api_key 空串即"故意清空用全局"，与白名单清除语义一致）
    - 返回完整 6 字段 dict（响应结构与 /api/settings/chat 的 llm 段同构）
    """
    base = {k: (global_llm or {}).get(k) for k in LLM_FIELD_NAMES}
    dept = dept_llm or {}
    if not isinstance(dept, dict):
        return base  # 脏数据容错：非 dict 视为未设置
    for k in LLM_FIELD_NAMES:
        v = dept.get(k)
        if v is None or v == "":
            continue  # 未设置/空串 → 用全局
        base[k] = v
    return base
