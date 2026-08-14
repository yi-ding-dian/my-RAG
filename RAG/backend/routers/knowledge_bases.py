"""知识库管理 API：/api/kbs CRUD（多租户权限）

权限矩阵：
- GET 列表：登录；super_admin 全量，其余仅本部门（无部门 → 空列表）
- POST 创建：super_admin（可指定部门，缺省 None）/ dept_admin（强制本部门，忽略 body）/ user → 403
- GET /{id}：can_access_kb（无权限 404 伪装防探测）
- PUT/DELETE /{id}：can_manage_kb（否则 403）
- DELETE 级联：drop collection + 删除全部文档元数据/文件/存储对象
- 计数：列表/详情实时统计（与现状一致），上传/删除文档后由文档路由 refresh_stats 持久化
- 维度冲突（P0）：GET /{id}/vector-status 检测向量维度 vs 模型维度；
  POST /{id}/rebuild-vectors 一键重建（can_manage_kb）；GET /{id}/rebuild-status
  查询重建进度；列表/详情附带 vector_status 摘要
"""
from __future__ import annotations

import asyncio
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import get_current_user, kb_or_404
from backend.models.rag_models import (CreateKBRequest, KnowledgeBase,
                                       TagAggregate, UpdateKBRequest,
                                       UpdateKBTagsRequest)
from backend.models.user_models import UserPublic
from backend.services import audit_service
from backend.services.dim_check import (get_kb_vector_status,
                                        get_rebuild_status, kb_vector_summary,
                                        run_rebuild_task, start_rebuild_task)
from backend.services.document_service import get_document_service
from backend.services.kb_service import get_kb_service
from backend.services.parser_probe import probe_parsers
from backend.services.retrieval_service import get_retrieval_service
from backend.services.storage_service import get_storage_service
from backend.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/kbs", tags=["知识库"])


async def _fill_stats(kbs) -> None:
    """实时统计 doc_count / chunk_count / vector_status 摘要（与现状一致：实时算）"""
    doc_svc = get_document_service()
    for kb in kbs:
        kb.doc_count = doc_svc.count_by_kb(kb.id)
        kb.chunk_count = doc_svc.chunk_count_by_kb(kb.id)
        try:
            # 维度摘要：模型维度有全局缓存，不重复实测；检测失败不阻塞列表
            kb.vector_status = await kb_vector_summary(kb.id)
        except Exception as e:
            logger.warning("向量状态检测失败 kb=%s: %s", kb.id, str(e)[:150])
            kb.vector_status = None


@router.get("", response_model=List[KnowledgeBase])
async def list_kbs(tag: List[str] = Query(default_factory=list,
                                          description="标签过滤（可重复传，交集语义=同时包含全部给定标签）"),
                   db: AsyncSession = Depends(get_db),
                   user: UserPublic = Depends(get_current_user)):
    """获取知识库列表（super_admin 全量，其余仅本部门；实时统计）

    ?tag=a&tag=b 表示只返回同时含标签 a 与 b 的知识库（多选交集）。
    """
    kb_svc = get_kb_service()
    if user.role == "super_admin":
        kbs = await kb_svc.list(db, tags=tag or None)
    elif user.department_id:
        kbs = await kb_svc.list(db, department_id=user.department_id, tags=tag or None)
    else:
        kbs = []  # 无部门的非超管：不可见任何知识库
    await _fill_stats(kbs)
    return kbs


# 注意：/tags 与 /parsers/status 必须注册在 /{kb_id} 之前，否则会被当作 kb_id 匹配
@router.get("/parsers/status")
async def parsers_status(user: UserPublic = Depends(get_current_user)):
    """解析器可用性探测（登录即可）：供解析配置弹窗打开时显示状态徽标

    响应契约: {mineru: {available, reason}, deepdoc: {available, reason},
    plain: {available: True, reason: ""}}；mineru 与 deepdoc 并行探测
    （默认超时 5s/8s，总耗时 ≤8s），探测失败不抛异常（=不可用+原因）。
    """
    return await probe_parsers()


