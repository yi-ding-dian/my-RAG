"""解析器可用性探测 + 自动降级 + 状态接口测试

覆盖：
- probe_parsers：mineru 健康探测（200/500/连接失败/超时）、deepdoc 登录探测
  （成功/登录失败/连接失败/未配置）、全部失败时 plain 仍 available、
  超时参数透传（弹窗 ≤8s / ingestion ≤5s）
- ingestion 降级链：engine=deepdoc + deepdoc 不可用 + mineru 可用 → 降级
  mineru（ingested + parser_config 记录实际值 + degrade 说明）；
  deepdoc+mineru 都不可用 → 降级 plain；engine=mineru 不可用 → plain；
  可用时不降级（响应无 degrade、parser_config 无 degrade 键）；
  txt 直读不触发探测
- 状态接口 GET /api/kbs/parsers/status：登录 200（含三解析器状态）、
  未登录 401、探测结果透传
conftest 的 autouse _mock_parser_probe 默认把调用方探测 mock 成全部可用
（离线不连真实服务、不触发降级），本文件的降级用例显式 monkeypatch 覆盖。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from conftest import create_kb, upload_doc, wait_for_status


# ==================== mock httpx.AsyncClient ====================


class FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeAsyncClient:
    """按 (method, url 后缀) 匹配预置响应；item 为 BaseException 时直接抛出

    probe 的 mineru/deepdoc 探测并行（asyncio.gather）共用同一实例：
    _used 计数是同步操作（无 await 间隙），无并发竞态。
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self._used = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _match(self, method, url):
        candidates = [item for m, suffix, item in self.responses
                      if m == method and url.endswith(suffix)]
        if not candidates:
            raise AssertionError(f"未预置响应: {method} {url}")
        n = self._used.get((method, url), 0)
        self._used[(method, url)] = n + 1
        item = candidates[0]
        if isinstance(item, list):  # 序列响应（同 URL 多次调用按序消费）
            if n >= len(item):
                raise AssertionError(
                    f"响应序列用尽: {method} {url}（第 {n + 1} 次调用）")
            item = item[n]
        if isinstance(item, BaseException):
            raise item
        return item

    async def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return self._match("get", url)

    async def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return self._match("post", url)


def _install_fake_http(monkeypatch, responses):
    fake = FakeAsyncClient(responses)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda timeout=None: fake)
    return fake


def _probe_cfg():
    """探测用的假配置（mineru.api_url + deepdoc 段）"""
    return SimpleNamespace(
        mineru=SimpleNamespace(api_url="http://mineru.test:8001"),
        deepdoc=SimpleNamespace(base_url="http://ragflow.test:9380",
                                email="test@example.com", password="test-password"))


def _all_down_status(mineru_reason="连接超时", deepdoc_reason="连接超时"):
    return {
        "mineru": {"available": False, "reason": mineru_reason},
        "deepdoc": {"available": False, "reason": deepdoc_reason},
        "plain": {"available": True, "reason": ""},
    }


# ==================== probe_parsers 单元测试 ====================


