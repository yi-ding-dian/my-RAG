"""认证/权限依赖（FastAPI Depends）

- get_current_user：Bearer token → UserPublic（失效/用户不存在/禁用 → 401）
- get_current_user_query_or_header：query ?token= 或 Bearer header 二选一
  （图片鉴权代理专用：<img> 标签无法携带 Authorization header，
  JWT 24h 有效期内进 URL，内网企业环境可接受；校验失败统一 401）
- require_super_admin：非 super_admin → 403
- require_user_admin：super_admin 或 dept_admin → 403（用户/部门管理）
- can_access_kb / can_manage_kb：纯函数（Agent 2 在知识库/文档/会话路由复用）
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.models.user_models import STATUS_ACTIVE, UserPublic
from backend.services.auth_service import decode_token
from backend.services import user_service

# auto_error=False：登录接口是 JSON body 而非 OAuth2 form，由本模块统一抛 401
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/auth/login", auto_error=False)

# 统一 401（防探测：token 缺失/非法/用户不存在/禁用一律同文案）
_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=401,
    detail="登录已过期，请重新登录",
    headers={"WWW-Authenticate": "Bearer"},
)


async def _resolve_user(token: str | None, db: AsyncSession) -> UserPublic:
    """共享鉴权：token → UserPublic；任何失败统一 401"""
    if not token:
        raise _CREDENTIALS_EXCEPTION
    user_id = decode_token(token)
    if not user_id:
        raise _CREDENTIALS_EXCEPTION
    user = await user_service.get(db, user_id)
    if user is None:
        raise _CREDENTIALS_EXCEPTION
    if user.status != STATUS_ACTIVE:
        raise _CREDENTIALS_EXCEPTION
    return user


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> UserPublic:
    """解析 Bearer token → 返回 UserPublic；任何失败统一 401（防探测）"""
    return await _resolve_user(token, db)


async def get_current_user_query_or_header(
    token: str | None = Query(default=None),
    bearer: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> UserPublic:
    """query ?token= 与 Bearer header 二选一（图片代理 <img> 无法带 header）

    - 前端 markdown 渲染时对 /api/files/images/... 的 img src 追加
      ?token=<JWT>（24h 有效期）；header 鉴权路径保持可用
    - 校验逻辑与 get_current_user 完全一致（统一 401 防探测）
    """
    return await _resolve_user(token or bearer, db)


async def require_super_admin(
    user: UserPublic = Depends(get_current_user),
) -> UserPublic:
    """仅超级管理员可访问（系统配置/审计等管理）"""
    if user.role != "super_admin":
        raise HTTPException(status_code=403, detail="仅超级管理员可执行此操作")
    return user


async def require_user_admin(
    user: UserPublic = Depends(get_current_user),
) -> UserPublic:
    """用户/部门管理：super_admin 或 dept_admin（user → 403）"""
    if user.role not in ("super_admin", "dept_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


def can_access_kb(kb, user: UserPublic) -> bool:
    """是否可访问知识库：super_admin 全量；其余要求同部门"""
    if user.role == "super_admin":
        return True
    return bool(kb.department_id) and kb.department_id == user.department_id


def can_manage_kb(kb, user: UserPublic) -> bool:
    """是否可管理知识库（建库/传文档/入库/删除）：super_admin 或本部门 dept_admin"""
    if user.role == "super_admin":
        return True
    return (user.role == "dept_admin"
            and bool(kb.department_id)
            and kb.department_id == user.department_id)


async def kb_or_404(db: AsyncSession, kb_id: str, user: UserPublic,
                    manage: bool = False, detail: str = "知识库不存在"):
    """知识库存在 + 权限校验（收敛各路由重复的"查 kb + 权限判定"样板）

    - 不存在 → 404（与无权限同文案，防存在性探测）
    - manage=False（读）：can_access_kb 不通过 → 404 伪装
    - manage=True（写）：can_manage_kb 不通过 → 403
    - detail: 404 文案（图片代理等特殊伪装场景传各自文案，默认"知识库不存在"）
    - 返回 kb（供路由继续使用）；kb_service 局部导入防循环依赖
    """
    from backend.services.kb_service import get_kb_service
    kb = await get_kb_service().get(db, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=detail)
    if manage:
        if not can_manage_kb(kb, user):
            raise HTTPException(status_code=403,
                                detail="仅超级管理员或本部门管理员可管理知识库")
    elif not can_access_kb(kb, user):
        raise HTTPException(status_code=404, detail=detail)
    return kb
