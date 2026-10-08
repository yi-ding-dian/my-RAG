"""用户回答反馈：/api/stats/chat-feedback*

从 `routers/stats.py` 拆出。`FeedbackScope` / `feedback_viewer_scope` 是预留的
部门管理员接入点——只改那一处即可收窄范围，路由内已按 scope 组织过滤条件。
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import get_current_user
from backend.models.user_models import (DepartmentORM, FeedbackORM, KBORM,
                                        UserORM, UserPublic)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stats")


class FeedbackScope:
    """反馈数据可见范围（当前恒为全量；预留给部门管理员接入）"""

    def __init__(self, all_departments: bool = True,
                 department_id: Optional[str] = None):
        self.all_departments = all_departments
        self.department_id = department_id


async def feedback_viewer_scope(
        user: UserPublic = Depends(get_current_user)) -> FeedbackScope:
    """反馈数据查看权限（依赖注入点，两个反馈接口共用）

    当前仅 super_admin 可看全量；其余角色 404 伪装（与 require_super_admin
    口径一致：不暴露接口存在性与权限边界）。
    ---- 后续接入部门管理员只改本函数 ----
    增加分支 user.role == "dept_admin" → 返回 FeedbackScope(
        all_departments=False, department_id=user.department_id)：路由内已按
    scope 组织过滤条件（all_departments 为 False 时自动收窄到该部门），
    路由签名与前端均无需改动。
    """
    if user.role != "super_admin":
        raise HTTPException(status_code=404, detail="资源不存在")
    return FeedbackScope()


def _day_bound(value: Optional[str], end: bool) -> Optional[str]:
    """日期参数归一：纯日期补足时分秒，带时间原样返回；空串 → None

    created_at 是 "%Y-%m-%d %H:%M:%S" 字符串，按字符串比较时
    "2026-09-11 08:00:00" <= "2026-09-11" 为 False（同前缀下更长的更大），
    date_to 不补 23:59:59 会把当天的反馈整体漏掉。
    """
    v = (value or "").strip()
    if len(v) == 10 and v.count("-") == 2:
        return v + (" 23:59:59" if end else " 00:00:00")
    return v or None


@router.get("/chat-feedback")
async def chat_feedback_stats(
        scope: FeedbackScope = Depends(feedback_viewer_scope)):
    """用户回答反馈汇总（仅超管）：总数/好评/差评 + 最近反馈"""
    from backend.services.feedback_service import feedback_stats
    return await feedback_stats()


@router.get("/chat-feedback/logs")
async def chat_feedback_logs(
        page: int = Query(1, ge=1, description="页码（从 1 开始）"),
        page_size: int = Query(20, ge=1, le=200, description="每页条数（1~200）"),
        rating: Optional[str] = Query(None, description="评价过滤（up/down），缺省全部"),
        username: Optional[str] = Query(None, description="用户名模糊搜索"),
        department_id: Optional[str] = Query(None, description="部门 ID 过滤"),
        keyword: Optional[str] = Query(None, description="关键词（模糊匹配点踩原因）"),
        date_from: Optional[str] = Query(None, description="起始日期（YYYY-MM-DD）"),
        date_to: Optional[str] = Query(None, description="结束日期（YYYY-MM-DD，含当天）"),
        db: AsyncSession = Depends(get_db),
        scope: FeedbackScope = Depends(feedback_viewer_scope)):
    """聊天反馈分页查询（仅超管；鉴权口径与上方 chat-feedback 汇总接口一致）

    - rating/username/department_id/keyword/date_from/date_to 全可选，多条件
      AND；rating 非法值 400 显式提示（便于前端拼写/大小写错误暴露），其余
      条件为空白串时按未传处理；page>=1、page_size 1~200（默认 20）；按
      created_at 倒序（同秒时按 id 倒序稳定排序，避免同秒提交顺序抖动）
    - 用户名/部门条件走 **outer join**：inner join 会让"用户已注销但反馈仍在"
      的记录在按用户名/部门筛选时凭空消失（展示侧本就对缺失回填空字符串）
    - username/display_name/department_name 查用户与部门表回填（用户已删除/
      无部门 → 空字符串）；kb_name 查知识库表（kb 已删除/从未入库 → 回退 kb_id）
    响应契约: {total, page, page_size, items: [...]}。
    """
    # rating 过滤条件（与反馈提交口径一致仅 up/down；非法值 400 而非静默
    # 空页，便于前端拼写/大小写错误显性暴露）
    conditions = []
    if rating is not None:
        if rating not in ("up", "down"):
            raise HTTPException(status_code=400,
                                detail="rating 仅支持 up（好评）/down（差评）")
        conditions.append(FeedbackORM.rating == rating)
    if username and username.strip():
        conditions.append(UserORM.username.like(f"%{username.strip()}%"))
    if department_id:
        conditions.append(UserORM.department_id == department_id)
    if keyword and keyword.strip():
        conditions.append(FeedbackORM.reason.like(f"%{keyword.strip()}%"))
    d_from = _day_bound(date_from, end=False)
    d_to = _day_bound(date_to, end=True)
    if d_from:
        conditions.append(FeedbackORM.created_at >= d_from)
    if d_to:
        conditions.append(FeedbackORM.created_at <= d_to)
    # 预留：部门管理员只看本部门（当前 scope 恒为全量，不会进该分支；接入
    # dept_admin 时由 feedback_viewer_scope 返回收窄的 scope 即自动生效）
    if not scope.all_departments:
        conditions.append(UserORM.department_id == scope.department_id)

    # outer join：用户注销后反馈记录仍要能展示与筛选（见 docstring）
    on_clause = FeedbackORM.user_id == UserORM.id
    total = (await db.execute(
        select(func.count()).select_from(FeedbackORM)
        .outerjoin(UserORM, on_clause).where(*conditions)
    )).scalar() or 0

    rows = (await db.execute(
        select(FeedbackORM).outerjoin(UserORM, on_clause).where(*conditions)
        .order_by(FeedbackORM.created_at.desc(), FeedbackORM.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    # 用户名映射（users 表；用户已删除 → 空字符串回退）
    user_ids = {r.user_id for r in rows}
    users = {}
    if user_ids:
        users = {u.id: u for u in (
            await db.execute(select(UserORM).where(UserORM.id.in_(user_ids)))
        ).scalars()}
    # 部门名映射（departments 表；用户无部门/部门已删除 → 空字符串）
    dept_ids = {u.department_id for u in users.values() if u.department_id}
    dept_names: Dict[str, str] = {}
    if dept_ids:
        dept_names = {d.id: d.name for d in (
            await db.execute(
                select(DepartmentORM).where(DepartmentORM.id.in_(dept_ids)))
        ).scalars()}
    # 知识库名映射（kbs 表；批量一次查询防 N+1，缺失回退 kb_id）
    kb_ids = {r.kb_id for r in rows if r.kb_id}
    kb_names: Dict[str, str] = {}
    if kb_ids:
        kb_names = {kb.id: kb.name for kb in (
            await db.execute(select(KBORM).where(KBORM.id.in_(kb_ids)))
        ).scalars()}

    items = []
    for r in rows:
        u = users.get(r.user_id)
        dept_id = u.department_id if u else None
        items.append({
            "id": r.id,
            "user_id": r.user_id,
            "username": u.username if u else "",
            "display_name": u.display_name if u else "",
            "department_id": dept_id,
            "department_name": dept_names.get(dept_id, "") if dept_id else "",
            "kb_id": r.kb_id,
            # kb 不存在回退 kb_id（kb_id 为 None 时同样回退 None，原样返回）
            "kb_name": kb_names.get(r.kb_id, r.kb_id),
            "session_id": r.session_id,
            "msg_idx": r.msg_idx,
            "rating": r.rating,
            "reason": r.reason or "",
            "created_at": r.created_at,
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items}
