"""检索服务：query embedding -> chroma search -> BM25 混合融合（RRF）-> 阈值过滤 -> Rerank

检索流程（get_retrieval_service().retrieve(kb_id, query)）：
1. query 向量化 + 向量检索 top_k 候选
2. enable_hybrid=True（默认）：再取 BM25 top_k 候选（jieba 分词，索引按 kb 惰性
   构建 + 缓存，collection 计数变化即重建；ingestion 成功后显式 invalidate），
   与向量候选并集做 RRF 融合（k=60）取 top_k；
   enable_hybrid=False：保持纯向量原逻辑
3. 阈值过滤：纯向量模式用向量相似度；混合模式沿用 vector_score 语义
   （BM25 单独命中的 chunk 无向量分数，不受阈值限制——向后兼容旧阈值配置）
4. rerank.enabled 且 base_url/model 非空时：取 top_n 候选调 rerank 服务重排，
   用 relevance_score 作为最终 score；失败（网络/超时/4xx）一律降级保留原顺序

Source.score 语义：融合后分数（纯向量模式 = 原向量分数）；vector_score 保留原始
向量分数供调试（混合模式下 BM25 单独命中为 None）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from backend.config import get_active_config
from backend.models.rag_models import Source
from backend.services.bm25 import BM25Index, tokenize
from backend.services.embedding_service import get_embedding_service
from backend.services.rerank_client import get_rerank_client
from backend.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)

# BM25 索引构建锁（模块级）：并发检索请求同时 miss 缓存时保证只构建一次
_bm25_build_lock = asyncio.Lock()


class RetrievalUnavailableError(Exception):
    """检索服务不可用（Embedding 服务调用失败/无输出）

    与"无命中"（正常返回空列表）严格区分：chat_service 捕获本异常以
    error 事件透传（"检索服务不可用：..."），检索测试接口转 500，
    而不是静默返回空结果误导用户。
    """


class RetrievalService:

    # RRF 融合参数（Reciprocal Rank Fusion，k 越大排名差异越平滑）
    RRF_K = 60

    def __init__(self):
        # BM25 索引缓存：kb_id -> (BM25Index, [(id, text, meta), ...])
        self._bm25_cache: Dict[str, Tuple[BM25Index, List[Tuple[str, str, dict]]]] = {}

    # ================= 主流程 =================

    async def retrieve(self, kb_id: str, query: str,
                       top_k: int | None = None,
                       min_score: float | None = None,
                       enable_hybrid: bool | None = None,
                       enable_rerank: bool | None = None) -> List[Source]:
        """检索知识库，返回按融合分数降序的 Source 列表（含 document_name）

        - top_k: 返回条数（默认取活跃配置 retrieval.top_k）
        - min_score: 相似度阈值，低于该分数的命中被过滤（默认取活跃配置
          retrieval.similarity_threshold；0=不过滤）
        - enable_hybrid/enable_rerank: 混合检索/重排开关（None=用配置默认；
          true/false=强制开关，供检索调试页对比实验使用）
        """
        if not query or not query.strip():
            return []
        cfg = get_active_config().retrieval
        if top_k is None:
            top_k = cfg.top_k
        if min_score is None:
            min_score = cfg.similarity_threshold
        if enable_hybrid is None:
            enable_hybrid = cfg.enable_hybrid

        # Rerank 生效条件：开关开（显式参数或配置）+ base_url/model 均非空
        rcfg = cfg.rerank
        if enable_rerank is None:
            enable_rerank = bool(rcfg.enabled)
        rerank_ready = (enable_rerank
                        and bool((rcfg.base_url or "").strip())
                        and bool((rcfg.model or "").strip()))
        if enable_hybrid is not None or enable_rerank is not None:
            logger.info("检索实验参数: kb=%s enable_hybrid=%s enable_rerank=%s "
                        "top_k=%d min_score=%.4f",
                        kb_id, enable_hybrid, enable_rerank, top_k, min_score)

        # rerank 启用时先产出更多候选（top_n），重排后再截取 top_k
        candidate_count = max(top_k, rcfg.top_n) if rerank_ready else top_k

        # 1) query 向量化（P1-2：Embedding 服务失败必须抛错，不能静默返回空
        # 结果误导"未检索到相关内容"；chat_service 已捕获并以 error 事件透传）
        emb_svc = get_embedding_service()
        try:
            query_vec = await emb_svc.embed([query])
        except Exception as e:
            logger.error("query 向量化失败: %s", e)
            raise RetrievalUnavailableError(
                f"检索服务不可用：Embedding 服务调用失败（{e.__class__.__name__}），"
                "请检查 Embedding 服务后重试") from e
        if not query_vec:
            raise RetrievalUnavailableError(
                "检索服务不可用：Embedding 服务未返回向量，请检查 Embedding 服务")

        # 2) 维度校验（P0 维度冲突检测，仅新增逻辑，不改变检索/BM25/rerank 流程）：
        # 更换 embedding 模型后 collection 旧维度向量与 query 维度不符，Chroma
        # query 抛错被 search 吞掉 → 静默"未检索到相关内容"；这里提前暴露明确错误，
        # chat_service 捕获后以 error 事件透传给用户（提示重建向量）
        vec = get_vector_store()
        dim_check = vec.get_embedding_dimension(kb_id)
        if dim_check is not None and dim_check != len(query_vec[0]):
            from backend.services.dim_check import VectorDimensionError
            raise VectorDimensionError(
                f"知识库向量维度与当前模型不匹配（collection {dim_check} 维 vs "
                f"模型 {len(query_vec[0])} 维），请在文档管理中重建向量")

        # 3) 向量检索候选（where 过滤软删文档：回收站文档 chunk 标记
        # doc_active=False，检索自动排除；历史数据由 vector_store 惰性补齐键）
        hits = vec.search(kb_id, query_vec[0], top_k=candidate_count,
                          where={"doc_active": True})

        # 3) 混合融合 / 纯向量
        if enable_hybrid:
            sources = await self._hybrid_retrieve(kb_id, query, hits,
                                                  candidate_count, min_score)
        else:
            sources = self._assemble(kb_id, query, hits, min_score)

        # 4) Rerank（启用且配置完整才执行；失败降级保留原顺序）
        if rerank_ready and sources:
            sources = await self._rerank(query, sources, rcfg, top_k)
        else:
            sources = sources[:top_k]
        logger.info("检索: kb=%s query=%s hits=%d", kb_id, query[:30], len(sources))

        # 检索质量日志（增量埋点：成功返回前统一记录，chat 问答与检索测试页
        # 共用此入口自动覆盖；日志失败仅告警不影响检索结果）
        try:
            from backend.services.retrieval_log import get_retrieval_log_service
            get_retrieval_log_service().log(
                kb_id, query,
                list(dict.fromkeys(s.document_id for s in sources
                                   if s.document_id)))
        except Exception:
            logger.exception("检索日志记录异常: kb=%s", kb_id)
        return sources

    # ================= 纯向量路径 =================

    def _assemble(self, kb_id: str, query: str, hits, min_score: float) -> List[Source]:
        """纯向量路径：阈值过滤 + Source 组装（与改造前行为一致）"""
        filtered = 0
        sources: List[Source] = []
        for cid, text, meta, score in hits:
            if score < min_score:
                filtered += 1
                continue
            sources.append(self._build_source(kb_id, cid, text, meta, score, score))
        if filtered:
            logger.info("检索过滤: kb=%s query=%s 低于阈值 %.2f 过滤 %d 条",
                        kb_id, query[:30], min_score, filtered)
        return sources

    # ================= 混合检索路径 =================

    async def _hybrid_retrieve(self, kb_id: str, query: str, vector_hits,
                               top_k: int, min_score: float) -> List[Source]:
        """BM25 + 向量 RRF 融合检索（索引惰性构建 + 缓存）"""
        bm25, items = await self._get_bm25(kb_id)
        if bm25 is None or not items:
            return self._assemble(kb_id, query, vector_hits, min_score)
        query_tokens = tokenize(query)
        if not query_tokens:
            return self._assemble(kb_id, query, vector_hits, min_score)
        # BM25 打分是 CPU 密集计算，放到线程池避免阻塞事件循环
        bm25_hits = await asyncio.to_thread(bm25.search, query_tokens,
                                            top_k=top_k)
        # 软删文档的 chunk 不进检索结果：BM25 索引为全量构建（索引大小与
        # collection count 判据耦合，不能局部过滤），这里按 metadata 的
        # doc_active 标志过滤命中（与向量路径 where 语义一致；缺失键视为活跃，
        # 兼容历史数据）
        bm25_hits = [h for h in bm25_hits
                     if (items[h[0]][2] or {}).get("doc_active", True)]
        if not bm25_hits:
            return self._assemble(kb_id, query, vector_hits, min_score)

        # doc_idx -> id/text/meta 映射（BM25 索引构建时已从 collection 全量拉取）
        ids = [it[0] for it in items]
        texts = [it[1] for it in items]
        metas = [it[2] for it in items]

        # RRF 融合：score = Σ 1/(k + rank + 1)（向量与 BM25 各一份排名贡献）
        fused: Dict[str, dict] = {}
        for rank, (cid, text, meta, vscore) in enumerate(vector_hits):
            entry = fused.setdefault(cid, {
                "rrf": 0.0, "vector_score": vscore, "text": text, "meta": meta,
            })
            entry["rrf"] += 1.0 / (self.RRF_K + rank + 1)
        for rank, (idx, bscore) in enumerate(bm25_hits):
            cid = ids[idx]
            entry = fused.setdefault(cid, {
                "rrf": 0.0, "vector_score": None,
                "text": texts[idx], "meta": metas[idx],
            })
            entry["rrf"] += 1.0 / (self.RRF_K + rank + 1)
            entry["bm25_score"] = bscore

        ordered = sorted(fused.items(), key=lambda kv: kv[1]["rrf"], reverse=True)

        # 阈值过滤（向后兼容：纯向量路径语义不变，min_score=0 不过滤）：
        # - 有向量分的条目：沿用向量分数与阈值比较（与改造前一致）
        # - BM25 单独命中（无向量分）：BM25 分归一化（除以本次查询的最大 BM25
        #   分，最强关键词命中恒为 1.0 不被误杀）后与阈值比较，弱匹配被过滤
        max_bm25 = max((sc for _, sc in bm25_hits), default=0.0)
        filtered = 0
        sources: List[Source] = []
        for cid, entry in ordered:
            vs = entry["vector_score"]
            if vs is not None:
                below = vs < min_score
            else:
                # BM25 单独命中：归一化 BM25 分（无命中词时 0，被阈值过滤）
                bscore = entry.get("bm25_score", 0.0)
                bnorm = bscore / max_bm25 if max_bm25 > 0 else 0.0
                below = bnorm < min_score
            if below:
                filtered += 1
                continue
            sources.append(self._build_source(
                kb_id, cid, entry["text"], entry["meta"], entry["rrf"], vs))
        if filtered:
            logger.info("检索过滤: kb=%s query=%s 低于阈值 %.2f 过滤 %d 条",
                        kb_id, query[:30], min_score, filtered)
        return sources

    # ================= BM25 索引缓存 =================

    async def _get_bm25(self, kb_id: str) -> Tuple[BM25Index | None, list]:
        """按 kb 惰性构建/复用 BM25 索引（collection 计数变化即重建）

        - 构建（chroma count/get_all + jieba 分词建索引）放到 asyncio.to_thread
          线程池执行，避免同步 CPU/IO 阻塞事件循环
        - 模块级锁 + 锁内 double-check：并发请求同时 miss 缓存时只构建一次
        - 构建失败缓存失败态（缓存值 None），避免反复尝试；invalidate_bm25
          或 count 变化后自然重建
        """
        vec = get_vector_store()
        count = await asyncio.to_thread(vec.count, kb_id)
        if count == 0:
            self._bm25_cache.pop(kb_id, None)
            return None, []
        cached = self._bm25_cache.get(kb_id)
        if cached is not None and cached[0].size == count:
            return cached
        if cached is None and kb_id in self._bm25_cache:
            return None, []  # 上次构建失败：不反复尝试（invalidate 后重建）
        async with _bm25_build_lock:
            # 等锁期间其他请求可能已构建完成，double-check
            cached = self._bm25_cache.get(kb_id)
            if cached is not None and cached[0].size == count:
                return cached
            try:
                items = await asyncio.to_thread(vec.get_all, kb_id)
                if not items:
                    return None, []
                bm25 = await asyncio.to_thread(BM25Index,
                                               [it[1] for it in items])
                self._bm25_cache[kb_id] = (bm25, items)
                logger.info("BM25 索引构建: kb=%s chunks=%d",
                            kb_id, len(items))
                return bm25, items
            except Exception as e:
                # 构建失败缓存失败态，避免每个请求都重复尝试
                self._bm25_cache[kb_id] = None
                logger.error("BM25 索引构建失败（缓存失败态，invalidate 后重建）: "
                             "kb=%s err=%s", kb_id, e)
                return None, []

    def invalidate_bm25(self, kb_id: str):
        """ingestion 入库成功后调用：强制下次检索重建该 kb 的 BM25 索引"""
        self._bm25_cache.pop(kb_id, None)

    # ================= Rerank =================

    async def _rerank(self, query: str, sources: List[Source],
                      rcfg, top_k: int) -> List[Source]:
        """Rerank 重排：候选取 top_n 调服务，按 relevance_score 重排截取 top_k；
        失败（返回 None）一律降级保留原顺序，绝不让问答失败"""
        if rcfg.top_n <= 0:
            return sources[:top_k]  # 配置异常（top_n<=0）跳过重排
        client = get_rerank_client()
        candidates = sources[:rcfg.top_n]
        scores = await client.rerank(
            query=query, documents=[s.text for s in candidates],
            model=rcfg.model, base_url=rcfg.base_url, top_n=rcfg.top_n)
        if scores is None:
            logger.warning("rerank 降级: kb 保持原顺序返回 %d 条", top_k)
            return sources[:top_k]
        ranked = sorted(zip(candidates, scores), key=lambda p: p[1], reverse=True)
        out: List[Source] = []
        for src, sc in ranked[:top_k]:
            src.score = round(float(sc), 4)  # 最终分数 = rerank relevance_score
            out.append(src)
        return out

    # ================= 组装 =================

    @staticmethod
    def _build_source(kb_id: str, cid: str, text: str, meta: dict,
                      score: float, vector_score: float | None) -> Source:
        """组装 Source（parent_child 模式且 retrieval_mode=parent：附父块全文作上下文）"""
        parent_text = None
        if meta.get("retrieval_mode") == "parent":
            pt = meta.get("parent_text")
            if pt:
                parent_text = pt
        return Source(
            id=cid,
            text=text,
            score=round(score, 4),
            document_id=meta.get("document_id", ""),
            document_name=meta.get("document_name", ""),
            kb_id=kb_id,
            chunk_index=int(meta.get("chunk_index", 0)),
            parent_text=parent_text,
            context=meta.get("context"),
            vector_score=round(vector_score, 4) if vector_score is not None else None,
            # 块偏移（入库时已随 metadata 落库，全部切块方式均有；
            # 历史数据缺失时 -1，检索测试页上下文截取降级为全文展示）
            char_start=int(meta.get("char_start", -1)),
            char_end=int(meta.get("char_end", -1)),
        )


_retrieval_service: RetrievalService | None = None


def get_retrieval_service() -> RetrievalService:
    global _retrieval_service
    if _retrieval_service is None:
        _retrieval_service = RetrievalService()
    return _retrieval_service
