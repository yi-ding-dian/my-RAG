"""DeepDoc 解析引擎测试：RSA 加密 / RAGFlow 调用链 / 异常路径 / 引擎分支 / 配置段

覆盖：
- _rsa_encrypt_password：生成临时密钥对验证可解密还原（PKCS1v15 + Base64 密码）
- parse_via_deepdoc 完整引导链：登录 → new_token → 建数据集 → 上传 → 触发 →
  轮询 DONE → 取 chunks（按 positions 页序拼接）→ DELETE 清理；调用顺序断言
- 失败路径：轮询 FAILED 抛中文异常（带 progress_msg）；超时抛异常；
  任一步失败清理仍执行（finally）
- parser_client engine=deepdoc 分支：pdf 走 deepdoc_client（mock）、docx 抛异常
- 配置档案：deepdoc 段默认值 / 密码脱敏 / 旧档案补缺
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from backend.config import get_active_config

# ==================== mock httpx.AsyncClient ====================


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self):
        return self._payload


class FakeAsyncClient:
    """按 (method, url 后缀) 匹配返回预置响应；记录全部调用序列

    用 endswith 而非 in 匹配：轮询 URL 以 /documents 结尾、chunks URL 以
    /chunks 结尾，避免子串互相包含导致误匹配。
    """

    def __init__(self, responses):
        self.responses = responses  # [(method, url_suffix, FakeResponse | [..]), ..]
        self.calls = []  # [(method, url, kwargs), ...]
        self._used = {}  # (method, url) -> 已匹配次数（同 URL 多次调用按序消费序列）

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
        if isinstance(item, list):  # 序列响应：同一 URL 多次调用按序消费
            if n >= len(item):
                raise AssertionError(
                    f"响应序列用尽: {method} {url}（第 {n + 1} 次调用）")
            return item[n]
        return item  # 单个响应：可无限复用（并发串行任务重复同一 URL）

    async def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return self._match("post", url)

    async def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return self._match("get", url)

    async def request(self, method, url, **kw):
        # httpx 的 delete 不支持 json body，deepdoc_client 用 request("DELETE")
        self.calls.append((method.lower(), url, kw))
        return self._match(method.lower(), url)


def _install_fake_http(monkeypatch, responses):
    """替换 deepdoc_client 使用的 httpx.AsyncClient 为 FakeAsyncClient"""
    fake = FakeAsyncClient(responses)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda timeout=None: fake)
    return fake


def _default_cfg(**overrides):
    base = {
        "base_url": "http://ragflow.test:9380",
        "email": "test@example.com",
        "password": "test-password",
        "timeout": 300.0,
        "dataset_prefix": "myrag-tmp-",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _ok_chain_responses(doc_id="doc-1", dataset_id="ds-1"):
    """完整成功链路的预置响应（全部 200）"""
    return [
        ("post", "/v1/user/login",
         FakeResponse(payload={"code": 0},
                      headers={"HTTP_AUTHORIZATION": "login-token"})),
        ("post", "/v1/api/new_token",
         FakeResponse(payload={"code": 0, "data": {"token": "ragflow-abc"}})),
        ("post", "/api/v1/datasets",
         FakeResponse(payload={"code": 0, "data": {"id": dataset_id}})),
        ("post", "/api/v1/datasets/ds-1/documents",
         FakeResponse(payload={"code": 0, "data": {"id": doc_id}})),
        ("post", "/api/v1/datasets/ds-1/chunks",
         FakeResponse(payload={"code": 0})),
        ("get", "/api/v1/datasets/ds-1/documents",
         FakeResponse(payload={"code": 0, "data": {
             "total": 1, "docs": [{"id": doc_id, "run": "DONE", "progress": 1.0}]}})),
        ("get", "/api/v1/datasets/ds-1/documents/doc-1/chunks",
         FakeResponse(payload={"code": 0, "data": {
             "total": 2, "chunks": [
                 {"content": "<table>第一章表</table> 内容B",
                  "positions": [{"page_idx": 1, "top": 10}]},
                 {"content": "封面标题",
                  "positions": [{"page_idx": 0, "top": 20}]},
             ]}})),
        ("delete", "/api/v1/datasets",
         FakeResponse(payload={"code": 0})),
    ]


# ==================== RSA 加密 ====================


class TestRsaEncrypt:
    def test_encrypt_decrypt_roundtrip(self, monkeypatch):
        """加密产物可解密还原为 Base64(密码)（临时密钥对，PKCS1v15）"""
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
        import backend.services.deepdoc_client as ddc

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        monkeypatch.setattr(ddc, "_RAGFLOW_RSA_PUBLIC_KEY", public_pem)

        password = "admin@123"
        cipher_b64 = ddc._rsa_encrypt_password(password)
        # 密文应为标准 Base64 且 PKCS1v15 解密后 == Base64(密码)
        cipher = base64.b64decode(cipher_b64)
        plain = key.decrypt(cipher, rsa_padding.PKCS1v15())
        assert plain == base64.b64encode(password.encode("utf-8"))

    def test_encrypt_with_default_public_key_outputs_valid_b64(self):
        """默认（真实 RAGFlow）公钥加密产物：Base64 可解码、2048 位密文长度 256 字节"""
        import backend.services.deepdoc_client as ddc
        cipher_b64 = ddc._rsa_encrypt_password("admin")
        cipher = base64.b64decode(cipher_b64)
        assert len(cipher) == 256, "2048 位 RSA 密文应为 256 字节"

    def test_empty_password_ok(self):
        import backend.services.deepdoc_client as ddc
        assert ddc._rsa_encrypt_password("")


# ==================== 完整引导链 ====================


class TestParseViaDeepdocChain:
    def test_full_chain_order_and_join(self, monkeypatch, tmp_path):
        """完整调用链：login→new_token→datasets→documents→chunks→poll→chunks→delete；
        chunks 按 positions 页序拼接（第 0 页在前），表格 HTML 保留"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        fake = _install_fake_http(monkeypatch, _ok_chain_responses())

        text, images = asyncio.run(ddc.parse_via_deepdoc(pdf, _default_cfg()))

        # 调用顺序断言（URL 后缀逐步断言）
        methods_urls = [(m, u) for m, u, _ in fake.calls]
        expected_order = [
            ("post", "/v1/user/login"),
            ("post", "/v1/api/new_token"),
            ("post", "/api/v1/datasets"),
            ("post", "/documents"),
            ("post", "/chunks"),
            ("get", "/documents"),
            ("get", "/chunks"),
            ("delete", "/datasets"),
        ]
        for i, (m, suffix) in enumerate(expected_order):
            assert i < len(methods_urls), f"第 {i} 步缺失: {m} {suffix}"
            assert methods_urls[i][0] == m and methods_urls[i][1].endswith(suffix), \
                f"第 {i} 步应为 {m} {suffix}，实际 {methods_urls[i]}"
        # 无多余调用
        assert len(methods_urls) == len(expected_order)

        # 数据集命名唯一：prefix + 时间戳 + 随机
        body = fake.calls[2][2]["json"]
        assert body["name"].startswith("myrag-tmp-")
        assert body["chunk_method"] == "naive"
        assert body["parser_config"] == {"layout_recognize": "DeepDOC"}

        # 触发解析 body 含 document_ids
        assert fake.calls[4][2]["json"] == {"document_ids": ["doc-1"]}

        # 清理 DELETE body
        assert fake.calls[7][2]["json"] == {"ids": ["ds-1"]}

        # 拼接结果：页 0 的"封面标题"在前，HTML 表格保留
        assert text.index("封面标题") < text.index("第一章表")
        assert "<table>第一章表</table>" in text
        assert images == []

    def test_login_uses_rsa_encrypted_password(self, monkeypatch, tmp_path):
        """登录请求的 password 是 RSA 密文（与明文不同，Base64 可解码）"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        fake = _install_fake_http(monkeypatch, _ok_chain_responses())

        asyncio.run(ddc.parse_via_deepdoc(pdf, _default_cfg(password="test-password")))
        body = fake.calls[0][2]["json"]
        assert body["email"] == "test@example.com"
        assert body["password"] != "test-password"
        assert base64.b64decode(body["password"])

    def test_new_token_headers_no_bearer_prefix(self, monkeypatch, tmp_path):
        """new_token 的 Authorization 头直接放登录 token 值（无 Bearer 前缀）"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        fake = _install_fake_http(monkeypatch, _ok_chain_responses())

        asyncio.run(ddc.parse_via_deepdoc(pdf, _default_cfg()))
        headers = fake.calls[1][2]["headers"]
        assert headers == {"Authorization": "login-token"}

    def test_chunks_pagination(self, monkeypatch, tmp_path):
        """total 超过单页 page_size 时自动翻页取全"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        responses = [
            ("post", "/v1/user/login",
             FakeResponse(headers={"HTTP_AUTHORIZATION": "t"})),
            ("post", "/v1/api/new_token",
             FakeResponse(payload={"data": {"token": "k"}})),
            ("post", "/api/v1/datasets",
             FakeResponse(payload={"data": {"id": "ds-1"}})),
            ("post", "/documents", FakeResponse(payload={"data": {"id": "d1"}})),
            ("post", "/chunks", FakeResponse()),
            ("get", "/documents",
             FakeResponse(payload={"data": {"docs": [{"id": "d1", "run": "DONE"}]}})),
            # 第 1 页 1 条（total=2）→ 翻页（同一 URL 按序消费序列响应）
            ("get", "/chunks", [
                FakeResponse(payload={"data": {"total": 2, "chunks": [
                    {"content": "A", "positions": [{"page_idx": 0}]}]}}),
                FakeResponse(payload={"data": {"total": 2, "chunks": [
                    {"content": "B", "positions": [{"page_idx": 1}]}]}}),
            ]),
            ("delete", "/datasets", FakeResponse()),
        ]
        fake = _install_fake_http(monkeypatch, responses)
        text, _ = asyncio.run(ddc.parse_via_deepdoc(pdf, _default_cfg()))
        assert text == "A\n\nB"
        get_calls = [c for c in fake.calls if c[0] == "get" and "chunks" in c[1]]
        assert len(get_calls) == 2, "应翻页两次"


# ==================== 失败路径 ====================


class TestDeepdocFailures:
    def test_poll_failed_raises_with_progress_msg(self, monkeypatch, tmp_path):
        """轮询 FAILED → 抛中文异常且带 progress_msg"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        responses = _ok_chain_responses()
        # 替换轮询响应为 FAILED
        responses[5] = (
            "get", "/api/v1/datasets/ds-1/documents",
            FakeResponse(payload={"data": {"docs": [
                {"id": "doc-1", "run": "FAILED", "progress": 0.3,
                 "progress_msg": "解析失败: 表格识别错误"}]}}))
        fake = _install_fake_http(monkeypatch, responses)

        with pytest.raises(RuntimeError) as exc:
            asyncio.run(ddc.parse_via_deepdoc(pdf, _default_cfg()))
        msg = str(exc.value)
        assert "DeepDoc 解析失败" in msg
        assert "表格识别错误" in msg
        # 清理仍执行
        assert any(m == "delete" for m, _, _ in fake.calls)

    def test_timeout_raises(self, monkeypatch, tmp_path):
        """轮询永不 DONE → 超时异常（轮询间隔缩短加速）"""
        import backend.services.deepdoc_client as ddc
        monkeypatch.setattr(ddc, "_POLL_INTERVAL", 0.01)
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        responses = _ok_chain_responses()
        responses[5] = (
            "get", "/api/v1/datasets/ds-1/documents",
            FakeResponse(payload={"data": {"docs": [
                {"id": "doc-1", "run": "RUNNING", "progress": 0.1,
                 "progress_msg": "解析中"}]}}))
        fake = _install_fake_http(monkeypatch, responses)

        with pytest.raises(RuntimeError) as exc:
            asyncio.run(ddc.parse_via_deepdoc(pdf, _default_cfg(timeout=0.15)))
        assert "超时" in str(exc.value)
        assert any(m == "delete" for m, _, _ in fake.calls)

    def test_cleanup_on_mid_chain_failure(self, monkeypatch, tmp_path):
        """触发解析失败（HTTP 400）→ 异常含步骤名，且 DELETE 清理仍执行（finally）"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        responses = _ok_chain_responses()
        responses[4] = (
            "post", "/api/v1/datasets/ds-1/chunks",
            FakeResponse(status_code=400,
                         payload={"code": 102, "message": "解析中，禁止重复触发"}))
        fake = _install_fake_http(monkeypatch, responses)

        with pytest.raises(RuntimeError) as exc:
            asyncio.run(ddc.parse_via_deepdoc(pdf, _default_cfg()))
        assert "触发解析失败" in str(exc.value)
        assert "禁止重复触发" in str(exc.value)
        # 清理必须执行（最后一次调用是 delete，且数据集已建）
        assert fake.calls[-1][0] == "delete"
        assert fake.calls[-1][2]["json"] == {"ids": ["ds-1"]}

    def test_login_fail_no_dataset_created(self, monkeypatch, tmp_path):
        """登录失败：未建数据集，无清理调用"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        responses = [
            ("post", "/v1/user/login",
             FakeResponse(status_code=400, payload={"message": "密码错误"})),
        ]
        fake = _install_fake_http(monkeypatch, responses)

        with pytest.raises(RuntimeError) as exc:
            asyncio.run(ddc.parse_via_deepdoc(pdf, _default_cfg()))
        assert "登录失败" in str(exc.value)
        assert len(fake.calls) == 1 and fake.calls[0][0] == "post"
        assert not any(m == "delete" for m, _, _ in fake.calls)

    def test_no_base_url_raises(self, tmp_path):
        """服务地址未配置 → 明确异常"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(ddc.parse_via_deepdoc(
                pdf, _default_cfg(base_url="")))
        assert "未配置" in str(exc.value)

    def test_semaphore_serializes_concurrent(self, monkeypatch, tmp_path):
        """并发两个解析任务：模块级信号量串行化（总耗时 ≈ 单任务 × 2）"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")
        monkeypatch.setattr(ddc, "_POLL_INTERVAL", 0.02)
        # 轮询第 1 次 RUNNING、第 2 次 DONE → 每个任务约 0.02s
        responses = [
            ("post", "/v1/user/login", FakeResponse(headers={"HTTP_AUTHORIZATION": "t"})),
            ("post", "/v1/api/new_token", FakeResponse(payload={"data": {"token": "k"}})),
            ("post", "/api/v1/datasets", FakeResponse(payload={"data": {"id": "ds-1"}})),
            ("post", "/documents", FakeResponse(payload={"data": {"id": "d1"}})),
            ("post", "/chunks", FakeResponse()),
            # 轮询序列：任务 A 两次（RUNNING→DONE）+ 任务 B 两次（RUNNING→DONE）
            ("get", "/documents", [
                FakeResponse(payload={"data": {"docs": [
                    {"id": "d1", "run": "RUNNING", "progress": 0.5}]}}),
                FakeResponse(payload={"data": {"docs": [
                    {"id": "d1", "run": "DONE"}]}}),
                FakeResponse(payload={"data": {"docs": [
                    {"id": "d1", "run": "RUNNING", "progress": 0.5}]}}),
                FakeResponse(payload={"data": {"docs": [
                    {"id": "d1", "run": "DONE"}]}}),
            ]),
            ("get", "/chunks",
             FakeResponse(payload={"data": {"total": 1,
                                            "chunks": [{"content": "X"}]}})),
            ("delete", "/datasets", FakeResponse()),
        ]
        _install_fake_http(monkeypatch, responses)

        async def _run_two():
            # 并发两个任务：模块级信号量（=1）串行化，都成功且不互相干扰
            return await asyncio.gather(
                ddc.parse_via_deepdoc(pdf, _default_cfg()),
                ddc.parse_via_deepdoc(pdf, _default_cfg()))

        results = asyncio.run(_run_two())
        assert all(r[0] == "X" for r in results)