@router.get("/tags", response_model=TagAggregate)
async def list_kb_tags(db: AsyncSession = Depends(get_db),
                       user: UserPublic = Depends(get_current_user)):
    """标签聚合（登录即可）：当前用户可见范围内所有标签及使用计数

    响应契约: {tags: [{name, count}]}，count 降序、同 count 按名称升序。
    前端筛选条用；super_admin 全量，其余仅本部门（与列表可见范围一致）。
    """
    kb_svc = get_kb_service()
    if user.role == "super_admin":
        items = await kb_svc.count_tags(db)
    elif user.department_id:
        items = await kb_svc.count_tags(db, department_id=user.department_id)
    else:
        items = []
    return {"tags": [{"name": n, "count": c} for n, c in items]}


@router.put("/{kb_id}/tags", response_model=KnowledgeBase)
async def set_kb_tags(request: Request, kb_id: str, body: UpdateKBTagsRequest,
                      db: AsyncSession = Depends(get_db),
                      user: UserPublic = Depends(get_current_user)):
    """覆盖式设置知识库标签（can_manage_kb，否则 403）

    body {tags: [...]}：≤10 个、每个 1-20 字符（自动去重去空白），
    空数组=清空；非法输入 400；返回更新后 KB（含实时统计）。
    """
    kb = await kb_or_404(db, kb_id, user, manage=True)
    try:
        updated = await get_kb_service().set_tags(db, kb_id, body.tags)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _fill_stats([updated])
    await audit_service.record_action(
        user, action="kb.tags-update", target_type="kb",
        target_id=kb_id, target_name=updated.name,
        detail={"tags": body.tags}, request=request)
    return updated


@router.post("", response_model=KnowledgeBase)
async def create_kb(request: Request, body: CreateKBRequest,
                    db: AsyncSession = Depends(get_db),
                    user: UserPublic = Depends(get_current_user)):
    """创建知识库（dept_admin 强制本部门，body 中的 department_id 被忽略）"""
    if user.role == "user":
        raise HTTPException(status_code=403,
                            detail="仅超级管理员或部门管理员可创建知识库")
    department_id = body.department_id
    if user.role == "dept_admin":
        if not user.department_id:
            raise HTTPException(status_code=403,
                                detail="当前账号未分配部门，无法创建知识库")
        department_id = user.department_id  # 强制覆盖，防越权
    try:
        kb = await get_kb_service().create(
            db, name=body.name, description=body.description,
            department_id=department_id, owner_id=user.id, tags=body.tags)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit_service.record_action(
        user, action="kb.create", target_type="kb",
        target_id=kb.id, target_name=kb.name,
        detail={"department_id": department_id}, request=request)
    return kb


@router.get("/{kb_id}", response_model=KnowledgeBase)
async def get_kb(kb_id: str, db: AsyncSession = Depends(get_db),
                 user: UserPublic = Depends(get_current_user)):
    """获取知识库详情（无权限 404 伪装）"""
    kb = await kb_or_404(db, kb_id, user)
    await _fill_stats([kb])
    return kb


@router.put("/{kb_id}", response_model=KnowledgeBase)
async def update_kb(request: Request, kb_id: str, body: UpdateKBRequest,
                    db: AsyncSession = Depends(get_db),
                    user: UserPublic = Depends(get_current_user)):
    """更新知识库名称/描述（can_manage_kb，否则 403）"""
    kb = await kb_or_404(db, kb_id, user, manage=True)
    try:
        updated = await get_kb_service().update(
            db, kb_id, name=body.name, description=body.description,
            tags=body.tags)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _fill_stats([updated])
    await audit_service.record_action(
        user, action="kb.update", target_type="kb",
        target_id=kb_id, target_name=updated.name,
        detail={"name": body.name, "tags": body.tags}, request=request)
    return updated


