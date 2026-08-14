"""用户服务（多租户 users 表 CRUD）

约束：
- username 唯一（重复 → ValueError → 路由层 409）
- 删除：不能删自己、不能删最后一个 super_admin
- 禁用走 update(status=disabled)，不删除数据
- 每部门唯一 dept_admin：创建/改角色为 dept_admin 时目标部门已有管理员 →
  DeptAdminLimitError（路由层 400"该部门已有一名管理员"）；dept_admin
  降级离开不受限（允许部门暂无管理员，超管可随时任命）
"""
from __future__ import annotations

import logging
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.user_models import (DepartmentORM, UserCreate, UserORM,
                                        UserPublic, UserUpdate, gen_id,
                                        now_str)
from backend.services.auth_service import hash_password

logger = logging.getLogger(__name__)

_ROLES = {"super_admin", "dept_admin", "user"}


class DeptAdminLimitError(ValueError):
    """部门唯一管理员约束违反（路由层转 400，区别于 409 的 username 冲突）"""


async def _to_public(db: AsyncSession, orm: UserORM) -> UserPublic:
    """ORM → UserPublic（join 部门名）"""
    dept_name = None
    if orm.department_id:
        dept = await db.get(DepartmentORM, orm.department_id)
        if dept:
            dept_name = dept.name
    return UserPublic(
        id=orm.id,
        username=orm.username,
        display_name=orm.display_name,
        role=orm.role,
        department_id=orm.department_id,
        department_name=dept_name,
        status=orm.status,
        created_at=orm.created_at,
        must_change_password=bool(orm.must_change_password),
        avatar=orm.avatar,
    )


async def list_users(db: AsyncSession, dept_id: Optional[str] = None) -> List[UserPublic]:
    """用户列表（可选按部门过滤，join 部门名）"""
    stmt = select(UserORM, DepartmentORM.name).outerjoin(
        DepartmentORM, UserORM.department_id == DepartmentORM.id)
    if dept_id:
        stmt = stmt.where(UserORM.department_id == dept_id)
    stmt = stmt.order_by(UserORM.created_at)
    rows = (await db.execute(stmt)).all()
    result = []
    for orm, dept_name in rows:
        result.append(UserPublic(
            id=orm.id, username=orm.username, display_name=orm.display_name,
            role=orm.role, department_id=orm.department_id,
            department_name=dept_name, status=orm.status,
            created_at=orm.created_at,
            must_change_password=bool(orm.must_change_password),
            avatar=orm.avatar))
    return result


async def get(db: AsyncSession, user_id: str) -> Optional[UserPublic]:
    """按 id 查用户（含部门名），不存在返回 None"""
    orm = await db.get(UserORM, user_id)
    if orm is None:
        return None
    return await _to_public(db, orm)


async def get_orm(db: AsyncSession, user_id: str) -> Optional[UserORM]:
    """按 id 查用户 ORM（内部用，如 auth/权限判断需要原始字段）"""
    return await db.get(UserORM, user_id)


async def get_by_username(db: AsyncSession, username: str) -> Optional[UserORM]:
    """按用户名查用户 ORM（唯一索引；内部用）"""
    result = await db.execute(
        select(UserORM).where(UserORM.username == username))
    return result.scalar_one_or_none()


async def count_dept_admins(db: AsyncSession, dept_id: str,
                            exclude_user_id: Optional[str] = None) -> int:
    """部门 dept_admin 数量（exclude_user_id 排除自己，如编辑自己部门时）"""
    stmt = select(func.count()).select_from(UserORM).where(
        UserORM.role == "dept_admin", UserORM.department_id == dept_id)
    if exclude_user_id:
        stmt = stmt.where(UserORM.id != exclude_user_id)
    result = await db.execute(stmt)
    return int(result.scalar() or 0)


