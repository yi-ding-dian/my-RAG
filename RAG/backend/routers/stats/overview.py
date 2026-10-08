"""系统统计概览：/api/stats

- GET /api/stats          统计（按角色过滤：super_admin 全量 / dept_admin
                          本部门 kb/doc/chunk + 本部门用户会话 / user 本部门
                          kb/doc/chunk + 自己会话；chunk 数取 document_service
                          统计，排除软删，与知识库列表/详情一致）
- GET /api/stats/quality  检索质量统计（近 30 天检索日志汇总：总次数/平均命中/
                          文档命中排行/零命中文档/日粒度）

RAGAS 评估见 ragas.py、用户反馈见 feedback.py——三块原本挤在同一个 963 行
文件里，改 RAGAS 要滚过反馈的代码。
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import get_current_user, kb_or_404
from backend.models.user_models import UserPublic
from backend.services.chat.service import get_chat_service
from backend.services.document.service import get_document_service
from backend.services.kb_service import get_kb_service
from backend.services.retrieval_log import get_retrieval_log_service
from backend.services.user_service import list_users

logger = logging.getLogger(__name__)
# 三个子 router 各自带 /api/stats 前缀，父 router 只做聚合与打标。
# 不能只让父带前缀：概览端点路径是空串 ""，父子都空会触发 FastAPI
# "Prefix and path cannot be both empty"
router = APIRouter(prefix="/api/stats")


@router.get("")
async def get_stats(db: AsyncSession = Depends(get_db),
                    user: UserPublic = Depends(get_current_user)):
    """自身统计：知识库/文档/切块/会话/消息（按角色过滤）"""
    kb_svc = get_kb_service()
    doc_svc = get_document_service()
    chat_svc = get_chat_service()

    # 1) 知识库范围（super_admin 全量；dept_admin/user 本部门；无部门 → 空）
    if user.role == "super_admin":
        kbs = await kb_svc.list(db)
    elif user.department_id:
        kbs = await kb_svc.list(db, department_id=user.department_id)
    else:
        kbs = []

    # 2) 会话范围（super_admin 全部；dept_admin 本部门用户；user 自己）
    if user.role == "super_admin":
        sessions = chat_svc.list_sessions(None)
    elif user.role == "dept_admin":
        if user.department_id:
            users = await list_users(db, user.department_id)
            sessions = chat_svc.list_sessions({u.id for u in users})
        else:
            sessions = []
    else:
        sessions = chat_svc.list_sessions(user.id)

    doc_count = sum(doc_svc.count_by_kb(kb.id) for kb in kbs)
    # P1-1: 切块数改用 document_service 统计（排除软删文档，与知识库列表/详情
    # 的 chunk_count 语义一致；Chroma count 含软删向量会导致两处数字不一致）
    chunk_count = sum(doc_svc.chunk_count_by_kb(kb.id) for kb in kbs)
    message_count = sum(s.message_count for s in sessions)

    return {
        "kb_count": len(kbs),
        "doc_count": doc_count,
        "chunk_count": chunk_count,
        "session_count": len(sessions),
        "message_count": message_count,
    }


# 检索质量统计窗口（天）
QUALITY_WINDOW_DAYS = 30


def _build_daily(entries) -> list:
    """近 30 天日粒度汇总（含今天，按日期正序；无检索的天 hit_rate=0）"""
    today = datetime.now().date()
    days = [today - timedelta(days=i) for i in range(QUALITY_WINDOW_DAYS - 1, -1, -1)]
    by_date: dict = defaultdict(list)
    for e in entries:
        try:
            d = datetime.fromisoformat(e.get("ts", "")).date()
        except (ValueError, TypeError):
            continue
        by_date[d].append(e)
    out = []
    for d in days:
        day_entries = by_date.get(d, [])
        total = len(day_entries)
        hit = sum(1 for e in day_entries if e.get("hit_doc_ids"))
        out.append({
            "date": d.isoformat(),
            "retrievals": total,
            # 命中率 = 当日有命中的检索数 / 当日检索总数
            "hit_rate": round(hit / total, 4) if total else 0,
        })
    return out


@router.get("/quality")
async def get_retrieval_quality(kb_id: str,
                                db: AsyncSession = Depends(get_db),
                                user: UserPublic = Depends(get_current_user)):
    """检索质量统计（近 30 天，登录即可；kb 不可访问 404 伪装）

    返回 {kb_id, window_days, total_retrievals, avg_hits_per_retrieval,
    hit_docs(文档命中排行 top10 降序), zero_hit_docs(窗口期从未命中的
    ingested 文档), daily(日粒度检索数/命中率)}；无检索数据时返回空数组不报错。
    """
    await kb_or_404(db, kb_id, user)

    entries = await get_retrieval_log_service().read(
        kb_id, window_days=QUALITY_WINDOW_DAYS)

    # 文档名/切块数映射（零命中文档判断用）
    docs = get_document_service().list_by_kb(kb_id)
    name_map = {d.id: d.original_name for d in docs}

    # 文档命中排行（按 hit_doc_ids 内出现的次数，top10 降序）
    counter: Counter = Counter()
    for e in entries:
        for doc_id in e.get("hit_doc_ids", []):
            counter[doc_id] += 1
    hit_docs = [{"doc_id": did, "doc_name": name_map.get(did, did), "hits": n}
                for did, n in counter.most_common(10)]

    # 零命中文档：窗口期内从未出现在任何检索命中的 ingested 文档
    hit_ids = set(counter)
    zero_hit_docs = [
        {"doc_id": d.id, "doc_name": d.original_name, "chunks": d.chunk_count}
        for d in docs if d.status == "ingested" and d.id not in hit_ids
    ]

    total = len(entries)
    avg_hits = (round(sum(len(e.get("hit_doc_ids", [])) for e in entries) / total, 2)
                if total else 0)
    return {
        "kb_id": kb_id,
        "window_days": QUALITY_WINDOW_DAYS,
        "total_retrievals": total,
        "avg_hits_per_retrieval": avg_hits,
        "hit_docs": hit_docs,
        "zero_hit_docs": zero_hit_docs,
        "daily": _build_daily(entries),
    }

