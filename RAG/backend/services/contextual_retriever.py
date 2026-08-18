"""上下文检索增强（Contextual Retrieval）：切块后用 LLM 为每个块生成简短上下文摘要

- 解决孤立分块缺乏全局背景的问题（参考 Anthropic 上下文检索做法）：
  向量化时用 "【上下文】摘要\n原文" 代替纯原文，检索命中显示也含摘要，
  提升检索质量；chunks_meta 的 text 保持原文（偏移契约不破坏），
  摘要存 chunks_meta.context 字段
- 摘要生成输入（完整文档视角）：
  - 文档背景 = 文档名 + 完整文档全文（全局视角；解析文本 <= 系统配置
    contextual_retrieval.max_full_doc_chars 阈值时使用，替代旧的
    "文档名 + 前 1500 字符截断"）
  - 当前块文本（截断 1000）
  - 输出：一句话中文摘要（LLM 生成后按 100 字符兜底截断）
  - 文档超过阈值 → 效果不佳，抛 DocTooLongError（整文档超限 = 任务失败，
    由 ingestion 层写回 failed 并提示换用其他切块方式/关闭增强——区别于
    单块调用失败仍跳过不阻塞）
- 阈值每次调用实时读 get_active_config().contextual_retrieval.max_full_doc_chars
  （活跃配置由配置档案驱动，改配置/重启即时生效，不缓存）
- 用激活的 LLM 模型（get_active_config().llm，多模型管理的激活模型）；
  parser_config.parse_llm_model 指定时改用该模型（从激活档案模型列表查完整
  配置覆盖，仅影响摘要，对话仍用激活模型；查不到/未指定回退激活模型），
  独立 AsyncOpenAI 客户端实例（key 比对自动重建，与 chat_service 同款模式）
- 并发限流 3（asyncio.Semaphore）；每调用超时 15s；
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

from backend.config import LLMConfig, get_active_config
from backend.services.chat_service import _llm_to_dict
from backend.services.llm_client import (LLMRequestError, LLMTimeoutError,
                                         get_llm_client, llm_completion)
from backend.services.settings_service import llm_cfg_for_parser
from backend.services.thinking_strategy import get_thinking_strategy

logger = logging.getLogger(__name__)


class DocTooLongError(ValueError):
    """文档超过上下文检索完整文档阈值（整文档超限 → 任务失败）

    - 抛出处：enrich_chunks 长度校验（解析文本 len > 系统配置阈值）
    - 语义：完整文档视角下超长文档提示效果不佳，不建议采用该方式入库；
      与单块摘要失败（跳过不阻塞）严格区分——单块失败仍跳过
    - ingestion 层不捕获（冒泡到任务外层）→ mark_failed 写回 error，
      前端据此展示提示
    """


def _wan(n: int) -> str:
    """字符数 → 万字表示（整万显示整数，否则保留 1 位小数）"""
    w = n / 10000
    return str(int(w)) if w == int(w) else f"{w:.1f}"


# 块文本输入截断
_CHUNK_INPUT_CHARS = 1000
# 摘要输出长度兜底截断（字符；prompt 要求 ≤80 字，模型可能超长）
_CONTEXT_MAX_CHARS = 100
# 单次调用 max_tokens：原 _CONTEXT_MAX_CHARS*2=200 在推理模型（DeepSeek/
# Qwen3 等带 thinking）上会被 reasoning 部分大量消耗（实测 DeepSeek 单块
# 摘要 reasoning≈85+content≈120，200 已捉襟见肘；512 时个别复杂块
# reasoning 仍超 512 被吃光 → 提升到 1024，实测 1.6s/块、空摘要基本消除；
# 本地 Qwen3 思考锁死时 200/1024 全被吃光 content 恒空，需换模型）。
_MAX_TOKENS = 1024
# 并发调用上限（asyncio.Semaphore）
_CONCURRENCY = 3
# 单次 LLM 调用超时（秒）：实测在线 LLM 单块摘要 2s 内完成，20s 过长
# （思考锁死模型会白等 20s 才超时）；15s 兼顾偶发慢响应与快速失败
_TIMEOUT = 15.0

_CTX_PROMPT = (
    "你是文档分析助手。以下是文档背景和其中一个片段"
    "（文档背景可能是完整全文，请先通览把握整体结构再作答），"
    "请用一句话（不超过80字）概括该片段在文档中的上下文位置与主题，"
    "直接输出摘要内容不要任何前缀。\n\n"
    "<document>\n{doc}\n</document>\n\n片段：\n{chunk}"
)

# ---- 独立 LLM 客户端（key 比对自动重建：配置变化即重建，无需重启；
#      实现统一在 llm_client.get_llm_client，缓存为模块级 key→client 字典） ----


def _get_client(llm_cfg: Optional[dict] = None) -> AsyncOpenAI:
    """按 LLM 配置 key 比对自动重建客户端（委托统一工厂 get_llm_client）

    保留模块级函数名（test_parse_llm_model 等测试 monkeypatch 依赖）。
    """
    return get_llm_client(llm_cfg)


async def _enrich_one(sem: asyncio.Semaphore, index: int, chunk_text: str,
                      doc_bg: str, llm_cfg: dict, strategy=None,
                      timeout: float = _TIMEOUT) -> Optional[dict]:
    """单个块的摘要生成：成功返回 {index, context}；失败/超时/空摘要 → None

    strategy: 思考关闭策略（thinking_strategy 模块产物）——apply(payload)
    统一处理 extra_body（在线 API）与 messages prefill 注入（本地 Qwen），
    关闭思考可显著加快摘要生成并节省 token
    """
    async with sem:
        try:
            # 消费处类型化：dict → LLMConfig（扩展字段忽略）；_get_client
            # 调用点仍传原 dict（测试 recorder 断言 dict 结构兼容）
            cfg = LLMConfig.from_dict(llm_cfg)
            client = _get_client(llm_cfg)
            payload = {
                "messages": [
                    {"role": "system", "content": "你是文档分析助手。"},
                    {"role": "user", "content": _CTX_PROMPT.format(
                        doc=doc_bg, chunk=chunk_text)},
                ],
            }
            if strategy is not None:
                strategy.apply(payload)
            # 统一调用包装：超时 → LLMTimeoutError；调用失败 → LLMRequestError
            resp = await llm_completion(
                client, model=cfg.model, messages=payload["messages"],
                max_tokens=_MAX_TOKENS, temperature=0.3,
                extra_body=payload.get("extra_body"), timeout=timeout,
            )
            try:
                content = (resp.choices[0].message.content or "").strip()
            except Exception:
                content = ""
            if not content:
                # 空摘要诊断：区分"模型思考锁死/思考过长吃光 token"与普通空输出
                # （推理模型 usage.completion_tokens_details.reasoning_tokens
                # 非零且接近 completion_tokens 时，说明 max_tokens 全部被
                # thinking 消耗，content 永远为空——如本地 Qwen3 思考锁死）
                reasoning_note = ""
                try:
                    usage = resp.usage
                    if usage is not None:
                        detail = getattr(usage, "completion_tokens_details", None)
                        rt = getattr(detail, "reasoning_tokens", None)
                        if rt:
                            reasoning_note = (
                                f"（reasoning 消耗 {rt}/{usage.completion_tokens} "
                                f"token——模型思考过长吃光 max_tokens，"
                                f"建议更换非思考锁死模型或调大 max_tokens）")
                except Exception:
                    pass
                logger.warning("上下文摘要为空，跳过: chunk#%d%s", index,
                               reasoning_note)
                return None
            # 摘要截断兜底（模型可能输出超长，避免污染向量化文本与 metadata）
            if len(content) > _CONTEXT_MAX_CHARS:
                content = content[:_CONTEXT_MAX_CHARS]
            return {"index": index, "context": content}
        except LLMTimeoutError:
            logger.warning("上下文摘要生成超时（>%.0fs），跳过: chunk#%d",
                           timeout, index)
            return None
        except LLMRequestError as e:
            # 可预期失败（网络/限流/HTTP 错误）：warning，单块失败跳过不阻塞
            logger.warning("上下文摘要生成失败，跳过: chunk#%d err=%s",
                           index, str(e)[:150])
            return None
        except Exception as e:
            # 兜底（未知异常）：同样跳过不阻塞，信息保留
            logger.warning("上下文摘要生成失败，跳过: chunk#%d err=%s",
                           index, str(e)[:150])
            return None


async def enrich_chunks(chunks, doc_text: str, cfg: Optional[dict] = None,
                        doc_name: Optional[str] = None,
                        timeout: float = _TIMEOUT) -> List[dict]:
    """为切块生成上下文摘要，返回 [{index, context}] 映射（含摘要的块）

    - chunks: List[Chunk]（切块结果，取 .text）
    - doc_text: 文档全文（解析产物，完整文档作上下文——≤阈值时）
    - cfg: parser_config（含 contextual_retrieval 开关；关/缺省直接返回空，
      不调用 LLM——与 ingestion 层判断双保险）
    - doc_name: 文档原始名（作文档背景首行）
    - timeout: 单次调用超时（秒，测试可缩小）
    - 完整文档视角：解析文本 <= 系统配置阈值
      （contextual_retrieval.max_full_doc_chars，默认 20000，每次调用实时
      读活跃配置不缓存）→ 完整文档作文档背景；超过 → 抛 DocTooLongError
      （整文档超限 = 任务失败，ingestion 层写回 failed）
    - 单块调用失败/超时跳过对应块，绝不抛异常（与整文档超限严格区分）
    """
    if not cfg or not cfg.get("contextual_retrieval"):
        return []
    if not chunks:
        return []
    # 完整文档阈值：实时读活跃配置（配置档案驱动，改配置即时生效）
    threshold = int(get_active_config().contextual_retrieval.max_full_doc_chars)
    doc_len = len(doc_text or "")
    if doc_len > threshold:
        raise DocTooLongError(
            f"文档约 {_wan(doc_len)} 万字，超过上下文检索完整文档阈值"
            f"（{_wan(threshold)} 万字），效果不佳，不建议采用该方式入库，"
            f"请换用其他切块方式或关闭上下文检索增强")
    # 解析 LLM 模型：parser_config.parse_llm_model 指定（上下文摘要专用模型，
    # 从激活档案模型列表查完整配置）→ 覆盖；未指定/查不到 → 激活模型
    # （调用点保持 dict 传递：_get_client 的测试 recorder 断言 dict 结构）
    llm_cfg = _llm_to_dict(get_active_config().llm)
    override = llm_cfg_for_parser(cfg.get("parse_llm_model"))
    if override:
        llm_cfg = {**llm_cfg, **override}
    llm_cfg_obj = LLMConfig.from_dict(llm_cfg)
    if not (llm_cfg_obj.base_url and llm_cfg_obj.model):
        logger.warning("LLM 未配置（base_url/model 为空），跳过上下文摘要生成")
        return []
    # 文档背景 = 文档名 + 完整文档全文（全局视角，替代旧的前 1500 字符截断）
    head = f"文档名称：{doc_name or ''}"
    doc_bg = f"{head}\n{doc_text or ''}"
    # 思考关闭策略：按模型服务商/部署方式选择（在线 DeepSeek → extra_body
    # 关闭思考；本地 LM Studio Qwen → messages 末尾注入空 <think> 块跳过思考，
    # 见 thinking_strategy；get_thinking_strategy 保留吃 dict 接口）
    strategy = get_thinking_strategy(llm_cfg, cfg.get("thinking_mode"))
    sem = asyncio.Semaphore(_CONCURRENCY)
    tasks = [
        asyncio.create_task(
            _enrich_one(sem, i, c.text[:_CHUNK_INPUT_CHARS], doc_bg,
                        llm_cfg, strategy, timeout=timeout))
        for i, c in enumerate(chunks)
    ]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r]
