"""聊天回答反馈服务（v1 最小闭环）：点赞/点踩落库 + 汇总统计

- create_feedback：用户对某条回答给出 up/down，可选原因（纠错/说明）
- feedback_stats：汇总总数/好评/差评 + 最近 20 条（统计页/质量反哺用）
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
