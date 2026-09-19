"""思考关闭策略模块化：按模型服务商/部署方式选择"关闭思考"的实现

背景（已实测确认）：
- DeepSeek 在线（api.deepseek.com）：OpenAI 兼容 extra_body 支持
  {"thinking": {"type": "disabled"/"enabled"}} + "reasoning_effort"
  （仅 enabled 时携带）——原 build_thinking_extra_body 逻辑，行为不变
- 本地 LM Studio 的 Qwen3 思考模型（base_url 如 http://192.168.0.74:1234/v1）：
  LM Studio 0.4.x 已知 bug 忽略 extra_body 的 thinking 参数 → 模型默认思考，
  图谱抽取/上下文摘要这类简单任务出现 60s 超时 / content 空
- 本地实测有效的关闭思考方案：请求 messages 末尾注入 assistant 消息
  {"role":"assistant","content":"<think>\\n\\n</think>","continue_assistant_turn":true}
  → 模型跳过思考直接输出（实测 2.7s、reasoning=0、正常 JSON）

设计：
- 策略抽象 ThinkingStrategy：apply(payload) 原地修改请求体
  （payload = {"messages": [...], "extra_body": 由策略填充}）
- 内置策略：
  1. ExtraBodyStrategy(thinking_mode)：在线 API（DeepSeek 等）——extra_body
     填 thinking enabled/disabled + reasoning_effort
     （复用迁移来的 build_thinking_extra_body，与改造前产物完全一致）
  2. QwenPrefillStrategy()：本地 LM Studio Qwen 思考模型且
     thinking_mode=disabled —— messages 末尾注入空 <think> 块 +
     continue_assistant_turn（跳过思考）；enabled* 时不注入（保持模型
     默认思考——本地 LM Studio 无法控制思考强度，选择函数 warning 说明）
  3. NoopStrategy()：默认兜底——不改请求
- 选择函数 get_thinking_strategy(llm_cfg, thinking_mode)：
  按模型配置的 **thinking_control**（模型级字段，见 config.LLMConfig）选择，
  **不再按 base_url 是不是内网判断**：
  - "prefill" → thinking_mode=disabled 时 QwenPrefill（注入空 <think> 块
    跳过思考）；enabled* 时 ExtraBody 不注入（保持模型默认思考——本地
    无法控制思考强度，warning 说明）
  - "api" → ExtraBody（DeepSeek 等支持 thinking 参数的在线 API）
  - "none"/缺省 → Noop（模型本身不思考，或部署端已关闭——不改请求）
  - llm_cfg 非 dict / base_url 空 → Noop（无法判断服务商，不改请求，兜底）
  为什么换判据（2026-09-18 实测）：旧判据"内网地址即注入 prefill"误伤两类
  模型——① 部署端已关思考的非思考模型（vLLM + Qwen3.5-9B，注入后回答
  231→131 字、出图 3/3→0/3）；② 内网 DeepSeek 网关（prefill 对它完全无效，
  思考照跑）。而真正需要 prefill 的只有"Qwen 系 chat template + 部署端
  忽略 API 参数"这一种（LM Studio 上的 Qwen3 思考模型：注入 3.5s，
  不注入 53~286s 且可能因思考吃光 token 答空白）。

接入点（知识图谱抽取 / 上下文摘要 / 查询实体抽取 / 聊天问答）：
组装 {"messages":[...]} → strategy.apply(payload) →
client.chat.completions.create(messages=payload["messages"],
extra_body=payload.get("extra_body"))——extra_body 与 messages 注入统一在
策略内处理，接入点无需知道具体实现；未来接入新服务商/模型时只需新增策略
类并在选择函数加分支。聊天问答（chat_service.stream_chat）读取 chat 段
thinking_mode（默认 disabled）按同一选择函数应用策略；注入属于请求层变换，
发生在 prompt 事件下发之后，prompt 事件内容保持组装后原始 messages。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---- DeepSeek 思考模式 → extra_body 组装（纯函数，知识图谱/上下文检索共用） ----
# DeepSeek API（extra_body 传）：{"thinking": {"type": "enabled"|"disabled"}} +
# "reasoning_effort": "low"|"high"|"max"（思考强度，仅 thinking.enabled 时传，
# 文档约束）；关闭思考可显著降低简单/延迟敏感任务（图谱抽取、上下文摘要）的
# 耗时与 token 消耗——默认关闭（用户明确意图）
_THINKING_EFFORT_MAP = {
    "enabled_low": "low",
    "enabled_high": "high",
    "enabled_max": "max",
}


def build_thinking_extra_body(thinking_mode: Optional[str] = None) -> dict:
    """parser_config.thinking_mode → LLM 调用 extra_body（纯函数，供测试直测）

    - None/缺省/disabled → {"thinking": {"type": "disabled"}}（默认：关闭思考，
      图谱抽取/摘要属简单延迟敏感任务，关闭加速并节省 token）
    - enabled_low/enabled_high/enabled_max → {"thinking": {"type": "enabled"},
      "reasoning_effort": "low"/"high"/"max"（思考强度，仅开启时携带）}
    - 未知值 → {}（不传 extra_body，跟随服务端默认，防御脏数据/旧配置）
    """
    if not thinking_mode or thinking_mode == "disabled":
        return {"thinking": {"type": "disabled"}}
    effort = _THINKING_EFFORT_MAP.get(thinking_mode)
    if not effort:
        return {}
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


# ---- 策略抽象 ----

class ThinkingStrategy:
    """策略抽象：apply(payload) 原地修改 LLM 请求体

    payload = {"messages": [...], "extra_body": 策略填充}——messages 注入
    （本地 prefill）与 extra_body（在线 thinking 控制）统一在策略内处理，
    接入点只组装 messages 后交给策略。
    """
    name: str = "base"

    def apply(self, payload: dict) -> None:
        raise NotImplementedError


class ExtraBodyStrategy(ThinkingStrategy):
    """在线 API（DeepSeek 等）：extra_body 传 thinking enabled/disabled + reasoning_effort

    与改造前 build_thinking_extra_body 直接传 extra_body 完全等价（产物一致）；
    thinking_mode 为 None/disabled → 关闭思考；enabled_low/high/max → 开启 +
    强度；未知值 → {}（跟随服务端默认，防御脏数据）
    """
    name = "extra_body"

    def __init__(self, thinking_mode: Optional[str] = None):
        self.thinking_mode = thinking_mode

    def apply(self, payload: dict) -> None:
        payload["extra_body"] = build_thinking_extra_body(self.thinking_mode)


# 本地 LM Studio Qwen 思考模型关闭思考的 prefill 消息（实测有效：
# 2.7s、reasoning=0、正常 JSON；注入后模型跳过思考直接输出）
_PREFILL_ASSISTANT_MSG = {
    "role": "assistant",
    "content": "<think>\n\n</think>",
    "continue_assistant_turn": True,
}


class QwenPrefillStrategy(ThinkingStrategy):
    """本地 LM Studio Qwen 思考模型（thinking_mode=disabled）关闭思考

    LM Studio 0.4.x 忽略 extra_body 的 thinking 参数 → 模型默认思考；
    在 messages 末尾注入空 <think> 块 + continue_assistant_turn 让模型
    跳过思考直接输出（仅 thinking_mode=disabled 时由选择函数选用；
    enabled* 时不注入，保持模型默认思考）
    """
    name = "qwen_prefill"

    def apply(self, payload: dict) -> None:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return  # 防御：无 messages 不改请求
        messages.append(_PREFILL_ASSISTANT_MSG)


class NoopStrategy(ThinkingStrategy):
    """默认兜底：不改请求（llm_cfg 无法判断服务商时——非 dict/base_url 空）"""
    name = "noop"

    def apply(self, payload: dict) -> None:
        return


# ---- 策略选择 ----

def get_thinking_strategy(llm_cfg, thinking_mode: Optional[str] = None) -> ThinkingStrategy:
    """按模型配置的 thinking_control 选择思考控制策略（纯函数，供测试直测）

    - "prefill"：注入空 <think> 块跳过思考——Qwen 系（chat template 含
      <think> 结构）且部署端忽略 API 参数的场景（LM Studio）；
      thinking_mode=disabled 时注入，enabled* 时不注入（保持模型默认思考）
    - "api"：extra_body 传 thinking 参数——支持它的在线 API（DeepSeek 等）
    - "none"/缺省：Noop 不改请求——模型本身不思考，或部署端已关闭
      （如 vLLM 启动参数关了思考）；对这类模型注入 prefill 会把回答压短、
      丢图片，对 DeepSeek 一类模型则完全无效
    - llm_cfg 非 dict / base_url 空 → Noop 兜底（无法判断服务商）
    """
    if not isinstance(llm_cfg, dict):
        return NoopStrategy()
    base_url = (llm_cfg.get("base_url") or "").strip()
    if not base_url:
        return NoopStrategy()
    mode = thinking_mode or "disabled"
    control = (llm_cfg.get("thinking_control") or "none").strip().lower()
    if control == "prefill":
        if mode != "disabled":
            logger.warning(
                "模型思考控制方式为 prefill（base_url=%s），thinking_mode=%s "
                "时不注入 prefill：注入只用于跳过思考，无法控制思考强度",
                base_url, mode)
        return (QwenPrefillStrategy() if mode == "disabled"
                else ExtraBodyStrategy(mode))
    if control == "api":
        return ExtraBodyStrategy(mode)
    return NoopStrategy()
