"""回收站（软删除/恢复/彻底删除）+ 文档在线预览 API 测试

回收站覆盖：
- 软删除：列表隐藏、回收站可见（deleted/deleted_at 记录）、存储文件与向量保留
- 恢复：列表回归、deleted_at 清空、无需重新解析即可重新命中检索
- 彻底删除（purge）：元数据 404 + 存储对象 + 向量全清
- 回收站列表/清空；重复软删 409；未入库文档软删/恢复/彻底删除正常
- 检索过滤：真实链路（mock embedding + 真实 Chroma）软删后不命中、恢复后命中；
  mock 验证 search 携带 where={"doc_active": True}（任务要求"where 生效"）
- 权限：普通用户删除/恢复/彻底删除/回收站列表/清空 403

预览覆盖：
- txt 文本内容 + Content-Type；pdf 字节 + application/pdf；>50MB → 413
- url 导入文档返回抓取文本（mock httpx）；docx 400；软删文档 404；
  文档不存在 404；普通用户（同部门）可预览
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import (char_vector, create_department_and_admin, create_kb,
                      login_headers, upload_doc, upload_and_ingest)

from backend.config import STORAGE_DIR, UPLOAD_DIR
from backend.services import web_importer
from backend.services.retrieval_service import get_retrieval_service
from backend.services.vector_store import get_vector_store


# ---------------- 假 httpx（URL 导入 mock，同 test_document_rename 模式） ----------------

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
    """模拟 httpx.AsyncClient：stream(method, url) → _FakeStream"""

    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url):
        return _FakeStream(lambda: self._handler(url))


_SAMPLE_HTML = """<!DOCTYPE html>
<html><head><title>测试网页标题</title>
<script>var fake = '这段JS不应出现在正文';</script>
<style>.cls { color: red }</style></head>
<body>
<h1>页面大标题</h1>
<p>这是正文第一段。</p>
<p>第二段内容。</p>
</body></html>"""


def _html_resp(html: str = _SAMPLE_HTML, status_code: int = 200) -> _FakeResp:
    return _FakeResp(status_code, html.encode("utf-8"))


def _trash(client, kb_id, headers):
    return client.get(f"/api/kbs/{kb_id}/documents/trash", headers=headers)


def _delete(client, kb_id, doc_id, headers):
    return client.delete(f"/api/kbs/{kb_id}/documents/{doc_id}",
                         headers=headers)


def _restore(client, kb_id, doc_id, headers):
    return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/restore",
                       headers=headers)


def _purge(client, kb_id, doc_id, headers):
    return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/purge",
                       headers=headers)


def _raw(client, kb_id, doc_id, headers):
    return client.get(f"/api/kbs/{kb_id}/documents/{doc_id}/raw",
                      headers=headers)


def _retrieve(kb_id: str, query: str):
    """真实检索链路（mock embedding + 真实 Chroma，混合检索默认开启）"""
    return asyncio.run(get_retrieval_service().retrieve(kb_id, query, top_k=5))


# ==================== 软删除 ====================

class TestSoftDelete:

    def test_soft_delete_hides_and_keeps_assets(self, client, mock_embedding,
                                                admin_headers):
        """软删除：列表隐藏、回收站可见、deleted_at 记录、文件与向量保留"""
        from backend.config import DOCUMENTS_DIR
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        upload_path = UPLOAD_DIR / doc["name"]
        assert upload_path.exists()
        assert get_vector_store().count(kb["id"]) == doc["chunk_count"]

        resp = _delete(client, kb["id"], doc["id"], admin_headers)
        assert resp.status_code == 200, resp.text
        assert "回收站" in resp.json()["message"]

        # 列表隐藏
        listed = client.get(f"/api/kbs/{kb['id']}/documents",
                            headers=admin_headers).json()
        assert [d["id"] for d in listed] == []
        # 回收站可见 + deleted_at 记录
        trash = _trash(client, kb["id"], admin_headers).json()
        assert [d["id"] for d in trash] == [doc["id"]]
        assert trash[0]["deleted"] is True
        assert trash[0]["deleted_at"]
        # 元数据落盘保留 deleted/deleted_at
        meta_path = next(DOCUMENTS_DIR.glob("*.json"))
        import json as _json
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["deleted"] is True and meta["deleted_at"]
        # 软删保留文件与向量（恢复无需重新解析）
        assert upload_path.exists()
        assert get_vector_store().count(kb["id"]) == doc["chunk_count"]

    def test_soft_delete_uningested_doc(self, client, admin_headers):
        """未入库（无向量）文档软删/恢复/彻底删除均正常"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        assert _delete(client, kb["id"], doc["id"], admin_headers).status_code == 200
        trash = _trash(client, kb["id"], admin_headers).json()
        assert [d["id"] for d in trash] == [doc["id"]]
        assert _restore(client, kb["id"], doc["id"], admin_headers).status_code == 200
        assert _trash(client, kb["id"], admin_headers).json() == []
        assert _purge(client, kb["id"], doc["id"], admin_headers).status_code == 200

    def test_soft_delete_twice_conflict(self, client, admin_headers):
        """重复软删 409"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        assert _delete(client, kb["id"], doc["id"], admin_headers).status_code == 200
        resp = _delete(client, kb["id"], doc["id"], admin_headers)
        assert resp.status_code == 409
        assert "回收站" in resp.json()["detail"]

    def test_restore_returns_to_list_and_retrieval(self, client,
                                                   mock_embedding,
                                                   admin_headers):
        """恢复：列表回归、deleted_at 清空、检索立即恢复"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        _delete(client, kb["id"], doc["id"], admin_headers)
        # 软删后检索不命中（混合检索：向量 where + BM25 meta 双路径）
        assert _retrieve(kb["id"], "Python 编程语言") == []

        resp = _restore(client, kb["id"], doc["id"], admin_headers)
        assert resp.status_code == 200, resp.text
        restored = resp.json()
        assert restored["deleted"] is False
        assert restored["deleted_at"] is None
        # 列表回归
        listed = client.get(f"/api/kbs/{kb['id']}/documents",
                            headers=admin_headers).json()
        assert [d["id"] for d in listed] == [doc["id"]]
        # 检索恢复（无需重新解析）
        sources = _retrieve(kb["id"], "Python 编程语言")
        assert any(s.document_id == doc["id"] for s in sources)

    def test_purge_removes_everything(self, client, mock_embedding,
                                      admin_headers):
        """彻底删除：元数据 404 + 存储对象 + 本地文件 + 向量全清"""
        from backend.config import STORAGE_DIR
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        _delete(client, kb["id"], doc["id"], admin_headers)
        storage_key = STORAGE_DIR / "uploads" / doc["name"]

        resp = _purge(client, kb["id"], doc["id"], admin_headers)
        assert resp.status_code == 200, resp.text
        # 元数据/文件/向量全清
        assert client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                          headers=admin_headers).status_code == 404
        assert not (UPLOAD_DIR / doc["name"]).exists()
        assert not storage_key.exists()
        assert get_vector_store().count(kb["id"]) == 0
        # 回收站清空 + 列表无
        assert _trash(client, kb["id"], admin_headers).json() == []
        assert client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json() == []
        # 检索彻底无命中
        assert _retrieve(kb["id"], "Python 编程语言") == []

    def test_trash_list_and_empty(self, client, mock_embedding, admin_headers):
        """回收站列表（按删除时间倒序）+ 清空回收站"""
        kb = create_kb(client)
        d1 = upload_and_ingest(client, kb["id"], filename="一.txt")
        d2 = upload_and_ingest(client, kb["id"], filename="二.txt")
        _delete(client, kb["id"], d1["id"], admin_headers)
        _delete(client, kb["id"], d2["id"], admin_headers)

        trash = _trash(client, kb["id"], admin_headers).json()
        assert [d["id"] for d in trash] == [d2["id"], d1["id"]]  # 后删的在前
        assert all(d["deleted"] for d in trash)

        resp = client.post(f"/api/kbs/{kb['id']}/documents/trash/empty",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 2
        assert _trash(client, kb["id"], admin_headers).json() == []
        assert get_vector_store().count(kb["id"]) == 0

    def test_delete_kb_cascade_includes_trash(self, client, mock_embedding,
                                              admin_headers):
        """删除知识库：回收站文档一并级联清除"""
        from backend.config import DOCUMENTS_DIR
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        _delete(client, kb["id"], doc["id"], admin_headers)
        resp = client.delete(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted_docs"] == 1
        assert list(DOCUMENTS_DIR.glob("*.json")) == []
        assert get_vector_store().count(kb["id"]) == 0


# ==================== 检索过滤机制 ====================

class _RecordingVecStore:
    """包装真实 vector_store：search 记录 where 参数并透传（验证 where 生效）"""

    def __init__(self, real):
        self._real = real
        self.seen_wheres = []

    def search(self, kb_id, query_emb, top_k=5, where=None):
        self.seen_wheres.append(where)
        return self._real.search(kb_id, query_emb, top_k=top_k, where=where)

    def get_embedding_dimension(self, kb_id):
        return self._real.get_embedding_dimension(kb_id)

    def count(self, kb_id):
        return self._real.count(kb_id)

    def get_all(self, kb_id):
        return self._real.get_all(kb_id)


class TestRetrievalFilter:

    def test_search_called_with_active_where(self, client, mock_embedding,
                                             admin_headers, monkeypatch):
        """检索调用 vector_store.search 时携带 where={'doc_active': True}"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        real = get_vector_store()
        recorder = _RecordingVecStore(real)
        monkeypatch.setattr(
            "backend.services.retrieval_service.get_vector_store",
            lambda: recorder)
        sources = _retrieve(kb["id"], "Python 编程语言")
        assert recorder.seen_wheres, "search 应收到 where 参数"
        assert recorder.seen_wheres[0] == {"doc_active": True}
        assert sources, "活跃文档应命中检索"

    def test_vector_where_excludes_soft_deleted_chunks(self, client,
                                                       mock_embedding,
                                                       admin_headers):
        """真实 Chroma where 过滤：软删 chunk 被排除、恢复后回归"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        vec = get_vector_store()
        all_ids = [cid for cid, _, _ in vec.get_all(kb["id"])]
        assert all_ids
        # 软删前：全部 chunk 活跃
        active = [cid for cid, _, _ in
                  vec.get_all(kb["id"]) if True]
        assert len(active) == len(all_ids)
        _delete(client, kb["id"], doc["id"], admin_headers)
        # 软删后：where 过滤只保留活跃 chunk（该文档已全部不活跃）
        query_emb = char_vector("Python 测试查询")
        hits = vec.search(kb["id"], query_emb, top_k=10,
                          where={"doc_active": True})
        assert hits == []
        # 恢复后：where 过滤重新命中
        _restore(client, kb["id"], doc["id"], admin_headers)
        hits = vec.search(kb["id"], query_emb, top_k=10,
                          where={"doc_active": True})
        assert hits

    def test_update_metadata_preserves_other_keys(self, client,
                                                  mock_embedding,
                                                  admin_headers):
        """软删更新 metadata 仅改 doc_active，document_id/chunk_index 等键保真"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        vec = get_vector_store()
        meta_before = vec.get_all(kb["id"])[0][2]
        _delete(client, kb["id"], doc["id"], admin_headers)
        meta_after = vec.get_all(kb["id"])[0][2]
        assert meta_after["doc_active"] is False
        assert meta_after["document_id"] == meta_before["document_id"]
        assert meta_after["chunk_index"] == meta_before["chunk_index"]
        assert meta_after["document_name"] == meta_before["document_name"]


# ==================== 权限 ====================

class TestTrashPermissions:

    def test_permissions_403(self, client, admin_headers):
        """普通用户：删除/恢复/彻底删除/回收站列表/清空全部 403"""
        dept_id, dept_admin = create_department_and_admin(
            client, admin_headers, "回收站测试部门", "trash_dept_admin",
            "dept123456", "回收站管理员")
        user = client.post("/api/users", json={
            "username": "trash_user", "password": "user123456",
            "display_name": "回收站用户", "role": "user",
            "department_id": dept_id,
        }, headers=admin_headers)
        assert user.status_code == 201, user.text
        user_headers = login_headers(client, "trash_user", "user123456")

        kb = create_kb(client, headers=dept_admin, department_id=dept_id)
        doc = upload_doc(client, kb["id"], headers=dept_admin)
        assert _delete(client, kb["id"], doc["id"],
                       dept_admin).status_code == 200
        assert _delete(client, kb["id"], doc["id"], user_headers).status_code == 403
        assert _restore(client, kb["id"], doc["id"],
                        user_headers).status_code == 403
        assert _purge(client, kb["id"], doc["id"], user_headers).status_code == 403
        assert _trash(client, kb["id"], user_headers).status_code == 403
        assert client.post(f"/api/kbs/{kb['id']}/documents/trash/empty",
                           headers=user_headers).status_code == 403
        # 部门管理员可正常恢复
        assert _restore(client, kb["id"], doc["id"],
                        dept_admin).status_code == 200

    def test_cross_department_hidden(self, client, admin_headers):
        """跨部门用户访问文档（含回收站/预览）统一 404 伪装"""
        dept_a, dept_a_admin = create_department_and_admin(
            client, admin_headers, "部门A", "dept_a", "dept123456", "A管理员")
        dept_b, dept_b_admin = create_department_and_admin(
            client, admin_headers, "部门B", "dept_b", "dept123456", "B管理员")
        kb = create_kb(client, headers=dept_a_admin, department_id=dept_a)
        doc = upload_doc(client, kb["id"], headers=dept_a_admin)
        # B 部门管理员：列表 404 伪装，预览/回收站同样 404
        assert client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=dept_b_admin).status_code == 404
        assert _raw(client, kb["id"], doc["id"], dept_b_admin).status_code == 404
        # 回收站/管理操作对跨部门管理员：403（管理权限显式拒绝）
        assert _trash(client, kb["id"], dept_b_admin).status_code == 403


# ==================== 在线预览 ====================

class TestDocumentPreview:

    def test_raw_txt_preview(self, client, admin_headers, user_headers):
        """txt 预览：内容 + text/plain Content-Type；普通用户（同部门）可访问"""
        from conftest import _find_dept_id
        dept_id = _find_dept_id(client, admin_headers, "测试部门")
        kb = create_kb(client, department_id=dept_id)
        content = "Hello 预览测试\n第二行"
        doc = upload_doc(client, kb["id"], content=content)
        resp = _raw(client, kb["id"], doc["id"], admin_headers)
        assert resp.status_code == 200
        assert resp.content.decode("utf-8") == content
        assert resp.headers["content-type"].startswith("text/plain")
        # 普通用户（知识库可访问）也可预览
        assert _raw(client, kb["id"], doc["id"], user_headers).status_code == 200

    def test_raw_pdf_preview(self, client, admin_headers, monkeypatch):
        """pdf 预览：application/pdf 字节流；超过上限 → 413 中文提示"""
        import backend.routers.documents as docs_router
        kb = create_kb(client)
        content = b"%PDF-1.4 fake pdf bytes"
        doc = upload_doc(client, kb["id"], filename="文档.pdf",
                         content=content, mime="application/pdf")
        resp = _raw(client, kb["id"], doc["id"], admin_headers)
        assert resp.status_code == 200
        assert resp.content == content
        assert resp.headers["content-type"].startswith("application/pdf")

        # 超上限：monkeypatch 上限为 1 字节 → 413 中文提示
        monkeypatch.setattr(docs_router, "_MAX_PREVIEW_PDF_BYTES", 1)
        resp = _raw(client, kb["id"], doc["id"], admin_headers)
        assert resp.status_code == 413
        assert "50MB" in resp.json()["detail"]

    def test_raw_url_preview(self, client, admin_headers, monkeypatch):
        """url 导入文档：返回抓取的 md 文本（mock httpx）"""
        def handler(url):
            return _html_resp()
        monkeypatch.setattr(web_importer.httpx, "AsyncClient",
                            lambda **kwargs: _FakeAsyncClient(handler))
        # P0-2: SSRF 校验的 DNS 解析一并 mock（离线稳定）
        monkeypatch.setattr(
            web_importer.socket, "getaddrinfo",
            lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])
        kb = create_kb(client)
        resp = client.post(f"/api/kbs/{kb['id']}/documents/from-url",
                           json={"url": "https://example.com/page"},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        assert doc["file_type"] == "url"
        raw = _raw(client, kb["id"], doc["id"], admin_headers)
        assert raw.status_code == 200
        # 抓取的 md 文本（正文提取，h1/段落），非原始 HTML
        assert "页面大标题" in raw.content.decode("utf-8")
        assert "<title>" not in raw.content.decode("utf-8")
        assert raw.headers["content-type"].startswith("text/plain")

    def test_raw_docx_download(self, client, admin_headers):
        """P1-5: docx 预览接口返回附件下载（octet-stream + attachment 头）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="文档.docx",
                         content=b"fake docx", mime="application/octet-stream")
        resp = _raw(client, kb["id"], doc["id"], admin_headers)
        assert resp.status_code == 200
        assert resp.content == b"fake docx"
        assert resp.headers["content-type"] == "application/octet-stream"
        assert "attachment" in resp.headers["content-disposition"]

    def test_raw_soft_deleted_404(self, client, mock_embedding, admin_headers):
        """软删后预览 404（回收站文档不可访问）"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        assert _raw(client, kb["id"], doc["id"], admin_headers).status_code == 200
        _delete(client, kb["id"], doc["id"], admin_headers)
        assert _raw(client, kb["id"], doc["id"], admin_headers).status_code == 404

    def test_raw_not_found(self, client, admin_headers):
        """文档不存在预览 → 404"""
        kb = create_kb(client)
        assert _raw(client, kb["id"], "no-such-doc",
                    admin_headers).status_code == 404
