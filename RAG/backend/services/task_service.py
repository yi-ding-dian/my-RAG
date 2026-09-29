"""后台任务状态服务：跨进程的任务抢占、进度读写与取消

配套 `models/task_models.py` 的 TaskORM。原来散在 `dim_check`（重建任务 dict）
与 `documents/crud`（图谱任务 set + Event）里的状态，统一收口到这里，
使任务行为与 worker 数无关。

用法（任务侧）::

    task_id = await claim_task(TASK_TYPE_GRAPH, doc_id)
    if task_id is None:            # 已有 running 任务，本次不重复启动
        return
    cancel_event = asyncio.Event()
    handle = TaskHandle(task_id)
    watcher = asyncio.create_task(watch_cancel(task_id, cancel_event))
    try:
        await handle.set_total(n)
        ...
        await handle.inc_done()
        await handle.finish(TASK_DONE)          # 释放 active_key
    except Exception:
        await handle.finish(TASK_FAILED)
    finally:
        cancel_event.set()                      # 让 watcher 退出
        await asyncio.gather(watcher, return_exceptions=True)

取消侧（任何 worker 的请求都能调）::

    await request_cancel(TASK_TYPE_GRAPH, doc_id)   # 只写库，跨进程生效
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, List, Optional

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError

from backend.db import get_session
from backend.models.task_models import (TASK_FAILED, TASK_RUNNING, TaskORM)
from backend.models.user_models import gen_id, now_str

logger = logging.getLogger(__name__)

# 取消标志轮询间隔（秒）：取消请求写库后，任务侧最多这么多秒响应。
# 图谱构建/向量重建都是分钟级任务，2 秒延迟用户无感；间隔太小则白耗查询。
_CANCEL_POLL_SECONDS = 2


def _active_key(type_: str, target_id: str) -> str:
    return f"{type_}:{target_id}"


# ==================== 抢占与查询 ====================

async def claim_task(type_: str, target_id: str) -> Optional[str]:
    """抢占任务：成功返回新 task_id；该目标已有 running 任务则返回 None

    防重靠 `tasks.active_key` 的 UNIQUE 约束（跨进程安全）——并发抢占时
    只有一个 INSERT 能成功，另一个抛 IntegrityError。这比"先查再插"可靠：
    后者两个 worker 可能同时查到"没有 running"然后都插入。
    """
    task_id = gen_id()
    async with get_session() as session:
        session.add(TaskORM(
            id=task_id, type=type_, target_id=target_id, status=TASK_RUNNING,
            done=0, total=0, failed=0, cancel_requested=0,
            active_key=_active_key(type_, target_id), created_at=now_str()))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return None
    return task_id


async def get_running_task_id(type_: str, target_id: str) -> Optional[str]:
    """该目标当前 running 任务的 id（无则 None）"""
    async with get_session() as session:
        return (await session.execute(
            select(TaskORM.id).where(
                TaskORM.active_key == _active_key(type_, target_id))
        )).scalar_one_or_none()


async def get_latest_task(type_: str, target_id: str) -> Optional[TaskORM]:
    """该目标最近一次任务（含已完成；状态查询用，重启后仍可读）

    **排序：running 优先，其次 created_at 倒序**。为什么 running 优先：
    created_at 是秒级字符串（全库惯例，字典序即时间序），同一秒内的多个
    任务无法用时间区分，id 又是随机 uuid 不能定序——若不优先取 running，
    "快任务完成后同一秒又触发一次"时会返回上一条，前端看到的状态就错了。
    而同一目标此刻若有 running 任务，它必然是用户关心的"当前这次"。

    返回的 ORM 对象在 session 关闭后仍可读普通列（get_session 设了
    expire_on_commit=False，且这些列已加载）。
    """
    async with get_session() as session:
        return (await session.execute(
            select(TaskORM)
            .where(TaskORM.type == type_, TaskORM.target_id == target_id)
            .order_by(case((TaskORM.status == TASK_RUNNING, 0), else_=1),
                      TaskORM.created_at.desc(), TaskORM.id.desc())
            .limit(1)
        )).scalar_one_or_none()


# ==================== 取消（跨进程） ====================

async def request_cancel(type_: str, target_id: str) -> bool:
    """写取消标志（任何 worker 的请求都可调）。无 running 任务返回 False

    只写数据库一列——任务实际跑在哪个 worker 上无关紧要，这是原进程内
    `asyncio.Event` 做不到的。
    """
    async with get_session() as session:
        result = await session.execute(
            update(TaskORM)
            .where(TaskORM.active_key == _active_key(type_, target_id))
            .values(cancel_requested=1))
        await session.commit()
        return bool(result.rowcount)


async def is_cancel_requested(task_id: str) -> bool:
    """该任务是否已收到取消请求"""
    async with get_session() as session:
        flag = (await session.execute(
            select(TaskORM.cancel_requested).where(TaskORM.id == task_id)
        )).scalar_one_or_none()
    return bool(flag)


async def watch_cancel(task_id: str, event: asyncio.Event,
                       interval: float = _CANCEL_POLL_SECONDS) -> None:
    """把数据库取消标志桥接成进程内 `asyncio.Event`（跨进程取消的实现）

    任务主体（如 `build_graph_for_doc`）的检查点仍在检查 `event.is_set()`，
    一行都不用改——桥接层替它把"别的 worker 写的标志"变成"本进程的事件"。

    任务结束后自行退出（发现 status 不再是 running），无需调用方 cancel，
    避免协程泄漏。
    """
    while not event.is_set():
        await asyncio.sleep(interval)
        try:
            async with get_session() as session:
                row = (await session.execute(
                    select(TaskORM.cancel_requested, TaskORM.status)
                    .where(TaskORM.id == task_id))).one_or_none()
        except Exception as e:  # noqa: BLE001
            # 数据库瞬时不可用不该让取消失效，继续轮询
            logger.warning("取消标志轮询失败（继续重试）: task=%s err=%s",
                           task_id, str(e)[:120])
            continue
        if row is None or row[1] != TASK_RUNNING:
            return                     # 任务已结束（或被清理）→ 退出
        if row[0]:
            event.set()
            return


# ==================== 进度读写 ====================

class TaskHandle:
    """任务进度读写句柄（任务主体通过它更新进度，不直接碰 SQL）

    **单写者假设**：一个任务只有一个协程在写进度（任务主体自己），所以
    errors 在内存累积后整体写库，不必每次读回再拼接；`done` / `failed`
    同时在本对象里留一份计数，供任务收尾写日志用（免一次查库）。
    """

    __slots__ = ("task_id", "_errors", "done", "failed")

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._errors: List[Dict] = []
        self.done = 0
        self.failed = 0

    async def set_total(self, total: int) -> None:
        await _update_fields(self.task_id, total=total)

    async def set_current(self, name: Optional[str]) -> None:
        await _update_fields(self.task_id, current_doc=name)

    async def inc_done(self) -> None:
        self.done += 1
        async with get_session() as session:
            await session.execute(
                update(TaskORM).where(TaskORM.id == self.task_id)
                .values(done=TaskORM.done + 1))
            await session.commit()

    async def inc_failed(self, *, doc_id: Optional[str], doc_name: Optional[str],
                         error: str) -> None:
        """记录一个失败文档（failed+1 且明细并入 errors，一次 UPDATE）"""
        self.failed += 1
        self._errors.append({"doc_id": doc_id, "doc_name": doc_name,
                             "error": error})
        async with get_session() as session:
            await session.execute(
                update(TaskORM).where(TaskORM.id == self.task_id)
                .values(failed=TaskORM.failed + 1,
                        errors=json.dumps(self._errors, ensure_ascii=False)))
            await session.commit()

    async def finish(self, status: str) -> None:
        """收尾：置终态 + 释放 active_key（放掉防重占位，允许下次启动）

        写失败只 warning：此时任务已跑完，卡在 running 的后果由启动时的
        reset_stuck_tasks() 兜底，不该让收尾异常冒泡改变调用方的返回。
        """
        try:
            await _update_fields(self.task_id, status=status, active_key=None,
                                 current_doc=None, finished_at=now_str())
        except Exception as e:  # noqa: BLE001
            logger.warning("任务收尾状态写入失败: task=%s err=%s",
                           self.task_id, str(e)[:150])


async def _update_fields(task_id: str, **values) -> None:
    async with get_session() as session:
        await session.execute(
            update(TaskORM).where(TaskORM.id == task_id).values(**values))
        await session.commit()


# ==================== 启动恢复 ====================

async def reset_stuck_tasks() -> int:
    """启动恢复：残留的 running 任务标记为 failed 并释放 active_key

    进程重启后原任务必然已消失，但状态仍占着 `active_key`（UNIQUE 约束），
    不清理则同一目标再也无法启动新任务，前端也一直显示"进行中"。
    与 main.py 既有的 recover_stuck_parsing / recover_stuck_graph_building
    同一模式。
    """
    async with get_session() as session:
        result = await session.execute(
            update(TaskORM).where(TaskORM.status == TASK_RUNNING).values(
                status=TASK_FAILED, active_key=None, current_doc=None,
                finished_at=now_str()))
        await session.commit()
        return int(result.rowcount or 0)
