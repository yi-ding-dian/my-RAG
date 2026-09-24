"""聊天回答反馈服务（v1 最小闭环）：点赞/点踩落库 + 汇总统计

- create_feedback：用户对某条回答给出 up/down，可选原因（纠错/说明）
- feedback_stats：汇总总数/好评/差评 + 最近 20 条（统计页/质量反哺用）
- get_feedback_msg_idxs：某会话被反馈过的消息下标（会话删除时决定归档裁剪范围）
只读+insert 的极简层，无并发写冲突（append 语义）。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from sqlalchemy import func, select

from backend.db import get_session
from backend.models.user_models import FeedbackORM, gen_id, now_str

logger = logging.getLogger(__name__)

_RATINGS = ("up", "down")


async def create_feedback(user_id: str, rating: str, *,
                          kb_id: Optional[str] = None,
                          session_id: Optional[str] = None,
                          msg_idx: int = -1,
                          reason: str = "") -> bool:
    """记录一条回答反馈；rating 非法返回 False（路由层已校验，双保险）"""
    if rating not in _RATINGS:
        return False
    async with get_session() as session:
        session.add(FeedbackORM(
            id=gen_id(),
            user_id=user_id,
            kb_id=kb_id,
            session_id=session_id,
            msg_idx=msg_idx,
            rating=rating,
            reason=(reason or "")[:1000],
            created_at=now_str(),
        ))
        await session.commit()
    return True


async def feedback_stats(recent_limit: int = 20) -> Dict:
    """汇总：总数/好评/差评 + 最近反馈列表（统计页展示/质量反哺）"""
    async with get_session() as session:
        total = (await session.execute(
            select(func.count()).select_from(FeedbackORM))).scalar() or 0
        up = (await session.execute(
            select(func.count()).select_from(FeedbackORM)
            .where(FeedbackORM.rating == "up"))).scalar() or 0
        rows = (await session.execute(
            select(FeedbackORM)
            .order_by(FeedbackORM.created_at.desc())
            .limit(recent_limit))).scalars().all()
    recent: List[dict] = [
        {
            "id": r.id,
            "user_id": r.user_id,
            "kb_id": r.kb_id,
            "session_id": r.session_id,
            "msg_idx": r.msg_idx,
            "rating": r.rating,
            "reason": r.reason or "",
            "created_at": r.created_at,
        }
        for r in rows
    ]
    return {"total": total, "up": up, "down": total - up, "recent": recent}


async def get_feedback_msg_idxs(session_id: str) -> Optional[set]:
    """某会话被反馈过的消息下标集合（会话删除时决定归档怎么裁剪）

    - 返回 None = 查询失败（DB 不可用）：调用方必须退化为"归档**完整**会话"，
      绝不能当成"无反馈"——那会把仍带反馈证据的会话直接物理删掉
    - 返回空集 = 确认该会话没有任何反馈（可安全物理删除，无需归档）
    """
    if not session_id:
        return set()
    try:
        async with get_session() as session:
            rows = (await session.execute(
                select(FeedbackORM.msg_idx)
                .where(FeedbackORM.session_id == session_id))).scalars().all()
        # 过滤非法下标：归档裁剪按下标定位消息，脏数据（负数/非整数）直接丢弃
        return {r for r in rows if isinstance(r, int) and r >= 0}
    except Exception as e:
        logger.warning("查询会话 %s 的反馈下标失败: %s", session_id, e)
        return None


async def get_feedback_msg_idxs_bulk(
        session_ids: List[str]) -> Dict[str, Optional[set]]:
    """批量版：一次查出多个会话的反馈下标（列表页批量删除会话时用）

    单条版每调一次就开一个 DB 会话，批量删 100+ 条会话会是 100+ 次往返；这里合并成
    一次 IN 查询。语义与单条版保持一致：

    - 返回 {session_id: set}：查询成功。**没出现在结果里的 id = 该会话无反馈**（空集）
    - 返回 {session_id: None}：查询失败 —— 调用方必须退化为"归档**完整**会话"，
      绝不能当成"无反馈"而把仍带反馈证据的会话物理删掉
    """
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return {}
    try:
        async with get_session() as session:
            rows = (await session.execute(
                select(FeedbackORM.session_id, FeedbackORM.msg_idx)
                .where(FeedbackORM.session_id.in_(ids)))).all()
        out: Dict[str, set] = {}
        for sid, idx in rows:
            if isinstance(idx, int) and idx >= 0:
                out.setdefault(sid, set()).add(idx)
        return out
    except Exception as e:
        logger.warning("批量查询反馈下标失败（%d 个会话）: %s", len(ids), e)
        return {sid: None for sid in ids}
