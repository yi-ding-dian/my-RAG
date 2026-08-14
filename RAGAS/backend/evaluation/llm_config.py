"""Qwen LLM 配置 — OpenAI 兼容接口"""
import logging
from typing import Optional
from openai import OpenAI, AsyncOpenAI
from langchain_openai import ChatOpenAI
from backend.config import settings

logger = logging.getLogger(__name__)


def create_qwen_chat(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> ChatOpenAI:
    """创建 Qwen ChatOpenAI 实例（用于 RAGAS langchain 旧模式）

    传入 base_url/api_key 等参数时使用覆盖值，否则回落全局 settings（profiles.json 活跃配置）。
    """
    return ChatOpenAI(
        model=model or settings.LLM_MODEL,
        openai_api_key=api_key if api_key not in (None, "") else settings.LLM_API_KEY,
        openai_api_base=base_url or settings.LLM_BASE_URL,
        temperature=temperature if temperature is not None else settings.LLM_TEMPERATURE,
        max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
    )


def create_qwen_client(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> OpenAI:
    """创建原生 OpenAI 同步客户端（参数缺省时回落全局 settings）"""
    return OpenAI(
        api_key=api_key if api_key not in (None, "") else settings.LLM_API_KEY,
        base_url=base_url or settings.LLM_BASE_URL,
    )


def create_async_qwen_client(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> AsyncOpenAI:
    """创建异步 OpenAI 客户端（支持并发请求，vLLM 自动做 batch 推理；参数缺省时回落全局 settings）"""
    return AsyncOpenAI(
        api_key=api_key if api_key not in (None, "") else settings.LLM_API_KEY,
        base_url=base_url or settings.LLM_BASE_URL,
    )


def create_qwen_embeddings():
    """创建 Embeddings 实例"""
    from langchain_openai import OpenAIEmbeddings
    return OpenAIEmbeddings(
        model=settings.EMBEDDING_MODEL,
        openai_api_key=settings.EMBEDDING_API_KEY,
        openai_api_base=settings.EMBEDDING_BASE_URL,
    )
