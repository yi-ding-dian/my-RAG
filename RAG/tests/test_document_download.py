"""文档下载接口测试：GET /api/kbs/{kb_id}/documents/{doc_id}/download

设计：can_access_kb（下载算读取，无权限 404 伪装）；文件来源对象存储
（key=uploads/{doc.name}）优先，存储不可用回退本地副本 data/uploads/，
两者均无 → 404；返回 attachment（文件名 original_name，RFC 5987）。
"""
from __future__ import annotations

from backend.config import UPLOAD_DIR
from conftest import create_kb, upload_doc


class _FailingStorage:
    """模拟 MinIO 存储：上传正常（测试文档仍能写入），读取抛异常
    （模拟存储不可用，触发下载回退本地副本分支）"""

    async def upload_bytes(self, key, data, content_type=None):
        pass

    async def read_bytes(self, key):
        raise RuntimeError("MinIO 不可用（测试构造）")


class TestDocumentDownload:

    def test_download_returns_original_bytes(self, client, admin_headers):
        """下载 → 200 + 原始字节一致 + attachment + UTF-8 文件名（中文）"""
        kb = create_kb(client)
        content = b"\xe4\xb8\xad\xe6\x96\x87\xe6\x96\x87\xe6\x9c\xac\xe5\x86\x85\xe5\xae\xb9"
        doc = upload_doc(client, kb["id"], filename="产品方案.txt", content=content)

        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/download",
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.content == content
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        # RFC 5987 filename*=UTF-8''（中文文件名 URL 编码后）
        assert "filename*=UTF-8''" in cd
        assert "filename*=UTF-8''%E4%BA%A7%E5%93%81%E6%96%B9%E6%A1%88.txt" in cd

    def test_download_docx_media_type(self, client, admin_headers):
        """docx 下载 → office MIME（非 octet-stream 兜底）"""
        kb = create_kb(client)
        content = b"PK\x03\x04fake-docx-bytes"
        doc = upload_doc(client, kb["id"], filename="方案.docx", content=content)
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/download",
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.content == content
        assert resp.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument")

    def test_download_pdf_media_type(self, client, admin_headers):
        """pdf 下载 → application/pdf；未解析文档（uploaded）也可下载"""
        kb = create_kb(client)
        content = b"%PDF-1.4 fake-pdf-bytes"
        doc = upload_doc(client, kb["id"], filename="报告.pdf", content=content)
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/download",
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.content == content
        assert resp.headers["content-type"] == "application/pdf"

    def test_download_401_without_token(self, client):
        """未登录 → 401"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/download")
        assert resp.status_code == 401

    def test_download_404_no_permission(self, client, admin_headers, user_headers):
        """无 kb 访问权限的普通用户 → 404 伪装（can_access_kb 语义）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="保密.txt",
                         content=b"secret")
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/download",
                          headers=user_headers)
        assert resp.status_code == 404

    def test_download_404_doc_missing(self, client, admin_headers):
        """文档不存在 → 404"""
        kb = create_kb(client)
        resp = client.get(f"/api/kbs/{kb['id']}/documents/nonexistent/download",
                          headers=admin_headers)
        assert resp.status_code == 404

    def test_download_404_trashed(self, client, admin_headers):
        """回收站内文档 → 404（不可下载）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="已删.txt",
                         content=b"gone")
        client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                      headers=admin_headers)
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/download",
                          headers=admin_headers)
        assert resp.status_code == 404

    def test_download_fallback_local_copy(self, client, admin_headers, monkeypatch):
        """存储不可用（MinIO 异常）→ 回退本地副本 data/uploads/，内容一致"""
        from backend.routers import documents as documents_router
        monkeypatch.setattr(documents_router, "get_storage_service",
                            lambda: _FailingStorage())
        kb = create_kb(client)
        content = b"local-copy-fallback-bytes"
        doc = upload_doc(client, kb["id"], filename="回退.txt", content=content)
        # 本地副本存在（上传双写）
        assert (UPLOAD_DIR / doc["name"]).exists()
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/download",
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.content == content
        cd = resp.headers["content-disposition"]
        # FileResponse 分支（starlette 生成）：attachment + filename*=utf-8''
        assert "attachment" in cd
        assert "filename*=" in cd
        assert "%E5%9B%9E%E9%80%80.txt" in cd  # 「回退.txt」URL 编码

    def test_download_404_storage_and_local_both_missing(
            self, client, admin_headers, monkeypatch):
        """存储不可用且本地副本已清理 → 404（中文 detail）"""
        from backend.routers import documents as documents_router
        monkeypatch.setattr(documents_router, "get_storage_service",
                            lambda: _FailingStorage())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="清理.txt",
                         content=b"cleaned")
        (UPLOAD_DIR / doc["name"]).unlink()
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/download",
                          headers=admin_headers)
        assert resp.status_code == 404
        assert "文件不存在" in resp.json()["detail"]

    def test_download_url_imported_doc(self, client, admin_headers, monkeypatch):
        """URL 网页导入文档（file_type=url）也可下载（原始 md 文本）"""
        async def _fake_fetch(url):
            return "网页标题", "网页正文内容（来自测试 mock）"
        monkeypatch.setattr("backend.routers.documents.fetch_webpage",
                            _fake_fetch)
        kb = create_kb(client)
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/from-url",
            json={"url": "http://example.com/page"},
            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        assert doc["file_type"] == "url"
        dl = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/download",
                        headers=admin_headers)
        assert dl.status_code == 200
        assert dl.headers["content-type"].startswith("text/markdown")
        assert len(dl.content) > 0
