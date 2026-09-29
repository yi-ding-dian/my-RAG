"""后台任务状态 ORM（跨 worker / 跨重启共享的任务进度、防重与取消）

背景：知识库向量重建（`services/dim_check.py`）与文档图谱构建
（`routers/documents/crud.py`）原本把任务状态放在模块级 dict / set / Event 里，
单进程够用，多 worker 部署时会分裂：

- 重建任务状态查不到——前端显示"没在跑"，实际在跑；
- 图谱防重失效——"路由校验 → 任务启动"的异步窗口内两个 worker 同时构建；
- 取消信号是进程内 `asyncio.Event`，取消请求打到别的 worker → 409「当前不在
  图谱构建中」，用户点取消点了没用。

落库后与进程数无关，进程重启也不丢（原 `dim_check` 的 JSON 落盘是"只写不读"：
`_load_tasks_from_disk` 只填 kb_id→task_id 映射、从不填充任务字典，重启后
`get_rebuild_status` 恒返回空状态）。

建表走 db.py 的 `create_all`（新表自动创建，无需 MIGRATIONS 条目——那里只
用于给存量表补列）；模型需在 db.py 初始化时被 import 才会注册到 metadata。

任务原语（抢占 / 进度读写 / 取消）见 `services/task_service.py`。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base

# ---- type 字段取值 ----
TASK_TYPE_REBUILD = "rebuild"      # 知识库向量重建（target_id = kb_id）
TASK_TYPE_GRAPH = "graph"          # 文档图谱构建（target_id = doc_id）

# ---- status 字段取值 ----
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"


class TaskORM(Base):
    """后台任务（一行一个任务）

    **active_key 是防重的关键**：running 时为 "{type}:{target_id}"，结束时置
    NULL；配合 UNIQUE 索引（MySQL 与 SQLite 都允许多个 NULL）即可在数据库层
    保证"同一目标同时只有一个 running 任务"——替代原先的进程内 set，跨进程
    天然安全，也不再依赖"校验 → 启动"之间的时序（原实现的异步窗口在多 worker
    下会漏）。

    **cancel_requested 是跨进程取消信号**：取消接口只写这一列，任务侧由
    `task_service.watch_cancel` 轮询桥接成进程内 `asyncio.Event`（Event 无法
    落库；桥接的好处是 `build_graph_for_doc` 的检查点一行不用改）。

    进程崩溃会留下 status=running 的僵尸任务（占着 active_key 导致同一目标
    再也无法启动新任务），由启动时的 `task_service.reset_stuck_tasks()` 清理。

    时间字段与库内其余表一致用 "%Y-%m-%d %H:%M:%S" 字符串（字典序即时间序）；
    errors 为 JSON 字符串（[{doc_id, doc_name, error}]，软失败明细）。
    """
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    done: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    current_doc: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    errors: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0)
    active_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32))
    finished_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        # 防重：同一目标的 running 任务全局唯一（NULL 不参与唯一约束）
        Index("uq_tasks_active_key", "active_key", unique=True),
        # 查最近一次任务：WHERE type=? AND target_id=? ORDER BY created_at DESC
        Index("idx_tasks_target", "type", "target_id", "created_at"),
    )
