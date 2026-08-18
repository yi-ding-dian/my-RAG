"""用户管理 API：/api/users（super_admin 全量 / dept_admin 仅本部门成员）

- GET    /api/users                用户列表（?department_id= 过滤；dept_admin 强制本部门）
- POST   /api/users                创建用户（dept_admin 强制本部门、角色限 user/dept_admin）
- PUT    /api/users/{id}           更新用户（含禁用/改密；dept_admin 仅本部门成员，
                                   不能动 super_admin/跨部门/改自己角色/禁自己）
- DELETE /api/users/{id}           删除用户（dept_admin 仅本部门成员，禁删自己/超管/
                                   最后一个 super_admin）
- POST/DELETE /api/users/me/avatar 当前登录用户上传/删除头像（avatar_router，
                                   登录即可，自己传自己的；普通用户不拦截）
越权访问统一 404 伪装（防探测）。
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Optional

from fastapi import (APIRouter, Depends, File, HTTPException, Query, Request,
                     UploadFile)
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import get_current_user, require_user_admin
from backend.models.user_models import (DepartmentORM, UserCreate, UserORM,
                                        UserPublic, UserUpdate)
from backend.services import audit_service, storage_service, user_service

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/users", tags=["用户管理"],
    dependencies=[Depends(require_user_admin)],
)

# 头像上传白名单扩展名（与前端 Upload accept 一致；按文件扩展名校验，
# 不信任浏览器 content_type）
AVATAR_EXT_WHITELIST = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# 头像大小上限：1MB
AVATAR_MAX_BYTES = 1024 * 1024

# 头像接口独立 router（无 require_user_admin 依赖）：登录即可管理自己的头像
avatar_router = APIRouter(prefix="/api/users", tags=["用户头像"])


@avatar_router.post("/me/avatar")
async def upload_my_avatar(
    file: UploadFile = File(..., description="头像图片（jpg/png/webp/gif，≤1MB）"),
    user: UserPublic = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传当前登录用户头像：存 storage avatars/{user_id}.{ext}，更新 users.avatar

    - 校验：扩展名白名单（jpg/jpeg/png/webp/gif）+ 大小 ≤1MB，否则 400
    - 先删旧头像文件再传新文件（storage.delete 不存在时静默，容错），
      避免旧文件残留
    - 返回 {avatar: "avatars/{user_id}.{ext}"}，前端拼 /api/files/avatars/{id} 代理 URL
    """
    filename = (file.filename or "").strip()
    ext = Path(filename).suffix.lower()
    if not filename or ext not in AVATAR_EXT_WHITELIST:
        raise HTTPException(
            status_code=400, detail="仅支持 jpg/png/webp/gif 格式的图片")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(data) > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=400, detail="头像大小不能超过 1MB")

    orm = await db.get(UserORM, user.id)
    if orm is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    key = f"avatars/{user.id}{ext}"
    storage = storage_service.get_storage_service()
    # 先删旧头像（delete 对不存在对象静默，失败仅 warning，不阻断上传）
    if orm.avatar and orm.avatar != key:
        await storage.delete(orm.avatar)
    await storage.upload_bytes(
        key, data, content_type=mimetypes.guess_type(filename)[0] or "image/*")
    orm.avatar = key
    await db.commit()
    logger.info("用户上传头像: %s -> %s", user.username, key)
    return {"avatar": key}


