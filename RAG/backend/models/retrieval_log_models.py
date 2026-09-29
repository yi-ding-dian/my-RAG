"""检索质量记录 ORM（每次检索落一行）

原实现写 `data/retrieval_logs/{YYYY-MM-DD}.jsonl`，读取时全量扫描 30 天文件
+ 逐行 `json.loads` + 过滤 kb_id —— 复杂度是 **O(30 天总历史量)** 而非
O(查询量)。而 `/api/stats/quality` 的权限是"登录即可"（`get_current_user`，
非超管），任意普通用户点开知识库「质量」页就会触发全量扫描：DAU 300 时约
41MB / 18 万行，10 人同时打开即可打满 CPU。落库后按 `(kb_id, created_at)`
索引只读该 kb 窗口内的行。

建表走 db.py 的 `create_all`（新表自动创建，无需 MIGRATIONS 条目——那里只
用于给存量表补列）；模型需在 db.py 初始化时被 import 才会注册到 metadata。

历史 JSONL 可用 `scripts/migrate_retrieval_logs_to_db.py` 导入（可选）。
"""
from __future__ import annotations

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class RetrievalLogORM(Base):
    """检索记录（一行 = 一次检索；质量统计与 RAGAS 采样的数据源）

    - `created_at` 统一 "%Y-%m-%d %H:%M:%S"（与库内其余表一致，字符串字典序
      即时间序，窗口过滤直接比较）
    - `hit_doc_ids` 存 JSON 数组字符串：命中排行/零命中判断要按**数组元素**
      聚合，SQL 无法直接做，仍由 Python 聚合——但数据量已从"全库 30 天"降到
      "单 kb 窗口内"，且走索引
    - 无命中的检索也落行（`hit_doc_ids` 为空数组），否则日粒度命中率的
      分母不成立
    """
    __tablename__ = "retrieval_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                    autoincrement=True)
    kb_id: Mapped[str] = mapped_column(String(32))
    query: Mapped[str] = mapped_column(String(128))
    hit_doc_ids: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        # 主查询：该 kb 窗口内的记录（质量统计 / RAGAS 采样）
        Index("idx_retrieval_logs_kb_time", "kb_id", "created_at"),
        # 全表清理超窗记录（不带 kb_id 前缀，用不上复合索引）
        Index("idx_retrieval_logs_time", "created_at"),
    )
