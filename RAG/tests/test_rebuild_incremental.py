"""B3: 重建向量竞态增量补齐 测试

背景：drop_collection 后只重建任务启动时的 ingested 快照；重建期间新入库
文档的向量被清空且不在快照 → ingested 但检索永久丢失。
修复：重建收尾做增量补齐——查当前 DB ingested 文档集合 vs collection 现有
doc_id 集合，缺失的重新向量化入库（文本取自 chunks_meta）。

覆盖：
- 单元：_backfill_missing_vectors 直接把缺失文档补齐（含软删保 doc_active=False）
- 集成：重建进行中（后台任务）新 ingest 文档 → 重建完成时其向量存在可检索
"""
from __future__ import annotations

import asyncio
import time

from conftest import char_vector, create_kb, upload_and_ingest


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


def _force_ingested(doc_svc, doc_id, chunks_meta):
    """状态机合法链路 uploaded→parsing→parsed→ingested 并写入 chunks_meta"""
    doc_svc.transition(doc_id, "parsing")
    doc_svc.transition(doc_id, "parsed")
    doc_svc.transition(doc_id, "ingested", chunks_meta=chunks_meta,
                       chunk_count=len(chunks_meta))


class TestBackfillUnit:

    def test_backfill_missing_vectors(self):
        """collection 无向量的 ingested 文档被补齐（快照内/未入库的不动）"""
        from backend.services import dim_check
        from backend.services.document_service import get_document_service
        from backend.services.vector_store import get_vector_store

        async def _run():
            doc_svc = get_document_service()
            vec = get_vector_store()
            kb_id = "kb_backfill_unit"

            # A：ingested 且已有向量（快照内文档，不应重复处理）
            a = doc_svc.create(kb_id=kb_id, original_name="A.txt", size=10)
            a_chunks = [{"text": "A 内容一", "char_start": 0, "char_end": 4}]
            _force_ingested(doc_svc, a.id, a_chunks)
            vec.add(kb_id, a.id, "A.txt", ["A 内容一"],
                    [char_vector("A 内容一")])
            # B：ingested 但 collection 无向量（模拟重建期间向量被 drop 清掉）
            b = doc_svc.create(kb_id=kb_id, original_name="B.txt", size=10)
            b_chunks = [{"text": "B 内容一", "char_start": 0, "char_end": 4},
                        {"text": "B 内容二", "char_start": 5, "char_end": 9}]
            _force_ingested(doc_svc, b.id, b_chunks)
            # C：uploaded（不参与补齐）
            doc_svc.create(kb_id=kb_id, original_name="C.txt", size=10)

            task = {"done": 1, "failed": 0, "errors": [], "current_doc": None}
            await dim_check._backfill_missing_vectors(
                kb_id, task, vec, FakeEmb(), snapshot_ids={a.id})

            assert task["done"] == 2 and task["failed"] == 0
            assert task["total"] == 2  # total 含增量文档（A+B，C 未入库不算）
            allv = vec.get_all(kb_id)
            doc_ids = {m.get("document_id") for _, _, m in allv}
            assert doc_ids == {a.id, b.id}
            b_metas = [m for _, _, m in allv if m.get("document_id") == b.id]
            assert len(b_metas) == 2  # B 两个块都补齐
            assert all(m.get("doc_active") is True for m in b_metas)
            # A 的原始向量未被重复写入（块数保持 1+2=3）
            assert len(allv) == 3

        asyncio.run(_run())

    def test_backfill_skips_docs_already_in_collection(self):
        """collection 已有向量的 ingested 文档（重建期间 ingest 直接写入
        新 collection）不重复补齐"""
        from backend.services import dim_check
        from backend.services.document_service import get_document_service
        from backend.services.vector_store import get_vector_store

        async def _run():
            doc_svc = get_document_service()
            vec = get_vector_store()
            kb_id = "kb_backfill_existing"
            a = doc_svc.create(kb_id=kb_id, original_name="A.txt", size=10)
            _force_ingested(doc_svc, a.id,
                            [{"text": "A 内容一", "char_start": 0, "char_end": 4}])
            vec.add(kb_id, a.id, "A.txt", ["A 内容一"],
                    [char_vector("A 内容一")])
            task = {"done": 0, "failed": 0, "errors": [], "current_doc": None}
            # 快照不含 A（模拟重建期间新入库），但 collection 已有其向量
            await dim_check._backfill_missing_vectors(
                kb_id, task, vec, FakeEmb(), snapshot_ids=set())
            assert task["done"] == 0 and task["failed"] == 0
            assert len(vec.get_all(kb_id)) == 1  # 未重复写入

        asyncio.run(_run())

    def test_backfill_preserves_deleted_flag(self):
        """软删的 ingested 文档补齐后 doc_active=False（检索不复活）"""
        from backend.services import dim_check
        from backend.services.document_service import get_document_service
        from backend.services.vector_store import get_vector_store

        async def _run():
            doc_svc = get_document_service()
            vec = get_vector_store()
            kb_id = "kb_backfill_deleted"
            b = doc_svc.create(kb_id=kb_id, original_name="B.txt", size=10)
            _force_ingested(doc_svc, b.id,
                            [{"text": "B 内容一", "char_start": 0, "char_end": 4}])
            doc_svc.soft_delete(b.id)  # 重建期间被软删
            task = {"done": 0, "failed": 0, "errors": [], "current_doc": None}
            await dim_check._backfill_missing_vectors(
                kb_id, task, vec, FakeEmb(), snapshot_ids=set())
            metas = [m for _, _, m in vec.get_all(kb_id)]
            assert len(metas) == 1
            assert metas[0]["doc_active"] is False

        asyncio.run(_run())


class FakeEmb:
    """离线伪 embedding（模块级：供 asyncio.run 内嵌使用）"""

    async def embed(self, texts):
        return [char_vector(t) for t in texts]


class TestRebuildIncrementalIntegration:

    def _slow_embedding(self, monkeypatch, sleep=0.3):
        """慢速 embedding：放大重建任务与并发 ingest 的窗口"""
        from backend.services import (embedding_service, ingestion_service,
                                      retrieval_service)

        class SlowEmb:
            async def embed(self, texts):
                await asyncio.sleep(sleep)
                return [char_vector(t) for t in texts]

        fake_getter = lambda: SlowEmb()  # noqa: E731
        for mod in (embedding_service, ingestion_service, retrieval_service):
            monkeypatch.setattr(mod, "get_embedding_service", fake_getter)

    def test_rebuild_keeps_newly_ingested_vectors(self, client, admin_headers,
                                                  monkeypatch):
        """重建进行中（后台任务）新入库文档：重建完成时向量存在可检索"""
        from backend.services.vector_store import get_vector_store

        self._slow_embedding(monkeypatch)
        kb = create_kb(client)
        doc_a = upload_and_ingest(client, kb["id"], filename="A.txt")
        resp = client.post(f"/api/kbs/{kb['id']}/rebuild-vectors",
                           headers=admin_headers)
        assert resp.status_code == 200
        # 重建任务后台进行中：上传并入库新文档 B
        doc_b = upload_and_ingest(client, kb["id"], filename="B.txt",
                                  content="# B 文档\n\n重建期间入库的内容。")
        st = wait_rebuild_done(client, kb["id"], admin_headers)
        assert st["running"] is False and st["failed"] == 0
        assert st["done"] >= 1
        # B 的向量在 collection 中（重建完成不丢重建期间新入库的文档）
        doc_ids = {m.get("document_id")
                   for _, _, m in get_vector_store().get_all(kb["id"])}
        assert doc_a["id"] in doc_ids
        assert doc_b["id"] in doc_ids