# ==================== parser_client engine=deepdoc 分支 ====================


class TestParserClientDeepdoc:
    async def _parse(self, path, file_type, engine="deepdoc"):
        from backend.services.parser_client import get_parser_client
        return await get_parser_client().parse(path, file_type, engine=engine)

    def test_pdf_engine_deepdoc(self, monkeypatch, tmp_path):
        """pdf + engine=deepdoc：走 deepdoc_client，返回 (text, [], "deepdoc")"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")

        async def fake_parse_via_deepdoc(file_path, cfg):
            assert file_path == pdf
            return ("<table>接地线规格</table> 说明", [])
        monkeypatch.setattr(ddc, "parse_via_deepdoc", fake_parse_via_deepdoc)

        text, images, method = asyncio.run(self._parse(pdf, "pdf"))
        assert method == "deepdoc"
        assert text == "<table>接地线规格</table> 说明"
        assert images == []

    def test_docx_engine_deepdoc_raises(self, tmp_path):
        """docx + engine=deepdoc：不支持 → RuntimeError（提示改用其他引擎）"""
        docx = tmp_path / "demo.docx"
        docx.write_bytes(b"mock")
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(self._parse(docx, "docx"))
        assert "仅支持 PDF" in str(exc.value)

    def test_pdf_engine_deepdoc_failure_promotes_error(self, monkeypatch,
                                                       tmp_path):
        """deepdoc_client 失败 → 异常含服务地址提示（上层标记 failed 用）"""
        import backend.services.deepdoc_client as ddc
        pdf = tmp_path / "demo.pdf"
        pdf.write_bytes(b"%PDF-1.4 mock")

        async def fake_fail(file_path, cfg):
            raise RuntimeError("登录 ragflow-server 失败: 连接拒绝")
        monkeypatch.setattr(ddc, "parse_via_deepdoc", fake_fail)

        with pytest.raises(RuntimeError) as exc:
            asyncio.run(self._parse(pdf, "pdf"))
        msg = str(exc.value)
        assert "DeepDoc 解析失败" in msg
        assert "127.0.0.1:59997" in msg or "9380" in msg  # 活跃配置服务地址

    def test_txt_ignores_deepdoc_engine(self, tmp_path):
        """txt/md 直读不受引擎影响（deepdoc 也只对 pdf/docx 生效）"""
        txt = tmp_path / "demo.txt"
        txt.write_text("纯文本内容", encoding="utf-8")
        text, images, method = asyncio.run(self._parse(txt, "txt"))
        assert text == "纯文本内容"
        assert method == "plain"


# ==================== ingestion 引擎选择（layout_recognize 联动） ====================


class TestIngestEngineSelection:
    """ingestion：layout_recognize=DeepDOC（或显式 deepdoc 引擎）→ engine=deepdoc"""

    def test_ingest_layout_deepdoc_forces_deepdoc_engine(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """layout_recognize=DeepDOC + engine=auto → 后端自动走 DeepDoc 引擎"""
        from backend.services.parser_client import ParserClient
        from conftest import create_kb, upload_doc, wait_for_status

        captured = {}

        async def fake_parse(self, file_path, file_type, engine="auto", **opts):
            captured["engine"] = engine
            captured["opts"] = opts
            return ("<table>接地线规格</table> 说明", [], "deepdoc")
        monkeypatch.setattr(ParserClient, "parse", fake_parse)

        kb = create_kb(client, headers=admin_headers)
        doc = upload_doc(client, kb["id"], filename="文档.pdf",
                         content=b"%PDF-1.4 mock", mime="application/pdf")
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"layout_recognize": "DeepDOC"},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert final["status"] == "ingested"
        assert captured["engine"] == "deepdoc", \
            "layout_recognize=DeepDOC 且 engine=auto 应走 DeepDoc 引擎"
        assert captured["opts"] == {}, "DeepDoc 不传 MinerU 解析参数"
        assert final["parse_method"] == "deepdoc"
        assert final["parser_config"]["layout_recognize"] == "DeepDOC"

    def test_ingest_explicit_deepdoc_engine(self, client, mock_embedding,
                                            admin_headers, monkeypatch):
        """parser_engine=deepdoc 显式指定 → engine=deepdoc 透传"""
        from backend.services.parser_client import ParserClient
        from conftest import create_kb, upload_doc, wait_for_status

        captured = {}

        async def fake_parse(self, file_path, file_type, engine="auto", **opts):
            captured["engine"] = engine
            return ("<table>x</table> 文本", [], "deepdoc")
        monkeypatch.setattr(ParserClient, "parse", fake_parse)

        kb = create_kb(client, headers=admin_headers)
        doc = upload_doc(client, kb["id"], filename="文档.pdf",
                         content=b"%PDF-1.4 mock", mime="application/pdf")
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"parser_engine": "deepdoc"},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert final["status"] == "ingested"
        assert captured["engine"] == "deepdoc"
        assert final["parse_method"] == "deepdoc"

    def test_ingest_layout_mineru_keeps_auto(self, client, mock_embedding,
                                             admin_headers, monkeypatch):
        """layout_recognize=MinerU（默认）+ engine=auto → engine=auto（现有逻辑不变）"""
        from backend.services.parser_client import ParserClient
        from conftest import create_kb, upload_doc, wait_for_status

        captured = {}

        async def fake_parse(self, file_path, file_type, engine="auto", **opts):
            captured["engine"] = engine
            return ("正文", [], "mineru")
        monkeypatch.setattr(ParserClient, "parse", fake_parse)

        kb = create_kb(client, headers=admin_headers)
        doc = upload_doc(client, kb["id"], filename="文档.pdf",
                         content=b"%PDF-1.4 mock", mime="application/pdf")
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers)
        assert final["status"] == "ingested"
        assert captured["engine"] == "auto"
        assert final["parse_method"] == "mineru"


# ==================== 配置档案：deepdoc 段 ====================


class TestDeepdocConfigSection:
    def test_profile_defaults_include_deepdoc(self, client, admin_headers):
        """创建档案缺省 deepdoc 段 → .env 出厂值补齐（conftest 指向本机不可达端口）"""
        resp = client.post("/api/settings/profiles",
                           json={"name": "DeepDoc 档案"},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        deepdoc = resp.json()["deepdoc"]
        assert deepdoc["base_url"] == "http://127.0.0.1:59997"
        assert deepdoc["email"] == "test@example.com"
        assert deepdoc["timeout"] == 300.0
        assert deepdoc["dataset_prefix"] == "myrag-tmp-"
        # 密码脱敏
        assert "****" in deepdoc["password"] and deepdoc["password"] != "test-password"

    def test_profile_password_masked_and_kept(self, client, admin_headers):
        """deepdoc.password 保存脱敏、脱敏回传保留原值、激活后全局配置可见原值"""
        resp = client.post("/api/settings/profiles", json={
            "name": "DeepDoc 档案",
            "deepdoc": {"base_url": "http://good:9380", "email": "a@b.c",
                        "password": "real-password", "timeout": 120,
                        "dataset_prefix": "tmp-"},
        }, headers=admin_headers)
        assert resp.status_code == 200
        profile = resp.json()
        assert profile["deepdoc"]["password"] != "real-password"
        assert "****" in profile["deepdoc"]["password"]

        # 脱敏值回传不覆盖原值
        resp = client.put(f"/api/settings/profiles/{profile['id']}", json={
            "deepdoc": {"base_url": "http://good2:9380",
                        "password": profile["deepdoc"]["password"]},
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["deepdoc"]["base_url"] == "http://good2:9380"

        # 激活后 get_active_config 可见原值
        resp = client.post(f"/api/settings/profiles/{profile['id']}/activate",
                           headers=admin_headers)
        assert resp.status_code == 200
        cfg = get_active_config()
        assert cfg.deepdoc.password == "real-password"
        assert cfg.deepdoc.base_url == "http://good2:9380"
        assert cfg.deepdoc.timeout == 120
        assert cfg.deepdoc.dataset_prefix == "tmp-"

    def test_old_profile_backfill_deepdoc(self):
        """旧档案缺 deepdoc 段 → _coerce 自动补默认值（兼容历史数据）"""
        from backend.services.settings_service import SettingsService
        old = {
            "id": "old1", "name": "旧档案",
            "llm": {"base_url": "http://x", "api_key": "k", "model": "m",
                    "temperature": 0.3, "max_tokens": 4096},
            "embedding": {"base_url": "http://x", "api_key": "k", "model": "e",
                          "dimension": 1024},
            "mineru": {"url": "http://x", "timeout": 300},
            "retrieval": {"top_k": 5},
            "chunking": {"chunk_size": 800, "overlap": 100},
            "chat": {"history_rounds": 8},
            "mysql": {}, "minio": {},
        }
        coerced = SettingsService._coerce(old)
        deepdoc = coerced["deepdoc"]
        assert deepdoc["base_url"] == "http://127.0.0.1:59997"
        assert deepdoc["timeout"] == 300.0
        assert deepdoc["dataset_prefix"] == "myrag-tmp-"

    def test_profile_update_deepdoc_timeout_float(self, client, admin_headers):
        """deepdoc.timeout 归一化为 float（字符串提交也转数字）"""
        resp = client.post("/api/settings/profiles", json={
            "name": "DeepDoc 档案",
            "deepdoc": {"base_url": "http://x:9380", "timeout": "60"},
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["deepdoc"]["timeout"] == 60.0


# ==================== 连接测试：deepdoc 登录探测 ====================


class TestDeepdocConnectionTest:
    def test_deepdoc_test_ok(self, client, admin_headers, monkeypatch):
        """连接测试 deepdoc 段：登录 200 + HTTP_AUTHORIZATION 头 → ok"""
        profile = client.post("/api/settings/profiles", json={
            "name": "连接测试",
            "deepdoc": {"base_url": "http://good:9380", "email": "a@b.c",
                        "password": "pw"},
        }, headers=admin_headers).json()
        monkeypatch.setattr(
            httpx, "post",
            lambda url, **kw: SimpleNamespace(
                status_code=200,
                headers={"HTTP_AUTHORIZATION": "mock-token"}, text=""))
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deepdoc"]["ok"] is True
        assert "连接成功" in data["deepdoc"]["message"]

    def test_deepdoc_test_fail(self, client, admin_headers, monkeypatch):
        """登录失败（401 无 token 头）→ ok=False，接口仍 200"""
        profile = client.post("/api/settings/profiles", json={
            "name": "连接测试",
            "deepdoc": {"base_url": "http://bad:9380", "email": "a@b.c",
                        "password": "pw"},
        }, headers=admin_headers).json()
        monkeypatch.setattr(
            httpx, "post",
            lambda url, **kw: SimpleNamespace(
                status_code=401, headers={}, text="unauthorized"))
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deepdoc"]["ok"] is False
        assert "登录失败" in data["deepdoc"]["message"]

    def test_deepdoc_test_no_base_url(self, monkeypatch):
        """服务地址未配置 → ok=False 且 message 明确（直接测连接测试方法）"""
        from backend.services.settings_service import SettingsService
        svc = SettingsService()
        # 空地址不发起任何网络请求，直接返回未配置
        result = asyncio.run(svc._test_deepdoc(
            {"base_url": "", "email": "a@b.c", "password": "pw"}))
        assert result["ok"] is False
        assert "未配置" in result["message"]
