"""检索质量日志：落库（retrieval_logs 表，见 models/retrieval_log_models.py）

- `log()`：每次检索完成后追加一条（query 截断 100 字，无命中时 hit_doc_ids
  为空数组——保证日粒度 hit_rate = 有命中的检索数 / 总检索数 语义成立，
  zero_hit 判断也准确）
- `read()`：近 window_days 天内该 kb 的条目，按时间正序；走
  `(kb_id, created_at)` 索引，只读该 kb 窗口内的行
  （原 JSONL 版是全量扫 30 天文件 + 逐行 json.loads + 再按 kb_id 过滤，
  复杂度 O(30 天总历史量)）
- 保留 RETENTION_DAYS 天：写入时顺带清理超窗记录，表大小有界

调用位置说明：由 `retrieval_service.retrieve` 成功返回前统一记录（chat SSE
问答与检索测试页共用同一入口），不在路由层重复埋点——所有检索入口自动覆盖，
且不触碰检索核心逻辑。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import delete, select

from backend.db import get_session
from backend.models.retrieval_log_models import RetrievalLogORM
from backend.models.user_models import now_str

logger = logging.getLogger(__name__)

# 日志保留天数（超过该天数的记录在写入时清理）
RETENTION_DAYS = 30
# query 截断长度（防止超长问题撑爆日志）
MAX_QUERY_CHARS = 100


def _read_start(days: int) -> str:
    """读取窗口起点："含今天在内共 days 天"的第一天（日期字符串）

    与原 JSONL 版一致（那边按文件名日期过滤 `fdate < today - (days-1)`）。
    日期字符串与 created_at（"%Y-%m-%d %H:%M:%S"）比较：字典序即时间序。
    """
    start = datetime.now().date() - timedelta(days=max(1, days) - 1)
    return start.strftime("%Y-%m-%d")


def _cleanup_cutoff(days: int) -> str:
    """清理阈值：早于该日期的记录删除

    比读取窗口再多留 1 天（与原实现的边界行为一致：cutoff = today - days，
    删除 `fdate < cutoff`）。
    """
    cutoff = datetime.now().date() - timedelta(days=max(1, days))
    return cutoff.strftime("%Y-%m-%d")


def _to_entry(row: RetrievalLogORM) -> Dict:
    """ORM → 对外契约（保持原 JSONL 的字段名与 ts 格式，调用方无需改动）"""
    try:
        hits = json.loads(row.hit_doc_ids) if row.hit_doc_ids else []
    except (TypeError, ValueError):
        hits = []
    return {
        # 原契约的 ts 是 ISO 格式（datetime.isoformat）；库里按项目惯例存空格
        # 分隔，这里换回 ISO——_build_daily 的 fromisoformat 两种都吃，但保持
        # 契约不变更稳妥
        "ts": row.created_at.replace(" ", "T"),
        "kb_id": row.kb_id,
        "query": row.query,
        "hit_doc_ids": hits,
    }


class RetrievalLogService:

    # ---------------- 写入 ----------------

    async def log(self, kb_id: str, query: str, hit_doc_ids: List[str]) -> None:
        """追加一条检索日志（检索完成时调用，含无命中的空数组条目）

        并发安全由数据库保证（原实现是文件追加 + 线程锁）。写失败仅告警，
        绝不影响检索主流程。
        """
        try:
            async with get_session() as session:
                session.add(RetrievalLogORM(
                    kb_id=kb_id,
                    query=(query or "").strip()[:MAX_QUERY_CHARS],
                    hit_doc_ids=json.dumps(list(hit_doc_ids),
                                           ensure_ascii=False),
                    created_at=now_str()))
                # 顺带清理超窗记录（全表）：让表大小有界，不必另起定时任务
                await session.execute(
                    delete(RetrievalLogORM).where(
                        RetrievalLogORM.created_at < _cleanup_cutoff(RETENTION_DAYS)))
                await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("检索日志写入失败: kb=%s err=%s", kb_id, str(e)[:150])

    # ---------------- 读取 ----------------

    async def read(self, kb_id: str, window_days: int = RETENTION_DAYS) -> List[Dict]:
        """读取近 window_days 天内（含今天）该 kb 的日志条目，按时间正序

        数据不足/查询失败返回空列表（调用方按无数据展示，不报错）。
        """
        try:
            async with get_session() as session:
                rows = (await session.execute(
                    select(RetrievalLogORM)
                    .where(RetrievalLogORM.kb_id == kb_id,
                           RetrievalLogORM.created_at >= _read_start(window_days))
                    .order_by(RetrievalLogORM.created_at, RetrievalLogORM.id)
                )).scalars().all()
        except Exception as e:  # noqa: BLE001
            logger.warning("检索日志读取失败: kb=%s err=%s", kb_id, str(e)[:150])
            return []
        return [_to_entry(r) for r in rows]


_retrieval_log_service: Optional[RetrievalLogService] = None


def get_retrieval_log_service() -> RetrievalLogService:
    global _retrieval_log_service
    if _retrieval_log_service is None:
        _retrieval_log_service = RetrievalLogService()
    return _retrieval_log_service
