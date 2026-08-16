"""聊天 API：/api/chat

- POST /stream       SSE 流式问答（meta -> delta -> done / error）
- POST /retrieve     检索调试（返回 Source 列表）
- GET /history       会话列表（按用户过滤，super_admin 全量；支持 kb_id 过滤）
- GET /history/{id}  会话详情（owner 或 super_admin）
- DELETE /history/{id} 删除会话（owner 或 super_admin）

权限矩阵：
- stream/retrieve：登录 + can_access_kb（kb 无权限 404 伪装）；带 session_id 时校验归属
- history 列表：仅本人（super_admin 全部）；kb_id 过滤
- history 详情/删除：owner 或 super_admin（否则 404 伪装防探测）
"""
from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_active_config
from backend.db import get_db
from backend.deps import get_current_user, kb_or_404
from backend.models.rag_models import (ChatHistoryItem, ChatRequest,
                                       RenameSessionRequest, RetrieveRequest,
                                       RetrieveResponse)
from backend.models.user_models import UserPublic
from backend.services import audit_service, department_service
from backend.services.chat_service import get_chat_service, sse_event
from backend.services.knowledge_graph_service import build_kg_source
from backend.services.retrieval_service import get_retrieval_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["聊天"])


def _check_session_owner(session, user: UserPublic):
    """会话归属校验（owner 或 super_admin；旧会话无 user_id 视为 super_admin 归属）"""
    if user.role == "super_admin":
        return
    if not session or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")


@router.post("/stream")
async def stream_chat(body: ChatRequest, db: AsyncSession = Depends(get_db),
                      user: UserPublic = Depends(get_current_user)):
    """SSE 流式问答（请求体 query 优先，message 向后兼容）"""
    await kb_or_404(db, body.kb_id, user)
    question = (body.query or body.message or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="query 与 message 均缺失")
    if body.top_k is not None and not 1 <= body.top_k <= 50:
        raise HTTPException(status_code=400, detail="top_k 需为 1~50")

    chat_svc = get_chat_service()
    # 传 session_id 时校验归属（归属他人 → 404 伪装；不存在则允许新建）
    if body.session_id:
        session = chat_svc.get_session(body.session_id)
        if session:
            _check_session_owner(session, user)

    # 当前用户所在部门的完整配置（llm/chat/retrieval 段字段级覆盖全局；
    # 无部门/未设置 → None = 纯全局活跃档案，现状行为）
    dept_config = None
    if user.department_id:
        dept_config = await department_service.get_department_config(
            db, user.department_id) or None

    async def event_generator():
        try:
            async for ev in chat_svc.stream_chat(
                    body.kb_id, question, body.session_id,
                    user_id=user.id, top_k=body.top_k,
                    dept_config=dept_config):
                yield ev
        except Exception as e:
            logger.exception("SSE 流异常: %s", e)
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


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(body: RetrieveRequest, db: AsyncSession = Depends(get_db),
                   user: UserPublic = Depends(get_current_user)):
    """检索调试：返回 {sources: [...]}（can_access_kb）

    - kb_id：单库检索（与既有行为一致，结果附带 kb_name）
    - kb_ids（1~5 个）：多知识库对比检索——每个库独立检索（各自 top_k
      候选），合并后按 score 降序截取全局 top_k，Source 附带 kb_id/kb_name；
      与 kb_id 都传时 kb_ids 优先
    - 任一库不存在或不可访问 → 404 伪装（防探测）
    """
    if not body.query or not body.query.strip():
        raise HTTPException(status_code=400, detail="检索 query 不能为空")
    kb_ids = body.kb_ids if body.kb_ids else ([body.kb_id] if body.kb_id else [])
    if not kb_ids:
        raise HTTPException(status_code=400, detail="kb_id 与 kb_ids 至少传一个")
    if len(kb_ids) > 5:
        raise HTTPException(status_code=400, detail="知识库数量需为 1~5 个")
    # 逐个校验存在 + 可访问（任一不满足 → 404 伪装）
    kbs = []
    for kid in kb_ids:
        kbs.append(await kb_or_404(db, kid, user))
    kb_name_map = {kb.id: kb.name for kb in kbs}
    try:
        retrieve_svc = get_retrieval_service()
        if len(kb_ids) == 1:
            # 单库：与既有行为完全一致
            sources = await retrieve_svc.retrieve(
                kb_ids[0], body.query.strip(), top_k=body.top_k,
                min_score=body.similarity_threshold,
                enable_hybrid=body.enable_hybrid,
                enable_rerank=body.enable_rerank)
        else:
            # 多库：各自独立检索 top_k 候选 → 合并按 score 降序取全局 top_k
            top_k = body.top_k or get_active_config().retrieval.top_k
            merged: List = []
            for kid in kb_ids:
                merged.extend(await retrieve_svc.retrieve(
                    kid, body.query.strip(), top_k=body.top_k,
                    min_score=body.similarity_threshold,
                    enable_hybrid=body.enable_hybrid,
                    enable_rerank=body.enable_rerank))
            merged.sort(key=lambda s: s.score, reverse=True)
            sources = merged[:top_k]
        # 知识图谱增强通道（与普通检索并行注入）：开关开且有图谱时，
        # 每个库独立尝试图谱上下文，作为"知识图谱"来源引用追加在末尾
        # （不参与排序/rerank；无图谱/失败自动跳过，不影响检索结果）。
        # 引用顺序规则：普通引用合并后按 score 降序截取全局 top_k，
        # 图谱引用（score=0）在截断之后 append——编号 = 列表顺序 1..N 连续
        # 无跳跃、无重复，图谱恒在最后（作为补充引用），前端 [n]/面板角标
        # 均按此顺序取，调用方不得对返回列表重排。
        kg_enabled = body.enable_kg
        if kg_enabled is None:
            kg_enabled = get_active_config().chat.kg_enhance
        if kg_enabled:
            for kid in kb_ids:
                kg = await build_kg_source(kid, body.query.strip(), enabled=True)
                if kg:
                    kg.kb_name = kb_name_map.get(kid, kg.kb_name)
                    sources.append(kg)
        for s in sources:
            s.kb_name = kb_name_map.get(s.kb_id, s.kb_name)
        return RetrieveResponse(sources=sources)
    except Exception as e:
        logger.exception("检索失败: %s", e)
        raise HTTPException(status_code=500, detail=f"检索失败: {e}")


