"""B4: 后台任务并发上限 测试

背景：无并发上限（无信号量），批量解析可同时打爆 MinerU；empty_trash
串行 purge 的同步部分（Chroma 删除等）阻塞事件循环，卡住其他请求。
修复：
- run_ingestion 包模块级 asyncio.Semaphore（INGEST_CONCURRENCY 默认 3）
- _purge_document 纯同步部分放 asyncio.to_thread

覆盖：
- 同时触发 5 个 ingest → embedding 并发峰值 ≤3
- empty_trash 期间其他请求不被 purge 同步部分阻塞
"""
from __future__ import annotations

import asyncio
import threading
import time

from conftest import char_vector, create_kb, upload_doc, wait_for_status


class TestIngestConcurrency:

    def test_ingest_concurrency_capped(self, client, admin_headers,
                                       monkeypatch):
        """同时触发 5 个 ingest：embedding 并发峰值 ≤3（信号量生效）"""
        from backend.services import (embedding_service, ingestion_service,
                                      retrieval_service)

        class CountingEmb:
            """统计同时进行的 embed 调用数（asyncio 单线程，无需加锁）"""

            def __init__(self):
                self.active = 0
                self.peak = 0

            async def embed(self, texts):
                self.active += 1
                self.peak = max(self.peak, self.active)
                await asyncio.sleep(0.5)
                self.active -= 1
                return [char_vector(t) for t in texts]

        emb = CountingEmb()
        fake_getter = lambda: emb  # noqa: E731
        for mod in (embedding_service, ingestion_service, retrieval_service):
            monkeypatch.setattr(mod, "get_embedding_service", fake_getter)

        kb = create_kb(client)
        docs = [upload_doc(client, kb["id"], filename=f"并发{i}.txt",
                           content=f"第{i}个并发文档的内容")
                for i in range(5)]
        for d in docs:
            resp = client.post(f"/api/kbs/{kb['id']}/documents/{d['id']}/ingest",
                               json={}, headers=admin_headers)
            assert resp.status_code == 200
        for d in docs:
            wait_for_status(client, kb["id"], d["id"], headers=admin_headers)
        # 5 个任务同池：并发不超过 3；且确实出现并发（>1）
        assert 2 <= emb.peak <= 3, f"并发峰值异常: {emb.peak}"


class TestEmptyTrashResponsive:

    def test_empty_trash_does_not_block_other_requests(self, client,
                                                       admin_headers,
                                                       monkeypatch,
                                                       mock_embedding):
        """empty_trash 的同步 purge 部分放线程池：期间其他请求不被阻塞"""
        kb = create_kb(client)
        docs = [upload_doc(client, kb["id"], filename=f"t{i}.txt",
                           content=f"回收站文档{i}")
                for i in range(3)]
        for d in docs:
            resp = client.delete(f"/api/kbs/{kb['id']}/documents/{d['id']}",
                                 headers=admin_headers)
            assert resp.status_code == 200

        # 慢速同步向量删除：放大 purge 的同步阻塞时长（修复前 3×0.4s 阻塞 loop）
        from backend.services.vector_store import get_vector_store
        vec = get_vector_store()
        orig_delete = vec.delete_by_document

        def slow_delete(kb_id, doc_id):
            time.sleep(0.4)
            return orig_delete(kb_id, doc_id)

        monkeypatch.setattr(vec, "delete_by_document", slow_delete)

        result = {}

        def do_empty():
            result["resp"] = client.post(
                f"/api/kbs/{kb['id']}/documents/trash/empty",
                headers=admin_headers)

        t = threading.Thread(target=do_empty, daemon=True)
        t.start()
        # 等 empty 请求被事件循环接管（第一个 purge 的同步删除进行中）
        time.sleep(0.3)
        start = time.monotonic()
        r = client.get("/api/kbs", headers=admin_headers)
        elapsed = time.monotonic() - start
        assert r.status_code == 200
        # 修复前同步阻塞累积 1.2s，修复后 to_thread 不占 loop
        assert elapsed < 0.5, f"其他请求被 purge 阻塞了 {elapsed:.2f}s"
        t.join(timeout=20)
        assert result["resp"].status_code == 200
        assert result["resp"].json()["count"] == 3
        trash = client.get(f"/api/kbs/{kb['id']}/documents/trash",
                           headers=admin_headers).json()
        assert trash == []
