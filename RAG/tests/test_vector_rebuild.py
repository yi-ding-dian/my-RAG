"""Embedding 维度冲突检测 + 一键重建向量 测试

覆盖：
- 维度状态检测：空 collection compatible / 入库后匹配 / 模型维度变化后不兼容
  （message 含"维度"与"重建向量"）/ 列表接口附带 vector_status 摘要 /
  模型不可用时 model_dim=None 且不阻塞
- 入库维度防护：维度不匹配 → ingest 失败 + error 含"维度"，原向量不受影响
- 检索维度防护：chat SSE error 事件透传"重建向量"提示（不再静默未检索到）
- 重建流程：删除+重写发生（collection 维度切换为新模型维度、计数保持）、
  部分失败汇总（errors 列表）、空库、状态 API 往返、仅重建 ingested 文档
- 权限：非管理员 POST 重建 403；vector-status/rebuild-status 可读
"""
from __future__ import annotations

import time

import pytest

from conftest import SAMPLE_TEXT, char_vector, create_kb, upload_and_ingest, upload_doc


def _patch_embedding_dim(monkeypatch, dim):
    """把 embedding mock 换成指定维度（替换源模块 + 引用复制模块，与 conftest 同构）"""
    from backend.services import (embedding_service, ingestion_service,
                                  retrieval_service)

    class Fake:
        async def embed(self, texts):
            return [char_vector(t, dim=dim) for t in texts]

    fake_getter = lambda: Fake()  # noqa: E731
    for mod in (embedding_service, ingestion_service, retrieval_service):
        monkeypatch.setattr(mod, "get_embedding_service", fake_getter)
    # mock 维度变化后清空模型维度缓存（配置 key 相同，缓存不自动失效）
    from backend.services import dim_check
    dim_check.clear_model_dim_cache()


def _patch_embedding_fail(monkeypatch, marker="fail-me"):
    """mock embedding：文本含 marker 时抛错（重建部分失败场景）"""
    from backend.services import (embedding_service, ingestion_service,
                                  retrieval_service)

    class Fake:
        async def embed(self, texts):
            if any(marker in t for t in texts):
                raise RuntimeError("mock embed 失败（测试构造）")
            return [char_vector(t) for t in texts]

    fake_getter = lambda: Fake()  # noqa: E731
    for mod in (embedding_service, ingestion_service, retrieval_service):
        monkeypatch.setattr(mod, "get_embedding_service", fake_getter)


def wait_rebuild_done(client, kb_id, headers, timeout=20.0):
    """轮询重建任务至结束，返回最终状态"""
    deadline = time.monotonic() + timeout
    while True:
        st = client.get(f"/api/kbs/{kb_id}/rebuild-status",
                        headers=headers).json()
        if not st["running"]:
            return st
        if time.monotonic() > deadline:
            raise AssertionError("重建任务超时未结束")
        time.sleep(0.1)


def wait_failed(client, kb_id, doc_id, headers, timeout=20.0):
    """轮询文档至 failed（中途 ingested 视为意外）"""
    deadline = time.monotonic() + timeout
    while True:
        doc = client.get(f"/api/kbs/{kb_id}/documents/{doc_id}",
                         headers=headers).json()
        if doc["status"] == "failed":
            return doc
        if doc["status"] == "ingested":
            raise AssertionError("维度不匹配时文档意外入库成功")
        if time.monotonic() > deadline:
            raise AssertionError("等待 failed 超时")
        time.sleep(0.1)


@pytest.fixture(autouse=True)
def _clear_dim_cache():
    """每个测试前后清空模型维度缓存（mock 维度切换后不串）"""
    from backend.services import dim_check
    dim_check.clear_model_dim_cache()
    yield
    dim_check.clear_model_dim_cache()


# ==================== 维度状态检测 ====================