@router.get("/history", response_model=List[ChatHistoryItem])
async def list_history(kb_id: Optional[str] = None,
                       db: AsyncSession = Depends(get_db),
                       user: UserPublic = Depends(get_current_user)):
    """会话历史列表：仅本人（super_admin 全部）；kb_id 可选过滤"""
    uid = None if user.role == "super_admin" else user.id
    return get_chat_service().list_sessions(user_id=uid, kb_id=kb_id)


@router.get("/history/{session_id}")
async def get_history(session_id: str,
                      db: AsyncSession = Depends(get_db),
                      user: UserPublic = Depends(get_current_user)):
    """会话详情（owner 或 super_admin，否则 404 伪装）"""
    session = get_chat_service().get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    _check_session_owner(session, user)
    return session


@router.delete("/history/{session_id}")
async def delete_history(request: Request, session_id: str,
                         db: AsyncSession = Depends(get_db),
                         user: UserPublic = Depends(get_current_user)):
    """删除会话（owner 或 super_admin，否则 404 伪装；成功记审计）"""
    session = get_chat_service().get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    _check_session_owner(session, user)
    ok = get_chat_service().delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    await audit_service.record_action(
        user, action="chat.delete", target_type="chat",
        target_id=session_id,
        target_name=(session.title or "会话")[:100], request=request)
    return {"message": "会话已删除"}


@router.post("/history/{session_id}/rename")
async def rename_history(session_id: str, req: RenameSessionRequest,
                         db: AsyncSession = Depends(get_db),
                         user: UserPublic = Depends(get_current_user)):
    """重命名会话（owner 或 super_admin，否则 404 伪装防探测；标题 1~50 字）

    先校验归属再落盘：会话文件不存在 → 404；标题 strip 后为空或超 50 字 → 400。
    重命名会更新 updated_at（_save_session 写回），列表按最新活动置顶。
    """
    title = req.title.strip()
    if not title or len(title) > 50:
        raise HTTPException(status_code=400, detail="标题长度需为 1~50 字")
    chat_svc = get_chat_service()
    session = chat_svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    _check_session_owner(session, user)
    updated = chat_svc.rename_session(session_id, title)
    if not updated:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "会话已重命名", "session_id": session_id, "title": title}


@router.get("/history/{session_id}/export")
async def export_history(request: Request, session_id: str,
                         db: AsyncSession = Depends(get_db),
                         user: UserPublic = Depends(get_current_user)):
    """导出会话为 Markdown 附件（owner 或 super_admin，否则 404 伪装）

    响应：text/markdown; charset=utf-8 + Content-Disposition attachment
    （filename*=UTF-8'' 编码，含中文标题；无消息返回仅含标题的空模板）。
    导出记审计（含消息数摘要）。
    """
    chat_svc = get_chat_service()
    session = chat_svc.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    _check_session_owner(session, user)
    md = chat_svc.build_export_markdown(session)
    await audit_service.record_action(
        user, action="chat.export", target_type="chat",
        target_id=session_id,
        target_name=(session.title or "会话")[:100],
        detail={"message_count": len(session.messages or [])}, request=request)
    filename = quote(f"{session.title or '会话'}.md")
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
