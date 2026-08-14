"""外部查询 API（知识库对外开放查询）

管理（仅 super_admin，prefix /api/ext-queries）：
- GET    /api/ext-queries                列表（附加 kb_names，含完整 token——内网管理端）
- POST   /api/ext-queries                新建 {name, kb_ids, config} → 完整配置（含 token）
- PUT    /api/ext-queries/{id}           编辑 name/kb_ids/config
- POST   /api/ext-queries/{id}/reset-token  重置 token（旧链接立即失效）→ {token}
- POST   /api/ext-queries/{id}/toggle    启用/停用切换
- DELETE /api/ext-queries/{id}           删除

外部（公开，prefix /api/ext，token 鉴权，无需系统账号）：
- GET  /api/ext/{config_id}/info?token=xxx   页面挂载校验 → {name, kb_names}
- POST /api/ext/{config_id}/chat              Bearer token；body {query, session_id?}
  → SSE 流式（meta(sources) → delta → done / error），复用 chat_service 的
    system 组装与 LLM 流式能力（_build_system_content/_build_knowledge/
    _build_refs/_get_client），LLM 用全局活跃配置、生成参数由 config 覆盖；
    多库检索（每库 top_k 候选 → 合并按 score 降序取全局 top_k）；
    无命中直接告知不调 LLM；每次查询落审计日志（ext_query_logs.jsonl）；
    每 config 每分钟限流（超限 429）。

安全设计（对外统一防探测）：
- 配置不存在 / token 不匹配 / 已停用 → 一律 401「链接无效或已失效」（不区分
  响应，避免暴露配置存在性）；管理端操作不存在配置 → 404（超管场景无探测风险）
- token 即访问凭证：泄露可被外部滥用，超管可重置（旧链接立即失效）/停用
- 限流：每 config 每分钟 20 次（内存滑动窗口），防 token 泄露后刷量
- 审计日志：只记 query 摘要（截 100 字）与命中数，不记回答内容
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_active_config
from backend.db import get_db
from backend.deps import require_super_admin
from backend.models.user_models import UserPublic
from backend.services import audit_service
from backend.services.chat_service import ChatService, _llm_to_dict, \
    get_chat_service, sse_event
from backend.services.ext_query_service import (get_ext_query_service,
                                                coerce_config)
from backend.services.kb_service import get_kb_service
from backend.services.retrieval_service import (RetrievalUnavailableError,
                                                get_retrieval_service)

logger = logging.getLogger(__name__)

# ==================== 请求模型 ====================

# 管理端 config 提交（字段全可选：None/缺省 = 跟随全局活跃配置；
# system_prompt 空串 = 内置默认模板，与聊天配置语义一致）
class ExtQueryConfigIn(BaseModel):
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    top_k: Optional[int] = None
    similarity_threshold: Optional[float] = None
    enable_multi_turn: Optional[bool] = None
    history_rounds: Optional[int] = None


class ExtQueryCreateRequest(BaseModel):
    name: str = Field(..., description="外部查询名称（1~50 字）")
    kb_ids: List[str] = Field(..., description="暴露的知识库 ID 列表（1~10 个，超管视角全部门）")
    config: Optional[ExtQueryConfigIn] = None


class ExtQueryUpdateRequest(BaseModel):
    name: Optional[str] = None
    kb_ids: Optional[List[str]] = None
    config: Optional[ExtQueryConfigIn] = None


class ExtQueryChatRequest(BaseModel):
    query: str = Field(..., description="外部用户问题")
    session_id: Optional[str] = Field(
        None, description="会话 ID（同一链接页面内多轮上下文；为空则不续上下文）")


class ExtQuerySyncRequest(BaseModel):
    """同步查询请求（MCP 接入用：一次请求返回完整回答 + 引用来源，非流式）"""
    query: str = Field(..., description="外部用户问题")
    top_k: Optional[int] = Field(
        None, description="检索条数覆盖（1~20；None=取配置 config.top_k，再取全局）")


# ==================== 共享辅助 ====================

_NOT_FOUND_MSG = "外部查询不存在"


async def _list_kb_map(db: AsyncSession) -> dict:
    """超管视角全部知识库 id → {id, name, department_id}（外部配置可引用任何部门库）"""
    kbs = await get_kb_service().list(db)
    return {kb.id: {"id": kb.id, "name": kb.name,
                    "department_id": kb.department_id} for kb in kbs}


def _attach_kb_names(items: list, kb_map: dict) -> list:
    """列表项附加 kb_names（前端展示库名/部门用）"""
    for it in items:
        it["kb_names"] = [kb_map[k] for k in it.get("kb_ids", [])
                          if k in kb_map]
    return items


async def _validate_kb_ids(db: AsyncSession, kb_ids: List[str]) -> None:
    """暴露的库必须存在（超管视角所有部门；任一不存在 → 400 指明）"""
    kb_map = await _list_kb_map(db)
    missing = [k for k in kb_ids if k not in kb_map]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"知识库不存在或不可用: {', '.join(missing[:3])}")


def _auth_ext(config_id: str, token: str) -> dict:
    """外部鉴权：配置存在 + token 匹配 + 已启用，任一不满足 → 统一 401

    防探测：不区分「配置不存在 / token 错误 / 已停用」的响应差异。
    """
    ext = get_ext_query_service().get(config_id)
    if not ext or not token or ext.get("token") != token or not ext.get("enabled", True):
        raise HTTPException(status_code=401, detail="链接无效或已失效")
    return ext


def _bearer_token(request: Request) -> Optional[str]:
    """解析 Authorization: Bearer xxx"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