class TestProbeParsers:
    def test_probe_all_ok(self, monkeypatch):
        """mineru /health 200 + deepdoc 登录 200 带 token → 全部可用"""
        import backend.services.parser_probe as pp
        _install_fake_http(monkeypatch, [
            ("get", "/health", FakeResponse(status_code=200)),
            ("post", "/v1/user/login",
             FakeResponse(status_code=200,
                          headers={"HTTP_AUTHORIZATION": "t"})),
        ])
        result = asyncio.run(pp.probe_parsers(_probe_cfg()))
        assert result["mineru"] == {"available": True, "reason": ""}
        assert result["deepdoc"] == {"available": True, "reason": ""}
        assert result["plain"]["available"] is True

    def test_probe_mineru_connect_error(self, monkeypatch):
        """MinerU 连接失败（所有端点异常）→ 不可用 + 原因；deepdoc 仍可用"""
        import backend.services.parser_probe as pp
        _install_fake_http(monkeypatch, [
            ("get", "/health", httpx.ConnectError("connection refused")),
            ("post", "/v1/user/login",
             FakeResponse(headers={"HTTP_AUTHORIZATION": "t"})),
        ])
        result = asyncio.run(pp.probe_parsers(_probe_cfg()))
        assert result["mineru"]["available"] is False
        assert "connection refused" in result["mineru"]["reason"]
        assert result["deepdoc"]["available"] is True

    def test_probe_mineru_timeout(self, monkeypatch):
        """MinerU 连接超时 → 不可用 + 超时原因（不再试后续端点）"""
        import backend.services.parser_probe as pp
        fake = _install_fake_http(monkeypatch, [
            ("get", "/health", httpx.TimeoutException("timed out")),
            ("post", "/v1/user/login",
             FakeResponse(headers={"HTTP_AUTHORIZATION": "t"})),
        ])
        result = asyncio.run(pp.probe_parsers(_probe_cfg()))
        assert result["mineru"]["available"] is False
        assert "超时" in result["mineru"]["reason"]
        # 超时只探测了 /health 一个端点（break）
        get_calls = [c for c in fake.calls if c[0] == "get"]
        assert len(get_calls) == 1

    def test_probe_mineru_http_500_falls_through_endpoints(self, monkeypatch):
        """MinerU 全部端点非 2xx（500/404/503）→ 不可用 + HTTP 原因"""
        import backend.services.parser_probe as pp
        fake = _install_fake_http(monkeypatch, [
            ("get", "/health", [
                FakeResponse(status_code=500),
                FakeResponse(status_code=404),
                FakeResponse(status_code=503),
            ]),
            ("post", "/v1/user/login",
             FakeResponse(headers={"HTTP_AUTHORIZATION": "t"})),
        ])
        result = asyncio.run(pp.probe_parsers(_probe_cfg()))
        assert result["mineru"]["available"] is False
        assert "HTTP 500" in result["mineru"]["reason"]
        assert len([c for c in fake.calls if c[0] == "get"]) == 3, \
            "500 后应继续探测 /api/health 与根路径"

    def test_probe_deepdoc_login_fail(self, monkeypatch):
        """DeepDoc 登录失败（401 无 token 头）→ 不可用 + 登录失败原因"""
        import backend.services.parser_probe as pp
        _install_fake_http(monkeypatch, [
            ("get", "/health", FakeResponse(status_code=200)),
            ("post", "/v1/user/login", FakeResponse(status_code=401)),
        ])
        result = asyncio.run(pp.probe_parsers(_probe_cfg()))
        assert result["deepdoc"]["available"] is False
        assert "登录失败" in result["deepdoc"]["reason"]
        assert result["mineru"]["available"] is True

    def test_probe_deepdoc_connect_error(self, monkeypatch):
        """DeepDoc 连接失败 → 不可用 + 连接失败原因"""
        import backend.services.parser_probe as pp
        _install_fake_http(monkeypatch, [
            ("get", "/health", FakeResponse(status_code=200)),
            ("post", "/v1/user/login",
             httpx.ConnectError("connection refused")),
        ])
        result = asyncio.run(pp.probe_parsers(_probe_cfg()))
        assert result["deepdoc"]["available"] is False
        assert "connection refused" in result["deepdoc"]["reason"]

    def test_probe_deepdoc_no_base_url(self, monkeypatch):
        """DeepDoc 服务地址未配置 → 不可用 + 明确原因（不发起网络请求）"""
        import backend.services.parser_probe as pp
        _install_fake_http(monkeypatch, [])
        cfg = SimpleNamespace(
            mineru=SimpleNamespace(api_url="http://m:8001"),
            deepdoc=SimpleNamespace(base_url="", email="a@b.c", password="x"))
        result = asyncio.run(pp.probe_parsers(cfg))
        assert result["deepdoc"]["available"] is False
        assert "未配置" in result["deepdoc"]["reason"]

    def test_probe_mineru_no_api_url(self, monkeypatch):
        """MinerU 服务地址未配置 → 不可用 + 明确原因"""
        import backend.services.parser_probe as pp
        result = asyncio.run(pp._probe_mineru("", 5.0))
        assert result["available"] is False
        assert "未配置" in result["reason"]

    def test_probe_all_down_plain_still_available(self, monkeypatch):
        """全部不可用不抛异常；plain 恒 available"""
        import backend.services.parser_probe as pp

        async def fake_mineru(api_url, timeout):
            return {"available": False, "reason": "连接超时"}

        async def fake_deepdoc(cfg, timeout):
            return {"available": False, "reason": "登录失败（HTTP 401）"}
        monkeypatch.setattr(pp, "_probe_mineru", fake_mineru)
        monkeypatch.setattr(pp, "_probe_deepdoc", fake_deepdoc)

        result = asyncio.run(pp.probe_parsers(_probe_cfg()))
        assert result["mineru"]["available"] is False
        assert result["deepdoc"]["available"] is False
        assert result["plain"]["available"] is True

    def test_probe_timeouts_passed_through(self, monkeypatch):
        """超时参数透传：弹窗默认 5s/8s，ingestion 内 3s/5s"""
        import backend.services.parser_probe as pp
        captured = {}

        async def fake_mineru(api_url, timeout):
            captured["mineru_timeout"] = timeout
            return {"available": True, "reason": ""}

        async def fake_deepdoc(cfg, timeout):
            captured["deepdoc_timeout"] = timeout
            return {"available": True, "reason": ""}
        monkeypatch.setattr(pp, "_probe_mineru", fake_mineru)
        monkeypatch.setattr(pp, "_probe_deepdoc", fake_deepdoc)

        # 弹窗（状态接口）：默认 ≤8s（并行，总耗时取较大超时）
        asyncio.run(pp.probe_parsers(_probe_cfg()))
        assert captured == {"mineru_timeout": 5.0, "deepdoc_timeout": 8.0}
        # ingestion 任务内：≤5s
        asyncio.run(pp.probe_parsers(
            _probe_cfg(), mineru_timeout=3.0, deepdoc_timeout=5.0))
        assert captured == {"mineru_timeout": 3.0, "deepdoc_timeout": 5.0}

    def test_probe_deepdoc_login_uses_rsa_password(self, monkeypatch):
        """DeepDoc 登录探测发送 RSA 加密密码（与解析同契约）"""
        import backend.services.parser_probe as pp
        fake = _install_fake_http(monkeypatch, [
            ("get", "/health", FakeResponse(status_code=200)),
            ("post", "/v1/user/login",
             FakeResponse(status_code=200,
                          headers={"HTTP_AUTHORIZATION": "t"})),
        ])
        asyncio.run(pp.probe_parsers(_probe_cfg()))
        body = next(c[2]["json"] for c in fake.calls if c[0] == "post")
        assert body["email"] == "test@example.com"
        assert body["password"] != "test-password"  # RSA 密文