class TestVectorStatus:

    def test_empty_collection_compatible(self, client, admin_headers,
                                         mock_embedding):
        kb = create_kb(client)
        resp = client.get(f"/api/kbs/{kb['id']}/vector-status",
                          headers=admin_headers)
        assert resp.status_code == 200
        st = resp.json()
        assert st["kb_id"] == kb["id"]
        assert st["collection_vectors"] == 0
        assert st["current_dim"] is None
        assert st["compatible"] is True
        assert "message" in st and st["message"]

    def test_ingested_compatible(self, client, admin_headers, mock_embedding):
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        st = client.get(f"/api/kbs/{kb['id']}/vector-status",
                        headers=admin_headers).json()
        assert st["collection_vectors"] > 0
        assert st["current_dim"] == 64
        assert st["model_dim"] == 64
        assert st["compatible"] is True

    def test_incompatible_after_model_change(self, client, admin_headers,
                                             mock_embedding, monkeypatch):
        """模型维度变化（64→32）：vector-status 报不兼容 + message 明确"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        _patch_embedding_dim(monkeypatch, 32)
        st = client.get(f"/api/kbs/{kb['id']}/vector-status",
                        headers=admin_headers).json()
        assert st["current_dim"] == 64
        assert st["model_dim"] == 32
        assert st["compatible"] is False
        assert "维度" in st["message"]
        assert "重建向量" in st["message"]

    def test_kb_list_carries_vector_status(self, client, admin_headers,
                                           mock_embedding):
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        kbs = client.get("/api/kbs", headers=admin_headers).json()
        entry = next(k for k in kbs if k["id"] == kb["id"])
        vs = entry["vector_status"]
        assert vs["current_dim"] == 64 and vs["model_dim"] == 64
        assert vs["compatible"] is True

    def test_model_unavailable_compatible(self, client, admin_headers,
                                          mock_embedding, monkeypatch):
        """embedding 模型不可用：model_dim=None，compatible=True（不阻塞问答）"""
        from backend.services import dim_check
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])

        async def broken_model_dim():
            return None

        monkeypatch.setattr(dim_check, "get_model_dimension", broken_model_dim)
        st = client.get(f"/api/kbs/{kb['id']}/vector-status",
                        headers=admin_headers).json()
        assert st["model_dim"] is None
        assert st["compatible"] is True


# ==================== 入库维度防护 ====================


class TestIngestGuard:

    def test_ingest_fails_on_dimension_mismatch(self, client, admin_headers,
                                                mock_embedding, monkeypatch):
        """维度不匹配：ingest 中止，status=failed，error 含"维度"，原向量不受影响"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])  # 64 维入库成功
        _patch_embedding_dim(monkeypatch, 32)  # 模型换成 32 维
        doc = upload_doc(client, kb["id"], filename="维度冲突.txt",
                         content="# 新文档\n\n换模型后入库的文档内容。")
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={}, headers=admin_headers)
        assert resp.status_code == 200
        failed = wait_failed(client, kb["id"], doc["id"], admin_headers)
        assert "维度" in (failed.get("error") or "")
        # 原文档向量不受影响（校验在删旧向量之前）
        st = client.get(f"/api/kbs/{kb['id']}/vector-status",
                        headers=admin_headers).json()
        assert st["collection_vectors"] > 0 and st["current_dim"] == 64


# ==================== 检索维度防护（SSE 透传） ====================


