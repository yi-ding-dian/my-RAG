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

logger = logging.getLogger(__name__)


class EmbeddingService:

    def __init__(self):
        self._client: AsyncOpenAI | None = None
        self._client_key: str | None = None

    def _get_client(self) -> AsyncOpenAI:
        """按配置 key 比对自动重建：前端改 Embedding 配置（地址/密钥/模型）后即时生效，无需重启"""
        cfg = get_active_config().embedding
        key = f"{cfg.base_url}|{cfg.api_key}|{cfg.model}|{cfg.timeout}"
        if self._client is None or self._client_key != key:
            self._client = AsyncOpenAI(
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                timeout=cfg.timeout,
            )
            self._client_key = key
        return self._client

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
                raise


_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
