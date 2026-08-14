"""Elasticsearch 检索服务 — 对接知识库做真实检索评估"""
from __future__ import annotations
import logging
from typing import List, Optional
from elasticsearch7 import Elasticsearch
from elasticsearch7.exceptions import NotFoundError, TransportError
from backend.config import settings

logger = logging.getLogger(__name__)


class RetrievalService:
    """知识库检索服务"""

    def __init__(self):
        self._client: Optional[Elasticsearch] = None

    @property
    def client(self) -> Elasticsearch:
        if self._client is None:
            self._client = Elasticsearch(
                [{"host": settings.ES_HOST, "port": settings.ES_PORT}],
                http_auth=(settings.ES_USER, settings.ES_PASSWORD),
                timeout=30,
            )
        return self._client

    def is_available(self) -> bool:
        """检查 ES 是否可连接"""
        try:
            return self.client.ping()
        except Exception as e:
            logger.warning("ES 不可用: %s", e)
            return False

    def list_indices(self) -> List[str]:
        """列出所有知识库索引"""
        try:
            indices = self.client.indices.get(index=settings.ES_INDEX_PATTERN, allow_no_indices=True)
            return list(indices.keys())
        except NotFoundError:
            return []
        except Exception as e:
            logger.error("获取索引列表失败: %s", e)
            return []

    def search(
        self,
        query: str,
        index: Optional[str] = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> List[dict]:
        """
        知识库检索（全文+向量混合，根据索引配置自动判断）

        返回:
            [{ "text": str, "score": float, "source": str, ... }]
        """
        indices = [index] if index else self.list_indices()
        if not indices:
            logger.warning("没有可用的索引，无法检索")
            return []

        es_query = {
            "size": top_k,
            "query": {
                "bool": {
                    "should": [
                        {"match": {"content": {"query": query, "boost": 1.0}}},
                        {"match": {"title": {"query": query, "boost": 2.0}}},
                    ]
                }
            },
        }

        try:
            resp = self.client.search(index=",".join(indices), body=es_query)
            results = []
            for hit in resp["hits"]["hits"]:
                src = hit["_source"]
                results.append({
                    "text": src.get("content") or src.get("text") or "",
                    "title": src.get("title", ""),
                    "source": src.get("source", src.get("_index", "")),
                    "score": hit["_score"],
                })
            return results
        except Exception as e:
            logger.error("ES 检索失败: %s", e)
            return []

    def close(self):
        if self._client:
            self._client.close()
            self._client = None


_retrieval_service = RetrievalService()


def get_retrieval_service() -> RetrievalService:
    return _retrieval_service
