"""文档重命名 + URL 网页导入 API 测试

覆盖：
- 重命名：成功（original_name 更新、自动补扩展名）、扩展名不一致 400、
  重名 400、空/超长 400、非管理员 403、文档/知识库不存在 404；
  内部存储名（name）与 uploads 文件不变（改名只影响展示名）
- URL 导入：monkeypatch httpx 返回假 HTML（含 title/script/正文）→
  文档创建成功、file_type=url、内容落盘 uploads/ 与 storage/、标题做文件名、
  script 内容不进正文；标题缺失 fallback <h1>/域名；重名自动加序号；
  抓取 404/超时 → 400 中文错误；非 http 400；超 5MB 拒绝；非管理员 403

全部离线：httpx 用假客户端注入（不连真实网络）。
"""
from __future__ import annotations

import httpx
import pytest

from conftest import create_kb, upload_doc

from backend.config import STORAGE_DIR, UPLOAD_DIR
from backend.services import web_importer

SAMPLE_HTML = """<!DOCTYPE html>
<html><head><title>测试网页标题</title>
<script>var fake = '这段JS不应出现在正文';</script>
<style>.cls { color: red }</style></head>
<body>
<h1>页面大标题</h1>
<p>这是正文第一段，包含 <b>加粗</b> 文字。</p>
<p>第二段 &amp; 符号反转义。</p>
</body></html>"""


# ---------------- 假 httpx（模拟 GET 抓取） ----------------

class _FakeResp:
    """模拟 httpx 流式响应：status_code + 分块 body"""

    def __init__(self, status_code: int, body: bytes = b""):
        self.status_code = status_code
        self.headers = {}          # P0-2: SSRF 逐跳校验读取
        self.is_redirect = False   # P0-2: 手动跟随重定向标记
        self._chunks = [body] if body else []

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeStream:
    def __init__(self, resp_factory):
        self._resp_factory = resp_factory

    async def __aenter__(self):
        return self._resp_factory()

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    """模拟 httpx.AsyncClient：stream(method, url) → _FakeStream

    handler(url) 返回 _FakeResp 或抛异常（如 httpx.TimeoutException）。
    """

    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url):
        return _FakeStream(lambda: self._handler(url))


