"""token 计数测试：/tokenize 探测、降级估算、按预算截断

背景：进 prompt 的检索片段可能很大（父子分块下父块实测 3.6 万字 = 2.3 万 token），
而生产模型 Qwen3.5-9B 窗口只有 15000——不做预算控制会撑爆上下文。
token 数优先调模型服务的 /tokenize（vLLM 内置，用的就是模型自己的分词器），
服务不支持时按字符数 × 0.62 估算（实测系数 0.618~0.633，取值偏保守=高估 token，
正是防爆场景想要的误差方向）。
"""
from __future__ import annotations

import asyncio

import pytest

from backend.services import token_counter as tc


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """每个用例前清空探测缓存（模块级 dict 会跨用例串味）"""
    tc.reset_probe_cache()
    yield
    tc.reset_probe_cache()


class TestEstimateFallback:
    """字符数估算（降级路径）"""

    def test_estimate_uses_conservative_ratio(self):
        assert tc.estimate_tokens("中" * 1000) == 620

    def test_empty_text(self):
        assert tc.estimate_tokens("") == 0
        assert tc.estimate_tokens(None) == 0

    def test_no_base_url_uses_estimate(self):
        """没给服务地址（未配置/测试）→ 直接估算，不发请求"""
        assert asyncio.run(tc.count_tokens("中" * 100, None, None)) == 62

    def test_truncate_to_tokens(self):
        text = "中" * 1000
        # 620 token 预算 → 1000 字符（1000*0.62=620）
        assert len(tc.truncate_to_tokens(text, 620)) == 1000
        # 预算减半 → 字符数减半左右
        assert len(tc.truncate_to_tokens(text, 310)) == 500
        # 文本本身短于预算 → 原样返回
        assert tc.truncate_to_tokens("短", 1000) == "短"
        assert tc.truncate_to_tokens(text, 0) == ""


class TestProbeAndDegrade:
    """探测模型服务是否支持 /tokenize，不支持则缓存并降级"""

    def test_unsupported_service_cached_and_degrades(self, monkeypatch):
        calls = {"n": 0}

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                calls["n"] += 1
                raise RuntimeError("404 Not Found")

        monkeypatch.setattr(tc.httpx, "AsyncClient", FakeClient)
        text = "中" * 100
        # 首次：探测失败 → 降级估算
        assert asyncio.run(tc.count_tokens(text, "http://x/v1", "m")) == 62
        assert calls["n"] == 1
        # 再次：已知不支持 → 直接估算，**不再发请求**
        assert asyncio.run(tc.count_tokens(text, "http://x/v1", "m")) == 62
        assert calls["n"] == 1, "不支持的服务不该反复重试"

    def test_supported_service_returns_exact_count(self, monkeypatch):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"count": 42, "max_model_len": 15000}

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                assert url.endswith("/tokenize"), f"须调根路径: {url}"
                assert not url.endswith("/v1/tokenize"), "实测 /v1/tokenize 是 404"
                return Resp()

        monkeypatch.setattr(tc.httpx, "AsyncClient", FakeClient)
        assert asyncio.run(tc.count_tokens("任意文本", "http://x:8000/v1", "m")) == 42

    def test_tokenize_url_strips_v1_suffix(self):
        assert tc._tokenize_url("http://h:8000/v1") == "http://h:8000/tokenize"
        assert tc._tokenize_url("http://h:8000/v1/") == "http://h:8000/tokenize"
        assert tc._tokenize_url("http://h:8000") == "http://h:8000/tokenize"


class TestCountMany:
    """批量：/tokenize 不收数组，逐个并发调用"""

    def test_batch_without_base_url(self):
        out = asyncio.run(tc.count_many(["中" * 100, "", "中" * 200], None, None))
        assert out == [62, 0, 124]

    def test_empty_list(self):
        assert asyncio.run(tc.count_many([], None, None)) == []
