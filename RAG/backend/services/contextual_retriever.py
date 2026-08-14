"""上下文检索增强（Contextual Retrieval）：切块后用 LLM 为每个块生成简短上下文摘要

- 解决孤立分块缺乏全局背景的问题（参考 Anthropic 上下文检索做法）：
  向量化时用 "【上下文】摘要\n原文" 代替纯原文，检索命中显示也含摘要，
  提升检索质量；chunks_meta 的 text 保持原文（偏移契约不破坏），
  摘要存 chunks_meta.context 字段
- 摘要生成输入：
  - 文档背景 = 文档名 + 全文前 1500 字符截断（全局上下文）
  - 当前块文本（截断 1000）
  - 输出：一句话中文摘要（LLM 生成后按 100 字符兜底截断）
- 用激活的 LLM 模型（get_active_config().llm，多模型管理的激活模型），
  独立 AsyncOpenAI 客户端实例（key 比对自动重建，与 chat_service 同款模式）
- 并发限流 3（asyncio.Semaphore）；每调用超时 20s；
  失败/超时 → 该块 context 跳过（None），warning 日志，绝不阻塞入库
- enrich_chunks 返回 [{index, context}] 映射（index 为 chunks 列表下标），
  调用方（ingestion_service）据此组装向量化文本与 metadata
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, List, Optional

from openai import AsyncOpenAI

from backend.config import get_active_config
from backend.services.chat_service import _llm_to_dict

logger = logging.getLogger(__name__)

# 文档背景截断（Anthropic 做法：全文前 1500 字符作全局上下文）
_DOC_BACKGROUND_CHARS = 1500
# 块文本输入截断
_CHUNK_INPUT_CHARS = 1000
# 摘要输出长度兜底截断（字符；prompt 要求 ≤80 字，模型可能超长）
_CONTEXT_MAX_CHARS = 100
# 并发调用上限（asyncio.Semaphore）
_CONCURRENCY = 3
# 单次 LLM 调用超时（秒）
_TIMEOUT = 20.0

_CTX_PROMPT = (
    "你是文档分析助手。以下是文档背景和其中一个片段，"
    "请用一句话（不超过80字）概括该片段在文档中的上下文位置与主题，"
    "直接输出摘要内容不要任何前缀。\n\n"
    "<document>\n{doc}\n</document>\n\n片段：\n{chunk}"
)

# ---- 独立 LLM 客户端（key 比对自动重建：配置变化即重建，无需重启）----
_client: Optional[AsyncOpenAI] = None
_client_key: Optional[str] = None


def _get_client(llm_cfg: Optional[dict] = None) -> AsyncOpenAI:
    """按 LLM 配置 key 比对自动重建客户端（与 chat_service._get_client 同款模式）"""
    global _client, _client_key
    if llm_cfg is None:
        llm_cfg = _llm_to_dict(get_active_config().llm)
    key = json.dumps(llm_cfg, sort_keys=True, ensure_ascii=False)
    if _client is None or _client_key != key:
        _client = AsyncOpenAI(
            base_url=llm_cfg.get("base_url", ""),
            api_key=llm_cfg.get("api_key", ""),
            timeout=float(llm_cfg.get("timeout") or 60),
        )
        _client_key = key
    return _client


async def _enrich_one(sem: asyncio.Semaphore, index: int, chunk_text: str,
                      doc_bg: str, llm_cfg: dict,
                      timeout: float = _TIMEOUT) -> Optional[dict]:
    """单个块的摘要生成：成功返回 {index, context}；失败/超时/空摘要 → None"""
    async with sem:
        try:
            client = _get_client(llm_cfg)
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=llm_cfg.get("model") or "",
                    messages=[
                        {"role": "system", "content": "你是文档分析助手。"},
                        {"role": "user", "content": _CTX_PROMPT.format(
                            doc=doc_bg, chunk=chunk_text)},
                    ],
                    max_tokens=_CONTEXT_MAX_CHARS * 2,
                    temperature=0.3,
                ),
                timeout=timeout,
            )
            try:
                content = (resp.choices[0].message.content or "").strip()
            except Exception:
                content = ""
            if not content:
                logger.warning("上下文摘要为空，跳过: chunk#%d", index)
                return None
            # 摘要截断兜底（模型可能输出超长，避免污染向量化文本与 metadata）
            if len(content) > _CONTEXT_MAX_CHARS:
                content = content[:_CONTEXT_MAX_CHARS]
            return {"index": index, "context": content}
        except asyncio.TimeoutError:
            logger.warning("上下文摘要生成超时（>%.0fs），跳过: chunk#%d",
                           timeout, index)
            return None
        except Exception as e:
            logger.warning("上下文摘要生成失败，跳过: chunk#%d err=%s",
                           index, str(e)[:150])
            return None


async def enrich_chunks(chunks, doc_text: str, cfg: Optional[dict] = None,
                        doc_name: Optional[str] = None,
                        timeout: float = _TIMEOUT) -> List[dict]:
    """为切块生成上下文摘要，返回 [{index, context}] 映射（含摘要的块）

    - chunks: List[Chunk]（切块结果，取 .text）
    - doc_text: 文档全文（解析产物，前 1500 字符作文档背景）
    - cfg: parser_config（含 contextual_retrieval 开关；关/缺省直接返回空，
      不调用 LLM——与 ingestion 层判断双保险）
    - doc_name: 文档原始名（作文档背景首行）
    - timeout: 单次调用超时（秒，测试可缩小）
    - 失败/超时跳过对应块，绝不抛异常
    """
    if not cfg or not cfg.get("contextual_retrieval"):
        return []
    if not chunks:
        return []
    llm_cfg = _llm_to_dict(get_active_config().llm)
    if not (llm_cfg.get("base_url") and llm_cfg.get("model")):
        logger.warning("LLM 未配置（base_url/model 为空），跳过上下文摘要生成")
        return []
    head = f"文档名称：{doc_name or ''}"
    doc_bg = f"{head}\n{(doc_text or '')[:_DOC_BACKGROUND_CHARS]}"
    sem = asyncio.Semaphore(_CONCURRENCY)
    tasks = [
        asyncio.create_task(
            _enrich_one(sem, i, c.text[:_CHUNK_INPUT_CHARS], doc_bg,
                        llm_cfg, timeout=timeout))
        for i, c in enumerate(chunks)
    ]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r]
