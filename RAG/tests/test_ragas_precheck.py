"""RAGAS 发起前可用性探测测试：GET /api/stats/ragas/precheck

覆盖：
- 路由契约：未登录 401；登录返回 {llm, embedding} 结构（探测结果透传）；
  探测失败不抛异常（不可用结果接口仍 200）；两个探测并行均被调用；
  端到端（真探测函数 + fake httpx）全可用 → 200 全可用
- _probe_llm：GET {base_url}/models 2xx（携带 Authorization）→ 可用；
  base_url 未配置 / HTTP 5xx / 超时 / 连接失败 → 不可用 + 中文 reason
- _probe_embedding：POST {base_url}/embeddings（断言请求体 model/input）
  有向量数据 → 可用；无向量数据 / HTTP 5xx / 未配置 / 超时 / 连接失败
  → 不可用 + 中文 reason

全部离线：httpx.AsyncClient 用 FakeAsyncClient 替换（不经真实网络）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest


# ==================== mock httpx.AsyncClient ====================


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if isinstance(self._json, BaseException):
            raise self._json
        return self._json


class FakeAsyncClient:
    """按 (method, url 后缀) 匹配预置响应；item 为 BaseException 时直接抛出"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _match(self, method, url):
        for m, suffix, item in self.responses:
            if m == method and url.endswith(suffix):
                if isinstance(item, BaseException):
                    raise item
                return item
        raise AssertionError(f"未预置响应: {method} {url}")

    async def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return self._match("get", url)

    async def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return self._match("post", url)


def _patch_httpx(monkeypatch, responses):
    """monkeypatch httpx.AsyncClient 为 FakeAsyncClient（返回其实例供断言）"""
    fake = FakeAsyncClient(responses)
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: fake)
    return fake


def _cfg(base_url="http://test:1234/v1", api_key="k", model="m"):
    return SimpleNamespace(base_url=base_url, api_key=api_key, model=model)


def _probe_llm(cfg):
    from backend.routers.stats import _probe_llm as fn
    return asyncio.run(fn(cfg))


def _probe_embedding(cfg):
    from backend.routers.stats import _probe_embedding as fn
    return asyncio.run(fn(cfg))


# ==================== _probe_llm 单元 ====================


class TestProbeLlm:

    def test_ok_with_auth_header(self, monkeypatch):
        """GET {base_url}/models 2xx → 可用，且携带 Authorization"""
        fake = _patch_httpx(monkeypatch,
                            [("get", "/models", FakeResponse(200))])
        assert _probe_llm(_cfg()) == {"available": True, "reason": ""}
        method, url, kw = fake.calls[0]
        assert method == "get"
        assert url.endswith("/v1/models")
        assert kw["headers"]["Authorization"] == "Bearer k"

    def test_no_base_url(self, monkeypatch):
        """地址未配置 → 不可用 + 未配置原因"""
        result = _probe_llm(_cfg(base_url=""))
        assert result["available"] is False
        assert "未配置" in result["reason"]

    def test_http_5xx(self, monkeypatch):
        """服务端 5xx → 不可用 + HTTP 状态"""
        _patch_httpx(monkeypatch, [("get", "/models", FakeResponse(500))])
        result = _probe_llm(_cfg())
        assert result["available"] is False
        assert "HTTP 500" in result["reason"]

    def test_http_4xx_unavailable(self, monkeypatch):
        """4xx（如 401）也视为不可用"""
        _patch_httpx(monkeypatch, [("get", "/models", FakeResponse(401))])
        result = _probe_llm(_cfg())
        assert result["available"] is False
        assert "HTTP 401" in result["reason"]

    def test_timeout(self, monkeypatch):
        """超时 → 不可用 + 中文超时原因"""
        _patch_httpx(monkeypatch,
                     [("get", "/models", httpx.TimeoutException("t"))])
        result = _probe_llm(_cfg())
        assert result["available"] is False
        assert "连接超时" in result["reason"]

    def test_connect_failed(self, monkeypatch):
        """连接失败 → 不可用 + 中文失败原因（不抛异常）"""
        _patch_httpx(monkeypatch,
                     [("get", "/models", ConnectionError("refused"))])
        result = _probe_llm(_cfg())
        assert result["available"] is False
        assert "连接失败" in result["reason"]


# ==================== _probe_embedding 单元 ====================


