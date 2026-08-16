"""思考关闭策略模块（backend/services/thinking_strategy.py）单测

覆盖：
- 策略选择 get_thinking_strategy：
  - 在线 base_url（api.deepseek.com）→ ExtraBody（thinking_mode 任意，原行为）
  - 本地 base_url（127.0.0.1/localhost/192.168./10./172.16-31.）+
    disabled → QwenPrefill（本地关闭思考的实测有效方案）
  - 本地 + enabled_low/high/max → ExtraBody（不注入 prefill，保持默认思考）
  - llm_cfg 异常（None/非 dict/base_url 空）→ Noop 兜底
- ExtraBody.apply：产物与 build_thinking_extra_body 完全一致
  （disabled/enabled_low/high/max/未知值），且不改动 messages
- QwenPrefill.apply：messages 末尾注入空 <think> 块 + continue_assistant_turn
  （位置/格式），不设置 extra_body；无 messages 防御不炸
- Noop.apply：payload 完全不变
"""
from __future__ import annotations

import pytest

from backend.services.thinking_strategy import (
    ExtraBodyStrategy, NoopStrategy, QwenPrefillStrategy,
    build_thinking_extra_body, get_thinking_strategy)

# 在线 API 代表（DeepSeek 路径）
_ONLINE_URL = "https://api.deepseek.com/v1"
# 本地内网地址（LM Studio 等，各私有段代表）
_LOCAL_URLS = [
    "http://127.0.0.1:1234/v1",
    "http://localhost:1234/v1",
    "http://192.168.0.74:1234/v1",
    "http://10.0.0.5:1234/v1",
    "http://172.16.5.5:1234/v1",
    "http://172.31.0.1:1234/v1",
]


# ==================== 策略选择 ====================

class TestGetThinkingStrategy:

    def test_online_url_always_extra_body(self):
        """在线 base_url → ExtraBody（thinking_mode 任意，原行为不变）"""
        for mode in (None, "", "disabled", "enabled_low", "enabled_high",
                     "enabled_max", "enabled_ultra"):
            s = get_thinking_strategy({"base_url": _ONLINE_URL,
                                       "model": "deepseek-chat"}, mode)
            assert isinstance(s, ExtraBodyStrategy), mode

    @pytest.mark.parametrize("url", _LOCAL_URLS)
    def test_local_url_disabled_qwen_prefill(self, url):
        """本地 base_url + disabled → QwenPrefill（prefill 注入跳过思考）"""
        s = get_thinking_strategy({"base_url": url}, "disabled")
        assert isinstance(s, QwenPrefillStrategy)

    @pytest.mark.parametrize("url", _LOCAL_URLS)
    @pytest.mark.parametrize("mode", ["enabled_low", "enabled_high",
                                      "enabled_max", "enabled"])
    def test_local_url_enabled_no_prefill(self, url, mode):
        """本地 base_url + enabled* → ExtraBody（不注入 prefill，
        本地无法控制思考强度 → 保持模型默认思考）"""
        s = get_thinking_strategy({"base_url": url}, mode)
        assert isinstance(s, ExtraBodyStrategy)

    def test_noop_fallback(self):
        """llm_cfg 无法判断服务商 → Noop 兜底（不改请求）"""
        assert isinstance(get_thinking_strategy(None, "disabled"), NoopStrategy)
        assert isinstance(get_thinking_strategy("str", "disabled"), NoopStrategy)
        assert isinstance(get_thinking_strategy({}, "disabled"), NoopStrategy)
        assert isinstance(get_thinking_strategy({"base_url": ""}, "disabled"),
                          NoopStrategy)
        assert isinstance(get_thinking_strategy({"base_url": None}, "disabled"),
                          NoopStrategy)
        assert isinstance(get_thinking_strategy({"base_url": "  "}, "disabled"),
                          NoopStrategy)


# ==================== ExtraBody：与原 build_thinking_extra_body 等价 ====================

class TestExtraBodyApply:

    def test_matches_legacy_builder(self):
        """apply 产物与改造前 build_thinking_extra_body 完全一致（等价断言）"""
        for mode in (None, "", "disabled", "enabled_low", "enabled_high",
                     "enabled_max", "enabled_ultra"):
            payload = {"messages": [{"role": "user", "content": "hi"}]}
            ExtraBodyStrategy(mode).apply(payload)
            assert payload["extra_body"] == build_thinking_extra_body(mode), mode

    def test_disabled_extra_body(self):
        payload = {"messages": []}
        ExtraBodyStrategy("disabled").apply(payload)
        assert payload["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_enabled_low_extra_body(self):
        payload = {"messages": []}
        ExtraBodyStrategy("enabled_low").apply(payload)
        assert payload["extra_body"] == {"thinking": {"type": "enabled"},
                                         "reasoning_effort": "low"}

    def test_unknown_mode_empty_extra_body(self):
        """未知 thinking_mode → extra_body={}（跟随服务端默认，防御脏数据）"""
        payload = {"messages": []}
        ExtraBodyStrategy("enabled_ultra").apply(payload)
        assert payload["extra_body"] == {}

    def test_messages_untouched(self):
        """ExtraBody 只设 extra_body，不改 messages"""
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "u"}]
        payload = {"messages": msgs}
        ExtraBodyStrategy("disabled").apply(payload)
        assert set(payload.keys()) == {"messages", "extra_body"}
        assert payload["messages"] == msgs


# ==================== QwenPrefill：prefill 消息注入 ====================

class TestQwenPrefillApply:

    def test_injects_empty_think_at_end(self):
        """messages 末尾注入空 <think> 块 + continue_assistant_turn（位置/格式）"""
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "u"}]
        payload = {"messages": msgs}
        expected_head = list(msgs)
        QwenPrefillStrategy().apply(payload)
        assert len(payload["messages"]) == 3
        tail = payload["messages"][-1]
        assert tail["role"] == "assistant"
        assert tail["content"] == "<think>\n\n</think>"
        assert tail["continue_assistant_turn"] is True
        # 注入前的消息不被改动
        assert payload["messages"][:2] == expected_head

    def test_no_extra_body_key(self):
        """prefill 策略不设置 extra_body（LM Studio 忽略该参数）"""
        payload = {"messages": []}
        QwenPrefillStrategy().apply(payload)
        assert "extra_body" not in payload

    def test_no_messages_safe(self):
        """无 messages 的 payload 防御：不炸、不改"""
        payload = {}
        QwenPrefillStrategy().apply(payload)
        assert payload == {}

    def test_repeated_apply_appends(self):
        payload = {"messages": [{"role": "user", "content": "u"}]}
        QwenPrefillStrategy().apply(payload)
        QwenPrefillStrategy().apply(payload)
        assert len(payload["messages"]) == 3


# ==================== Noop：兜底 ====================

class TestNoopApply:

    def test_payload_unchanged(self):
        """Noop 不改请求（含既有 extra_body 也不动）"""
        payload = {"messages": [{"role": "user", "content": "u"}],
                   "extra_body": {"thinking": {"type": "disabled"}}}
        NoopStrategy().apply(payload)
        assert payload == {"messages": [{"role": "user", "content": "u"}],
                           "extra_body": {"thinking": {"type": "disabled"}}}
