"""知识库/文档 API 集成测试

覆盖：KB CRUD（空名 400）、中文文件名上传（UUID 内部名）、ingest 状态机
（轮询至 ingested / parsing 重复触发 409 / ingested 可重入库）、文档列表
（size/chunk_count）、详情（chunks 对象数组）、删除级联（元数据/文件/向量）、
系统统计计数。全部进程内 TestClient + 离线 mock embedding。
多租户改造后：所有 API 需登录（默认 admin 登录态，helper 自动注入；
直接请求显式传 admin_headers）。
"""
from __future__ import annotations

import re

from conftest import create_kb, upload_and_ingest, upload_doc, wait_for_status


class TestKBAPI:
    """知识库 CRUD"""

    def test_create_and_list(self, client, admin_headers):
        kb = create_kb(client)
        assert kb["id"]
        assert kb["name"] == "测试知识库"
        # 多租户字段：响应含部门/创建人
        assert "department_id" in kb and kb["department_id"] is None
        assert "owner_id" in kb and kb["owner_id"]
        kbs = client.get("/api/kbs", headers=admin_headers).json()
        assert any(k["id"] == kb["id"] for k in kbs)
        # 列表字段包含实时统计
        assert all("doc_count" in k and "chunk_count" in k for k in kbs)

    def test_create_empty_name_400(self, client, admin_headers):
        resp = client.post("/api/kbs", json={"name": "   ", "description": ""},
                           headers=admin_headers)
        assert resp.status_code == 400

    def test_get_update_delete(self, client, admin_headers):
        kb = create_kb(client)
        # 详情
        detail = client.get(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert detail.status_code == 200
        assert detail.json()["id"] == kb["id"]
        # 更新
        upd = client.put(f"/api/kbs/{kb['id']}", json={"name": "新名称"},
                         headers=admin_headers)
        assert upd.status_code == 200
        assert upd.json()["name"] == "新名称"
        # 不存在的知识库 → 404
        assert client.get("/api/kbs/nonexist",
                          headers=admin_headers).status_code == 404
        assert client.put("/api/kbs/nonexist", json={"name": "x"},
                          headers=admin_headers).status_code == 404
        assert client.delete("/api/kbs/nonexist",
                             headers=admin_headers).status_code == 404
        # 删除
        resp = client.delete(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted_docs"] == 0
        assert client.get(f"/api/kbs/{kb['id']}",
                          headers=admin_headers).status_code == 404
        assert client.get("/api/kbs", headers=admin_headers).json() == []


class TestUpload:
    """文档上传"""

    def test_upload_chinese_filename_uuid_stored(self, client, admin_headers):
        """中文文件名 → 内部 UUID 文件名，原名存元数据"""
        kb = create_kb(client)
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/upload",
            files={"file": ("测试文档.txt", "内容".encode("utf-8"), "text/plain")},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        # 内部名是 UUID 文件名（不含中文，规避中文路径问题）
        assert re.fullmatch(r"[0-9a-f]{12}\.txt", doc["name"]), doc["name"]
        assert doc["original_name"] == "测试文档.txt"
        assert doc["status"] == "uploaded"
        assert doc["size"] == len("内容".encode("utf-8"))
        assert doc["kb_id"] == kb["id"]
        assert doc["file_type"] == "txt"

    def test_upload_unsupported_type_400(self, client, admin_headers):
        kb = create_kb(client)
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/upload",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert "不支持的文件类型" in resp.text

    def test_upload_unknown_kb_404(self, client, admin_headers):
        resp = client.post(
            "/api/kbs/nonexist/documents/upload",
            files={"file": ("a.txt", b"hi", "text/plain")},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_upload_duplicate_name_409(self, client, admin_headers):
        """同知识库同名文档（未删）→ 第二次上传 409 + detail 提示"""
        kb = create_kb(client)
        upload_doc(client, kb["id"], filename="同名.txt")
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/upload",
            files={"file": ("同名.txt", b"other", "text/plain")},
            headers=admin_headers,
        )
        assert resp.status_code == 409, resp.text
        assert "同名" in resp.json()["detail"]
        # 列表仍只有 1 份（409 未创建元数据）
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        assert len(docs) == 1

    def test_upload_duplicate_name_force_ok(self, client, admin_headers):
        """force=true 跳过同名检测 → 200，允许同名共存（列表两份）"""
        kb = create_kb(client)
        upload_doc(client, kb["id"], filename="同名.txt")
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/upload?force=true",
            files={"file": ("同名.txt", b"other", "text/plain")},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        assert len(docs) == 2
        assert all(d["original_name"] == "同名.txt" for d in docs)

    def test_upload_duplicate_name_in_trash_ok(self, client, admin_headers):
        """回收站（软删）中的同名不算：软删后重传同名 → 200"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="同名.txt")
        resp = client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200, resp.text
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/upload",
            files={"file": ("同名.txt", b"re-upload", "text/plain")},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text

    def test_upload_duplicate_name_cross_kb_ok(self, client, admin_headers):
        """不同知识库同名文档互不冲突 → 均 200"""
        kb1 = create_kb(client, name="库A")
        kb2 = create_kb(client, name="库B")
        upload_doc(client, kb1["id"], filename="同名.txt")
        resp = client.post(
            f"/api/kbs/{kb2['id']}/documents/upload",
            files={"file": ("同名.txt", b"other", "text/plain")},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text


class TestIngest:
    """入库状态机"""

    def test_ingest_flow(self, client, mock_embedding, admin_headers):
        """上传 → ingest（显式 method=title 标题切块）→ 轮询到 ingested，切块/向量入库完成，统计联动

        说明：默认切块已按契约改为 naive（SAMPLE_TEXT ~200 字符仅切 1 块），
        本用例显式传 method=title 保持"多块"断言语义（SAMPLE_TEXT 含 #/## 三级标题）。
        """
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"], ingest_body={"method": "title"})
        assert doc["status"] == "ingested"
        assert doc["chunk_count"] >= 3
        assert doc["parse_method"] == "plain"
        assert doc["parser_id"] == "title"
        assert doc["chunk_preview"], "切块预览非空"
        # 列表含 size/chunk_count
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        assert len(docs) == 1
        assert docs[0]["size"] > 0
        assert docs[0]["chunk_count"] == doc["chunk_count"]
        # 知识库 doc_count/chunk_count 联动
        kb_detail = client.get(f"/api/kbs/{kb['id']}",
                               headers=admin_headers).json()
        assert kb_detail["doc_count"] == 1
        assert kb_detail["chunk_count"] == doc["chunk_count"]

    def test_ingest_parsing_state_409(self, client, mock_embedding,
                                      admin_headers):
        """parsing 中间态重复触发 ingest → 409（防重复触发）"""
        from backend.services.document_service import get_document_service
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        # 直接把状态机拨到 parsing（模拟后台解析中）
        get_document_service().transition(doc["id"], "parsing")
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
            headers=admin_headers)
        assert resp.status_code == 409
        assert "当前状态不可触发入库" in resp.text

    def test_reingest_after_ingested(self, client, mock_embedding,
                                     admin_headers):
        """ingested 状态允许重新入库（返回 200，最终仍 ingested）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        doc_id = docs[0]["id"]
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc_id}/ingest",
                           headers=admin_headers)
        assert resp.status_code == 200
        final = wait_for_status(client, kb["id"], doc_id)
        assert final["status"] == "ingested"

    def test_ingest_unknown_doc_404(self, client, mock_embedding,
                                    admin_headers):
        kb = create_kb(client)
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/nonexist/ingest",
            headers=admin_headers)
        assert resp.status_code == 404

    def test_ingest_unknown_kb_404(self, client, admin_headers):
        resp = client.post("/api/kbs/nonexist/documents/x/ingest",
                           headers=admin_headers)
        assert resp.status_code == 404


