"""Embedding 服务：AsyncOpenAI 调用 bge-m3（dim=1024）

- 批量 32（env: EMBEDDING_BATCH_SIZE）
- 超长截断 8000 字符（env: EMBEDDING_MAX_CHARS）
- 失败重试 1 次
- 运行时读取 get_active_config()（阶段2 配置档案即时生效）
"""
from __future__ import annotations

import logging
from typing import List

from openai import AsyncOpenAI

from backend.config import get_active_config
from backend.services.llm_client import get_llm_client

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding 调用失败（重试后仍失败）

    可预期失败（服务不可用/超时/限流等）：调用方捕获后按业务降级或写回
    明确错误（ingestion mark_failed / retrieval 包装 RetrievalUnavailableError）。
    重试后仍失败属服务故障，日志级别 error。
    """


class EmbeddingService:

    def __init__(self):
        pass

    def _get_client(self) -> AsyncOpenAI:
        """按配置 key 比对自动重建（委托统一工厂 get_llm_client）

        EmbeddingConfig → 4 字段 dict + 显式 timeout 覆盖：缓存 key 为 4
        字段 JSON 序列化，任一字段（地址/密钥/模型/超时）变化即重建——
        与历史 `|` 拼接 key 的重建时机完全等价。保留实例方法签名。
        """
        cfg = get_active_config().embedding
        return get_llm_client(
            {"base_url": cfg.base_url, "api_key": cfg.api_key,
             "model": cfg.model, "timeout": cfg.timeout},
            timeout=cfg.timeout)

    def _truncate(self, text: str) -> str:
        cfg = get_active_config().embedding
        if len(text) > cfg.max_chars:
            return text[:cfg.max_chars]
        return text

    async def embed(self, texts: List[str]) -> List[List[float]]:
        """批量向量化，返回与输入等长的向量列表"""
        if not texts:
            return []
        cfg = get_active_config().embedding
        client = self._get_client()
        results: List[List[float]] = []
        batch_size = cfg.batch_size
        for i in range(0, len(texts), batch_size):
            batch = [self._truncate(t) for t in texts[i:i + batch_size]]
            vectors = await self._embed_batch(client, cfg.model, batch)
            results.extend(vectors)
        return results

    async def _embed_batch(self, client: AsyncOpenAI, model: str,
                           batch: List[str]) -> List[List[float]]:
        """单批调用，失败重试 1 次后抛出"""
        for attempt in range(2):
            try:
                resp = await client.embeddings.create(model=model, input=batch)
                ordered = sorted(resp.data, key=lambda x: x.index)
                return [item.embedding for item in ordered]
            except Exception as e:
                if attempt == 0:
                    logger.warning("embedding 调用失败，重试: %s", e)
                    continue
                logger.error("embedding 调用失败（重试后）: %s", e)
                raise EmbeddingError(
                    f"embedding 调用失败（重试后）: {e}") from e


_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
