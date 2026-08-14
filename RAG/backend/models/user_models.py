"""用户/部门/知识库 ORM 模型与 Pydantic schema（多租户+团队协作）

- ORM：UserORM / DepartmentORM / KBORM 三张表（MySQL users/departments/kbs，
  测试环境 sqlite 同构，不使用任何 MySQL 专有 SQL）
- Pydantic schema：对外契约（UserPublic 永不包含密码字段）
- 时间统一 "%Y-%m-%d %H:%M:%S" 字符串（与现有 JSON 落盘一致）
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base

# 角色 / 状态枚举（pydantic Literal 约束 + ORM 层字符串，MySQL ENUM 与 sqlite 兼容性考虑）
ROLE_SUPER_ADMIN = "super_admin"
ROLE_DEPT_ADMIN = "dept_admin"
ROLE_USER = "user"

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


def now_str() -> str:
    """当前时间字符串（"%Y-%m-%d %H:%M:%S"）"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def gen_id() -> str:
    """id 生成：uuid4 hex 前 12 位（与现有 JSON 存储一致）"""
    import uuid
    return uuid.uuid4().hex[:12]


# ==================== ORM 模型 ====================

class DepartmentORM(Base):
    """部门（多租户隔离单元）"""
    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))
    # 部门级聊天配置（JSON 字符串，如 {"chat": {...}, "retrieval": {...}}；
    # None=未设置，全部使用全局活跃档案配置；字段级合并：部门只覆盖它
    # 设置的字段，其余用全局）
    chat_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 部门级完整配置（JSON 字符串，如 {"llm": {...}, "chat": {...},
    # "retrieval": {...}}；None=未设置，全部使用全局活跃档案配置）。
    # llm 段为部门级 LLM 配置（字段级覆盖全局）；chat/retrieval 段与
    # 旧 chat_config 列同构——读取兼容：department_config 段优先，
    # 缺失回退旧 chat_config 列（存量数据不强制搬迁，读时合并即可）
    department_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class UserORM(Base):
    """用户（部门内协作；department_id 为空 = 直属全局）"""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))  # super_admin / dept_admin / user
    department_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("departments.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE)
    created_at: Mapped[str] = mapped_column(String(32))
    # 首次登录强制改密标志（0=否/1=是；新建用户=1，admin 种子与存量迁移=0）
    must_change_password: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False)
    # 头像存储标识（如 avatars/{user_id}.png；None=未上传，前端用默认 SVG 兜底）
    avatar: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class KBORM(Base):
    """知识库（MySQL 侧元数据；documents/chat 仍走 JSON）"""
    __tablename__ = "kbs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    department_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("departments.id"), nullable=True)
    owner_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("users.id"), nullable=True)
    doc_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="标签（JSON 数组字符串，如 [\"制度\",\"运维\"]；None=无标签）")
    created_at: Mapped[str] = mapped_column(String(32))


class AuditLogORM(Base):
    """审计操作日志（企业合规：关键操作落库，仅 super_admin 可查询）

    字段全部字符串/文本（MySQL 与 sqlite 同构）；created_at 统一
    "%Y-%m-%d %H:%M:%S"（与其余表一致，字符串字典序即时间序，范围过滤
    直接比较）。detail 为 JSON 字符串（请求体关键字段摘要，不含敏感信息）。
    """
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    username: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    target_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(32), index=True)


class AuditLogPublic(BaseModel):
    """审计记录（对外契约；detail 为 JSON 字符串，前端自行展示）"""
    id: str
    user_id: str
    username: str
    role: str
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    target_name: Optional[str] = None
    detail: Optional[str] = None
    ip: Optional[str] = None
    status: str
    created_at: str


# ==================== Pydantic schema（对外契约） ====================

class UserPublic(BaseModel):
    """用户公开信息（绝不包含密码）"""
    id: str
    username: str
    display_name: str
    role: Literal["super_admin", "dept_admin", "user"]
    department_id: Optional[str] = None
    department_name: Optional[str] = Field(None, description="部门名（列表接口 join 填充）")
    status: Literal["active", "disabled"]
    created_at: str
    must_change_password: bool = Field(
        False, description="首次登录强制改密标志（新建用户为 true）")
    avatar: Optional[str] = Field(
        None, description="头像存储标识（如 avatars/{user_id}.png；None=默认头像）")


class UserCreate(BaseModel):
    """创建用户请求"""
    username: str = Field(..., min_length=1, max_length=64, description="登录名（唯一）")
    password: str = Field(..., min_length=1, max_length=128, description="明文密码（服务端 bcrypt 哈希）")
    display_name: str = Field(..., min_length=1, max_length=64, description="显示名")
    role: Literal["super_admin", "dept_admin", "user"] = Field("user", description="角色")
    department_id: Optional[str] = Field(None, description="所属部门 ID（可为空）")


class UserUpdate(BaseModel):
    """更新用户请求（全可选，传哪个改哪个）"""
    display_name: Optional[str] = Field(None, max_length=64)
    role: Optional[Literal["super_admin", "dept_admin", "user"]] = None
    department_id: Optional[str] = None
    status: Optional[Literal["active", "disabled"]] = None
    password: Optional[str] = Field(None, max_length=128, description="新密码（重哈希）")


class DepartmentPublic(BaseModel):
    """部门公开信息"""
    id: str
    name: str
    description: Optional[str] = None
    created_at: str


class DepartmentCreate(BaseModel):
    """创建部门请求"""
    name: str = Field(..., min_length=1, max_length=64, description="部门名（唯一）")
    description: Optional[str] = Field(None, max_length=512)


class DepartmentUpdate(BaseModel):
    """更新部门请求"""
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    description: Optional[str] = Field(None, max_length=512)


class LoginRequest(BaseModel):
    """登录请求（JSON body，非 form）"""
    username: str = Field(..., min_length=1, description="用户名")
    password: str = Field(..., min_length=1, description="密码")


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., min_length=1, description="旧密码")
    new_password: str = Field(..., min_length=1, max_length=128, description="新密码")