# ==================== 管理 API（仅 super_admin） ====================

admin_router = APIRouter(prefix="/api/ext-queries", tags=["外部查询管理"])


@admin_router.get("")
async def list_ext_queries(db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(require_super_admin)):
    """列表（含完整 token：内网管理端可直接复制链接分发；外部访问凭证即 token，
    超管负责保管——泄露可重置/停用）"""
    kb_map = await _list_kb_map(db)
    return _attach_kb_names(get_ext_query_service().list(), kb_map)


@admin_router.post("", status_code=201)
async def create_ext_query(request: Request, body: ExtQueryCreateRequest,
                           db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(require_super_admin)):
    """新建外部查询 → 完整配置（token 仅在本次响应返回明文，用于前端展示复制链接）"""
    await _validate_kb_ids(db, body.kb_ids)
    try:
        item = get_ext_query_service().create(
            body.name, body.kb_ids,
            coerce_config(body.config.model_dump() if body.config else {}),
            user_id=user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit_service.record_action(
        user, action="ext.create", target_type="ext_query", target_id=item["id"],
        target_name=item["name"][:100],
        detail={"kb_count": len(item["kb_ids"])}, request=request)
    kb_map = await _list_kb_map(db)
    return _attach_kb_names([item], kb_map)[0]


@admin_router.put("/{config_id}")
async def update_ext_query(request: Request, config_id: str,
                           body: ExtQueryUpdateRequest,
                           db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(require_super_admin)):
    """编辑名称/暴露库/查询参数（token 不变，链接继续有效）"""
    if body.kb_ids is not None:
        await _validate_kb_ids(db, body.kb_ids)
    try:
        item = get_ext_query_service().update(
            config_id,
            name=body.name,
            kb_ids=body.kb_ids,
            config=coerce_config(body.config.model_dump()) if body.config else None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not item:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_MSG)
    await audit_service.record_action(
        user, action="ext.update", target_type="ext_query", target_id=config_id,
        target_name=(item["name"] or "")[:100],
        detail={"kb_count": len(item["kb_ids"])}, request=request)
    kb_map = await _list_kb_map(db)
    return _attach_kb_names([item], kb_map)[0]


@admin_router.post("/{config_id}/reset-token")
async def reset_ext_token(request: Request, config_id: str,
                          user: UserPublic = Depends(require_super_admin)):
    """重置访问 token（旧链接立即失效），返回新 token（仅本次响应明文）"""
    ext_svc = get_ext_query_service()
    ext = ext_svc.get(config_id)
    if not ext:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_MSG)
    token = ext_svc.reset_token(config_id)
    await audit_service.record_action(
        user, action="ext.reset-token", target_type="ext_query",
        target_id=config_id, target_name=(ext["name"] or "")[:100],
        request=request)
    return {"token": token, "message": "访问令牌已重置，旧链接已失效"}


