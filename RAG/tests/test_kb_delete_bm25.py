"""P2-8 删除知识库须失效 BM25 缓存测试

问题：DELETE /api/kbs/{kb_id} 级联删除未调用 invalidate_bm25，内存中残留
已删 kb 的 BM25 索引（重建同 id 的新 kb 会复用旧索引）。
修复：删除 KB 成功后调用 get_retrieval_service().invalidate_bm25(kb_id)。
"""
from __future__ import annotations

from conftest import create_kb, upload_and_ingest


class TestDeleteKbInvalidatesBm25:

    def test_delete_kb_invalidates_bm25(self, client, mock_embedding,
                                        admin_headers):
        """删除知识库 → invalidate_bm25(kb_id) 被调用"""
        from backend.services.retrieval_service import get_retrieval_service
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])  # 构建向量（内部也会失效一次 BM25）

        svc = get_retrieval_service()
        calls: list = []
        orig = svc.invalidate_bm25

        def spy(kid):
            calls.append(kid)
            return orig(kid)

        svc.invalidate_bm25 = spy  # 装上监听后再删库

        resp = client.delete(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert kb["id"] in calls, "删除知识库后必须失效该 kb 的 BM25 索引"

    def test_delete_kb_without_docs_still_invalidates(self, client,
                                                      admin_headers):
        """空知识库删除同样失效 BM25（幂等，不报错）"""
        from backend.services.retrieval_service import get_retrieval_service
        kb = create_kb(client)
        svc = get_retrieval_service()
        calls: list = []
        orig = svc.invalidate_bm25
        svc.invalidate_bm25 = lambda kid: calls.append(kid) or orig(kid)

        resp = client.delete(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert kb["id"] in calls