async def create(db: AsyncSession, data: UserCreate) -> UserPublic:
    """创建用户（username 唯一性校验失败抛 ValueError → 路由层 409；
    dept_admin 角色且部门已有管理员 → DeptAdminLimitError → 路由层 400）"""
    username = (data.username or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    existing = await get_by_username(db, username)
    if existing:
        raise ValueError("用户名已存在")
    # 每部门唯一 dept_admin（创建时目标部门已有管理员 → 拒绝）
    if data.role == "dept_admin" and data.department_id:
        if await count_dept_admins(db, data.department_id) > 0:
            raise DeptAdminLimitError("该部门已有一名管理员")
    orm = UserORM(
        id=gen_id(),
        username=username,
        password_hash=hash_password(data.password),
        display_name=(data.display_name or username).strip(),
        role=data.role,
        department_id=data.department_id,
        status="active",
        created_at=now_str(),
        must_change_password=1,  # 新建用户首次登录须强制修改密码
    )
    db.add(orm)
    await db.commit()
    await db.refresh(orm)
    logger.info("创建用户: %s (%s)", username, orm.id)
    return await _to_public(db, orm)


async def update(db: AsyncSession, user_id: str, data: UserUpdate,
                 current_user_id: Optional[str] = None) -> Optional[UserPublic]:
    """更新用户（password 存在则重哈希；不存在返回 None）

    超管保护（与 delete 同款，P0）：
    - 不能修改当前登录账号的角色或状态（防误操作把自己禁用/降级 → 系统锁死）
    - 不能把最后一个激活 super_admin 禁用或降级（否则系统无管理员）
    越界 → ValueError → 路由层 400。
    """
    orm = await db.get(UserORM, user_id)
    if orm is None:
        return None
    payload = data.model_dump(exclude_unset=True)
    # 自保护：登录账号的 role/status 不可由自己修改（改 display_name/密码等不受影响）
    if current_user_id and user_id == current_user_id and (
            "role" in payload or "status" in payload):
        raise ValueError("不能修改当前登录账号的角色或状态")
    # 最后一个激活 super_admin 保护：降级（role 改为其他）或禁用均禁止
    if orm.role == "super_admin":
        demote = "role" in payload and payload["role"] != "super_admin"
        disable = payload.get("status") == "disabled"
        if (demote or disable) and await count_active_super_admins(db) <= 1:
            raise ValueError("不能禁用或降级最后一个超级管理员")
    # 每部门唯一 dept_admin：改角色为 dept_admin（或已是 dept_admin 被调整
    # 部门）时，目标部门已有其他管理员 → 拒绝；dept_admin 降级离开不受限
    # （允许部门暂无管理员，超管可随时任命）
    if payload.get("role", orm.role) == "dept_admin":
        new_dept = payload.get("department_id", orm.department_id)
        if new_dept and await count_dept_admins(
                db, new_dept, exclude_user_id=orm.id) > 0:
            raise DeptAdminLimitError("该部门已有一名管理员")
    if "password" in payload:
        password = payload.pop("password")
        if password:
            payload["password_hash"] = hash_password(password)
    for key, value in payload.items():
        setattr(orm, key, value)
    await db.commit()
    await db.refresh(orm)
    logger.info("更新用户: %s", orm.username)
    return await _to_public(db, orm)


async def count_super_admins(db: AsyncSession) -> int:
    """super_admin 总数（防删最后一个）"""
    result = await db.execute(
        select(func.count()).select_from(UserORM)
        .where(UserORM.role == "super_admin"))
    return int(result.scalar() or 0)


async def count_active_super_admins(db: AsyncSession) -> int:
    """激活状态 super_admin 总数（防禁用/降级最后一个可用的超管）"""
    result = await db.execute(
        select(func.count()).select_from(UserORM)
        .where(UserORM.role == "super_admin", UserORM.status == "active"))
    return int(result.scalar() or 0)


async def delete(db: AsyncSession, user_id: str, current_user_id: str) -> bool:
    """删除用户：不存在返回 False；删自己/删最后一个 super_admin 抛 ValueError"""
    orm = await db.get(UserORM, user_id)
    if orm is None:
        return False
    if user_id == current_user_id:
        raise ValueError("不能删除当前登录账号")
    if orm.role == "super_admin" and await count_super_admins(db) <= 1:
        raise ValueError("不能删除最后一个超级管理员")
    await db.delete(orm)
    await db.commit()
    logger.info("删除用户: %s (%s)", orm.username, orm.id)
    return True