@avatar_router.delete("/me/avatar")
async def delete_my_avatar(
    user: UserPublic = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除当前登录用户头像：删 storage 文件 + 清空 users.avatar（回默认 SVG）

    - 无头像时也返回 200（幂等，前端"恢复默认"按钮可重复点击）
    - 文件删除失败仅 warning（storage.delete 本身容错），字段照常清空
    """
    orm = await db.get(UserORM, user.id)
    if orm is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if orm.avatar:
        await storage_service.get_storage_service().delete(orm.avatar)
        orm.avatar = None
        await db.commit()
        logger.info("用户删除头像: %s", user.username)
    return {"message": "头像已恢复默认", "avatar": None}


async def _check_department(db: AsyncSession, dept_id: Optional[str]):
    """部门引用校验：传了 department_id 但部门不存在 → 400"""
    if not dept_id:
        return
    if await db.get(DepartmentORM, dept_id) is None:
        raise HTTPException(status_code=400, detail="部门不存在")


def _is_dept_admin(user: UserPublic) -> bool:
    return user.role == "dept_admin"


@router.get("")
async def list_users(
    department_id: Optional[str] = Query(None, description="按部门过滤"),
    db: AsyncSession = Depends(get_db),
    user: UserPublic = Depends(require_user_admin),
):
    """用户列表（join 部门名；super_admin 全量，dept_admin 强制本部门）"""
    if _is_dept_admin(user):
        # 无部门归属则无成员可管理（隔离不泄露全局用户）
        if not user.department_id:
            return []
        return await user_service.list_users(db, dept_id=user.department_id)
    return await user_service.list_users(db, dept_id=department_id)


@router.post("", status_code=201)
async def create_user(request: Request, body: UserCreate,
                      user: UserPublic = Depends(require_user_admin),
                      db: AsyncSession = Depends(get_db)):
    """创建用户（username 唯一，password bcrypt 哈希；成功记审计，detail 不含密码）

    dept_admin：department_id 强制本部门（body 指定其他部门被覆盖，与 kb 创建
    同模式）；role 仅允许 user/dept_admin（super_admin → 400）。
    """
    if _is_dept_admin(user):
        if not user.department_id:
            raise HTTPException(status_code=403,
                                detail="当前账号未分配部门，无法创建用户")
        if body.role == "super_admin":
            raise HTTPException(status_code=400, detail="部门管理员不能创建超级管理员")
        body.department_id = user.department_id  # 强制覆盖，防越权
    else:
        await _check_department(db, body.department_id)
    try:
        result = await user_service.create(db, body)
    except user_service.DeptAdminLimitError as e:
        # 部门唯一管理员约束（400，区别于 username 冲突的 409）
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await audit_service.record_action(
        user, action="user.create", target_type="user",
        target_id=result.id, target_name=result.username,
        detail={"role": result.role, "department_id": result.department_id},
        request=request)
    return result


@router.put("/{user_id}")
async def update_user(request: Request, user_id: str, body: UserUpdate,
                      user: UserPublic = Depends(require_user_admin),
                      db: AsyncSession = Depends(get_db)):
    """更新用户（display_name/role/department_id/status/password 可部分更新）

    dept_admin：仅本部门成员；不能修改 super_admin（404 伪装）、不能跨部门、
    不能改 role 为 super_admin、不能修改自己的 role/禁用自己。
    成功记审计（操作者为当前账号），detail 仅摘要非敏感字段（password 绝不落库）。
    """
    target = await user_service.get_orm(db, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    payload = body.model_dump(exclude_unset=True)
    if _is_dept_admin(user):
        # 越权访问一律 404 伪装（无部门归属/跨部门成员/super_admin 用户）
        if (not user.department_id
                or target.department_id != user.department_id
                or target.role == "super_admin"):
            raise HTTPException(status_code=404, detail="用户不存在")
        if "role" in payload and payload["role"] == "super_admin":
            raise HTTPException(status_code=400, detail="不能将用户设置为超级管理员")
        if "department_id" in payload and payload["department_id"] != user.department_id:
            raise HTTPException(status_code=400, detail="不能将用户分配到其他部门")
        if user_id == user.id:
            if "role" in payload:
                raise HTTPException(status_code=400, detail="不能修改自己的角色")
            if payload.get("status") == "disabled":
                raise HTTPException(status_code=400, detail="不能禁用当前登录账号")
    else:
        await _check_department(db, body.department_id)
    try:
        # current_user_id 传入服务层：超管保护（禁自己/禁最后一个超管 → 400）
        result = await user_service.update(
            db, user_id, body, current_user_id=user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    changed = body.model_dump(exclude_none=True)
    await audit_service.record_action(
        user, action="user.update", target_type="user",
        target_id=user_id, target_name=result.username,
        detail={k: changed[k] for k in ("display_name", "role",
                                        "department_id", "status")
                if k in changed},
        request=request)
    return result


@router.delete("/{user_id}")
async def delete_user(user_id: str,
                      request: Request,
                      user: UserPublic = Depends(require_user_admin),
                      db: AsyncSession = Depends(get_db)):
    """删除用户：禁删自己、禁删最后一个超级管理员（409）；成功记审计（目标用户名）

    dept_admin：仅本部门成员；super_admin 用户与跨部门成员一律 404 伪装。
    """
    if _is_dept_admin(user):
        target = await user_service.get_orm(db, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        # 越权访问一律 404 伪装（无部门归属/跨部门成员/super_admin 用户）
        if (not user.department_id
                or target.department_id != user.department_id
                or target.role == "super_admin"):
            raise HTTPException(status_code=404, detail="用户不存在")
        target_public = await user_service.get(db, user_id)
    else:
        target_public = await user_service.get(db, user_id)
    try:
        ok = await user_service.delete(db, user_id, current_user_id=user.id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="用户不存在")
    # 连带删除头像对象（key 与上传时一致 avatars/{user_id}.{ext}，orm.avatar
    # 即上传时持久化的 key，严格复用同一拼法）；失败仅 warning，不阻断用户删除
    if target_public and target_public.avatar:
        try:
            await storage_service.get_storage_service().delete(target_public.avatar)
        except Exception as e:
            logger.warning("删除用户头像失败 %s: %s",
                           target_public.avatar, str(e)[:150])
    # 连带删除用户画像文件（data/user_memory/{user_id}.json；不存在静默）
    try:
        from backend.services.user_memory_service import get_user_memory_service
        get_user_memory_service().delete_file(user_id)
    except Exception as e:
        logger.warning("删除用户画像文件失败 %s: %s", user_id, str(e)[:150])
    await audit_service.record_action(
        user, action="user.delete", target_type="user",
        target_id=user_id,
        target_name=target_public.username if target_public else user_id,
        request=request)
    return {"message": "用户已删除"}