@admin_router.post("/{config_id}/toggle")
async def toggle_ext_query(request: Request, config_id: str,
                           user: UserPublic = Depends(require_super_admin)):
    """启用/停用切换（停用后该链接所有请求立即 401）"""
    ext_svc = get_ext_query_service()
    item = ext_svc.toggle(config_id)
    if not item:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_MSG)
    await audit_service.record_action(
        user, action="ext.toggle", target_type="ext_query", target_id=config_id,
        target_name=(item["name"] or "")[:100],
        detail={"enabled": item["enabled"]}, request=request)
    return item


@admin_router.delete("/{config_id}")
async def delete_ext_query(request: Request, config_id: str,
                           user: UserPublic = Depends(require_super_admin)):
    """删除（链接立即失效）"""
    ext_svc = get_ext_query_service()
    ext = ext_svc.get(config_id)
    if not ext:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_MSG)
    ext_svc.delete(config_id)
    await audit_service.record_action(
        user, action="ext.delete", target_type="ext_query", target_id=config_id,
        target_name=(ext["name"] or "")[:100], request=request)
    return {"message": "外部查询已删除"}


# ==================== 外部查询 API（公开，token 鉴权） ====================

ext_router = APIRouter(prefix="/api/ext", tags=["外部查询"])

# 无命中告知文案（与内部聊天一致）
_NO_HIT_TIP = ("未检索到相关内容，我无法回答该问题。"
               "请尝试换一种问法，或先与管理员确认知识库内容。")


@ext_router.get("/{config_id}/info")
async def ext_info(config_id: str, token: str = Query(default=""),
                   db: AsyncSession = Depends(get_db)):
    """页面挂载校验：{name, kb_names}；无效链接统一 401「链接无效或已失效」"""
    ext = _auth_ext(config_id, token)
    kb_map = await _list_kb_map(db)
    return {"id": ext["id"], "name": ext["name"],
            "kb_names": [kb_map[k] for k in ext["kb_ids"] if k in kb_map]}


