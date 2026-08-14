"""认证 API：/api/auth

- POST /api/auth/login          登录（公开，JSON body）→ {access_token, token_type, user}
- GET  /api/auth/me             当前用户信息（登录）
- POST /api/auth/change-password 修改密码（登录，校验旧密码）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import get_current_user
from backend.models.user_models import (ChangePasswordRequest, LoginRequest,
                                        UserPublic)
from backend.services import audit_service, auth_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/login")
async def login(request: Request, body: LoginRequest,
                db: AsyncSession = Depends(get_db)):
    """登录：用户名+密码 → JWT token（Bearer，24h 过期）

    失败统一 401「用户名或密码错误」（不区分用户不存在/密码错/禁用，防枚举）
    审计：成功/失败均记录（失败时操作对象记在 target_name/detail，防枚举
    文案不变）。
    """
    user = await auth_service.login(db, body.username, body.password)
    if user is None:
        await audit_service.record_action(
            None, action="auth.login", target_type="user",
            target_name=body.username, detail={"username": body.username},
            status="failed", request=request)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    await audit_service.record_action(
        user, action="auth.login", target_type="user",
        target_id=user.id, target_name=user.username,
        status="success", request=request)
    token = auth_service.create_access_token(user.id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": UserPublic(
            id=user.id, username=user.username, display_name=user.display_name,
            role=user.role, department_id=user.department_id,
            department_name=None, status=user.status, created_at=user.created_at,
            must_change_password=bool(user.must_change_password),
            avatar=user.avatar,
        ),
    }


@router.get("/me")
async def me(user: UserPublic = Depends(get_current_user)) -> UserPublic:
    """当前登录用户信息（前端恢复会话用）"""
    return user


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: UserPublic = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """修改密码：校验旧密码正确后重哈希（成功记审计，detail 不落密码）"""
    from backend.models.user_models import UserORM
    orm = await db.get(UserORM, user.id)
    if orm is None or not auth_service.verify_password(
            body.old_password, orm.password_hash):
        raise HTTPException(status_code=400, detail="旧密码不正确")
    if body.new_password == body.old_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")
    orm.password_hash = auth_service.hash_password(body.new_password)
    # 改密成功即清除首次登录强制改密标志（无论是否处于强制阶段）
    if orm.must_change_password:
        orm.must_change_password = 0
    await db.commit()
    await audit_service.record_action(
        user, action="auth.change-password", target_type="user",
        target_id=user.id, target_name=user.username, request=request)
    logger.info("用户修改密码: %s", user.username)
    return {"message": "密码修改成功"}
