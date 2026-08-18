"""LLM 客户端统一工厂 + 可预期失败异常（重构收口）

背景：chat_service / agentic_chunker / knowledge_graph_service /
contextual_retriever / embedding_service 5 处近似重复的 AsyncOpenAI 工厂
（key 比对自动重建）统一收口到本模块：

- get_llm_client(llm_cfg=None, timeout=None)：统一 AsyncOpenAI 构造 + 缓存
  - llm_cfg：dict（6 字段 LLM 配置）/ LLMConfig / None（= 全局活跃配置）；
    dict 里的扩展字段（如部门合并产物）按 _LLM_KEYS 归一化后忽略
  - timeout：显式覆盖（默认 float(llm_cfg.timeout or 60)，与历史各调用点
    完全一致）；覆盖值并入缓存 key（重建语义与"配置变化即重建"一致）
  - 缓存：模块级 key→client 字典。key = 6 字段配置 dict 的 JSON 序列化
    （sort_keys，与历史各调用点 json.dumps 产物完全一致 → 重建时机不变）；
    配置 A→B→A 时复用缓存旧实例（AsyncOpenAI 为无状态工厂对象，可安全
    复用，行为等价）；各模块 _get_client 保留原名委托本工厂（测试 patch
    兼容）
- llm_to_dict：LLM 配置对象 → 6 字段 dict（原 chat_service._llm_to_dict
  迁移，chat_service 保留 `_llm_to_dict` 名字转发，4 处历史 import 不破坏）
- 可预期失败异常（任务 3）：
  - LLMTimeoutError：LLM 调用超时（wait_for 超时包装）——warning 级日志
  - LLMRequestError：LLM 调用失败（网络/限流/HTTP 等），消息 = 原始错误
    文本（调用方拼接上下文时保持历史文案，mark_failed 等错误消息不变）
  - llm_completion：统一单次调用包装——asyncio.TimeoutError →
    LLMTimeoutError；其余异常 → LLMRequestError（CancelledError 属
    BaseException 正常传播，不包装）
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict, Optional

from openai import AsyncOpenAI

from backend.config import LLMConfig, get_active_config

# LLM 客户端字段清单（与 config.LLMConfig 六字段对应；dict 归一化只取这些
# key——dict 里可能的扩展字段（部门合并/模型列表条目）不参与 key 与构造）
_LLM_KEYS = ("base_url", "api_key", "model", "temperature",
             "max_tokens", "timeout")


def llm_to_dict(llm_cfg) -> dict:
    """LLM 配置对象 → 6 字段 dict（dict 只取 _LLM_KEYS；LLMConfig 用
    model_dump；其他对象 getattr 兜底——与原 chat_service._llm_to_dict 一致）"""
    if isinstance(llm_cfg, dict):
        return {k: llm_cfg.get(k) for k in _LLM_KEYS}
    if isinstance(llm_cfg, LLMConfig):
        return llm_cfg.model_dump()
    try:
        return llm_cfg.model_dump()  # 其他 pydantic v2 BaseModel
    except AttributeError:
        return {k: getattr(llm_cfg, k, None) for k in _LLM_KEYS}


# ---- 可预期失败异常（LLM 链路） ----

class LLMTimeoutError(Exception):
    """LLM 调用超时（asyncio.TimeoutError 类型化包装）

    可预期失败：调用方捕获后按业务降级（跳过单块/回退切块/返回空），
    记 warning 级日志；不属代码缺陷，不打堆栈。
    """


class LLMRequestError(Exception):
    """LLM 调用失败（网络错误/限流/HTTP 错误等）

    消息 = 原始错误文本（str(原异常)），调用方拼接上下文时历史文案不变
    （如 "知识图谱抽取失败，跳过: chunk#%d err=%s" 的 err 仍为原始文本）。
    """


async def llm_completion(client, *, model: str, messages: list,
                         max_tokens: int, temperature: float,
                         extra_body=None, timeout: Optional[float] = None):
    """统一 LLM 单次调用：超时/调用失败 → LLMTimeoutError / LLMRequestError

    - asyncio.TimeoutError → LLMTimeoutError（调用方按超时语义降级）
    - 其余异常 → LLMRequestError（消息 = 原始错误文本，日志/错误消息不变）
    - asyncio.CancelledError（BaseException）不捕获，正常传播
    """
    try:
        return await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                extra_body=extra_body,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise LLMTimeoutError(
            f"LLM 调用超时（>{timeout:g}s）" if timeout else "LLM 调用超时") \
            from None
    except Exception as e:
        raise LLMRequestError(str(e)) from e


# ---- 统一客户端工厂（key 比对自动重建，配置变化即重建无需重启） ----

_client_cache: Dict[str, AsyncOpenAI] = {}


def get_llm_client(llm_cfg=None, timeout: Optional[float] = None) -> AsyncOpenAI:
    """统一 LLM 客户端工厂（AsyncOpenAI 构造 + key 比对重建缓存）

    - llm_cfg：6 字段配置 dict / LLMConfig / None（= 全局活跃配置 llm 段）
    - timeout：显式覆盖（None = 用 llm_cfg.timeout or 60，与历史一致）；
      覆盖值并入缓存 key（timeout 变化即重建）
    - 缓存 key：llm_to_dict 归一化 6 字段 dict 的 JSON 序列化——与历史
      各调用点 key 完全一致，配置（地址/密钥/模型/参数/timeout）任一变化
      即重建独立客户端
    """
    cfg_dict = llm_to_dict(llm_cfg) if llm_cfg is not None \
        else llm_to_dict(get_active_config().llm)
    if timeout is not None:
        key_dict = dict(cfg_dict)
        key_dict["timeout"] = timeout
    else:
        key_dict = cfg_dict
    key = json.dumps(key_dict, sort_keys=True, ensure_ascii=False)
    if key not in _client_cache:
        eff_timeout = (timeout if timeout is not None
                       else float(cfg_dict.get("timeout") or 60))
        _client_cache[key] = AsyncOpenAI(
            base_url=cfg_dict.get("base_url", ""),
            api_key=cfg_dict.get("api_key", ""),
            timeout=eff_timeout,
        )
    return _client_cache[key]
