"""审计日志查询 API：/api/audit（全部仅超级管理员，require_super_admin）

- GET /api/audit/logs    分页查询（倒序）+ 过滤（action/target_type/username/时间范围）
- GET /api/audit/actions  可选操作类型列表（前端筛选下拉数据源）
- DELETE /api/audit/logs?date=YYYY-MM-DD  按天删除审计记录（created_at 前缀匹配）
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import require_super_admin
from backend.models.user_models import AuditLogORM, AuditLogPublic
from backend.services.audit_service import AUDIT_ACTION_LABELS

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/audit", tags=["审计日志"],
    dependencies=[Depends(require_super_admin)],
)


@router.get("/logs")
async def list_audit_logs(
    page: int = Query(1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(20, ge=1, le=200, description="每页条数（1~200）"),
    action: Optional[str] = Query(None, description="操作类型精确过滤（如 kb.create）"),
    target_type: Optional[str] = Query(None, description="目标类型精确过滤（如 kb/doc/user/config）"),
    username: Optional[str] = Query(None, description="用户名模糊过滤"),
    start_time: Optional[str] = Query(None, description="开始时间（含，%Y-%m-%d %H:%M:%S）"),
    end_time: Optional[str] = Query(None, description="结束时间（含，%Y-%m-%d %H:%M:%S）"),
    db: AsyncSession = Depends(get_db),
):
    """审计日志分页查询（created_at 倒序）

    响应契约: {total, page, page_size, items: [AuditLogPublic, ...]}
    时间过滤基于 created_at 字符串比较（格式固定，字典序即时间序）。
    """
    conditions = []
    if action:
        conditions.append(AuditLogORM.action == action)
    if target_type:
        conditions.append(AuditLogORM.target_type == target_type)
    if username:
        conditions.append(AuditLogORM.username.like(f"%{username}%"))
    if start_time:
        conditions.append(AuditLogORM.created_at >= start_time)
    if end_time:
        conditions.append(AuditLogORM.created_at <= end_time)

    total = (await db.execute(
        select(func.count()).select_from(AuditLogORM).where(*conditions)
    )).scalar() or 0

    stmt = (select(AuditLogORM).where(*conditions)
            .order_by(AuditLogORM.created_at.desc(), AuditLogORM.id.desc())
            .offset((page - 1) * page_size).limit(page_size))
    rows = (await db.execute(stmt)).scalars().all()

    items = [
        AuditLogPublic(
            id=r.id, user_id=r.user_id, username=r.username, role=r.role,
            action=r.action, target_type=r.target_type, target_id=r.target_id,
            target_name=r.target_name, detail=r.detail, ip=r.ip,
            status=r.status, created_at=r.created_at,
        ) for r in rows
    ]
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/actions")
async def list_audit_actions():
    """可选操作类型列表（静态常量，前端筛选下拉用）

    响应契约: {actions: [{action, label}, ...]}
    """
    return {"actions": [
        {"action": a, "label": label} for a, label in AUDIT_ACTION_LABELS.items()
    ]}


@router.delete("/logs")
async def delete_audit_logs_by_date(
    date: str = Query(..., description="删除该天（YYYY-MM-DD）全部审计记录"),
    db: AsyncSession = Depends(get_db),
):
    """按天删除审计记录（删除前前端二次确认）

    created_at 为 '%Y-%m-%d %H:%M:%S' 字符串，按前缀 LIKE 'YYYY-MM-DD%' 匹配
    （sqlite/MySQL 均稳定）。返回 {message, deleted}。
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式须为 YYYY-MM-DD")
    result = await db.execute(
        delete(AuditLogORM).where(AuditLogORM.created_at.like(f"{date}%")))
    await db.commit()
    return {"message": f"已删除 {date} 的审计记录", "deleted": result.rowcount}
