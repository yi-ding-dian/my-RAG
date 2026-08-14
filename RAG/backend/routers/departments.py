"""部门管理 API：/api/departments（super_admin 全量 / dept_admin 仅本部门）

- GET    /api/departments       部门列表（dept_admin 仅返回本部门）
- POST   /api/departments       创建部门（仅 super_admin）
- PUT    /api/departments/{id}  更新部门（dept_admin 可编辑本部门名称/描述）
- DELETE /api/departments/{id}  删除部门（仅 super_admin；有用户/知识库引用 → 409）
越权访问统一 404 伪装（防探测）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import require_super_admin, require_user_admin
from backend.models.user_models import (DepartmentCreate, DepartmentUpdate,
                                        UserPublic)
from backend.services import audit_service, department_service

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/departments", tags=["部门管理"],
    dependencies=[Depends(require_user_admin)],
)


@router.get("")
async def list_departments(db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(require_user_admin)):
    """部门列表：super_admin 全量；dept_admin 仅本部门（无归属则空）"""
    if user.role == "dept_admin":
        if not user.department_id:
            return []
        dept = await department_service.get(db, user.department_id)
        return [dept] if dept else []
    return await department_service.list_departments(db)


@router.post("", status_code=201)
async def create_department(request: Request, body: DepartmentCreate,
                            user: UserPublic = Depends(require_super_admin),
                            db: AsyncSession = Depends(get_db)):
    """创建部门（name 唯一；仅 super_admin，dept_admin → 403；成功记审计）"""
    try:
        result = await department_service.create(db, body)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await audit_service.record_action(
        user, action="dept.create", target_type="dept",
        target_id=result.id, target_name=result.name, request=request)
    return result


@router.put("/{dept_id}")
async def update_department(request: Request, dept_id: str,
                            body: DepartmentUpdate,
                            user: UserPublic = Depends(require_user_admin),
                            db: AsyncSession = Depends(get_db)):
    """更新部门（改名校验唯一性；dept_admin 仅本部门，其他部门 404 伪装；记审计）"""
    if user.role == "dept_admin":
        # 越权访问一律 404 伪装（防探测）
        if dept_id != user.department_id:
            raise HTTPException(status_code=404, detail="部门不存在")
    try:
        result = await department_service.update(db, dept_id, body)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="部门不存在")
    await audit_service.record_action(
        user, action="dept.update", target_type="dept",
        target_id=result.id, target_name=result.name,
        detail=body.model_dump(exclude_none=True), request=request)
    return result


@router.delete("/{dept_id}")
async def delete_department(dept_id: str,
                            request: Request,
                            user: UserPublic = Depends(require_super_admin),
                            db: AsyncSession = Depends(get_db)):
    """删除部门：仅 super_admin，dept_admin → 403；有用户/知识库引用 → 409；
    成功记审计（目标部门名）"""
    target = await department_service.get(db, dept_id)
    try:
        ok = await department_service.delete(db, dept_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="部门不存在")
    await audit_service.record_action(
        user, action="dept.delete", target_type="dept",
        target_id=dept_id,
        target_name=target.name if target else dept_id,
        request=request)
    return {"message": "部门已删除"}