@router.delete("/{kb_id}")
async def delete_kb(request: Request, kb_id: str,
                    db: AsyncSession = Depends(get_db),
                    user: UserPublic = Depends(get_current_user)):
    """删除知识库（can_manage_kb；级联：存储对象 + 文档 + 向量 + 元数据）"""
    kb = await kb_or_404(db, kb_id, user, manage=True)
    # 1) 删除存储对象（原始文档 + 解析图片）
    # include_deleted=True 与 delete_by_kb 口径一致：回收站文档的元数据
    # 也会被级联删除，其 uploads/{name} 与 images/{doc_id}/ 对象必须一并
    # 清理，否则变孤儿对象
    doc_svc = get_document_service()
    storage = get_storage_service()
    for doc in doc_svc.list_by_kb(kb_id, include_deleted=True):
        try:
            await storage.delete(f"uploads/{doc.name}")
            await storage.delete_prefix(f"images/{doc.id}/")
        except Exception as e:
            logger.warning("删除存储对象失败 %s: %s", doc.id, str(e)[:150])
    # 2) 级联删除全部文档（元数据 + uploads/parsed 文件）
    doc_ids = doc_svc.delete_by_kb(kb_id)
    # 3) 删除向量 collection + 失效 BM25 缓存（P2-8：否则重建后的新 kb 可能
    #    复用同 id 的旧索引，或内存中残留已删 kb 的索引）
    get_vector_store().drop_collection(kb_id)
    get_retrieval_service().invalidate_bm25(kb_id)
    # 4) 删除知识库元数据
    await get_kb_service().delete(db, kb_id)
    await audit_service.record_action(
        user, action="kb.delete", target_type="kb",
        target_id=kb_id, target_name=kb.name,
        detail={"deleted_docs": len(doc_ids)}, request=request)
    logger.info("知识库级联删除完成: %s docs=%d", kb_id, len(doc_ids))
    return {"message": "知识库已删除", "deleted_docs": len(doc_ids)}


# ==================== 向量维度状态 + 一键重建（P0） ====================


@router.get("/{kb_id}/vector-status")
async def vector_status(kb_id: str, db: AsyncSession = Depends(get_db),
                        user: UserPublic = Depends(get_current_user)):
    """检测知识库向量维度 vs 当前激活模型维度（can_access_kb，无权限 404 伪装）

    响应契约: {kb_id, collection_vectors, current_dim, model_dim, compatible, message}
    - current_dim: collection 内向量维度（空 collection 为 null）
    - model_dim: 当前激活 embedding 模型实测维度（模型不可用/未配置为 null）
    - compatible: collection 空 / 维度相同 / 模型维度无法检测 → True；明确不匹配 → False
    - dimension 检测时 embedding 调用失败不抛 500：返回 None + 友好 message
    """
    await kb_or_404(db, kb_id, user)
    return await get_kb_vector_status(kb_id)


@router.post("/{kb_id}/rebuild-vectors")
async def rebuild_vectors(request: Request, kb_id: str,
                          db: AsyncSession = Depends(get_db),
                          user: UserPublic = Depends(get_current_user)):
    """一键重建向量（can_manage_kb，否则 403）

    流程：清空 collection 旧向量 → 逐个已入库文档重新 embedding（当前激活模型）
    → 写回；串行执行防内存爆炸；后台任务，进度轮询 GET /rebuild-status。
    响应契约: {task_id}（已有 running 任务时复用，幂等防重复触发）
    """
    kb = await kb_or_404(db, kb_id, user, manage=True)
    task_id = start_rebuild_task(kb_id)
    asyncio.create_task(run_rebuild_task(kb_id, task_id))
    await audit_service.record_action(
        user, action="kb.rebuild-vectors", target_type="kb",
        target_id=kb_id, target_name=kb.name,
        detail={"task_id": task_id}, request=request)
    return {"task_id": task_id}


@router.get("/{kb_id}/rebuild-status")
async def rebuild_status(kb_id: str, db: AsyncSession = Depends(get_db),
                         user: UserPublic = Depends(get_current_user)):
    """查询重建进度（can_access_kb，无权限 404 伪装）

    响应契约: {kb_id, task_id, running, done, total, failed, current_doc,
    finished_at, errors: [{doc_id, doc_name, error}]}
    - 无任务历史时: task_id=null, running=false, done/total/failed=0
    """
    await kb_or_404(db, kb_id, user)
    return get_rebuild_status(kb_id)
