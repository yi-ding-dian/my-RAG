"""审计日志查询 API：/api/audit（super_admin 全量 / dept_admin 限本部门）

- GET /api/audit/logs    分页查询（倒序）+ 过滤（action/target_type/username/时间范围）；
                         dept_admin 只返回本部门用户的日志（audit_logs.user_id IN
                         本部门用户 id 集合，user_id 与 users.id 同源），分页/筛选
                         在部门过滤后生效；未归属部门 → 403；super_admin 全量不变
- GET /api/audit/actions  可选操作类型列表（静态常量，前端筛选下拉数据源；
                          dept_admin 审计 Tab 同样需要）
- DELETE /api/audit/logs?date=YYYY-MM-DD  按天删除审计记录（仅 super_admin，
                          dept_admin 只读不可删）
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import (require_super_admin,
                          require_super_or_dept_admin)
from backend.models.user_models import AuditLogORM, AuditLogPublic, UserORM
from backend.services.audit_service import AUDIT_ACTION_LABELS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/audit", tags=["审计日志"])


async def _dept_scoped_user_ids(db: AsyncSession,
                                department_id: Optional[str]) -> list[str]:
    """本部门全部用户 id 集合（users.department_id 匹配，含停用账号——
    其历史操作日志仍属本部门，应可追溯）"""
    rows = await db.execute(
        select(UserORM.id).where(UserORM.department_id == department_id))
    return list(rows.scalars().all())


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
    user=Depends(require_super_or_dept_admin),
):
    """审计日志分页查询（created_at 倒序）

    super_admin：全量；dept_admin：只返回本部门用户的操作日志
    （user_id IN 本部门用户集合；登录失败等无归属 user_id 的记录不可见）。
    响应契约: {total, page, page_size, items: [AuditLogPublic, ...]}
    时间过滤基于 created_at 字符串比较（格式固定，字典序即时间序）。
    """
    conditions = []
    if user.role == "dept_admin":
        if not user.department_id:
            raise HTTPException(
                status_code=403,
                detail="部门管理员未归属部门，无法查看日志")
        conditions.append(
            AuditLogORM.user_id.in_(
                await _dept_scoped_user_ids(db, user.department_id)))
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
async def list_audit_actions(
    user=Depends(require_super_or_dept_admin),
):
    """可选操作类型列表（静态常量，前端筛选下拉用；dept_admin 审计 Tab 同用）

    响应契约: {actions: [{action, label}, ...]}
    """
    return {"actions": [
        {"action": a, "label": label} for a, label in AUDIT_ACTION_LABELS.items()
    ]}


@router.delete("/logs")
async def delete_audit_logs_by_date(
    date: str = Query(..., description="删除该天（YYYY-MM-DD）全部审计记录"),
    db: AsyncSession = Depends(get_db),
    user=Depends(require_super_admin),
):
    """按天删除审计记录（删除前前端二次确认；仅 super_admin，dept_admin 只读）

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
