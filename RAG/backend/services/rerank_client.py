"""Rerank 重排序客户端（OpenAI 兼容 /rerank 协议，严格降级）

- 启用条件：配置 rerank.enabled=True 且 base_url/model 均非空（缺任一即跳过，
  绝不让问答/检索失败）
- 调用：POST {base_url}/rerank
    body: {"model": ..., "query": ..., "documents": [...], "top_n": ...}
    vLLM / Cohere 兼容服务均接受此格式
- 响应兼容 {"results": [{"index", "relevance_score"}]} 与 {"data": [...]} 两种形态
- 失败（网络/超时/4xx/解析异常）一律返回 None，调用方保留原顺序，仅日志 warning
"""
from __future__ import annotations

import logging
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

# 单次 rerank 调用超时（秒）
RERANK_TIMEOUT = 10.0


class RerankClient:

    @staticmethod
    def is_enabled(cfg) -> bool:
        """启用判定：开关开启且 base_url/model 均非空（任一缺失即跳过）"""
        return (bool(cfg.enabled)
                and bool((cfg.base_url or "").strip())
                and bool((cfg.model or "").strip()))

    async def rerank(self, query: str, documents: List[str],
                     model: str = "", base_url: str = "",
                     top_n: Optional[int] = None) -> Optional[List[float]]:
        """调用 rerank 服务，返回按 documents 顺序的 relevance_score 列表

        - 成功：List[float]（与 documents 等长，按 index 归位；个别缺失项按
          有效分数最小值补齐，保持原顺序排在有效项之后）
        - 失败/响应异常：None（调用方保留原顺序降级），包括：
          有效条目（index+relevance_score 均合法）不足半数、分数全 0
        """
        if not documents:
            return []
        url = f"{str(base_url).rstrip('/')}/rerank"
        payload: dict = {"model": model, "query": query, "documents": documents}
        if top_n:
            payload["top_n"] = int(top_n)
        try:
            async with httpx.AsyncClient(timeout=RERANK_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("rerank 调用失败（HTTP %s）: %s",
                               resp.status_code, resp.text[:200])
                return None
            data = resp.json()
        except Exception as e:
            logger.warning("rerank 调用失败（降级为原顺序）: %s", e)
            return None
        # 兼容 results / data 两种响应形态
        results = data.get("results")
        if results is None:
            results = data.get("data")
        if not isinstance(results, list):
            logger.warning("rerank 响应格式异常（降级为原顺序）: %s",
                           str(data)[:200])
            return None
        scores: List[Optional[float]] = [None] * len(documents)
        valid = 0
        for item in results:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(documents)):
                continue
            raw = item.get("relevance_score")
            if raw is None:
                continue
            try:
                scores[idx] = float(raw)
            except (TypeError, ValueError):
                continue
            valid += 1
        # 有效条目不足半数 → 判定失败（响应不完整，排序失真，触发上层降级）
        if valid * 2 < len(documents):
            logger.warning("rerank 响应有效条目不足半数（%d/%d），"
                           "降级为原顺序", valid, len(documents))
            return None
        # 分数全 0 → 判定失败（无区分度，重排无意义）
        if all(sc in (None, 0.0) for sc in scores):
            logger.warning("rerank 响应分数全 0，降级为原顺序")
            return None
        # 仅个别缺失：按有效分数最小值补齐，稳定排序下保持原顺序排在有效项之后
        fallback = min(sc for sc in scores if sc is not None)
        return [fallback if sc is None else sc for sc in scores]


_rerank_client: RerankClient | None = None


def get_rerank_client() -> RerankClient:
    global _rerank_client
    if _rerank_client is None:
        _rerank_client = RerankClient()
    return _rerank_client
