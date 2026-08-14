"""P1-1 统计切块数与软删一致性测试

问题：stats 用 Chroma count 求和（含软删文档的向量），知识库页 chunk_count 用
document_service.chunk_count_by_kb（排除软删），两侧数字不一致。
修复：stats 改用 document_service 统计。本测试断言软删前后两处始终一致。
"""
from __future__ import annotations

from conftest import create_kb, upload_and_ingest


class TestStatsChunkConsistency:

    def test_stats_chunk_count_matches_kb_detail(self, client, mock_embedding,
                                                 admin_headers):
        """入库后：/api/stats 与知识库详情的 chunk_count 一致"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"], filename="统计测试.txt",
                                ingest_body={"method": "title"})  # 多块
        n = doc["chunk_count"]
        assert n >= 3

        stats = client.get("/api/stats", headers=admin_headers).json()
        kb_detail = client.get(f"/api/kbs/{kb['id']}",
                               headers=admin_headers).json()
        assert stats["chunk_count"] == kb_detail["chunk_count"] == n

    def test_soft_delete_keeps_both_consistent(self, client, mock_embedding,
                                               admin_headers):
        """软删文档后：两处同时排除软删文档的切块（Chroma count 会含软删向量）"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"], filename="软删测试.txt",
                                ingest_body={"method": "title"})
        n = doc["chunk_count"]

        resp = client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200

        stats = client.get("/api/stats", headers=admin_headers).json()
        kb_detail = client.get(f"/api/kbs/{kb['id']}",
                               headers=admin_headers).json()
        # 软删后文档不计入 chunk_count（两侧一致为 0，而非 Chroma 残留的 n）
        assert stats["chunk_count"] == kb_detail["chunk_count"] == 0

    def test_multi_kb_stats_sum(self, client, mock_embedding, admin_headers):
        """多知识库求和：stats 总数 = 各 kb 详情之和"""
        total = 0
        for i in range(2):
            kb = create_kb(client, name=f"统计库{i}")
            doc = upload_and_ingest(client, kb["id"],
                                    filename=f"文档{i}.txt",
                                    ingest_body={"method": "title"})
            total += doc["chunk_count"]
        stats = client.get("/api/stats", headers=admin_headers).json()
        assert stats["chunk_count"] == total
        assert stats["doc_count"] == 2