class TestProbeEmbedding:

    def test_ok_with_request_body(self, monkeypatch):
        """POST {base_url}/embeddings 有向量数据 → 可用；断言请求体 model/input"""
        fake = _patch_httpx(monkeypatch, [
            ("post", "/embeddings",
             FakeResponse(200, {"data": [{"embedding": [0.1, 0.2]}]})),
        ])
        assert _probe_embedding(_cfg()) == {"available": True, "reason": ""}
        method, url, kw = fake.calls[0]
        assert method == "post"
        assert url.endswith("/v1/embeddings")
        assert kw["json"] == {"model": "m", "input": "连接测试"}
        assert kw["headers"]["Authorization"] == "Bearer k"

    def test_no_base_url(self, monkeypatch):
        """地址未配置 → 不可用 + 未配置原因"""
        result = _probe_embedding(_cfg(base_url=""))
        assert result["available"] is False
        assert "未配置" in result["reason"]

    def test_no_model(self, monkeypatch):
        """模型未配置 → 不可用 + 未配置原因"""
        result = _probe_embedding(_cfg(model=""))
        assert result["available"] is False
        assert "模型未配置" in result["reason"]

    def test_empty_vector_data(self, monkeypatch):
        """2xx 但无向量数据 → 不可用 + 响应异常原因"""
        _patch_httpx(monkeypatch, [
            ("post", "/embeddings", FakeResponse(200, {"data": []})),
        ])
        result = _probe_embedding(_cfg())
        assert result["available"] is False
        assert "无向量数据" in result["reason"]

    def test_non_json_body(self, monkeypatch):
        """2xx 但响应非 JSON → 不可用（不抛异常）"""
        _patch_httpx(monkeypatch, [
            ("post", "/embeddings", FakeResponse(200, ValueError("bad json"))),
        ])
        result = _probe_embedding(_cfg())
        assert result["available"] is False
        assert "无向量数据" in result["reason"]

    def test_http_5xx(self, monkeypatch):
        """服务端 5xx → 不可用 + HTTP 状态"""
        _patch_httpx(monkeypatch,
                     [("post", "/embeddings", FakeResponse(500))])
        result = _probe_embedding(_cfg())
        assert result["available"] is False
        assert "HTTP 500" in result["reason"]

    def test_timeout(self, monkeypatch):
        """超时 → 不可用 + 中文超时原因"""
        _patch_httpx(monkeypatch,
                     [("post", "/embeddings", httpx.TimeoutException("t"))])
        result = _probe_embedding(_cfg())
        assert result["available"] is False
        assert "连接超时" in result["reason"]

    def test_connect_failed(self, monkeypatch):
        """连接失败 → 不可用 + 中文失败原因（不抛异常）"""
        _patch_httpx(monkeypatch,
                     [("post", "/embeddings", ConnectionError("refused"))])
        result = _probe_embedding(_cfg())
        assert result["available"] is False
        assert "连接失败" in result["reason"]


# ==================== 路由契约 ====================


class TestPrecheckRoute:

    def test_requires_login(self, client):
        """未登录 401"""
        resp = client.get("/api/stats/ragas/precheck")
        assert resp.status_code == 401

    def test_contract_transparent(self, client, admin_headers, monkeypatch):
        """登录返回 {llm, embedding} 结构，探测结果透传"""
        async def fake_llm(cfg):
            return {"available": True, "reason": ""}

        async def fake_emb(cfg):
            return {"available": False, "reason": "Embedding 服务地址未配置"}

        monkeypatch.setattr("backend.routers.stats._probe_llm", fake_llm)
        monkeypatch.setattr("backend.routers.stats._probe_embedding", fake_emb)
        resp = client.get("/api/stats/ragas/precheck", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == {
            "llm": {"available": True, "reason": ""},
            "embedding": {"available": False, "reason": "Embedding 服务地址未配置"},
        }

    def test_probe_failure_not_raise(self, client, admin_headers, monkeypatch):
        """探测失败不抛异常：不可用结果接口仍 200"""
        async def fake_llm(cfg):
            return {"available": False, "reason": "连接失败: x"}

        async def fake_emb(cfg):
            return {"available": False, "reason": "连接超时（5s）"}

        monkeypatch.setattr("backend.routers.stats._probe_llm", fake_llm)
        monkeypatch.setattr("backend.routers.stats._probe_embedding", fake_emb)
        resp = client.get("/api/stats/ragas/precheck", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["llm"] == {"available": False, "reason": "连接失败: x"}
        assert data["embedding"] == {"available": False, "reason": "连接超时（5s）"}

    def test_both_probes_called(self, client, admin_headers, monkeypatch):
        """路由并行调用两个探测（llm/embedding 都被执行）"""
        called = []

        async def fake_llm(cfg):
            called.append("llm")
            return {"available": True, "reason": ""}

        async def fake_emb(cfg):
            called.append("emb")
            return {"available": True, "reason": ""}

        monkeypatch.setattr("backend.routers.stats._probe_llm", fake_llm)
        monkeypatch.setattr("backend.routers.stats._probe_embedding", fake_emb)
        client.get("/api/stats/ragas/precheck", headers=admin_headers)
        assert sorted(called) == ["emb", "llm"]

    def test_end_to_end_all_ok(self, client, admin_headers, monkeypatch):
        """端到端：真探测函数 + fake httpx 全可用 → 200 全可用（无真实网络）"""
        fake = _patch_httpx(monkeypatch, [
            ("get", "/models", FakeResponse(200)),
            ("post", "/embeddings",
             FakeResponse(200, {"data": [{"embedding": [0.1]}]})),
        ])
        resp = client.get("/api/stats/ragas/precheck", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["llm"]["available"] is True
        assert data["embedding"]["available"] is True
        assert len(fake.calls) == 2
