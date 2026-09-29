#!/usr/bin/env python
"""检索日志 JSONL → 数据库迁移：data/retrieval_logs/*.jsonl → retrieval_logs 表

**背景**：检索日志原按天写 JSONL 文件，`read()` 读取时全量扫描 30 天文件 +
逐行 `json.loads` + 按 kb_id 过滤（复杂度 O(30 天总历史量)）。已迁到
`retrieval_logs` 表（走 `(kb_id, created_at)` 索引，O(查询量)）。
本脚本把历史文件导入库，供质量统计与 RAGAS 采样继续使用。

**用法**::

    python scripts/migrate_retrieval_logs_to_db.py            # dry-run（默认，只统计）
    python scripts/migrate_retrieval_logs_to_db.py --execute  # 实际写入

**幂等**：按 `(kb_id, query, created_at)` 跳过库中已存在的记录，可重复执行。
**原 JSONL 文件不删除**（保留作回退；确认无误后可手工清理）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# 允许 `python scripts/xxx.py` 直接跑（把项目根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from backend.config import DATA_DIR  # noqa: E402
from backend.db import get_session, init_db  # noqa: E402
from backend.models.retrieval_log_models import RetrievalLogORM  # noqa: E402

LOG_DIR = DATA_DIR / "retrieval_logs"
_TS_FMT = "%Y-%m-%d %H:%M:%S"
_MAX_QUERY_CHARS = 100


def _parse_ts(raw: str) -> str:
    """JSONL 的 ts（ISO 格式）→ 库内格式（空格分隔）；解析失败返回空串"""
    try:
        return datetime.fromisoformat(str(raw)).strftime(_TS_FMT)
    except (TypeError, ValueError):
        return ""


def _read_files() -> tuple[list[dict], int]:
    """读全部 JSONL，返回 (有效条目, 损坏/不可用行数)"""
    entries: list[dict] = []
    bad = 0
    for f in sorted(LOG_DIR.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            ts = _parse_ts(d.get("ts", ""))
            kb_id = d.get("kb_id")
            if not ts or not kb_id:
                bad += 1
                continue
            entries.append({
                "kb_id": str(kb_id),
                "query": str(d.get("query") or "")[:_MAX_QUERY_CHARS],
                "hit_doc_ids": json.dumps(list(d.get("hit_doc_ids") or []),
                                          ensure_ascii=False),
                "created_at": ts,
            })
    return entries, bad


async def _existing_keys() -> set:
    """库中现有记录的 (kb_id, query, created_at) 集合（幂等去重用）"""
    async with get_session() as session:
        rows = (await session.execute(
            select(RetrievalLogORM.kb_id, RetrievalLogORM.query,
                   RetrievalLogORM.created_at))).all()
    return {(r[0], r[1], r[2]) for r in rows}


async def _count() -> int:
    async with get_session() as session:
        return int((await session.execute(
            select(func.count()).select_from(RetrievalLogORM))).scalar() or 0)


async def _insert(entries: list[dict]) -> None:
    async with get_session() as session:
        session.add_all([RetrievalLogORM(**e) for e in entries])
        await session.commit()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="检索日志 JSONL → retrieval_logs 表（数据库）")
    ap.add_argument("--execute", action="store_true",
                    help="实际写入（默认只 dry-run 统计，不落库）")
    args = ap.parse_args()

    if not LOG_DIR.exists():
        print(f"目录不存在，无需迁移: {LOG_DIR}")
        return 0
    files = sorted(LOG_DIR.glob("*.jsonl"))
    if not files:
        print(f"无 JSONL 文件，无需迁移: {LOG_DIR}")
        return 0

    entries, bad = _read_files()
    print(f"扫描 {len(files)} 个 JSONL 文件：有效 {len(entries)} 条，"
          f"跳过损坏/不可用 {bad} 行")

    asyncio.run(init_db())
    before = asyncio.run(_count())
    existing = asyncio.run(_existing_keys())
    fresh = [e for e in entries
             if (e["kb_id"], e["query"], e["created_at"]) not in existing]
    print(f"库中现有 {before} 条；本次待导入 {len(fresh)} 条"
          f"（按 kb_id+query+时间 跳过重复 {len(entries) - len(fresh)} 条）")

    if not args.execute:
        print("\n[dry-run] 未写入。确认无误后加 --execute 执行导入。")
        return 0
    if not fresh:
        print("\n没有需要导入的新记录。")
        return 0

    asyncio.run(_insert(fresh))
    after = asyncio.run(_count())
    print(f"\n✓ 导入完成：{before} → {after} 条（+{after - before}）")
    print(f"  原 JSONL 文件保留未删（确认无误后可手工清理 {LOG_DIR}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
