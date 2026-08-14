"""检索质量日志：data/retrieval_logs/{YYYY-MM-DD}.jsonl

- log(): 每次检索完成后追加一条 {"ts": iso, "kb_id", "query"(前100字),
  "hit_doc_ids": [...]}；无命中时 hit_doc_ids 为空数组（保证日粒度
  hit_rate = 有命中的检索数 / 总检索数 语义成立，zero_hit 判断也准确）
- 按天轮转，保留 30 天（写入/读取时清理过期文件；单例首次创建即清理一次，
  覆盖"启动"与"写入"两个时机）
- 线程锁保证并发追加不交错（单行追加 + OS append 原子性双保险）

调用位置说明：由 retrieval_service.retrieve 成功返回前统一记录（chat
SSE 问答与检索测试页共用同一入口），不在路由层重复埋点——所有检索入口
自动覆盖，且不触碰检索核心逻辑。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from backend.config import DATA_DIR

logger = logging.getLogger(__name__)

# 日志保留天数（超过该天数的 jsonl 文件在写入/读取时清理）
RETENTION_DAYS = 30
# query 落盘截断长度（防止超长问题撑爆日志）
MAX_QUERY_CHARS = 100

LOG_DIR = DATA_DIR / "retrieval_logs"


class RetrievalLogService:

    def __init__(self):
        self._lock = threading.Lock()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self._cleanup_expired()
        except Exception as e:
            logger.warning("检索日志目录初始化清理失败: %s", e)

    # ---------------- 写入 ----------------

    def log(self, kb_id: str, query: str, hit_doc_ids: List[str]) -> None:
        """追加一条检索日志（检索完成时调用，含无命中的空数组条目）"""
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kb_id": kb_id,
            "query": (query or "").strip()[:MAX_QUERY_CHARS],
            "hit_doc_ids": list(hit_doc_ids),
        }
        try:
            with self._lock:
                self._cleanup_expired()
                path = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            # 日志失败绝不影响检索主流程
            logger.warning("检索日志写入失败: kb=%s err=%s", kb_id, e)

    # ---------------- 读取 ----------------

    def read(self, kb_id: str, window_days: int = RETENTION_DAYS) -> List[Dict]:
        """读取近 window_days 天内（含今天）该 kb 的日志条目，按时间正序

        只读当前留存的文件；文件缺失/行损坏静默跳过（数据不足返回空列表，
        调用方按无数据展示，不报错）。
        """
        today = datetime.now().date()
        start = today - timedelta(days=window_days - 1)
        entries: List[Dict] = []
        try:
            for f in sorted(LOG_DIR.glob("*.jsonl")):
                fdate = self._parse_filename_date(f)
                if fdate is None or fdate < start or fdate > today:
                    continue
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("kb_id") == kb_id:
                        entries.append(d)
        except Exception as e:
            logger.warning("检索日志读取失败: kb=%s err=%s", kb_id, e)
        return entries

    # ---------------- 清理 ----------------

    @staticmethod
    def _parse_filename_date(path: Path) -> Optional[object]:
        """从文件名（YYYY-MM-DD.jsonl）解析日期；命名不合规返回 None"""
        try:
            return datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _cleanup_expired(self) -> None:
        """删除超过 RETENTION_DAYS 天的日志文件（命名不合规的文件保留）"""
        cutoff = datetime.now().date() - timedelta(days=RETENTION_DAYS)
        for f in LOG_DIR.glob("*.jsonl"):
            fdate = self._parse_filename_date(f)
            if fdate is not None and fdate < cutoff:
                try:
                    f.unlink(missing_ok=True)
                    logger.info("检索日志过期清理: %s", f.name)
                except OSError as e:
                    logger.warning("检索日志清理失败: %s err=%s", f.name, e)


_retrieval_log_service: Optional[RetrievalLogService] = None


def get_retrieval_log_service() -> RetrievalLogService:
    global _retrieval_log_service
    if _retrieval_log_service is None:
        _retrieval_log_service = RetrievalLogService()
    return _retrieval_log_service
