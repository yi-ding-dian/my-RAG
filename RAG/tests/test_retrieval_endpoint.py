"""检索调试接口参数测试：向后兼容 + 参数覆盖

覆盖：无参数默认行为不变（含 Source 新增偏移/向量分字段）、top_k 覆盖生效、
similarity_threshold 阈值覆盖生效（全过滤/不过滤）、混合检索开关切换
（True=BM25+向量 RRF 融合；False=纯向量，score 语义为向量分数）、重排
参数接受（未配置服务时降级不报错）。全部离线（mock embedding）。
"""
from __future__ import annotations

from conftest import create_kb, upload_and_ingest


class TestRetrieveParams:
    """检索调试接口参数"""

    def _kb_with_title_chunks(self, client):
        """建库并 title 切块入库（多块，便于验证 top_k/阈值差异）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], ingest_body={
            "method": "title", "split_level": 2,
        })
        return kb

    def _retrieve(self, client, headers, kb_id, query="Python 是什么语言？", **extra):
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb_id, "query": query, **extra,
        }, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["sources"]

    def test_default_behavior_unchanged(self, client, mock_embedding, admin_headers):
        """无额外参数：行为与既有契约一致，sources 结构完整（含偏移/向量分新字段）"""
        kb = self._kb_with_title_chunks(client)
        sources = self._retrieve(client, admin_headers, kb["id"])
        assert sources, "入库后检索应至少命中一条"
        s = sources[0]
        for field in ("id", "text", "score", "document_id", "document_name",
                      "kb_id", "chunk_index", "vector_score",
                      "char_start", "char_end"):
            assert field in s, f"Source 缺字段: {field}"
        assert s["kb_id"] == kb["id"]
        assert 0 <= s["score"] <= 1
        assert s["vector_score"] is None or 0 <= s["vector_score"] <= 1

    def test_source_char_offsets_from_metadata(self, client, mock_embedding,
                                               admin_headers):
        """命中 Source 的 char_start/char_end 来自入库 metadata（偏移切片可还原）"""
        kb = self._kb_with_title_chunks(client)
        sources = self._retrieve(client, admin_headers, kb["id"])
        assert sources
        for s in sources:
            assert isinstance(s["char_start"], int) and s["char_start"] >= 0, \
                f"char_start 应为非负偏移: {s['char_start']}"
            assert isinstance(s["char_end"], int) and s["char_end"] > s["char_start"]
            # 偏移定位的文本与命中文本一致（全文切片验证）
            doc = client.get(f"/api/kbs/{kb['id']}/documents/{s['document_id']}",
                             headers=admin_headers).json()
            full = doc.get("full_text") or ""
            assert full, "入库后详情应返回 full_text"
            assert full[s["char_start"]:s["char_end"]] == s["text"], \
                "char_start/char_end 切片应与命中文本一致"

    def test_top_k_override(self, client, mock_embedding, admin_headers):
        """top_k 覆盖生效：1 → 只回 1 条；放大 → 不少于默认"""
        kb = self._kb_with_title_chunks(client)
        assert len(self._retrieve(client, admin_headers, kb["id"], top_k=1)) == 1
        default_len = len(self._retrieve(client, admin_headers, kb["id"]))
        assert len(self._retrieve(client, admin_headers, kb["id"], top_k=100)) >= default_len
        # 结果顺序：按相似度降序
        sources = self._retrieve(client, admin_headers, kb["id"], top_k=5)
        scores = [s["score"] for s in sources]
        assert scores == sorted(scores, reverse=True), "命中应按相似度降序"

    def test_similarity_threshold_override_filters_all(self, client, mock_embedding,
                                                       admin_headers):
        """similarity_threshold=1.0：全部低于阈值 → 空列表（前端显示"无命中"）"""
        kb = self._kb_with_title_chunks(client)
        sources = self._retrieve(client, admin_headers, kb["id"],
                                 similarity_threshold=1.0)
        assert sources == []

    def test_similarity_threshold_override_none_filter(self, client, mock_embedding,
                                                       admin_headers):
        """similarity_threshold=0.0：不过滤，与默认（配置阈值 0）结果一致"""
        kb = self._kb_with_title_chunks(client)
        default_ids = [s["id"] for s in self._retrieve(client, admin_headers, kb["id"])]
        zero_ids = [s["id"] for s in self._retrieve(
            client, admin_headers, kb["id"], similarity_threshold=0.0)]
        assert zero_ids == default_ids

    def test_hybrid_disabled_pure_vector_score(self, client, mock_embedding,
                                               admin_headers):
        """enable_hybrid=False：纯向量路径，score 即向量分数（=vector_score）"""
        kb = self._kb_with_title_chunks(client)
        sources = self._retrieve(client, admin_headers, kb["id"],
                                 enable_hybrid=False)
        assert sources
        for s in sources:
            assert s["score"] == s["vector_score"], \
                "纯向量模式下 score 应等于原始向量分数"

    def test_hybrid_enabled_accepted(self, client, mock_embedding, admin_headers):
        """enable_hybrid=True/rerank 参数：接受且不报错（rerank 未配置时降级）"""
        kb = self._kb_with_title_chunks(client)
        for extra in ({"enable_hybrid": True},
                      {"enable_rerank": True},
                      {"enable_rerank": False},
                      {"enable_hybrid": True, "enable_rerank": True}):
            resp = client.post("/api/chat/retrieve", json={
                "kb_id": kb["id"], "query": "Python 是什么语言？", **extra,
            }, headers=admin_headers)
            assert resp.status_code == 200, resp.text
            assert "sources" in resp.json()

    def test_combined_overrides(self, client, mock_embedding, admin_headers):
        """组合覆盖：top_k+阈值+混合/重排同时传，全部生效且互不冲突

        混合模式下阈值过滤沿用向量分数语义（BM25 单独命中的块无向量分，
        不受阈值限制），故命中数 <= top_k 且每条要么向量分达标、要么为
        BM25 单独命中（vector_score=None）。
        """
        kb = self._kb_with_title_chunks(client)
        sources = self._retrieve(client, admin_headers, kb["id"], top_k=2,
                                 similarity_threshold=0.9,
                                 enable_hybrid=True, enable_rerank=True)
        assert len(sources) <= 2
        for s in sources:
            assert s["vector_score"] is None or s["vector_score"] >= 0.9, \
                f"命中向量分应不低于覆盖阈值: {s['vector_score']}"