def _monkeypatch_httpx(monkeypatch, handler):
    """替换 web_importer 模块中的 httpx.AsyncClient 为假客户端

    P0-2 起抓取链路先做 SSRF 校验（DNS 解析），一并 mock 为公网 IP，
    保证测试离线稳定（不依赖真实 DNS/网络）。
    """
    monkeypatch.setattr(
        web_importer.httpx, "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(handler))
    monkeypatch.setattr(
        web_importer.socket, "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])


def _html_resp(html: str = SAMPLE_HTML, status_code: int = 200) -> _FakeResp:
    return _FakeResp(status_code, html.encode("utf-8"))


def _import_url(client, kb_id, url, headers=None):
    from conftest import _resolve_headers
    return client.post(f"/api/kbs/{kb_id}/documents/from-url",
                       json={"url": url},
                       headers=_resolve_headers(client, headers))


def _rename(client, kb_id, doc_id, name, headers=None):
    from conftest import _resolve_headers
    return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/rename",
                       json={"name": name},
                       headers=_resolve_headers(client, headers))


# ==================== 重命名 ====================

class TestRenameDocument:

    def test_rename_ok_auto_append_ext(self, client, admin_headers):
        """重命名成功：无扩展名自动补 .txt，内部名与上传文件不变，列表/详情/落盘同步"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="测试文档.txt")

        resp = _rename(client, kb["id"], doc["id"], "新名字", admin_headers)
        assert resp.status_code == 200, resp.text
        renamed = resp.json()
        assert renamed["original_name"] == "新名字.txt"
        assert renamed["file_type"] == "txt"
        # 内部存储名与上传文件不变（改名只影响展示名）
        assert renamed["name"] == doc["name"]
        assert (UPLOAD_DIR / renamed["name"]).exists()

        # 列表与详情同步
        listed = next(d for d in client.get(
            f"/api/kbs/{kb['id']}/documents", headers=admin_headers).json()
            if d["id"] == doc["id"])
        assert listed["original_name"] == "新名字.txt"
        detail = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                            headers=admin_headers).json()
        assert detail["original_name"] == "新名字.txt"
        # 元数据落盘（data/documents/{id}.json）
        import json
        from backend.config import DOCUMENTS_DIR
        on_disk = json.loads(
            (DOCUMENTS_DIR / f"{doc['id']}.json").read_text(encoding="utf-8"))
        assert on_disk["original_name"] == "新名字.txt"

    def test_rename_ok_same_ext(self, client, admin_headers):
        """带相同扩展名直接使用"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="旧名.txt")
        resp = _rename(client, kb["id"], doc["id"], "新名.txt", admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["original_name"] == "新名.txt"

    def test_rename_wrong_ext_400(self, client, admin_headers):
        """扩展名不一致（.txt → .md）→ 400 明确报错"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="旧名.txt")
        resp = _rename(client, kb["id"], doc["id"], "新名.md", admin_headers)
        assert resp.status_code == 400
        assert "扩展名" in resp.json()["detail"]
        # 原文件名未被修改
        detail = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                            headers=admin_headers).json()
        assert detail["original_name"] == "旧名.txt"

    def test_rename_duplicate_400(self, client, admin_headers):
        """同知识库内与其他文档重名 → 400"""
        kb = create_kb(client)
        doc_a = upload_doc(client, kb["id"], filename="文档A.txt")
        upload_doc(client, kb["id"], filename="文档B.txt")
        resp = _rename(client, kb["id"], doc_a["id"], "文档B",
                       admin_headers)  # 自动补 .txt 后与文档B重名
        assert resp.status_code == 400
        assert "已存在同名文档" in resp.json()["detail"]

    def test_rename_blank_400(self, client, admin_headers):
        """空名/全空格 → 400"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        assert _rename(client, kb["id"], doc["id"], "   ",
                       admin_headers).status_code == 400

    def test_rename_too_long_400(self, client, admin_headers):
        """超 255 字符 → 400"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _rename(client, kb["id"], doc["id"], "长" * 256, admin_headers)
        assert resp.status_code == 400
        assert "255" in resp.json()["detail"]

    def test_rename_user_forbidden_403(self, client, admin_headers,
                                       user_headers):
        """普通用户重命名 → 403（can_manage_kb）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _rename(client, kb["id"], doc["id"], "新名", user_headers)
        assert resp.status_code == 403

    def test_rename_doc_missing_404(self, client, admin_headers):
        """文档不存在 → 404"""
        kb = create_kb(client)
        assert _rename(client, kb["id"], "no-such-doc", "新名",
                       admin_headers).status_code == 404

    def test_rename_kb_missing_404(self, client, admin_headers):
        """知识库不存在 → 404"""
        assert _rename(client, "no-such-kb", "whatever", "新名",
                       admin_headers).status_code == 404


# ==================== URL 网页导入 ====================

class TestImportFromUrl:

    def test_from_url_ok(self, client, admin_headers, monkeypatch):
        """抓取成功：标题做文件名、正文落盘、script 内容剔除、file_type=url"""
        _monkeypatch_httpx(monkeypatch, lambda url: _html_resp())
        kb = create_kb(client)

        resp = _import_url(client, kb["id"], "https://example.com/page",
                           admin_headers)
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        assert doc["original_name"] == "测试网页标题.md"
        assert doc["file_type"] == "url"
        assert doc["status"] == "uploaded"
        assert doc["size"] > 0
        # 内部名为 UUID + .md
        assert doc["name"].endswith(".md")

        # 正文落盘 uploads/ 与对象存储（local 后端）一致
        content = (UPLOAD_DIR / doc["name"]).read_text(encoding="utf-8")
        assert "这是正文第一段，包含" in content
        assert "加粗" in content
        assert "第二段 & 符号反转义。" in content  # &amp; 已反转义
        stored = (STORAGE_DIR / "uploads" / doc["name"]).read_text(
            encoding="utf-8")
        assert stored == content
        # script/style 内容不进正文
        assert "fake" not in content
        assert "color" not in content

        # 列表可见
        listed = [d for d in client.get(
            f"/api/kbs/{kb['id']}/documents", headers=admin_headers).json()
            if d["id"] == doc["id"]]
        assert len(listed) == 1 and listed[0]["original_name"] == "测试网页标题.md"

    def test_from_url_title_h1_fallback(self, client, admin_headers,
                                        monkeypatch):
        """无 <title> 时取第一个 <h1> 做文件名"""
        html = "<html><body><h1>我的H1大标题</h1><p>正文</p></body></html>"
        _monkeypatch_httpx(monkeypatch, lambda url: _html_resp(html))
        kb = create_kb(client)
        doc = _import_url(client, kb["id"], "https://example.com/a",
                          admin_headers).json()
        assert doc["original_name"] == "我的H1大标题.md"

    def test_from_url_no_title_domain_fallback(self, client, admin_headers,
                                               monkeypatch):
        """无 <title>/<h1> 时用域名做文件名"""
        html = "<html><body><p>只有正文</p></body></html>"
        _monkeypatch_httpx(monkeypatch, lambda url: _html_resp(html))
        kb = create_kb(client)
        doc = _import_url(client, kb["id"], "https://example.com/a",
                          admin_headers).json()
        assert doc["original_name"] == "example.com.md"

    def test_from_url_duplicate_numbering(self, client, admin_headers,
                                          monkeypatch):
        """同标题重复导入：自动加序号 (1)"""
        _monkeypatch_httpx(monkeypatch, lambda url: _html_resp())
        kb = create_kb(client)
        doc1 = _import_url(client, kb["id"], "https://example.com/a",
                           admin_headers).json()
        doc2 = _import_url(client, kb["id"], "https://example.com/b",
                           admin_headers).json()
        assert doc1["original_name"] == "测试网页标题.md"
        assert doc2["original_name"] == "测试网页标题(1).md"

    def test_from_url_title_truncated(self, client, admin_headers,
                                      monkeypatch):
        """超长标题截断 80 字符"""
        long_title = "很" * 100
        html = f"<title>{long_title}</title><p>正文</p>"
        _monkeypatch_httpx(monkeypatch, lambda url: _html_resp(html))
        kb = create_kb(client)
        doc = _import_url(client, kb["id"], "https://example.com/a",
                          admin_headers).json()
        assert doc["original_name"] == f"{'很' * 80}.md"

    def test_from_url_404_error(self, client, admin_headers, monkeypatch):
        """抓取返回 404 → 400 中文错误"""
        _monkeypatch_httpx(monkeypatch,
                           lambda url: _html_resp(html="Not Found",
                                                  status_code=404))
        kb = create_kb(client)
        resp = _import_url(client, kb["id"], "https://example.com/missing",
                           admin_headers)
        assert resp.status_code == 400
        assert "404" in resp.json()["detail"]

    def test_from_url_timeout_error(self, client, admin_headers, monkeypatch):
        """抓取超时 → 400 中文错误（含"超时"）"""
        def handler(url):
            raise httpx.TimeoutException("mock timeout")
        _monkeypatch_httpx(monkeypatch, handler)
        kb = create_kb(client)
        resp = _import_url(client, kb["id"], "https://example.com/slow",
                           admin_headers)
        assert resp.status_code == 400
        assert "超时" in resp.json()["detail"]

    def test_from_url_network_error(self, client, admin_headers, monkeypatch):
        """网络错误（连接失败）→ 400 中文错误"""
        def handler(url):
            raise httpx.ConnectError("mock connect error")
        _monkeypatch_httpx(monkeypatch, handler)
        kb = create_kb(client)
        resp = _import_url(client, kb["id"], "https://example.com/down",
                           admin_headers)
        assert resp.status_code == 400
        assert "无法访问该网址" in resp.json()["detail"]

    def test_from_url_invalid_scheme_400(self, client, admin_headers):
        """非 http/https（ftp/无协议）→ 400，不发起抓取"""
        kb = create_kb(client)
        for bad in ("ftp://example.com/a", "file:///etc/passwd",
                    "javascript:alert(1)", "example.com/a"):
            resp = _import_url(client, kb["id"], bad, admin_headers)
            assert resp.status_code == 400, bad
            assert "仅支持 http/https" in resp.json()["detail"]

    def test_from_url_oversize_400(self, client, admin_headers, monkeypatch):
        """响应体超过 5MB → 400 拒绝"""
        big = ("<html><body><p>" + "x" * (5 * 1024 * 1024 + 10) +
               "</p></body></html>")
        _monkeypatch_httpx(monkeypatch, lambda url: _html_resp(big))
        kb = create_kb(client)
        resp = _import_url(client, kb["id"], "https://example.com/big",
                           admin_headers)
        assert resp.status_code == 400
        assert "5MB" in resp.json()["detail"]

    def test_from_url_then_ingest(self, client, admin_headers, monkeypatch,
                                  mock_embedding):
        """URL 导入后触发解析入库：file_type=url 走直读解析（与文件上传一致）"""
        from conftest import wait_for_status
        _monkeypatch_httpx(monkeypatch, lambda url: _html_resp())
        kb = create_kb(client)
        doc = _import_url(client, kb["id"], "https://example.com/a",
                          admin_headers).json()
        assert doc["file_type"] == "url"

        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        ingested = wait_for_status(client, kb["id"], doc["id"])
        assert ingested["status"] == "ingested"
        assert ingested["chunk_count"] > 0
        assert ingested["file_type"] == "url"

    def test_from_url_user_forbidden_403(self, client, admin_headers,
                                         user_headers, monkeypatch):
        """普通用户导入 → 403（不发起抓取）"""
        called = {"n": 0}

        def handler(url):
            called["n"] += 1
            return _html_resp()
        _monkeypatch_httpx(monkeypatch, handler)
        kb = create_kb(client)
        resp = _import_url(client, kb["id"], "https://example.com/a",
                           user_headers)
        assert resp.status_code == 403
        assert called["n"] == 0
