"""混合检索（BM25+向量 RRF 融合）与 Rerank 重排序测试

覆盖：
- BM25 纯函数：中文/英文分词、停用词与单字符过滤、关键词打分排序、IDF 特性
- 混合检索集成：真实入库（mock embedding 离线），向量候选固定（monkeypatch
  vector_store.search）时 BM25 能找回向量找不到的关键词 chunk；RRF 双命中
  分数高于单命中；enable_hybrid=False 保持纯向量；入库后 BM25 索引自动重建
- Rerank：固定分数重排生效（score 变为 relevance_score、vector_score 保留）、
  失败降级不抛异常且顺序不变、默认配置不调用
- 配置兼容：旧档案（无 enable_hybrid/rerank 字段）加载不报错且默认值正确
全部离线（conftest 的 mock_embedding + admin 登录态）。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from conftest import create_kb, upload_and_ingest

from backend.config import RerankConfig, RetrievalConfig, get_active_config
from backend.services.bm25 import BM25Index, tokenize
from backend.services.rerank_client import RerankClient
from backend.services.retrieval_service import get_retrieval_service
from backend.services.vector_store import get_vector_store

# 测试文档（一个含独特关键词，另一个常规内容）
KW_TEXT = "量子霸权是量子计算领域的核心概念。"
OTHER_TEXT = "Python 是一种高级编程语言，强调代码可读性。"
INGEST_BODY = {"chunk_size": 100, "overlap": 0}


def _ingest_two_docs(client, kb_id):
    """入库关键词文档 + 普通文档，各 1 个 chunk"""
    upload_and_ingest(client, kb_id, filename="关键词文档.txt",
                      content=KW_TEXT, ingest_body=INGEST_BODY)
    upload_and_ingest(client, kb_id, filename="普通文档.txt",
                      content=OTHER_TEXT, ingest_body=INGEST_BODY)


def _real_items(kb_id):
    """从真实 chroma 拉全量 (id, text, meta)"""
    return get_vector_store().get_all(kb_id)


class _FixedVecStore:
    """包装真实 vector_store：search 返回固定候选，count/get_all 走真实（BM25 用）"""

    def __init__(self, real, fixed_hits):
        self._real = real
        self._hits = list(fixed_hits)

    def search(self, kb_id, query_emb, top_k=5, where=None):
        # 与真实 Chroma where 语义一致：doc_active=True 过滤（缺失键视为活跃）
        if where is not None and where.get("doc_active") is True:
            return [h for h in self._hits[:top_k]
                    if (h[2] or {}).get("doc_active", True)]
        return self._hits[:top_k]

    def count(self, kb_id):
        return self._real.count(kb_id)

    def get_embedding_dimension(self, kb_id):
        return self._real.get_embedding_dimension(kb_id)

    def get_all(self, kb_id):
        return self._real.get_all(kb_id)


class _FakeHttpxClient:
    """伪 httpx.AsyncClient：post 返回固定 JSON 响应（测 rerank 响应解析用）"""

    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        return SimpleNamespace(status_code=self._status, text=str(self._payload),
                               json=lambda: self._payload)


class _FakeRerankClient:
    """伪 rerank 客户端：按文本内容给固定分数；fail=True 模拟服务失败"""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def is_enabled(self, cfg):
        return True

    async def rerank(self, query, documents, model="", base_url="", top_n=None):
        self.calls.append((query, list(documents)))
        if self.fail:
            return None
        return [1.0 if "Python" in d else 0.1 for d in documents]


def _enable_rerank_config(monkeypatch, top_n=10):
    """注入检索配置：混合开启 + rerank 启用（base_url/model 非空）"""
    rr = RerankConfig(enabled=True, base_url="http://rerank.local/v1",
                      model="rerank-model", top_n=top_n)
    cfg = SimpleNamespace(retrieval=RetrievalConfig(
        top_k=5, enable_hybrid=True, rerank=rr))
    monkeypatch.setattr("backend.services.retrieval_service.get_active_config",
                        lambda: cfg)
    return cfg


# ==================== BM25 纯函数 ====================

class TestBM25:

    def test_tokenize_chinese_english_filter(self):
        """中文按词切分、英文小写、过滤停用词/单字符/标点"""
        toks = tokenize("Python 是一种高级编程语言，AI 很强大！")
        assert "python" in toks and "ai" in toks
        assert "是" not in toks and "的" not in toks  # 停用词
        assert "强大" in toks
        toks2 = tokenize("量子霸权 q a b 。。")
        assert "量子" in toks2 or "霸权" in toks2
        assert "q" not in toks2 and "a" not in toks2 and "b" not in toks2  # 单字符
        assert "。。" not in toks2  # 纯标点

    def test_tokenize_stopwords_only_empty(self):
        """全停用词 query 分词为空（检索侧退化为纯向量）"""
        assert tokenize("的 了 是 什么 怎么") == []

    def test_keyword_chunk_scores_higher(self):
        """含 query 关键词的 chunk 分数显著高于无关 chunk"""
        index = BM25Index([KW_TEXT, OTHER_TEXT, "今日天气晴朗，适合户外运动。"])
        scores = index.scores(tokenize("量子霸权"))
        assert scores[0] > 0
        assert scores[0] > scores[1] and scores[0] > scores[2]
        assert scores[1] == 0.0 and scores[2] == 0.0  # 无任何命中词

    def test_search_returns_sorted_topk(self):
        """search 返回 (doc_idx, score) 降序，仅含命中文档，top_k 截断"""
        index = BM25Index(["苹果香蕉", "苹果", "香蕉橙子"])
        hits = index.search(tokenize("苹果"), top_k=10)
        hit_ids = [i for i, _ in hits]
        assert 0 in hit_ids and 1 in hit_ids
        assert 2 not in hit_ids  # 不含"苹果"
        assert all(sc > 0 for _, sc in hits)
        assert hits[0][1] >= hits[1][1]  # 降序
        assert len(index.search(tokenize("苹果"), top_k=1)) == 1

    def test_rare_term_higher_idf(self):
        """罕见词（df 小）对该文档的得分贡献高于高频词"""
        index = BM25Index(["量子霸权理论", "编程语言设计", "自然语言处理"])
        s_rare = index.scores(tokenize("量子"))
        s_common = index.scores(tokenize("语言"))
        assert s_rare[0] > s_common[1], "罕见词 IDF 更高，得分应更高"


# ==================== 混合检索集成 ====================

class TestHybridRetrieval:

    def test_hybrid_finds_keyword_chunk(self, client, mock_embedding,
                                        admin_headers, monkeypatch):
        """混合模式找回纯向量找不到的关键词 chunk（向量候选固定为另一篇）"""
        kb = create_kb(client)
        _ingest_two_docs(client, kb["id"])
        items = _real_items(kb["id"])
        kw = next(it for it in items if "量子" in it[1])
        other = next(it for it in items if "量子" not in it[1])
        # 向量候选只返回 other（含关键词的 chunk 不在向量结果 → 纯向量找不到）
        monkeypatch.setattr(
            "backend.services.retrieval_service.get_vector_store",
            lambda: _FixedVecStore(get_vector_store(),
                                   [(other[0], other[1], other[2], 0.95)]))

        svc = get_retrieval_service()
        # 纯向量模式：关键词 chunk 不在结果
        pure = asyncio.run(svc.retrieve(kb["id"], "量子霸权是什么", top_k=5,
                                        enable_hybrid=False))
        assert pure and pure[0].text == other[1]
        assert not any("量子" in s.text for s in pure), "纯向量模式不应命中关键词 chunk"

        # 混合模式：BM25 命中关键词 chunk（vector_score=None），且排在向量命中之后
        hybrid = asyncio.run(svc.retrieve(kb["id"], "量子霸权是什么", top_k=5))
        assert any(s.text == kw[1] for s in hybrid), "混合模式应找回关键词 chunk"
        kw_src = next(s for s in hybrid if s.text == kw[1])
        assert kw_src.vector_score is None, "BM25 单独命中无向量分数"
        assert 0 < kw_src.score <= 1
        # BM25 索引构建自真实 collection（count/get_all 未被替换）
        assert any(s.vector_score == 0.95 for s in hybrid)

    def test_rrf_fusion_double_hit_ranks_first(self, client, mock_embedding,
                                               admin_headers, monkeypatch):
        """RRF 融合：双命中（向量+BM25）的 chunk 分数高于单命中"""
        kb = create_kb(client)
        _ingest_two_docs(client, kb["id"])
        items = _real_items(kb["id"])
        kw = next(it for it in items if "量子" in it[1])
        other = next(it for it in items if "量子" not in it[1])
        # 向量候选：other 第 1、kw 第 2；BM25：kw 命中（RRF k=60）
        fixed = [(other[0], other[1], other[2], 0.95),
                 (kw[0], kw[1], kw[2], 0.80)]
        monkeypatch.setattr(
            "backend.services.retrieval_service.get_vector_store",
            lambda: _FixedVecStore(get_vector_store(), fixed))

        svc = get_retrieval_service()
        sources = asyncio.run(svc.retrieve(kb["id"], "量子霸权", top_k=5))
        assert len(sources) == 2
        assert sources[0].text == kw[1], "双命中 chunk 应排第一"
        # RRF: kw = 1/61 + 1/62（双命中）；other = 1/61（单命中）
        assert abs(sources[0].score - (1 / 61 + 1 / 62)) < 0.001
        assert sources[1].score == round(1 / 61, 4)
        assert sources[0].vector_score == 0.8  # 保留原向量分数

    def test_hybrid_empty_kb_returns_empty(self, client, mock_embedding,
                                           admin_headers):
        """空库混合检索返回空列表（不报错）"""
        kb = create_kb(client)
        svc = get_retrieval_service()
        assert asyncio.run(svc.retrieve(kb["id"], "任何问题", top_k=5)) == []

    def test_bm25_index_rebuilt_after_ingest(self, client, mock_embedding,
                                             admin_headers):
        """入库新文档后 BM25 索引自动重建（collection 计数变化），命中新关键词"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], content=KW_TEXT,
                          ingest_body=INGEST_BODY)
        svc = get_retrieval_service()
        first = asyncio.run(svc.retrieve(kb["id"], "量子霸权", top_k=5))
        assert any("量子" in s.text for s in first)
        # 再入库含新关键词的文档
        upload_and_ingest(client, kb["id"], filename="新材料.txt",
                          content="石墨烯是一种新型二维材料。",
                          ingest_body=INGEST_BODY)
        second = asyncio.run(svc.retrieve(kb["id"], "石墨烯", top_k=5))
        assert any("石墨烯" in s.text for s in second), \
            "入库后 BM25 索引应重建并命中新文档关键词"

    def test_bm25_index_rebuilt_after_doc_delete(self, client, mock_embedding,
                                                 admin_headers):
        """删除文档后检索不返回已删 chunk（BM25 缓存按 collection 计数变化自动重建）"""
        kb = create_kb(client)
        _ingest_two_docs(client, kb["id"])
        items = _real_items(kb["id"])
        kw = next(it for it in items if "量子" in it[1])
        svc = get_retrieval_service()
        first = asyncio.run(svc.retrieve(kb["id"], "量子霸权", top_k=5))
        assert any(s.text == kw[1] for s in first)
        # 删除含关键词的文档（count 变化 → 下次检索自动重建 BM25）
        get_vector_store().delete_by_document(kb["id"], kw[2]["document_id"])
        second = asyncio.run(svc.retrieve(kb["id"], "量子霸权", top_k=5))
        assert second, "剩余文档仍可检索"
        assert not any(s.text == kw[1] for s in second), \
            "删除后不应返回已删 chunk"

    def test_concurrent_retrieve_builds_bm25_once(self, client, mock_embedding,
                                                  admin_headers, monkeypatch):
        """并发检索同时 miss 缓存：BM25 索引只构建一次，均正常返回"""
        import backend.services.retrieval_service as rs_mod
        kb = create_kb(client)
        _ingest_two_docs(client, kb["id"])
        real_cls = rs_mod.BM25Index
        builds = []

        def counting_cls(texts):
            builds.append(1)
            return real_cls(texts)

        monkeypatch.setattr(rs_mod, "BM25Index", counting_cls)
        svc = get_retrieval_service()

        async def _run_two():
            return await asyncio.gather(
                svc.retrieve(kb["id"], "量子霸权", top_k=5),
                svc.retrieve(kb["id"], "量子霸权", top_k=5))

        results = asyncio.run(_run_two())
        assert len(results) == 2
        for r in results:
            assert any("量子" in s.text for s in r)
        assert len(builds) == 1, "并发下 BM25 索引应只构建一次"

    def test_bm25_only_weak_match_filtered_by_threshold(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """BM25-only 弱匹配（归一化分低于阈值）被过滤，强关键词命中保留"""
        WEAK_TEXT = ("Python 是一种高级编程语言，强调代码可读性和简洁性，"
                     "广泛应用于互联网软件开发与数据处理领域。")
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], filename="强命中.txt",
                          content=KW_TEXT, ingest_body=INGEST_BODY)
        upload_and_ingest(client, kb["id"], filename="弱匹配.txt",
                          content=WEAK_TEXT, ingest_body=INGEST_BODY)
        items = _real_items(kb["id"])
        strong = next(it for it in items if "量子" in it[1])
        weak = next(it for it in items if "Python" in it[1])
        # 预校验：强命中 BM25 分最高、弱匹配归一化分低于 1（文档文本改动时测试仍自洽）
        index = BM25Index([strong[1], weak[1]])
        raw = index.scores(tokenize("量子霸权 可读性"))
        assert raw[0] > raw[1] > 0, f"预校验失败 raw={raw}"
        norm_weak = raw[1] / raw[0]
        assert norm_weak < 0.9, f"预校验失败 norm_weak={norm_weak}"
        threshold = (1.0 + norm_weak) / 2  # 介于弱匹配归一化分与 1.0 之间
        # 向量候选只返回强命中 → 弱匹配 chunk 走 BM25-only 路径
        monkeypatch.setattr(
            "backend.services.retrieval_service.get_vector_store",
            lambda: _FixedVecStore(get_vector_store(),
                                   [(strong[0], strong[1], strong[2], 0.95)]))
        svc = get_retrieval_service()
        sources = asyncio.run(svc.retrieve(kb["id"], "量子霸权 可读性",
                                           top_k=5, min_score=threshold))
        texts = [s.text for s in sources]
        assert strong[1] in texts, "强关键词命中不应被误杀"
        assert weak[1] not in texts, "BM25-only 弱匹配应被阈值过滤"

    def test_retrieve_api_hybrid_default(self, client, mock_embedding,
                                         admin_headers):
        """默认配置（混合开启）下 retrieve 接口契约不变：{sources: [...]} + vector_score 可选字段"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "Python 是什么语言？",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "sources" in data and data["sources"]
        s = data["sources"][0]
        assert "vector_score" in s, "Source 应含 vector_score 可选字段"
        assert 0 <= s["score"] <= 1


# ==================== Rerank ====================

class TestRerank:

    def test_rerank_reorders_and_sets_score(self, client, mock_embedding,
                                            admin_headers, monkeypatch):
        """rerank 重排生效：顺序按 relevance_score 变化，score 变为 relevance_score，
        vector_score 保留原向量分数"""
        _enable_rerank_config(monkeypatch)
        fake = _FakeRerankClient()
        monkeypatch.setattr("backend.services.retrieval_service.get_rerank_client",
                            lambda: fake)
        kb = create_kb(client)
        _ingest_two_docs(client, kb["id"])
        items = _real_items(kb["id"])
        kw = next(it for it in items if "量子" in it[1])
        other = next(it for it in items if "量子" not in it[1])
        fixed = [(other[0], other[1], other[2], 0.95),
                 (kw[0], kw[1], kw[2], 0.80)]
        monkeypatch.setattr(
            "backend.services.retrieval_service.get_vector_store",
            lambda: _FixedVecStore(get_vector_store(), fixed))

        svc = get_retrieval_service()
        sources = asyncio.run(svc.retrieve(kb["id"], "量子霸权", top_k=5))
        # 混合结果 kw 第一（双命中）；rerank 给 other 高分 → other 反超到第一
        assert fake.calls and fake.calls[0][0] == "量子霸权"
        assert len(fake.calls[0][1]) == 2, "候选应为 top_n 内的全部 sources"
        assert sources[0].text == other[1], "rerank 后高分文档应排第一"
        assert sources[0].score == 1.0 and sources[1].score == 0.1, \
            "score 应变为 rerank relevance_score"
        assert sources[0].vector_score == 0.95, "vector_score 应保留原向量分数"

    def test_rerank_failure_degrades(self, client, mock_embedding,
                                     admin_headers, monkeypatch):
        """rerank 调用失败：降级保留原顺序与融合分数，不抛异常"""
        _enable_rerank_config(monkeypatch)
        fake = _FakeRerankClient(fail=True)
        monkeypatch.setattr("backend.services.retrieval_service.get_rerank_client",
                            lambda: fake)
        kb = create_kb(client)
        _ingest_two_docs(client, kb["id"])
        items = _real_items(kb["id"])
        kw = next(it for it in items if "量子" in it[1])
        other = next(it for it in items if "量子" not in it[1])
        fixed = [(other[0], other[1], other[2], 0.95),
                 (kw[0], kw[1], kw[2], 0.80)]
        monkeypatch.setattr(
            "backend.services.retrieval_service.get_vector_store",
            lambda: _FixedVecStore(get_vector_store(), fixed))

        svc = get_retrieval_service()
        sources = asyncio.run(svc.retrieve(kb["id"], "量子霸权", top_k=5))
        assert sources, "降级后仍应返回结果"
        assert sources[0].text == kw[1], "失败应保持混合融合顺序（kw 双命中第一）"
        assert abs(sources[0].score - (1 / 61 + 1 / 62)) < 0.001, \
            "失败后 score 保持融合分数而非 relevance_score"

    def test_rerank_skipped_when_disabled(self, client, mock_embedding,
                                          admin_headers, monkeypatch):
        """默认配置（rerank.enabled=False）下不调用 rerank 服务"""
        fake = _FakeRerankClient()
        monkeypatch.setattr("backend.services.retrieval_service.get_rerank_client",
                            lambda: fake)
        kb = create_kb(client)
        _ingest_two_docs(client, kb["id"])
        svc = get_retrieval_service()
        sources = asyncio.run(svc.retrieve(kb["id"], "量子霸权", top_k=5))
        assert sources
        assert fake.calls == [], "rerank 未启用时不应调用服务"

    def test_rerank_all_zero_scores_degrades_to_original(
            self, client, mock_embedding, admin_headers, monkeypatch):
        """rerank 返回全 0 分数：判定失败，检索降级保留原融合顺序与分数"""
        import httpx
        _enable_rerank_config(monkeypatch)
        fake_http = _FakeHttpxClient({"results": [
            {"index": 0, "relevance_score": 0.0},
            {"index": 1, "relevance_score": 0.0},
        ]})
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake_http)
        kb = create_kb(client)
        _ingest_two_docs(client, kb["id"])
        items = _real_items(kb["id"])
        kw = next(it for it in items if "量子" in it[1])
        other = next(it for it in items if "量子" not in it[1])
        fixed = [(other[0], other[1], other[2], 0.95),
                 (kw[0], kw[1], kw[2], 0.80)]
        monkeypatch.setattr(
            "backend.services.retrieval_service.get_vector_store",
            lambda: _FixedVecStore(get_vector_store(), fixed))
        svc = get_retrieval_service()
        sources = asyncio.run(svc.retrieve(kb["id"], "量子霸权", top_k=5))
        assert sources[0].text == kw[1], "全 0 降级后保持融合顺序（kw 双命中第一）"
        assert abs(sources[0].score - (1 / 61 + 1 / 62)) < 0.001, \
            "降级后 score 保持融合分数而非被 0 覆盖"

    def test_rerank_all_zero_scores_returns_none(self, monkeypatch):
        """RerankClient：全 0 分数响应判定失败（None）"""
        import httpx
        fake_http = _FakeHttpxClient({"results": [
            {"index": 0, "relevance_score": 0.0},
            {"index": 1, "relevance_score": 0.0},
            {"index": 2, "relevance_score": 0.0},
        ]})
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake_http)
        r = RerankClient()
        assert asyncio.run(r.rerank("q", ["a", "b", "c"], model="m",
                                    base_url="http://x/v1")) is None

    def test_rerank_insufficient_valid_returns_none(self, monkeypatch):
        """RerankClient：有效条目不足半数判定失败（None）"""
        import httpx
        fake_http = _FakeHttpxClient(
            {"results": [{"index": 0, "relevance_score": 0.9}]})
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake_http)
        r = RerankClient()
        assert asyncio.run(r.rerank("q", ["a", "b", "c"], model="m",
                                    base_url="http://x/v1")) is None

    def test_rerank_missing_single_keeps_original_order(self, monkeypatch):
        """RerankClient：仅个别缺失时按有效分最小值补齐，保持原顺序排在有效项之后"""
        import httpx
        fake_http = _FakeHttpxClient({"results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 2, "relevance_score": 0.5},
        ]})
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake_http)
        r = RerankClient()
        scores = asyncio.run(r.rerank("q", ["a", "b", "c"], model="m",
                                      base_url="http://x/v1"))
        assert scores == [0.5, 0.9, 0.5], "缺失项补齐后不改变顺序"


# ==================== 配置兼容 ====================

class TestConfigCompatibility:

    def _write_old_settings_json(self):
        """构造旧格式 settings.json（retrieval 段无 enable_hybrid/rerank）"""
        from backend.services import settings_service as ss
        old_profile = {
            "id": "old01",
            "name": "旧档案",
            "active": True,
            "llm": {"base_url": "http://x/v1", "api_key": "k", "model": "m",
                    "temperature": 0.3, "max_tokens": 1024},
            "embedding": {"base_url": "http://x/v1", "api_key": "k",
                          "model": "b", "dimension": 1024},
            "mineru": {"url": "http://x:8001", "timeout": 300.0},
            "retrieval": {"top_k": 3, "similarity_threshold": 0.0},
            "chunking": {"chunk_size": 800, "overlap": 100},
            "chat": {"history_rounds": 8},
            "mysql": {"host": "127.0.0.1", "port": 3306, "user": "u",
                      "password": "p", "database": "d", "url": ""},
            "minio": {"endpoint": "127.0.0.1:9000", "access_key": "a",
                      "secret_key": "s", "bucket": "b", "secure": False,
                      "region": ""},
        }
        ss.SETTINGS_FILE.write_text(json.dumps(
            {"profiles": [old_profile], "active_id": "old01"},
            ensure_ascii=False), encoding="utf-8")
        return ss

    def test_old_settings_json_loads_with_defaults(self, client):
        """旧档案文件（无新字段）加载不报错，默认值正确补齐"""
        ss = self._write_old_settings_json()
        svc = ss.SettingsService()  # _load -> _coerce 补默认 + _apply_active
        p = svc.get_profile("old01")
        assert p["retrieval"]["enable_hybrid"] is True
        assert p["retrieval"]["rerank"] == {
            "enabled": False, "base_url": "", "model": "", "top_n": 10}
        cfg = get_active_config().retrieval
        assert cfg.enable_hybrid is True
        assert cfg.rerank.enabled is False and cfg.rerank.top_n == 10
        assert cfg.rerank.base_url == "" and cfg.rerank.model == ""

    def test_create_profile_old_style_gets_defaults(self, client, admin_headers):
        """API 创建只传旧检索字段：返回与运行时配置均带正确默认值"""
        resp = client.post("/api/settings/profiles", json={
            "name": "旧样式检索",
            "retrieval": {"top_k": 3, "similarity_threshold": 0.0},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        r = resp.json()["retrieval"]
        assert r["enable_hybrid"] is True
        assert r["rerank"] == {"enabled": False, "base_url": "",
                               "model": "", "top_n": 10}
        pid = resp.json()["id"]
        assert client.post(f"/api/settings/profiles/{pid}/activate",
                           headers=admin_headers).status_code == 200
        cfg = get_active_config().retrieval
        assert cfg.enable_hybrid is True
        assert cfg.rerank.enabled is False and cfg.rerank.top_n == 10

    def test_update_rerank_partial_keeps_defaults(self, client, admin_headers):
        """rerank 部分字段更新不丢其他字段默认值（字段级合并）"""
        resp = client.post("/api/settings/profiles", json={
            "name": "rerank 部分更新",
            "retrieval": {"rerank": {"enabled": True}},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        pid = resp.json()["id"]
        rr = resp.json()["retrieval"]["rerank"]
        assert rr["enabled"] is True
        assert rr["base_url"] == "" and rr["model"] == ""
        assert rr["top_n"] == 10
        resp = client.put(f"/api/settings/profiles/{pid}",
                          json={"retrieval": {"rerank": {"top_n": 20}}},
                          headers=admin_headers)
        rr = resp.json()["retrieval"]["rerank"]
        assert rr["top_n"] == 20 and rr["enabled"] is True, "部分更新不应丢已设字段"