# ==================== 状态接口 ====================


class TestParsersStatusEndpoint:
    def test_status_ok_logged_in(self, client, admin_headers):
        """登录 GET /api/kbs/parsers/status → 200，三解析器状态齐全"""
        resp = client.get("/api/kbs/parsers/status", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        for key in ("mineru", "deepdoc", "plain"):
            assert key in data
            assert "available" in data[key]
            assert "reason" in data[key]

    def test_status_unauthorized(self, client):
        """未登录 → 401"""
        resp = client.get("/api/kbs/parsers/status")
        assert resp.status_code == 401

    def test_status_reflects_probe_result(self, client, admin_headers,
                                          monkeypatch):
        """探测结果透传：deepdoc 不可用时接口返回不可用 + 原因"""
        async def fake(cfg=None, **kw):
            return _all_down_status(
                deepdoc_reason="登录失败（HTTP 401）")
        monkeypatch.setattr(
            "backend.routers.knowledge_bases.probe_parsers", fake)
        resp = client.get("/api/kbs/parsers/status", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deepdoc"]["available"] is False
        assert "登录失败" in data["deepdoc"]["reason"]
        assert data["mineru"]["available"] is False
        assert data["plain"]["available"] is True


# ==================== ingestion 自动降级 ====================


def _install_probe(monkeypatch, status):
    """覆盖路由层与 ingestion 层的探测结果为指定状态"""
    async def fake(cfg=None, **kw):
        return status
    for mod in ("backend.routers.documents",
                "backend.services.ingestion_service"):
        monkeypatch.setattr(mod + ".probe_parsers", fake)


def _install_fake_parse(monkeypatch, text="<table>x</table> 说明",
                        method="mineru"):
    """mock ParserClient.parse 捕获 engine/opts，返回固定文本"""
    from backend.services.parser_client import ParserClient
    captured = {}

    async def fake_parse(self, file_path, file_type, engine="auto", **opts):
        captured["engine"] = engine
        captured["opts"] = opts
        return (text, [], method)
    monkeypatch.setattr(ParserClient, "parse", fake_parse)
    return captured


def _upload_pdf(client, kb_id, filename="文档.pdf"):
    return upload_doc(client, kb_id, filename=filename,
                      content=b"%PDF-1.4 mock", mime="application/pdf")


class TestIngestDegrade:
    def test_deepdoc_down_degrades_to_mineru(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """engine=deepdoc + deepdoc 不可用 + mineru 可用 → 降级 mineru：
        响应带 degrade 提示；文档 ingested；parser_config.degrade 记录；
        parser_config 记录实际值（engine=mineru / layout=MinerU）"""
        _install_probe(monkeypatch, {
            "mineru": {"available": True, "reason": ""},
            "deepdoc": {"available": False, "reason": "连接超时"},
            "plain": {"available": True, "reason": ""},
        })
        captured = _install_fake_parse(monkeypatch, method="mineru")

        kb = create_kb(client, headers=admin_headers)
        doc = _upload_pdf(client, kb["id"])
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"parser_engine": "deepdoc"},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["degrade"] == (
            "DeepDoc 服务不可用（连接超时），将自动切换 MinerU/纯文本解析")

        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert final["status"] == "ingested"
        assert captured["engine"] == "mineru"
        assert "已自动切换 MinerU" in (final["parser_config"].get("degrade") or "")
        assert final["parse_method"] == "mineru"
        # parser_config 记录实际值（重跑沿用实际配置，避免再次降级）
        assert final["parser_config"]["parser_engine"] == "mineru"
        assert final["parser_config"]["layout_recognize"] == "MinerU"

    def test_deepdoc_and_mineru_down_degrades_to_plain(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """deepdoc + mineru 都不可用 → 降级 plain，degrade 说明两级都带"""
        _install_probe(monkeypatch, _all_down_status())
        captured = _install_fake_parse(monkeypatch, method="plain")

        kb = create_kb(client, headers=admin_headers)
        doc = _upload_pdf(client, kb["id"])
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"parser_engine": "deepdoc"},
                           headers=admin_headers)
        assert resp.status_code == 200
        assert "将自动切换 MinerU/纯文本解析" in resp.json()["degrade"]

        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert final["status"] == "ingested"
        assert captured["engine"] == "plain"
        assert "已降级纯文本提取" in (final["parser_config"].get("degrade") or "")
        assert "MinerU 也不可用" in (final["parser_config"].get("degrade") or "")
        assert final["parse_method"] == "plain"
        assert final["parser_config"]["parser_engine"] == "plain"
        assert final["parser_config"]["layout_recognize"] == "PlainText"

    def test_mineru_down_degrades_to_plain(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """engine=mineru 不可用 → 降级 plain + degrade 记录"""
        _install_probe(monkeypatch, _all_down_status(
            mineru_reason="连接超时"))
        captured = _install_fake_parse(monkeypatch, method="plain")

        kb = create_kb(client, headers=admin_headers)
        doc = _upload_pdf(client, kb["id"])
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"parser_engine": "mineru"},
                           headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["degrade"] == (
            "MinerU 服务不可用（连接超时），将自动切换纯文本解析")

        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert captured["engine"] == "plain"
        assert "已切换纯文本提取" in (final["parser_config"].get("degrade") or "")
        assert final["parse_method"] == "plain"

    def test_no_degrade_when_available(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """所选解析器可用 → 不降级：响应无 degrade、parser_config 无 degrade 键"""
        _install_probe(monkeypatch, {
            "mineru": {"available": True, "reason": ""},
            "deepdoc": {"available": True, "reason": ""},
            "plain": {"available": True, "reason": ""},
        })
        captured = _install_fake_parse(monkeypatch, method="deepdoc")

        kb = create_kb(client, headers=admin_headers)
        doc = _upload_pdf(client, kb["id"])
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"parser_engine": "deepdoc",
                                 "layout_recognize": "DeepDOC"},
                           headers=admin_headers)
        assert resp.status_code == 200
        assert "degrade" not in resp.json()

        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert captured["engine"] == "deepdoc"
        assert final["parser_config"].get("degrade") is None
        assert final["parse_method"] == "deepdoc"
        assert final["parser_config"]["layout_recognize"] == "DeepDOC"

    def test_txt_no_probe_no_degrade(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """txt/md 直读不受探测影响：engine=deepdoc 也不探测、无 degrade"""
        called = {}

        async def fake(cfg=None, **kw):
            called["probed"] = True
            return _all_down_status()
        monkeypatch.setattr(
            "backend.routers.documents.probe_parsers", fake)
        monkeypatch.setattr(
            "backend.services.ingestion_service.probe_parsers", fake)
        captured = _install_fake_parse(monkeypatch, method="plain")

        kb = create_kb(client, headers=admin_headers)
        doc = upload_doc(client, kb["id"])  # txt（SAMPLE_TEXT）
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"parser_engine": "deepdoc"},
                           headers=admin_headers)
        assert resp.status_code == 200
        assert "degrade" not in resp.json()
        assert "probed" not in called, "txt 直读不应触发解析器探测"

        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert final["status"] == "ingested"
        assert final["parser_config"].get("degrade") is None

    def test_deepdoc_down_docx_degrades_to_mineru(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """docx + deepdoc 不可用 → 降级 mineru（docx 走 MinerU 正常）"""
        _install_probe(monkeypatch, {
            "mineru": {"available": True, "reason": ""},
            "deepdoc": {"available": False, "reason": "登录失败（HTTP 401）"},
            "plain": {"available": True, "reason": ""},
        })
        captured = _install_fake_parse(monkeypatch, method="mineru")

        kb = create_kb(client, headers=admin_headers)
        doc = upload_doc(client, kb["id"], filename="文档.docx",
                         content=b"mock docx", mime="application/docx")
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"parser_engine": "deepdoc"},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert final["status"] == "ingested"
        assert captured["engine"] == "mineru"
        assert "已自动切换 MinerU" in (final["parser_config"].get("degrade") or "")

    def test_degrade_visible_in_list(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """降级说明随文档列表/详情返回（前端列表可展示）"""
        _install_probe(monkeypatch, {
            "mineru": {"available": True, "reason": ""},
            "deepdoc": {"available": False, "reason": "连接超时"},
            "plain": {"available": True, "reason": ""},
        })
        _install_fake_parse(monkeypatch, method="mineru")

        kb = create_kb(client, headers=admin_headers)
        doc = _upload_pdf(client, kb["id"])
        client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                    json={"parser_engine": "deepdoc"},
                    headers=admin_headers)
        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert "已自动切换 MinerU" in (final["parser_config"].get("degrade") or "")

        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        assert docs[0]["parser_config"].get("degrade") == final["parser_config"].get("degrade")