@ext_router.post("/{config_id}/chat")
async def ext_chat(config_id: str, body: ExtQueryChatRequest,
                   request: Request,
                   db: AsyncSession = Depends(get_db)):
    """外部流式问答（SSE：meta → delta → done / error）

    - 鉴权：Authorization: Bearer {token}（配置不存在/错 token/停用 → 401）
    - 限流：每 config 每分钟 RATE_LIMIT_PER_MIN 次（超限 → 429）
    - 流程复用现有能力：多库检索（每库 top_k）→ sources 合并 → system 组装
      （config.system_prompt 优先，{knowledge}/{refs} 占位符支持）→ LLM 流式
      （全局活跃 LLM 配置，temperature/top_p/max_tokens 由 config 覆盖）→
      无命中直接告知；查询审计日志落盘；多轮上下文（内存，仅同 session_id 内）
    """
    ext = _auth_ext(config_id, _bearer_token(request) or "")
    ext_svc = get_ext_query_service()
    if not ext_svc.check_rate_limit(config_id):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    question = (body.query or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="query 不能为空")

    conf = ext["config"]
    cfg = get_active_config()
    top_k = int(conf.get("top_k") or cfg.retrieval.top_k)
    min_score = conf.get("similarity_threshold")
    # kb_name 映射在请求生命周期内查好，闭包给事件生成器使用（StreamingResponse
    # 生成器执行时 db session 已不可用）
    kb_map = await _list_kb_map(db)

    async def event_generator():
        try:
            # 1) 多库检索：每库 top_k 候选 → 合并按 score 降序取全局 top_k
            merged: List = []
            try:
                for kid in ext["kb_ids"]:
                    merged.extend(await get_retrieval_service().retrieve(
                        kid, question, top_k=top_k, min_score=min_score))
            except RetrievalUnavailableError as e:
                yield sse_event("error", {"message": str(e)})
                return
            except Exception as e:
                logger.exception("外部查询检索失败: %s", e)
                yield sse_event("error", {"message": f"检索失败: {e}"})
                return
            merged.sort(key=lambda s: s.score, reverse=True)
            sources = merged[:top_k]
            for s in sources:
                if s.kb_id in kb_map:
                    s.kb_name = kb_map[s.kb_id]["name"]
            yield sse_event("meta", {
                "sources": [s.model_dump(mode="json") for s in sources],
            })

            # 2) 无命中：直接告知，不调用 LLM（日志 hit_count=0）
            if not sources:
                ext_svc.log_query(config_id, question, 0)
                yield sse_event("delta", {"text": _NO_HIT_TIP})
                yield sse_event("done", {"session_id": body.session_id or "",
                                         "message_count": 0})
                return

            # 3) 组装 prompt（复用 chat_service 的 system 组装：config 的
            #    system_prompt 优先（支持 {knowledge}/{refs} 占位符），
            #    空/缺省 → 内置默认模板）
            refs = ChatService._build_refs(sources)
            knowledge = ChatService._build_knowledge(sources)
            system_content = ChatService._build_system_content(
                conf.get("system_prompt"), refs, knowledge)
            messages: List[dict] = [{"role": "system", "content": system_content}]
            if conf.get("enable_multi_turn", True):
                rounds = int(conf.get("history_rounds") or cfg.chat.history_rounds)
                history = ext_svc.get_context(config_id, body.session_id or "",
                                              rounds)
                messages.extend(history)
            messages.append({"role": "user", "content": question})

            # 4) LLM 流式（全局活跃 LLM 配置；生成参数 config 非 None 覆盖）
            llm_cfg = _llm_to_dict(get_active_config().llm)
            client = get_chat_service()._get_client(llm_cfg)
            request_kwargs: dict = {
                "model": llm_cfg["model"],
                "messages": messages,
                "temperature": (conf.get("temperature")
                                if conf.get("temperature") is not None
                                else llm_cfg["temperature"]),
                "max_tokens": (conf.get("max_tokens")
                               if conf.get("max_tokens") is not None
                               else llm_cfg["max_tokens"]),
                "stream": True,
            }
            if conf.get("top_p") is not None:
                request_kwargs["top_p"] = conf["top_p"]
            answer_parts: List[str] = []
            try:
                stream = await client.chat.completions.create(**request_kwargs)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = delta.content if delta else None
                    if content:
                        answer_parts.append(content)
                        yield sse_event("delta", {"text": content})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("外部查询 LLM 流式调用失败: %s", e)
                err_msg = f"LLM 调用失败: {e}"
                ext_svc.append_context(config_id, body.session_id or "",
                                       question, err_msg)
                ext_svc.log_query(config_id, question, len(sources))
                yield sse_event("error", {"message": err_msg})
                return

            # 5) done + 上下文追加 + 审计日志
            ext_svc.append_context(config_id, body.session_id or "",
                                   question, "".join(answer_parts))
            ext_svc.log_query(config_id, question, len(sources))
            yield sse_event("done", {"session_id": body.session_id or "",
                                     "message_count": len(answer_parts)})
        except Exception as e:
            logger.exception("外部查询 SSE 流异常: %s", e)
            yield sse_event("error", {"message": f"服务异常: {e}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ==================== 外部同步查询（MCP 接入，非流式） ====================

# 图片链接正则：/api/files/images/{doc_id}/{name}（文件名白名单 \w.- 含中文；
# 链接到空白/引号/右括号为止，兼容 markdown ![](url) 语法）
_IMAGE_URL_RE = re.compile(r"/api/files/images/[^\s)\"']+")

# 回答/引用文本长度上限（对齐外部接入 6000 字规范；单条引用 2000 字）
ANSWER_MAX_LEN = 6000
SOURCE_MAX_LEN = 2000
# 请求体 top_k 覆盖范围（与 config top_k 同范围）
TOP_K_MAX = 20


def _truncate(text: str, max_len: int) -> str:
    """文本截断：超过 max_len 直接截断（保证长度严格 ≤max_len）"""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _extract_image_urls(text: str) -> List[str]:
    """从引用文本提取图片链接（/api/files/images/...，去重保序；无则空数组）"""
    seen = set()
    urls = []
    for m in _IMAGE_URL_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            urls.append(m)
    return urls


@ext_router.post("/{config_id}/query")
async def ext_query_sync(config_id: str, body: ExtQuerySyncRequest,
                         request: Request,
                         db: AsyncSession = Depends(get_db)):
    """外部同步问答（MCP/脚本接入：一次请求返回完整回答与引用，非 SSE）

    - 鉴权：Authorization: Bearer {token}（配置不存在/错 token/停用 → 401，
      与 /chat 完全一致）
    - 限流：与 /chat 共用同一 config 限流桶（每 config 每分钟
      RATE_LIMIT_PER_MIN 次，超限 → 429）
    - 流程复用现有能力：多库检索（每库 top_k 候选 → 合并按 score 降序取
      全局 top_k）→ system 组装（config.system_prompt 优先，
      {knowledge}/{refs} 占位符支持）→ **非流式** LLM（stream=False 一次
      返回；temperature/top_p/max_tokens 由 config 覆盖）→ 无命中直接
      固定文案不调 LLM；每次查询落审计日志（同一 ext_query_logs.jsonl，
      仅 query 摘要与命中数）
    - 响应：{answer(≤6000 截断), sources: [{document_name, text(≤2000/条),
      image_urls}]}——image_urls 从引用文本提取 /api/files/images/ 链接
      （无则空数组），供 Agent 端展示知识库图片
    - 错误：检索失败 → 400、LLM 失败 → 502（中文提示，与外部接口风格一致）
    """
    ext = _auth_ext(config_id, _bearer_token(request) or "")
    ext_svc = get_ext_query_service()
    if not ext_svc.check_rate_limit(config_id):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    question = (body.query or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="query 不能为空")

    conf = ext["config"]
    cfg = get_active_config()
    if body.top_k is not None:
        if not 1 <= body.top_k <= TOP_K_MAX:
            raise HTTPException(status_code=422, detail=f"top_k 需为 1~{TOP_K_MAX}")
        top_k = body.top_k
    else:
        top_k = int(conf.get("top_k") or cfg.retrieval.top_k)
    min_score = conf.get("similarity_threshold")

    # 1) 多库检索：每库 top_k 候选 → 合并按 score 降序取全局 top_k
    merged: List = []
    try:
        for kid in ext["kb_ids"]:
            merged.extend(await get_retrieval_service().retrieve(
                kid, question, top_k=top_k, min_score=min_score))
    except RetrievalUnavailableError as e:
        logger.warning("外部同步查询检索服务不可用: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("外部同步查询检索失败: %s", e)
        raise HTTPException(status_code=400, detail=f"检索失败: {e}")
    merged.sort(key=lambda s: s.score, reverse=True)
    sources = merged[:top_k]

    # 2) 无命中：固定文案直接返回，不调用 LLM（日志 hit_count=0）
    if not sources:
        ext_svc.log_query(config_id, question, 0)
        return {"answer": _NO_HIT_TIP, "sources": []}

    # 3) 组装 prompt（复用 chat_service 的 system 组装：config.system_prompt
    #    优先（支持 {knowledge}/{refs} 占位符），空/缺省 → 内置默认模板；
    #    同步查询无会话概念，不带多轮历史）
    refs = ChatService._build_refs(sources)
    knowledge = ChatService._build_knowledge(sources)
    system_content = ChatService._build_system_content(
        conf.get("system_prompt"), refs, knowledge)
    messages: List[dict] = [{"role": "system", "content": system_content},
                            {"role": "user", "content": question}]

    # 4) 非流式 LLM（stream=False 一次返回完整回答；生成参数 config 非 None
    #    覆盖全局 LLM 配置）
    llm_cfg = _llm_to_dict(get_active_config().llm)
    client = get_chat_service()._get_client(llm_cfg)
    request_kwargs: dict = {
        "model": llm_cfg["model"],
        "messages": messages,
        "temperature": (conf.get("temperature")
                        if conf.get("temperature") is not None
                        else llm_cfg["temperature"]),
        "max_tokens": (conf.get("max_tokens")
                       if conf.get("max_tokens") is not None
                       else llm_cfg["max_tokens"]),
        "stream": False,
    }
    if conf.get("top_p") is not None:
        request_kwargs["top_p"] = conf["top_p"]
    try:
        resp = await client.chat.completions.create(**request_kwargs)
        answer = ""
        if getattr(resp, "choices", None):
            answer = resp.choices[0].message.content or ""
    except Exception as e:
        logger.exception("外部同步查询 LLM 调用失败: %s", e)
        raise HTTPException(status_code=502, detail=f"LLM 调用失败: {e}")

    # 5) 截断与响应组装：answer ≤6000；sources 每条 text ≤2000，
    #    附图片 URL 列表（无图片 → 空数组）；审计日志落盘
    answer = _truncate(answer, ANSWER_MAX_LEN)
    out_sources = []
    for s in sources:
        text = s.parent_text or s.text
        out_sources.append({
            "document_name": s.document_name,
            "text": _truncate(text, SOURCE_MAX_LEN),
            "image_urls": _extract_image_urls(text),
        })
    ext_svc.log_query(config_id, question, len(sources))
    return {"answer": answer, "sources": out_sources}
