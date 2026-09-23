"""token 计数：优先调用模型服务的 /tokenize（vLLM 内置），失败降级为字符数估算

**为什么需要**：进 prompt 的检索片段可能很大——父子分块下父块实测可达 3.6 万字
（= 2.3 万 token），而生产模型 `Qwen3.5-9B-GPTQ-4bit` 的窗口只有 15000 token，
**单个父块就能撑爆上下文**。故组装引用段前要按预算裁剪（见 chat_service）。

**为什么调服务而不是本地分词器**：
- vLLM 内置 `POST /tokenize`，**用的就是模型自己的分词器**，天然准确，零依赖；
- 本地接 `tokenizers` 库要装包 + 下载 tokenizer.json，而且项目跑着多模型
  （DeepSeek / Qwen 分词器不同），得维护 N 套——成本远高于收益。

**降级**：服务不支持时（LM Studio 等）按**字符数 × 0.62** 估算。该系数实测自
中文技术文档（2000 字符→1267 token = 0.633；36774 字符→22730 token = 0.618），
且**偏保守（高估 token）**——正是"防爆"场景想要的误差方向。

**路径**：模型配置里的 `base_url` 形如 `http://host:8000/v1`，而 tokenize 在
**根路径** `/tokenize`（实测 `/v1/tokenize` 返回 404），故要剥掉 `/v1` 后缀。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# 降级系数：字符数 → token 数（中文技术文档实测 0.618~0.633，取 0.62 偏保守）
_FALLBACK_TOKENS_PER_CHAR = 0.62
# tokenize 探测超时（本地服务很快，超时即视为不支持，不让它拖慢问答）
_TIMEOUT = 5.0

# base_url → 是否支持 /tokenize（进程内缓存）：None=未探测 / True=可用 / False=不支持
_tokenize_support: Dict[str, bool] = {}


def _tokenize_url(base_url: str) -> str:
    """模型 base_url（.../v1）→ tokenize 根路径 URL（实测 /v1/tokenize 是 404）"""
    return base_url.rstrip("/").removesuffix("/v1") + "/tokenize"


def estimate_tokens(text: str) -> int:
    """字符数估算（降级路径；实测系数 0.618~0.633，取 0.62 偏保守）"""
    return int(len(text or "") * _FALLBACK_TOKENS_PER_CHAR)


async def count_tokens(text: str, base_url: Optional[str],
                       model: Optional[str],
                       timeout: float = _TIMEOUT) -> int:
    """单条文本的 token 数：优先 /tokenize，不支持/失败 → 字符数估算

    探测结果按 base_url 缓存：一旦确认不支持就不再重试（避免每次问答都白等
    一轮超时）；服务临时抖动导致的失败会被记为"不支持"，下次问答仍走估算——
    估算只影响裁剪精度、不影响正确性，比反复超时拖慢问答划算。
    """
    if not text:
        return 0
    if not base_url or _tokenize_support.get(base_url) is False:
        return estimate_tokens(text)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(_tokenize_url(base_url),
                                  json={"model": model or "", "prompt": text})
            r.raise_for_status()
            count = int((r.json() or {}).get("count") or 0)
            if count <= 0:                      # 返回体异常 → 当作估算
                return estimate_tokens(text)
            _tokenize_support[base_url] = True
            return count
    except Exception as e:
        if _tokenize_support.get(base_url) is None:
            logger.info("模型服务不支持 /tokenize（%s），改用字符数估算: %s",
                        base_url, e.__class__.__name__)
        _tokenize_support[base_url] = False
        return estimate_tokens(text)


async def count_many(texts: List[str], base_url: Optional[str],
                     model: Optional[str]) -> List[int]:
    """批量计数：/tokenize 不收数组（实测 prompt 只接受 string），故逐个**并发**调用"""
    if not texts:
        return []
    return list(await asyncio.gather(
        *(count_tokens(t, base_url, model) for t in texts)))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按 token 预算截断文本（用于"首条就超预算"的场景）

    用**字符数等比**换算而非二分调 tokenize：截断只是兜底（有上下文总比没有强），
    不值得为它多打几轮 HTTP；估算系数偏保守，结果只会略短于预算。
    """
    if max_tokens <= 0 or not text:
        return ""
    max_chars = int(max_tokens / _FALLBACK_TOKENS_PER_CHAR)
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def reset_probe_cache() -> None:
    """清空探测缓存（测试用；生产无需调用）"""
    _tokenize_support.clear()
