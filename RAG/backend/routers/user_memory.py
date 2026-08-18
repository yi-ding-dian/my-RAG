"""用户画像/偏好记忆 API：/api/users/{user_id}/memory

权限矩阵（设计决策点 5）：
- GET  本人（任意角色）/ super_admin 全量 / dept_admin 仅本部门成员；
      越权访问一律 404 伪装（防存在性探测，与 users.py 同模式）
- PUT  仅本人（enabled 开关 / items 条目全量替换，至少传一个）；
      管理员只读 → 403（语义明确：管理员可看不可改）；普通用户访问他人 → 404 伪装
- DELETE 仅本人（?item_id= 删单条；缺省清空全部）；写权限判定同 PUT
- 未登录 401（get_current_user）；目标用户不存在 → 404

数据存储：data/user_memory/{user_id}.json（user_memory_service）。
"""
from __future__ import annotations

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import get_current_user
from backend.models.user_models import UserPublic
from backend.services import user_service
from backend.services.user_memory_service import get_user_memory_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["用户画像"])


class UserMemoryItemInput(BaseModel):
    """条目载荷（id 可选：有 id=更新既有条目，无 id=新增）"""
    id: Optional[str] = Field(None, max_length=64)
    type: Literal["profile", "preference"] = "profile"
    content: str = Field(..., min_length=1, max_length=500)
    confidence: Optional[float] = Field(None, ge=0, le=1)


class UserMemoryUpdate(BaseModel):
    """更新载荷：enabled 开关 / items 全量替换（至少传一个）"""
    enabled: Optional[bool] = None
    items: Optional[List[UserMemoryItemInput]] = Field(None, max_length=200)


async def _target_or_404(db: AsyncSession, user_id: str) -> UserPublic:
    """目标用户存在校验（不存在 → 404）"""
    target = await user_service.get(db, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return target


def _check_read(user: UserPublic, target: UserPublic) -> None:
    """读权限：本人 / super_admin 全量 / dept_admin 仅本部门（越权 404 伪装）"""
    if user.id == target.id or user.role == "super_admin":
        return
    if (user.role == "dept_admin" and user.department_id
            and target.department_id == user.department_id):
        return
    raise HTTPException(status_code=404, detail="用户不存在")


def _check_write(user: UserPublic, target: UserPublic) -> None:
    """写权限：仅本人；管理员（super_admin/dept_admin）只读 → 403；
    普通用户访问他人 → 404 伪装（防探测）"""
    if user.id == target.id:
        return
    if user.role in ("super_admin", "dept_admin"):
        raise HTTPException(status_code=403,
                            detail="仅本人可编辑用户画像，管理员只读")
    raise HTTPException(status_code=404, detail="用户不存在")


@router.get("/{user_id}/memory")
async def get_user_memory(user_id: str, db: AsyncSession = Depends(get_db),
                          user: UserPublic = Depends(get_current_user)):
    """查看用户画像（本人 / 超管全量 / dept_admin 本部门；越权 404 伪装）"""
    target = await _target_or_404(db, user_id)
    _check_read(user, target)
    data = get_user_memory_service().get_memory(user_id)
    return {
        "user_id": user_id,
        "memory_enabled": data.get("memory_enabled", True),
        "updated_at": data.get("updated_at", ""),
        "items": data.get("items", []),
    }


@router.put("/{user_id}/memory")
async def update_user_memory(user_id: str, body: UserMemoryUpdate,
                             db: AsyncSession = Depends(get_db),
                             user: UserPublic = Depends(get_current_user)):
    """更新本人画像：enabled 开关 / items 条目全量替换（管理员只读 403）"""
    target = await _target_or_404(db, user_id)
    _check_write(user, target)
    if body.enabled is None and body.items is None:
        raise HTTPException(status_code=400,
                            detail="enabled 与 items 至少传一个")
    data = get_user_memory_service().update_memory(
        user_id, enabled=body.enabled,
        items=([i.model_dump() for i in body.items]
               if body.items is not None else None))
    logger.info("用户更新画像: %s (enabled=%s items=%d)", user_id,
                body.enabled,
                len(body.items) if body.items is not None else -1)
    return {
        "user_id": user_id,
        "memory_enabled": data.get("memory_enabled", True),
        "updated_at": data.get("updated_at", ""),
        "items": data.get("items", []),
    }


@router.delete("/{user_id}/memory")
async def delete_user_memory(
    user_id: str,
    item_id: Optional[str] = Query(None, description="条目 id；缺省=清空全部"),
    db: AsyncSession = Depends(get_db),
    user: UserPublic = Depends(get_current_user),
):
    """删除本人画像条目（?item_id= 删单条；不带清空全部；管理员只读 403）"""
    target = await _target_or_404(db, user_id)
    _check_write(user, target)
    svc = get_user_memory_service()
    if item_id:
        if not svc.delete_item(user_id, item_id):
            raise HTTPException(status_code=404, detail="画像条目不存在")
        return {"message": "条目已删除"}
    svc.clear(user_id)
    return {"message": "用户画像已清空"}
