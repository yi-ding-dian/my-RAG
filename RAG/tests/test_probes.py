"""统一探测服务测试：probes.py 六类探测（成功/超时/连接失败/401/未配置）

覆盖：
- probe_llm / probe_embedding（httpx 轻量形态，stats precheck 用）：
  GET /models 2xx 带 Authorization、POST /embeddings 请求体/向量数据、
  未配置 / 5xx / 401 / 超时 / 连接失败 / 无向量数据 / 非 JSON
- probe_mineru（异步，parser 探测用）：200 可用、超时只试首端点、连接失败、
  未配置、500 后继续端点；probe_mineru_sync（同步，设置页用）：
  200 可用、ok_under=400（404 不可用继续试）
- probe_deepdoc（异步）：200+token 可用、401 无 token 不可用、连接失败、
  未配置、RSA 加密密码；probe_deepdoc_sync（同步）
- probe_llm_sdk / probe_embedding_sdk（设置页 SDK 形态）：成功（返回/维度）、
  失败，client_cls 注入可测
- probe_mysql：URL 覆盖模式跳过、mock aiomysql.connect 成功/失败
- probe_minio：endpoint 未配置、mock Minio 桶存在/连接失败
- 契约映射：settings_service._test_*（{ok, latency_ms, message}）、
  stats._probe_llm/_probe_embedding（{available, reason}）

全部离线 mock（httpx / aiomysql / minio / OpenAI 客户端类）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from backend.services import probes
from backend.services.probes import (probe_deepdoc, probe_deepdoc_sync,
                                     probe_embedding, probe_embedding_sdk,
                                     probe_llm, probe_llm_sdk, probe_mineru,
                                     probe_mineru_sync, probe_minio,
                                     probe_mysql)


# ==================== mock httpx.AsyncClient（异步形态） ====================


class FakeResponse:
    def __init__(self, status_code=200, headers=None, json_data=None):
        self.status_code = status_code
        self.headers = headers or {}
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


def _patch_async_http(monkeypatch, responses):
    fake = FakeAsyncClient(responses)
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=None: fake)
    return fake


def _cfg(base_url="http://test:1234/v1", api_key="k", model="m"):
    return SimpleNamespace(base_url=base_url, api_key=api_key, model=model)


# ==================== probe_llm / probe_embedding（httpx 轻量形态） ====================


class TestProbeLlm:
    def test_ok_with_auth_header(self, monkeypatch):
        """GET {base_url}/models 2xx → ok，且携带 Authorization"""
        fake = _patch_async_http(monkeypatch,
                                 [("get", "/models", FakeResponse(200))])
        result = asyncio.run(probe_llm(_cfg()))
        assert result["ok"] is True
        assert "连接成功" in result["reason"]
        method, url, kw = fake.calls[0]
        assert method == "get"
        assert url.endswith("/v1/models")
        assert kw["headers"]["Authorization"] == "Bearer k"

    def test_no_base_url(self):
        """地址未配置 → 不可用 + 未配置原因（不发请求）"""
        result = asyncio.run(probe_llm(_cfg(base_url="")))
        assert result["ok"] is False
        assert "未配置" in result["reason"]

    def test_http_5xx(self, monkeypatch):
        """服务端 5xx → 不可用 + HTTP 状态"""
        _patch_async_http(monkeypatch, [("get", "/models", FakeResponse(500))])
        result = asyncio.run(probe_llm(_cfg()))
        assert result["ok"] is False
        assert "HTTP 500" in result["reason"]

    def test_http_401(self, monkeypatch):
        """401 → 不可用（与 404 存在性探测同契约）"""
        _patch_async_http(monkeypatch, [("get", "/models", FakeResponse(401))])
        result = asyncio.run(probe_llm(_cfg()))
        assert result["ok"] is False
        assert "HTTP 401" in result["reason"]

    def test_timeout(self, monkeypatch):
        """超时 → 不可用 + 中文超时原因"""
        _patch_async_http(monkeypatch,
                          [("get", "/models", httpx.TimeoutException("t"))])
        result = asyncio.run(probe_llm(_cfg()))
        assert result["ok"] is False
        assert "连接超时" in result["reason"]

    def test_connect_failed(self, monkeypatch):
        """连接失败 → 不可用 + 中文失败原因（不抛异常）"""
        _patch_async_http(monkeypatch,
                          [("get", "/models", ConnectionError("refused"))])
        result = asyncio.run(probe_llm(_cfg()))
        assert result["ok"] is False
        assert "连接失败" in result["reason"]
        assert "refused" in result["reason"]


class TestProbeEmbedding:
    def test_ok_with_request_body(self, monkeypatch):
        """POST {base_url}/embeddings 有向量数据 → 可用；断言请求体 model/input"""
        fake = _patch_async_http(monkeypatch, [
            ("post", "/embeddings",
             FakeResponse(200, json_data={"data": [{"embedding": [0.1, 0.2]}]})),
        ])
        result = asyncio.run(probe_embedding(_cfg()))
        assert result["ok"] is True
        method, url, kw = fake.calls[0]
        assert method == "post"
        assert url.endswith("/v1/embeddings")
        assert kw["json"] == {"model": "m", "input": "连接测试"}
        assert kw["headers"]["Authorization"] == "Bearer k"

    def test_no_base_url(self):
        """地址未配置 → 不可用 + 未配置原因"""
        result = asyncio.run(probe_embedding(_cfg(base_url="")))
        assert result["ok"] is False
        assert "未配置" in result["reason"]

    def test_no_model(self):
        """模型未配置 → 不可用 + 未配置原因"""
        result = asyncio.run(probe_embedding(_cfg(model="")))
        assert result["ok"] is False
        assert "模型未配置" in result["reason"]

    def test_empty_vector_data(self, monkeypatch):
        """2xx 但无向量数据 → 不可用 + 响应异常原因"""
        _patch_async_http(monkeypatch, [
            ("post", "/embeddings", FakeResponse(200, json_data={"data": []})),
        ])
        result = asyncio.run(probe_embedding(_cfg()))
        assert result["ok"] is False
        assert "无向量数据" in result["reason"]

    def test_non_json_body(self, monkeypatch):
        """2xx 但响应非 JSON → 不可用（不抛异常）"""
        _patch_async_http(monkeypatch, [
            ("post", "/embeddings",
             FakeResponse(200, json_data=ValueError("bad json"))),
        ])
        result = asyncio.run(probe_embedding(_cfg()))
        assert result["ok"] is False
        assert "无向量数据" in result["reason"]

    def test_http_5xx(self, monkeypatch):
        """服务端 5xx → 不可用 + HTTP 状态"""
        _patch_async_http(monkeypatch,
                          [("post", "/embeddings", FakeResponse(500))])
        result = asyncio.run(probe_embedding(_cfg()))
        assert result["ok"] is False
        assert "HTTP 500" in result["reason"]

    def test_timeout(self, monkeypatch):
        """超时 → 不可用 + 中文超时原因"""
        _patch_async_http(monkeypatch,
                          [("post", "/embeddings",
                            httpx.TimeoutException("t"))])
        result = asyncio.run(probe_embedding(_cfg()))
        assert result["ok"] is False
        assert "连接超时" in result["reason"]

    def test_connect_failed(self, monkeypatch):
        """连接失败 → 不可用 + 中文失败原因"""
        _patch_async_http(monkeypatch,
                          [("post", "/embeddings", ConnectionError("refused"))])
        result = asyncio.run(probe_embedding(_cfg()))
        assert result["ok"] is False
        assert "连接失败" in result["reason"]


# ==================== probe_mineru（异步） ====================


class TestProbeMineru:
    def test_ok(self, monkeypatch):
        """/health 200 → 可用"""
        fake = _patch_async_http(monkeypatch,
                                 [("get", "/health", FakeResponse(200))])
        result = asyncio.run(probe_mineru({"url": "http://m:8001"}))
        assert result["ok"] is True
        assert "连接成功" in result["reason"]
        assert fake.calls[0][1].endswith("/health")

    def test_http_500_falls_through_endpoints(self, monkeypatch):
        """全部端点 5xx → 不可用 + HTTP 原因（继续试后续端点）"""
        fake = _patch_async_http(monkeypatch, [
            ("get", "/health", FakeResponse(500)),
        ])
        result = asyncio.run(probe_mineru({"url": "http://m:8001"}))
        assert result["ok"] is False
        assert "HTTP 500" in result["reason"]
        assert len([c for c in fake.calls if c[0] == "get"]) == 3

    def test_timeout_stops_after_first_endpoint(self, monkeypatch):
        """超时 → 不可用 + 超时原因（不再试后续端点）"""
        fake = _patch_async_http(monkeypatch, [
            ("get", "/health", httpx.TimeoutException("t")),
        ])
        result = asyncio.run(probe_mineru({"url": "http://m:8001"}, timeout=5.0))
        assert result["ok"] is False
        assert "连接超时" in result["reason"]
        assert len([c for c in fake.calls if c[0] == "get"]) == 1

    def test_connect_failed(self, monkeypatch):
        """连接失败 → 不可用 + 失败原因"""
        _patch_async_http(monkeypatch, [
            ("get", "/health", ConnectionError("connection refused")),
        ])
        result = asyncio.run(probe_mineru({"url": "http://m:8001"}))
        assert result["ok"] is False
        assert "connection refused" in result["reason"]

    def test_no_api_url(self):
        """服务地址未配置 → 不可用 + 未配置原因"""
        result = asyncio.run(probe_mineru({"url": ""}))
        assert result["ok"] is False
        assert "未配置" in result["reason"]


class TestProbeMineruSync:
    def test_ok(self, monkeypatch):
        """200 → 可用（httpx.get 同步形态）"""
        monkeypatch.setattr(httpx, "get",
                            lambda url, **kw: SimpleNamespace(status_code=200))
        result = probe_mineru_sync({"url": "http://m:8001"})
        assert result["ok"] is True

    def test_ok_under_400_semantics(self, monkeypatch):
        """ok_under=400：404 不可用并继续试下一端点（设置页历史契约）"""
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return SimpleNamespace(status_code=404)
        monkeypatch.setattr(httpx, "get", fake_get)
        result = probe_mineru_sync({"url": "http://m:8001"})
        assert result["ok"] is False
        assert "HTTP 404" in result["reason"]
        assert len(calls) == 3, "404 后应继续探测 /api/health 与根路径"

    def test_ok_under_500_semantics(self, monkeypatch):
        """ok_under=500：404 视为服务在（解析前可用性契约）"""
        monkeypatch.setattr(httpx, "get",
                            lambda url, **kw: SimpleNamespace(status_code=404))
        result = probe_mineru_sync({"url": "http://m:8001"}, ok_under=500)
        assert result["ok"] is True

    def test_connect_failed(self, monkeypatch):
        """连接失败 → 不可用 + 失败原因"""
        def fake_get(url, **kw):
            raise ConnectionError("mock: MinerU 不可达")
        monkeypatch.setattr(httpx, "get", fake_get)
        result = probe_mineru_sync({"url": "http://m:8001"})
        assert result["ok"] is False
        assert "连接失败" in result["reason"]


# ==================== probe_deepdoc ====================


class TestProbeDeepdoc:
    def test_ok(self, monkeypatch):
        """登录 200 + token 头 → 可用；断言 RSA 加密密码"""
        fake = _patch_async_http(monkeypatch, [
            ("post", "/v1/user/login",
             FakeResponse(200, headers={"HTTP_AUTHORIZATION": "t"})),
        ])
        cfg = SimpleNamespace(base_url="http://ragflow:9380",
                              email="test@example.com", password="test-password")
        result = asyncio.run(probe_deepdoc(cfg))
        assert result["ok"] is True
        assert "连接成功" in result["reason"]
        body = fake.calls[0][2]["json"]
        assert body["email"] == "test@example.com"
        assert body["password"] != "test-password"  # RSA 密文

    def test_login_401(self, monkeypatch):
        """登录 401 无 token → 不可用 + 登录失败原因"""
        _patch_async_http(monkeypatch, [
            ("post", "/v1/user/login", FakeResponse(401)),
        ])
        result = asyncio.run(probe_deepdoc(
            SimpleNamespace(base_url="http://r:9380", email="a@b.c",
                            password="x")))
        assert result["ok"] is False
        assert "登录失败" in result["reason"]
        assert "HTTP 401" in result["reason"]

    def test_timeout(self, monkeypatch):
        """超时 → 不可用 + 超时原因"""
        _patch_async_http(monkeypatch, [
            ("post", "/v1/user/login", httpx.TimeoutException("t")),
        ])
        result = asyncio.run(probe_deepdoc(
            SimpleNamespace(base_url="http://r:9380", email="a@b.c",
                            password="x"), timeout=5.0))
        assert result["ok"] is False
        assert "连接超时" in result["reason"]

    def test_connect_failed(self, monkeypatch):
        """连接失败 → 不可用 + 失败原因"""
        _patch_async_http(monkeypatch, [
            ("post", "/v1/user/login",
             httpx.ConnectError("connection refused")),
        ])
        result = asyncio.run(probe_deepdoc(
            SimpleNamespace(base_url="http://r:9380", email="a@b.c",
                            password="x")))
        assert result["ok"] is False
        assert "connection refused" in result["reason"]

    def test_no_base_url(self):
        """服务地址未配置 → 不可用 + 未配置原因（不发请求）"""
        result = asyncio.run(probe_deepdoc(
            SimpleNamespace(base_url="", email="a@b.c", password="x")))
        assert result["ok"] is False
        assert "未配置" in result["reason"]

    def test_sync_form(self, monkeypatch):
        """同步形态（httpx.post，设置页连接测试用）：200+token 可用"""
        monkeypatch.setattr(
            httpx, "post",
            lambda url, **kw: SimpleNamespace(
                status_code=200,
                headers={"HTTP_AUTHORIZATION": "mock-login-token"}))
        result = probe_deepdoc_sync(
            {"base_url": "http://ragflow:9380", "email": "a@b.c",
             "password": "pw"})
        assert result["ok"] is True
        assert "连接成功" in result["reason"]


# ==================== SDK 形态（设置页连接测试用） ====================


class _FakeOpenAI:
    """伪 OpenAI 同步客户端：base_url 含 'bad' 时调用失败，否则成功"""

    def __init__(self, base_url="", api_key="", timeout=5.0):
        self._base_url = base_url or ""
        self._api_key = api_key or ""
        self._timeout = timeout

    def _check(self, what):
        if "bad" in self._base_url:
            raise ConnectionError(f"mock: {what} 连接失败")

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    @property
    def embeddings(self):
        return self

    def create(self, **kwargs):
        if "input" in kwargs:
            self._check("Embedding")
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])
        self._check("LLM")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="hi"))])


class TestSdkProbes:
    def test_llm_ok(self):
        """最小 chat 请求成功 → ok + 连接成功原因（含返回内容）"""
        result = probe_llm_sdk({"base_url": "http://good:1234/v1",
                                "model": "m"}, client_cls=_FakeOpenAI)
        assert result["ok"] is True
        assert "连接成功" in result["reason"]
        assert "hi" in result["reason"]

    def test_llm_fail(self):
        """调用失败 → ok=False + 失败原因（不抛异常）"""
        result = probe_llm_sdk({"base_url": "http://bad:1/v1",
                                "model": "m"}, client_cls=_FakeOpenAI)
        assert result["ok"] is False
        assert "连接失败" in result["reason"]

    def test_embedding_ok_reports_dim(self):
        """embed 成功 → ok + 向量维度"""
        result = probe_embedding_sdk({"base_url": "http://good:1/v1",
                                      "model": "bge"}, client_cls=_FakeOpenAI)
        assert result["ok"] is True
        assert "向量维度: 3" in result["reason"]

    def test_embedding_fail(self):
        result = probe_embedding_sdk({"base_url": "http://bad:1/v1",
                                      "model": "bge"}, client_cls=_FakeOpenAI)
        assert result["ok"] is False
        assert "连接失败" in result["reason"]


# ==================== MySQL / MinIO ====================


class _FakeCursor:
    async def execute(self, sql):
        return 0

    async def fetchone(self):
        return (1,)

    async def close(self):
        pass


class _FakeMySQLConn:
    async def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


class TestProbeMysql:
    def test_url_override_skipped(self):
        """URL 覆盖模式（非 mysql 直连）→ 跳过直连测试"""
        result = asyncio.run(probe_mysql({"url": "sqlite+aiosqlite://"}))
        assert result["ok"] is False
        assert "URL 覆盖模式" in result["reason"]

    def test_connect_ok(self, monkeypatch):
        """直连成功（mock aiomysql.connect）"""
        import aiomysql

        async def fake_connect(**kwargs):
            return _FakeMySQLConn()

        monkeypatch.setattr(aiomysql, "connect", fake_connect)
        result = asyncio.run(probe_mysql({
            "host": "127.0.0.1", "port": 3306, "user": "u",
            "password": "p", "database": "db", "url": ""}))
        assert result["ok"] is True
        assert "连接成功" in result["reason"]
        assert "127.0.0.1:3306/db" in result["reason"]

    def test_connect_failed(self, monkeypatch):
        """连接失败 → ok=False + 失败信息（接口不抛）"""
        import aiomysql

        async def fake_connect(**kwargs):
            raise ConnectionError("mock: 连接被拒")

        monkeypatch.setattr(aiomysql, "connect", fake_connect)
        result = asyncio.run(probe_mysql({
            "host": "127.0.0.1", "port": 3306, "user": "u",
            "password": "p", "database": "db", "url": ""}))
        assert result["ok"] is False
        assert "连接失败" in result["reason"]


class _FakeMinio:
    def __init__(self, *args, **kwargs):
        self._exists = True

    def bucket_exists(self, bucket):
        if not self._exists:
            raise ConnectionError("mock: MinIO 不可达")
        return True


class TestProbeMinio:
    def test_endpoint_missing(self):
        """endpoint 未配置 → ok=False 明确提示"""
        result = asyncio.run(probe_minio(
            {"endpoint": "", "access_key": "ak", "secret_key": "sk",
             "bucket": "my-rag", "secure": False}))
        assert result["ok"] is False
        assert "endpoint 未配置" in result["reason"]

    def test_bucket_ok(self, monkeypatch):
        """桶探测成功 → ok + 桶信息"""
        import minio
        monkeypatch.setattr(minio, "Minio", _FakeMinio)
        result = asyncio.run(probe_minio(
            {"endpoint": "127.0.0.1:9000", "access_key": "ak",
             "secret_key": "sk", "bucket": "my-rag", "secure": False}))
        assert result["ok"] is True
        assert "桶 my-rag 存在" in result["reason"]

    def test_connect_failed(self, monkeypatch):
        """桶探测失败 → ok=False（不抛异常）"""
        import minio

        class _Down:
            def __init__(self, *a, **kw):
                pass

            def bucket_exists(self, bucket):
                raise ConnectionError("mock: MinIO 不可达")

        monkeypatch.setattr(minio, "Minio", _Down)
        result = asyncio.run(probe_minio(
            {"endpoint": "127.0.0.1:9000", "access_key": "ak",
             "secret_key": "sk", "bucket": "my-rag", "secure": False}))
        assert result["ok"] is False
        assert "连接失败" in result["reason"]


# ==================== 契约映射（settings / stats 薄包装） ====================


class TestContractMapping:
    def test_stats_available_mapping(self, monkeypatch):
        """stats precheck 薄包装：probes {ok, latency_ms, reason} →
        {available, reason}（成功时 reason 空串）"""
        from backend.routers.stats import (_probe_embedding, _probe_llm)
        fake = _patch_async_http(monkeypatch, [
            ("get", "/models", FakeResponse(200)),
            ("post", "/embeddings",
             FakeResponse(200, json_data={"data": [{"embedding": [0.1]}]})),
        ])
        assert asyncio.run(_probe_llm(_cfg())) == \
            {"available": True, "reason": ""}
        assert asyncio.run(_probe_embedding(_cfg())) == \
            {"available": True, "reason": ""}
        assert len(fake.calls) == 2

    def test_stats_failure_reason_passthrough(self, monkeypatch):
        """失败时 reason 透传（中文原因）"""
        from backend.routers.stats import _probe_llm
        _patch_async_http(monkeypatch,
                          [("get", "/models", httpx.TimeoutException("t"))])
        result = asyncio.run(_probe_llm(_cfg()))
        assert result == {"available": False, "reason": "连接超时（5s）"}

    def test_settings_message_mapping(self, monkeypatch):
        """settings 薄包装：{ok, latency_ms, message}（message = reason + 耗时）"""
        from backend.services.settings_service import get_settings_service
        svc = get_settings_service()
        monkeypatch.setattr(httpx, "get",
                            lambda url, **kw: SimpleNamespace(status_code=200))
        result = svc._test_mineru({"url": "http://m:8001"})
        assert set(result) == {"ok", "latency_ms", "message"}
        assert result["ok"] is True
        assert result["message"].startswith("连接成功")
        assert "耗时" in result["message"]

    def test_parser_probe_mapping(self, monkeypatch):
        """parser_probe 薄包装：probes 结果 → {available, reason}
        （成功时 reason 空串，契约与测试一致）"""
        import backend.services.parser_probe as pp
        fake = _patch_async_http(monkeypatch, [
            ("get", "/health", FakeResponse(200)),
            ("post", "/v1/user/login",
             FakeResponse(200, headers={"HTTP_AUTHORIZATION": "t"})),
        ])
        result = asyncio.run(pp.probe_parsers())
        assert result["mineru"] == {"available": True, "reason": ""}
        assert result["deepdoc"] == {"available": True, "reason": ""}
        assert fake.calls[0][1].endswith("/health")
