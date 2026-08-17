"""MinerU 解析后端（backend）参数测试

覆盖：
1. IngestRequest.backend 校验：非法值同步 400；None/auto 允许且不持久化；
   hybrid-auto-engine/pipeline 合法并持久化到 parser_config（重跑沿用）
2. ingestion 透传：mock parser 断言 parse_opts.backend（仅显式选择时透传，
   auto/None 不透传）
3. parser_client 表单构造：backend 出现在 /file_parse form data；
   None/缺省时无该字段
4. 显式 backend="auto" 可重置上次持久化的 backend（新配置覆盖旧值）

契约：backend ∈ {auto, hybrid-auto-engine, pipeline}；None=不传跟随服务端默认；
auto 与 None 语义等价（不持久化、不透传）。
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import create_kb, upload_doc, wait_for_status
from backend.services.parser_client import _build_mineru_form_data

# 解析后端合法值（与 backend/services/ingestion_service.py _VALID_MINERU_BACKENDS 契约一致）
VALID_BACKENDS = ("hybrid-auto-engine", "pipeline")

PDF_BYTES = b"%PDF-1.4 fake"


def _ingest(client, kb_id, doc_id, body=None, headers=None):
    """触发入库（body 可选），返回原始响应"""
    return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                       json=body, headers=headers)


def _get_doc(client, kb_id, doc_id, headers=None):
    return client.get(f"/api/kbs/{kb_id}/documents/{doc_id}",
                      headers=headers).json()


class _FakeParser:
    """伪 parser：记录 parse_opts，返回构造的 (text, images, method)

    delay: 模拟解析耗时（取消测试用——任务在解析中时用户点取消）
    """

    def __init__(self, text: str, images: list, delay: float = 0.0):
        self.text = text
        self.images = images
        self.delay = delay
        self.last_opts: dict | None = None

    async def parse(self, path, file_type, engine="auto", **opts):
        self.last_opts = opts
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.text, self.images, "mineru"


def _install_fake_parser(monkeypatch, text: str, images: list,
                         delay: float = 0.0) -> _FakeParser:
    """替换 get_parser_client（源模块与消费模块两处引用复制，同 test_parser_images_chain）"""
    fake = _FakeParser(text, images, delay=delay)
    monkeypatch.setattr("backend.services.parser_client.get_parser_client",
                        lambda: fake)
    monkeypatch.setattr("backend.services.ingestion_service.get_parser_client",
                        lambda: fake)
    return fake


class TestIngestBackendValidation:
    """backend 参数校验：非法 400 / None 与 auto 不持久化 / 合法值持久化"""

    def test_backend_invalid_400(self, client, mock_embedding, admin_headers):
        """backend 非法值 → 同步 400，任务不启动（状态不变）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"backend": "magic"}, headers=admin_headers)
        assert resp.status_code == 400
        assert "backend" in resp.text
        assert "auto" in resp.text and "pipeline" in resp.text  # 错误提示列合法值
        assert _get_doc(client, kb["id"], doc["id"], admin_headers)[
            "status"] == "uploaded"

    def test_backend_none_default_ok(self, client, mock_embedding, admin_headers):
        """不传 backend（None）：成功，parser_config 不出现 backend 键（跟随服务端默认）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "naive"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert "backend" not in final["parser_config"]

    def test_backend_auto_not_persisted(self, client, mock_embedding, admin_headers):
        """backend="auto"：合法（=跟随服务端默认），不持久化不透传"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"backend": "auto"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert "backend" not in final["parser_config"]

    @pytest.mark.parametrize("backend", VALID_BACKENDS)
    def test_backend_valid_persisted(self, client, mock_embedding, admin_headers,
                                     backend):
        """hybrid-auto-engine/pipeline：合法，持久化到 parser_config"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"backend": backend}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_config"]["backend"] == backend

    def test_backend_reingest_keeps(self, client, mock_embedding, admin_headers):
        """重跑不传 backend：沿用上次持久化的 backend（不回退默认）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"backend": "pipeline"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        first = wait_for_status(client, kb["id"], doc["id"])
        assert first["parser_config"]["backend"] == "pipeline"
        # 第二次无 body 重跑 → 沿用 pipeline
        resp = _ingest(client, kb["id"], doc["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        second = wait_for_status(client, kb["id"], doc["id"])
        assert second["status"] == "ingested"
        assert second["parser_config"]["backend"] == "pipeline"

    def test_backend_reset_via_auto(self, client, mock_embedding, admin_headers):
        """显式 backend="auto" 重置：新配置覆盖上次持久化的 backend（不再沿用）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"backend": "hybrid-auto-engine"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        first = wait_for_status(client, kb["id"], doc["id"])
        assert first["parser_config"]["backend"] == "hybrid-auto-engine"
        # 显式传 auto → 重置为跟随服务端默认（parser_config 无 backend 键）
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"backend": "auto"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        second = wait_for_status(client, kb["id"], doc["id"])
        assert second["status"] == "ingested"
        assert "backend" not in second["parser_config"]


class TestIngestBackendPassthrough:
    """ingestion 透传：parse_opts.backend 仅显式选择时出现（pdf + mock parser）"""

    def _run(self, client, monkeypatch, ingest_body, headers) -> _FakeParser:
        """上传 PDF 并触发入库（mock parser 拦截），返回伪 parser 记录"""
        fake = _install_fake_parser(monkeypatch, "# 标题\n\n内容段落", [])
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="测试文档.pdf",
                         content=PDF_BYTES, mime="application/pdf")
        resp = _ingest(client, kb["id"], doc["id"],
                       body=ingest_body, headers=headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        return fake

    def test_backend_hybrid_passed(self, client, monkeypatch, mock_embedding,
                                   admin_headers):
        """backend=hybrid-auto-engine → parse_opts.backend 透传"""
        fake = self._run(client, monkeypatch,
                         {"backend": "hybrid-auto-engine"}, admin_headers)
        assert fake.last_opts is not None
        assert fake.last_opts["backend"] == "hybrid-auto-engine"

    def test_backend_pipeline_passed(self, client, monkeypatch, mock_embedding,
                                     admin_headers):
        """backend=pipeline → parse_opts.backend 透传"""
        fake = self._run(client, monkeypatch, {"backend": "pipeline"},
                         admin_headers)
        assert fake.last_opts is not None
        assert fake.last_opts["backend"] == "pipeline"

    def test_backend_auto_not_passed(self, client, monkeypatch, mock_embedding,
                                     admin_headers):
        """backend=auto → parse_opts 无 backend（跟随服务端默认）"""
        fake = self._run(client, monkeypatch, {"backend": "auto"}, admin_headers)
        assert fake.last_opts is not None
        assert "backend" not in fake.last_opts

    def test_backend_unset_not_passed(self, client, monkeypatch, mock_embedding,
                                      admin_headers):
        """不传 backend → parse_opts 无 backend"""
        fake = self._run(client, monkeypatch, {"method": "naive"}, admin_headers)
        assert fake.last_opts is not None
        assert "backend" not in fake.last_opts


class TestParserBackendFormData:
    """parser_client 表单构造：backend 出现在 form data / None 时无该字段"""

    def test_form_data_builder_backend_present(self):
        """_build_mineru_form_data：backend 非空时同名透传"""
        data = _build_mineru_form_data({"backend": "hybrid-auto-engine"})
        assert data["backend"] == "hybrid-auto-engine"
        data = _build_mineru_form_data({"backend": "pipeline"})
        assert data["backend"] == "pipeline"

    def test_form_data_builder_backend_absent(self):
        """_build_mineru_form_data：backend None/缺省时 form 无该字段"""
        assert "backend" not in _build_mineru_form_data({"backend": None})
        assert "backend" not in _build_mineru_form_data({})

    def test_request_top_level_backend(self, monkeypatch, tmp_path):
        """_parse_via_mineru 全链路：backend 以顶层 form 字段发送"""
        from backend.services.parser_client import ParserClient

        class _FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": {"a": {"md_content": "ok"}}}

        class _FakeAsyncClient:
            def __init__(self):
                self.post_data = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kwargs):
                self.post_data = kwargs.get("data")
                return _FakeResp()

        fake = _FakeAsyncClient()
        monkeypatch.setattr("backend.services.parser_client.httpx.AsyncClient",
                            lambda timeout=None: fake)
        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(PDF_BYTES)
        import asyncio
        parser = ParserClient()
        asyncio.run(parser._parse_via_mineru(
            "http://mineru:8001", pdf, 30.0, backend="hybrid-auto-engine"))
        assert fake.post_data["backend"] == "hybrid-auto-engine"


class TestCancelIngestion:
    """取消解析：仅 parsing 可取消；取消后任务停止、文档回 failed
    （error="用户取消解析"），可重新发起解析"""

    def _wait_settled(self, client, kb_id, doc_id, timeout=20.0, headers=None):
        """等待任务结束（ingested/failed 任一终态），返回最终文档"""
        import time
        deadline = time.monotonic() + timeout
        while True:
            doc = _get_doc(client, kb_id, doc_id, headers)
            if doc["status"] in ("ingested", "failed"):
                return doc
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"等待任务结束超时，当前状态: {doc['status']} "
                    f"error={doc.get('error')}")
            time.sleep(0.2)

    def test_cancel_not_parsing_409(self, client, admin_headers):
        """非解析中（uploaded）取消 → 409，提示当前状态"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest/cancel",
            headers=admin_headers)
        assert resp.status_code == 409
        assert "当前不在解析中" in resp.json()["detail"]
        # 状态未被改动
        assert _get_doc(client, kb["id"], doc["id"],
                        admin_headers)["status"] == "uploaded"

    def test_cancel_parsing_marks_failed_and_stops(self, client, monkeypatch,
                                                   mock_embedding,
                                                   admin_headers):
        """解析中取消：任务检查点命中 → 状态回 failed（error="用户取消解析"），
        未写入 ingested（解析仍在外部解析器执行，无法打断但结果不落库）"""
        # 伪 parser 延迟 3s 模拟解析中（给取消留出窗口）
        fake = _install_fake_parser(monkeypatch, "# 标题\n\n内容段落", [],
                                    delay=3.0)
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"], {"method": "naive"},
                       admin_headers)
        assert resp.status_code == 200
        # 等进入 parsing 后再取消（取消仅对解析中生效）
        import time
        deadline = time.monotonic() + 10
        while True:
            d = _get_doc(client, kb["id"], doc["id"], admin_headers)
            if d["status"] == "parsing":
                break
            if time.monotonic() > deadline:
                raise AssertionError("未进入 parsing 状态")
            time.sleep(0.1)
        resp2 = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest/cancel",
            headers=admin_headers)
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["doc_id"] == doc["id"]
        final = self._wait_settled(client, kb["id"], doc["id"],
                                   headers=admin_headers)
        assert final["status"] == "failed"
        assert final["error"] == "用户取消解析"
        # 重新发起解析（failed -> parsing 合法）可再次入库：这次不取消 → ingested
        fake.delay = 0.0
        resp3 = _ingest(client, kb["id"], doc["id"], {"method": "naive"},
                        admin_headers)
        assert resp3.status_code == 200
        final2 = wait_for_status(client, kb["id"], doc["id"], status="ingested",
                                 timeout=20.0, headers=admin_headers)
        assert final2["status"] == "ingested"

    def test_cancel_after_ingested_409(self, client, monkeypatch,
                                       mock_embedding, admin_headers):
        """已入库后取消 → 409（解析已结束，不可取消）"""
        _install_fake_parser(monkeypatch, "# 标题\n\n内容段落", [])
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        assert _ingest(client, kb["id"], doc["id"], {"method": "naive"},
                       admin_headers).status_code == 200
        final = wait_for_status(client, kb["id"], doc["id"], status="ingested",
                                timeout=20.0, headers=admin_headers)
        assert final["status"] == "ingested"
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest/cancel",
            headers=admin_headers)
        assert resp.status_code == 409
        assert "当前不在解析中" in resp.json()["detail"]
