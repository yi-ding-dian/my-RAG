"""后台任务服务测试：跨进程的任务抢占、进度读写与取消

覆盖（对应 `services/task_service.py` + `models/task_models.py`）：
- 抢占防重：同目标第二次 claim 返回 None；不同目标互不影响
  （靠 tasks.active_key 的 UNIQUE 约束，不是"先查再插"）
- finish 释放 active_key → 同目标可再次抢占
- TaskHandle 进度落库：total / current_doc / done / failed / errors
- 取消：request_cancel → is_cancel_requested；watch_cancel 把库标志桥接成
  进程内 Event；任务结束后 watcher 自行退出（不泄漏协程）
- reset_stuck_tasks 清理僵尸任务并释放 active_key（进程崩溃后的启动恢复）
- **真·多进程**：进程 A 抢占并写取消标志，进程 B（全新解释器）能读到——
  这是本次从进程内 set/Event 迁到数据库的核心保证

隔离方式同 test_rate_limit.py：独立 sqlite 文件库 + NullPool，并把
`task_service.get_session` 替换为指向该库（避免与 TestClient 全局 engine
的 event loop 冲突）。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from backend.models.task_models import (TASK_DONE, TASK_FAILED, TASK_RUNNING,
                                        TASK_TYPE_GRAPH, TASK_TYPE_REBUILD,
                                        TaskORM)
from backend.services import task_service
from backend.services.task_service import TaskHandle


@pytest.fixture
def ts_db(tmp_path, monkeypatch):
    """独立 sqlite 文件库（只建 tasks 表）+ 替换 task_service 会话工厂"""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}", poolclass=NullPool)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(TaskORM.__table__.create)

    asyncio.run(_create())
    monkeypatch.setattr(
        task_service, "get_session",
        lambda: AsyncSession(engine, expire_on_commit=False))
    yield engine
    asyncio.run(engine.dispose())


class TestClaim:
    """抢占防重（active_key UNIQUE 约束）"""

    def test_same_target_second_claim_none(self, ts_db):
        async def _run():
            first = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            second = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            return first, second
        first, second = asyncio.run(_run())
        assert first is not None, "首次抢占应成功"
        assert second is None, "同目标已有 running 任务 → 抢占失败"

    def test_different_targets_independent(self, ts_db):
        async def _run():
            a = await task_service.claim_task(TASK_TYPE_GRAPH, "docA")
            b = await task_service.claim_task(TASK_TYPE_GRAPH, "docB")
            return a, b
        a, b = asyncio.run(_run())
        assert a and b and a != b

    def test_different_types_same_target_independent(self, ts_db):
        """同 id 的图谱任务与重建任务互不干扰（active_key 含 type 前缀）"""
        async def _run():
            g = await task_service.claim_task(TASK_TYPE_GRAPH, "x1")
            r = await task_service.claim_task(TASK_TYPE_REBUILD, "x1")
            return g, r
        g, r = asyncio.run(_run())
        assert g and r and g != r

    def test_get_running_task_id(self, ts_db):
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            found = await task_service.get_running_task_id(TASK_TYPE_GRAPH, "doc1")
            missing = await task_service.get_running_task_id(TASK_TYPE_GRAPH, "nope")
            return tid, found, missing
        tid, found, missing = asyncio.run(_run())
        assert found == tid
        assert missing is None


class TestFinish:
    """收尾释放占位"""

    def test_finish_releases_claim(self, ts_db):
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            await TaskHandle(tid).finish(TASK_DONE)
            again = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            return tid, again
        tid, again = asyncio.run(_run())
        assert again is not None, "finish 后应能再次抢占"
        assert again != tid

    def test_finish_clears_active_key_and_sets_status(self, ts_db):
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            await TaskHandle(tid).finish(TASK_FAILED)
            return await task_service.get_latest_task(TASK_TYPE_GRAPH, "doc1")
        task = asyncio.run(_run())
        assert task.status == TASK_FAILED
        assert task.active_key is None
        assert task.finished_at, "收尾应写入 finished_at"


class TestHandleProgress:
    """进度读写"""

    def test_progress_persisted(self, ts_db):
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_REBUILD, "kb1")
            h = TaskHandle(tid)
            await h.set_total(3)
            await h.set_current("a.pdf")
            await h.inc_done()
            await h.inc_failed(doc_id="d1", doc_name="b.pdf", error="boom")
            await h.set_current(None)
            await h.finish(TASK_DONE)
            return await task_service.get_latest_task(TASK_TYPE_REBUILD, "kb1")
        task = asyncio.run(_run())
        assert task.total == 3
        assert task.done == 1
        assert task.failed == 1
        assert task.current_doc is None
        assert task.status == TASK_DONE
        errors = json.loads(task.errors)
        assert len(errors) == 1
        assert errors[0]["doc_name"] == "b.pdf"
        assert errors[0]["error"] == "boom"

    def test_handle_counters_track_db(self, ts_db):
        """句柄内存计数与落库一致（收尾日志靠它，免一次查库）"""
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_REBUILD, "kb1")
            h = TaskHandle(tid)
            for _ in range(3):
                await h.inc_done()
            await h.inc_failed(doc_id=None, doc_name=None, error="e")
            return h.done, h.failed, await task_service.get_latest_task(
                TASK_TYPE_REBUILD, "kb1")
        done, failed, task = asyncio.run(_run())
        assert (done, failed) == (3, 1)
        assert (task.done, task.failed) == (3, 1)

    def test_latest_task_includes_finished(self, ts_db):
        """已完成任务仍可查（重启后前端能看到上次结果）"""
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_REBUILD, "kb1")
            await TaskHandle(tid).finish(TASK_DONE)
            return await task_service.get_latest_task(TASK_TYPE_REBUILD, "kb1")
        task = asyncio.run(_run())
        assert task is not None and task.id and task.status == TASK_DONE

    def test_latest_task_none_when_never_run(self, ts_db):
        assert asyncio.run(
            task_service.get_latest_task(TASK_TYPE_REBUILD, "never")) is None

    def test_latest_task_prefers_running(self, ts_db):
        """★ 同一秒内 finish 后立即重新 claim → 应返回 running 的那条

        created_at 是秒级字符串，两条任务时间戳相同、id 是随机 uuid 不能
        定序；不优先取 running 就会返回已完成的上一条，前端看到"已完成"
        而任务其实在跑（快任务完成 + 立刻重触发时会出现）。
        """
        async def _run():
            first = await task_service.claim_task(TASK_TYPE_REBUILD, "kb1")
            await TaskHandle(first).finish(TASK_DONE)
            second = await task_service.claim_task(TASK_TYPE_REBUILD, "kb1")
            task = await task_service.get_latest_task(TASK_TYPE_REBUILD, "kb1")
            return first, second, task
        first, second, task = asyncio.run(_run())
        assert first != second
        assert task.id == second, "应返回 running 的新任务而非已完成的上一条"
        assert task.status == TASK_RUNNING


class TestCancel:
    """跨进程取消：库标志 + Event 桥接"""

    def test_request_cancel_flag(self, ts_db):
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            before = await task_service.is_cancel_requested(tid)
            ok = await task_service.request_cancel(TASK_TYPE_GRAPH, "doc1")
            after = await task_service.is_cancel_requested(tid)
            return before, ok, after
        before, ok, after = asyncio.run(_run())
        assert before is False
        assert ok is True
        assert after is True

    def test_request_cancel_without_running_task(self, ts_db):
        """无 running 任务 → False（路由层据此返回 409）"""
        assert asyncio.run(
            task_service.request_cancel(TASK_TYPE_GRAPH, "nope")) is False

    def test_watch_cancel_bridges_flag_to_event(self, ts_db):
        """★ 桥接：库里的取消标志 → 进程内 asyncio.Event

        `build_graph_for_doc` 的检查点仍检查 Event，一行没改。
        """
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            ev = asyncio.Event()
            watcher = asyncio.create_task(
                task_service.watch_cancel(tid, ev, interval=0.05))
            await task_service.request_cancel(TASK_TYPE_GRAPH, "doc1")
            try:
                await asyncio.wait_for(ev.wait(), timeout=5)
                return True
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        assert asyncio.run(_run()) is True

    def test_watch_cancel_exits_when_task_finished(self, ts_db):
        """任务结束后 watcher 自行退出（不会永久轮询 → 协程泄漏）"""
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            ev = asyncio.Event()
            watcher = asyncio.create_task(
                task_service.watch_cancel(tid, ev, interval=0.05))
            await TaskHandle(tid).finish(TASK_DONE)
            # 不主动 cancel，等它自己发现 status != running 后退出
            await asyncio.wait_for(watcher, timeout=5)
            return ev.is_set()
        assert asyncio.run(_run()) is False, "任务正常结束不应置取消事件"

    def test_cancel_flag_survives_finish(self, ts_db):
        """取消标志不因任务结束而丢失（供追溯"这次是被取消的"）"""
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            await task_service.request_cancel(TASK_TYPE_GRAPH, "doc1")
            await TaskHandle(tid).finish("cancelled")
            task = await task_service.get_latest_task(TASK_TYPE_GRAPH, "doc1")
            return task.cancel_requested
        assert asyncio.run(_run()) == 1


class TestResetStuck:
    """启动恢复：僵尸任务清理"""

    def test_reset_marks_failed_and_releases(self, ts_db):
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            n = await task_service.reset_stuck_tasks()
            # 在重新抢占**之前**查，否则拿到的是新任务
            after_reset = await task_service.get_latest_task(
                TASK_TYPE_GRAPH, "doc1")
            again = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            return tid, n, after_reset, again
        tid, n, after_reset, again = asyncio.run(_run())
        assert n == 1, "应清理 1 个残留 running 任务"
        assert after_reset.status == TASK_FAILED
        assert after_reset.id == tid, "最近任务仍是那一条（只是状态被拨正）"
        assert again is not None, "释放后应能再次抢占"

    def test_reset_noop_when_clean(self, ts_db):
        async def _run():
            tid = await task_service.claim_task(TASK_TYPE_GRAPH, "doc1")
            await TaskHandle(tid).finish(TASK_DONE)
            return await task_service.reset_stuck_tasks()
        assert asyncio.run(_run()) == 0

    def test_reset_multiple_targets(self, ts_db):
        async def _run():
            await task_service.claim_task(TASK_TYPE_GRAPH, "d1")
            await task_service.claim_task(TASK_TYPE_GRAPH, "d2")
            await task_service.claim_task(TASK_TYPE_REBUILD, "k1")
            return await task_service.reset_stuck_tasks()
        assert asyncio.run(_run()) == 3


class TestCrossProcess:
    """★ 多 worker 核心保证：任务状态与会话无关，跨进程共享"""

    def test_two_processes_share_task_state(self, tmp_path):
        """真起两个 Python 子进程：

        进程 A 抢占任务并写取消标志 → 进程 B（全新解释器，内存里没有任何
        相关对象）应能查到该 running 任务与取消标志。

        旧实现在此必然失败：`_GRAPH_RUNNING` 是 A 进程的 set，B 看不到；
        `_GRAPH_CANCEL` 存的 asyncio.Event 更是无法跨进程传递，取消请求
        打到 B 会误报 409"当前不在图谱构建中"。
        """
        env = {
            **os.environ,
            "DATA_DIR": str(tmp_path),
            "MYSQL_URL": f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}",
            "JWT_SECRET": "test-secret-test-secret",
        }
        proj = str(Path(__file__).resolve().parents[1])
        script = (
            "import asyncio, sys\n"
            "from backend.db import Base, get_engine\n"
            "from backend.models.task_models import TASK_TYPE_GRAPH\n"
            "from backend.services import task_service\n"
            "async def main():\n"
            "    mode = sys.argv[1]\n"
            "    if mode == 'init':\n"
            "        async with get_engine().begin() as conn:\n"
            "            await conn.run_sync(Base.metadata.create_all)\n"
            "        print('ready')\n"
            "    elif mode == 'claim':\n"
            "        tid = await task_service.claim_task(TASK_TYPE_GRAPH, 'docX')\n"
            "        ok = await task_service.request_cancel(TASK_TYPE_GRAPH, 'docX')\n"
            "        print('claimed' if tid and ok else 'failed')\n"
            "    else:\n"
            "        tid = await task_service.get_running_task_id(\n"
            "            TASK_TYPE_GRAPH, 'docX')\n"
            "        cancel = await task_service.is_cancel_requested(tid) if tid else False\n"
            "        print(f'{bool(tid)}|{cancel}')\n"
            "asyncio.run(main())\n"
        )

        def _run(mode: str) -> str:
            r = subprocess.run(
                [sys.executable, "-c", script, mode],
                cwd=proj, env=env, capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, f"子进程失败({mode}):\n{r.stderr}"
            return r.stdout.strip().splitlines()[-1]

        assert _run("init") == "ready"
        assert _run("claim") == "claimed"
        # 全新解释器：只能从库里读到 running 任务与取消标志
        assert _run("check") == "True|True", \
            "另一进程应看到 running 任务与已置的取消标志"
