"""聊天/LLM 配置的字段级合并与提取（纯函数，无单例依赖）

从 backend/services/settings/service.py 按职责拆分而来（行为零变化）：
- chat_payload：从档案提取聊天设置字段（GET/POST /api/settings/chat 共用响应结构）
- merge_chat_config / merge_department_llm：部门级配置与全局配置的字段级合并
- active_llm_item：从 llm 段（{models, active}）取激活模型条目

依赖 settings.schema 的白名单常量（CHAT_FIELD_NAMES / CHAT_RETRIEVAL_FIELD_NAMES /
LLM_FIELD_NAMES）；不依赖 SettingsService 单例（避免循环依赖）。
"""
from __future__ import annotations

from typing import Optional, Tuple

from backend.services.settings.schema import (CHAT_AGENTIC_FIELD_NAMES,
                                              CHAT_FIELD_NAMES,
                                              CHAT_RETRIEVAL_FIELD_NAMES)


def chat_payload(profile: dict) -> dict:
    """提取活跃档案的聊天设置字段（GET/POST /api/settings/chat 共用响应结构）"""
    chat = profile.get("chat") or {}
    retrieval = profile.get("retrieval") or {}
    agentic = profile.get("agentic") or {}
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
            # 引用的提示词库条目名（空 = 不引用，直接用 system_prompt）；
            # 漏了这行会让超管在全局设的引用拿不到（部门设了才进得来）
            "system_prompt_ref": chat.get("system_prompt_ref", ""),
            "kg_enhance": chat.get("kg_enhance", True),
            # 查询改写（默认开，旧档案缺字段兜底；LLM 结合历史把问题改写为
            # 独立检索查询——口语→正式 + 指代消解，见 query_rewriter）
            "query_rewrite": chat.get("query_rewrite", True),
            # 查询改写用的历史轮数（默认 3，旧档案缺字段兜底）
            "query_rewrite_rounds": chat.get("query_rewrite_rounds", 3),
            # 思考模式（聊天问答）：默认 disabled 关闭思考（缺省/旧档案兜底，
            # 简单延迟敏感任务更快更省 token）
            "thinking_mode": chat.get("thinking_mode", "disabled"),
            # 引用摘要窗口大小（字）：回答里 [n] 悬浮浮层的摘录长度。
            # **曾经漏了这一行**：字段在 schema 里可读可写、落盘也正确，但接口
            # 响应里没有它 → 前端读不到、永远用兜底的 600，改配置毫无效果
            "citation_snippet_chars": chat.get("citation_snippet_chars", 600),
            # 聊天识图的读图提示词（空 = 内置默认；支持 {max_chars} 占位符）。
            # 部门可覆盖，留空 = 跟随全局——同一坑：漏了这行部门就永远改不动
            # （image_model 不在此处：它不给部门覆盖，见 schema 注释）
            "image_prompt": chat.get("image_prompt", ""),
        },
        "agentic": {
            "enabled": agentic.get("enabled", False),
            "max_retries": agentic.get("max_retries", 1),
            "recheck_threshold": agentic.get("recheck_threshold", 0.55),
            "abstain_threshold": agentic.get("abstain_threshold", 0.25),
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
    dagentic = dept.get("agentic") if isinstance(dept.get("agentic"), dict) else {}
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
    for k in CHAT_AGENTIC_FIELD_NAMES:
        v = dagentic.get(k)
        if v is None:
            continue
        base["agentic"][k] = v
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


# 部门在**选中的条目**之上还允许微调的字段；其余一律跟着条目走
DEPT_LLM_TUNABLE: Tuple[str, ...] = ("temperature", "max_tokens")


def _llm_result_fields() -> Tuple[str, ...]:
    """合并结果要保证存在的字段名

    与 schema 的「部门可提交白名单」是两回事：那个管**输入**（现只有条目名 +
    温度/Token），这里管**输出结构**——下游拿合并结果构造 LLM 客户端、判定
    思考策略，字段缺了会读到 None 而退回默认行为。曾经两者共用
    `LLM_FIELD_NAMES`，白名单一收紧输出结构就跟着塌了。
    """
    from backend.config import LLMConfig

    return tuple(LLMConfig.model_fields)


def merge_department_llm(global_llm: dict, dept_llm: dict,
                         global_models: Optional[list] = None) -> dict:
    """部门**选中的模型条目** → 取该条目完整配置（再叠加本部门的微调）

    - `dept_llm["model"]` 存的是**条目名**（超管在「LLM 模型管理」里配的
      `name`），不是模型名——部门只"选"，看不到也改不了连接信息与密钥
    - 名字被超管删掉/改名 → 回退 `global_llm`（全局激活条目），不断服务
    - `temperature` / `max_tokens`：部门可在选中条目之上再覆盖（微调）
    - `global_models`：全局模型条目列表；不传 → 直接回退全局（安全降级）

    为什么不再逐字段覆盖：部门想"换一个模型"就得把 base_url/api_key/model/
    temperature/max_tokens/timeout/thinking_control/top_p **全抄一遍**，抄漏
    一个就静默漂移——实测软件部漏了 thinking_control，用着思考模型
    qwen3.6-35b-a3b-apex-quality 却继承了激活条目 Qwen3.5-9B 的 `'none'`，
    思考根本没关掉；top_p 同理继承了个 None。改成按条目取整份配置，这类
    漂移从根上消失。
    """
    if not isinstance(dept_llm, dict):
        return dict(global_llm or {})  # 脏数据容错：非 dict 视为未设置
    base: dict = {}
    want = str(dept_llm.get("model") or "").strip()
    if want and global_models:
        hit = next((m for m in global_models
                    if isinstance(m, dict)
                    and str(m.get("name") or "").strip() == want), None)
        if hit is not None:
            # 整份条目直接用。**刻意不走 llm_to_dict**：它的 _LLM_KEYS 只有
            # 6 个字段，会把 top_p 这类模型级参数悄悄丢掉——正是要修的病。
            base = {k: v for k, v in hit.items() if k != "name"}
    if not base:
        base = dict(global_llm or {})
    for k in _llm_result_fields():  # 输出字段保证存在（缺失补 None，原语义）
        base.setdefault(k, None)
    for k in DEPT_LLM_TUNABLE:
        v = dept_llm.get(k)
        if v is None or v == "":
            continue  # 未设置/空串 → 跟随选中条目
        base[k] = v
    return base