class TestRetrievalGuard:

    def test_chat_error_event_on_dimension_mismatch(self, client, admin_headers,
                                                    mock_embedding, monkeypatch):
        """检索维度不匹配 → SSE error 事件带"重建向量"提示（不再静默未检索到）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        _patch_embedding_dim(monkeypatch, 32)
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert "error" in resp.text
        assert "维度" in resp.text
        assert "重建向量" in resp.text
        assert "未检索到相关内容" not in resp.text


# ==================== 一键重建 ====================


class TestRebuildVectors:

    def test_rebuild_rewrites_with_new_model_dim(self, client, admin_headers,
                                                 mock_embedding, monkeypatch):
        """重建：collection 维度切换为新模型维度，向量计数保持（删除+重写发生）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], filename="文档A.txt")
        upload_and_ingest(client, kb["id"], filename="文档B.txt",
                          content=SAMPLE_TEXT)
        before = client.get(f"/api/kbs/{kb['id']}/vector-status",
                            headers=admin_headers).json()
        assert before["current_dim"] == 64

        _patch_embedding_dim(monkeypatch, 32)
        resp = client.post(f"/api/kbs/{kb['id']}/rebuild-vectors",
                           headers=admin_headers)
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]
        st = wait_rebuild_done(client, kb["id"], admin_headers)
        assert st["task_id"] == task_id
        assert st["done"] == 2 and st["failed"] == 0
        assert st["total"] == 2 and st["running"] is False
        assert st["current_doc"] is None and st["finished_at"]
        assert st["errors"] == []

        after = client.get(f"/api/kbs/{kb['id']}/vector-status",
                           headers=admin_headers).json()
        assert after["current_dim"] == 32
        assert after["model_dim"] == 32
        assert after["compatible"] is True
        assert after["collection_vectors"] == before["collection_vectors"]

    def test_rebuild_partial_failure(self, client, admin_headers,
                                     mock_embedding, monkeypatch):
        """单个文档 embedding 失败：继续后续文档，失败汇总进 errors"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], filename="好文档.txt")
        upload_and_ingest(client, kb["id"], filename="坏文档.txt",
                          content="# 标题\n\nfail-me 触发失败的文档内容。")
        _patch_embedding_fail(monkeypatch, marker="fail-me")
        client.post(f"/api/kbs/{kb['id']}/rebuild-vectors",
                    headers=admin_headers)
        st = wait_rebuild_done(client, kb["id"], admin_headers)
        assert st["done"] == 1
        assert st["failed"] == 1
        assert st["errors"] and "坏文档" in st["errors"][0]["doc_name"]
        # 成功文档的向量写回
        st2 = client.get(f"/api/kbs/{kb['id']}/vector-status",
                         headers=admin_headers).json()
        assert st2["collection_vectors"] > 0

    def test_rebuild_empty_kb(self, client, admin_headers, mock_embedding):
        """无已入库文档：任务正常结束，done/failed=0"""
        kb = create_kb(client)
        client.post(f"/api/kbs/{kb['id']}/rebuild-vectors",
                    headers=admin_headers)
        st = wait_rebuild_done(client, kb["id"], admin_headers)
        assert st["done"] == 0 and st["failed"] == 0 and st["total"] == 0
        assert st["running"] is False

    def test_rebuild_status_before_task(self, client, admin_headers,
                                        mock_embedding):
        """从未触发过重建：status 返回空默认态"""
        kb = create_kb(client)
        st = client.get(f"/api/kbs/{kb['id']}/rebuild-status",
                        headers=admin_headers).json()
        assert st["task_id"] is None
        assert st["running"] is False
        assert st["done"] == 0 and st["total"] == 0 and st["failed"] == 0

    def test_rebuild_restores_ingested_docs_only(self, client, admin_headers,
                                                 mock_embedding):
        """仅重建 status=ingested 的文档（uploaded 不参与）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], filename="文档A.txt")
        upload_doc(client, kb["id"], filename="未入库.txt",
                   content="# 未入库\n\n不参与重建。")
        client.post(f"/api/kbs/{kb['id']}/rebuild-vectors",
                    headers=admin_headers)
        st = wait_rebuild_done(client, kb["id"], admin_headers)
        assert st["done"] == 1 and st["total"] == 1


# ==================== 权限 ====================


class TestRebuildPermission:

    def test_rebuild_requires_manage(self, client, admin_headers,
                                     dept_admin_headers, user_headers,
                                     mock_embedding):
        """重建仅 can_manage_kb：user 403；dept_admin 200；状态接口 user 可读"""
        depts = client.get("/api/departments", headers=admin_headers).json()
        dept_id = next(d["id"] for d in depts if d["name"] == "测试部门")
        kb = create_kb(client, department_id=dept_id)
        # user：可读状态，不可重建
        resp = client.get(f"/api/kbs/{kb['id']}/vector-status",
                          headers=user_headers)
        assert resp.status_code == 200
        resp = client.post(f"/api/kbs/{kb['id']}/rebuild-vectors",
                           headers=user_headers)
        assert resp.status_code == 403
        resp = client.get(f"/api/kbs/{kb['id']}/rebuild-status",
                          headers=user_headers)
        assert resp.status_code == 200
        # dept_admin：可重建
        resp = client.post(f"/api/kbs/{kb['id']}/rebuild-vectors",
                           headers=dept_admin_headers)
        assert resp.status_code == 200
        assert resp.json()["task_id"]
        wait_rebuild_done(client, kb["id"], dept_admin_headers)