class TestDocumentDetail:
    """文档详情与删除"""

    def test_detail_chunks_array(self, client, mock_embedding, admin_headers):
        """详情返回 chunks 对象数组 [{text, index}]"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        detail = client.get(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}",
            headers=admin_headers).json()
        assert isinstance(detail["chunks"], list) and detail["chunks"]
        first = detail["chunks"][0]
        assert first["index"] == 0
        assert isinstance(first["text"], str) and first["text"]
        # 切块数 < 20 时 preview 全量返回
        assert detail["chunk_count"] == len(detail["chunks"])

    def test_delete_document_cascade(self, client, mock_embedding,
                                     admin_headers):
        """删除文档（软删）：列表隐藏/回收站可见/向量与文件保留；purge 彻底清除"""
        from backend.config import UPLOAD_DIR
        from backend.services.vector_store import get_vector_store
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        upload_path = UPLOAD_DIR / doc["name"]
        assert upload_path.exists()
        assert get_vector_store().count(kb["id"]) == doc["chunk_count"]

        # 软删除：移入回收站
        resp = client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["doc_id"] == doc["id"]
        # 列表隐藏、回收站可见
        assert [d["id"] for d in client.get(
            f"/api/kbs/{kb['id']}/documents", headers=admin_headers).json()] == []
        trash = client.get(f"/api/kbs/{kb['id']}/documents/trash",
                           headers=admin_headers).json()
        assert [d["id"] for d in trash] == [doc["id"]]
        assert trash[0]["deleted"] is True and trash[0]["deleted_at"]
        # 软删保留文件与向量（恢复无需重新解析）
        assert upload_path.exists()
        assert get_vector_store().count(kb["id"]) == doc["chunk_count"]

        # 彻底删除（purge）：元数据 404 + 上传文件删除 + 向量清除
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/purge",
            headers=admin_headers)
        assert resp.status_code == 200
        assert client.get(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}",
            headers=admin_headers).status_code == 404
        assert not upload_path.exists()
        assert get_vector_store().count(kb["id"]) == 0

    def test_delete_kb_cascade(self, client, mock_embedding, admin_headers):
        """删除知识库：collection/文档元数据/上传文件全部级联清除"""
        from backend.config import DOCUMENTS_DIR, UPLOAD_DIR
        from backend.services.document_service import get_document_service
        from backend.services.vector_store import get_vector_store
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        kb_id = kb["id"]

        resp = client.delete(f"/api/kbs/{kb_id}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted_docs"] == 1
        # 知识库元数据删除
        assert client.get(f"/api/kbs/{kb_id}",
                          headers=admin_headers).status_code == 404
        # 文档元数据清空
        assert list(DOCUMENTS_DIR.glob("*.json")) == []
        # 上传文件清空
        assert list(UPLOAD_DIR.iterdir()) == []
        # 向量 collection 已删
        assert get_vector_store().count(kb_id) == 0
        # 文档服务内无残留
        assert get_document_service().list_by_kb(kb_id) == []

    def test_delete_kb_removes_trash_doc_objects(self, client, admin_headers):
        """删知识库：回收站文档的 uploads/images 存储对象一并删除（无孤儿）

        回归：存储清理循环与 delete_by_kb 同口径（include_deleted=True），
        否则回收站文档元数据被删后其对象变孤儿。
        """
        from backend.config import STORAGE_DIR
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        # 软删除进回收站（元数据/对象保留，可恢复）
        resp = client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200
        # 模拟该回收站文档的解析图片对象（LocalBackend: data/storage/images/...）
        upload_obj = STORAGE_DIR / "uploads" / doc["name"]
        img_obj = STORAGE_DIR / "images" / doc["id"] / "x.png"
        img_obj.parent.mkdir(parents=True, exist_ok=True)
        img_obj.write_bytes(b"x")
        assert upload_obj.exists() and img_obj.exists()

        resp = client.delete(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted_docs"] == 1
        # 回收站文档的存储对象一并删除（修复点）
        assert not upload_obj.exists(), "回收站文档 uploads 对象应被删除"
        assert not img_obj.exists(), "回收站文档 images 对象应被删除"

    def test_delete_unknown_doc_404(self, client, admin_headers):
        kb = create_kb(client)
        resp = client.delete(f"/api/kbs/{kb['id']}/documents/nonexist",
                             headers=admin_headers)
        assert resp.status_code == 404


class TestStats:
    """系统统计"""

    def test_stats_counts(self, client, mock_embedding, admin_headers):
        """入库后 /api/stats 各计数正确"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        stats = client.get("/api/stats", headers=admin_headers).json()
        assert stats["kb_count"] == 1
        assert stats["doc_count"] == 1
        assert stats["chunk_count"] == doc["chunk_count"]
        assert stats["session_count"] == 0
        assert stats["message_count"] == 0

    def test_stats_empty(self, client, admin_headers):
        """空系统统计全 0"""
        stats = client.get("/api/stats", headers=admin_headers).json()
        assert stats == {
            "kb_count": 0, "doc_count": 0, "chunk_count": 0,
            "session_count": 0, "message_count": 0,
        }
