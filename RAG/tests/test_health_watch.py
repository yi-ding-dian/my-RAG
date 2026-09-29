"""后台健康自检测试：不依赖前端轮询的告警触发源

覆盖：
- **状态变化才记录**：首次失败点亮红灯、持续失败不重复记（否则每 60s 刷一条）、
  恢复记 info、未配置的可选依赖不算故障
- 单轮编排：先探测依赖、再复核灯色并上报
- **上报口径与页面一致**（复用 `routers.logs._health_state`）
- 单项探测抛异常被隔离（自检不因某一项挂掉而整轮失败）
- 生命周期：start 幂等、stop 可重复调用

探测函数全部 monkeypatch——测试不发真实网络请求。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from backend.config import settings as config_settings
from backend.services import health_watch

LOGGER_NAME = "backend.services.health_watch"


@pytest.fixture(autouse=True)
def _clean_probe_state():
    """每个用例前清空探测状态（否则上一条用例的"上次正常/异常"会串味）"""
    health_watch.reset_probe_state()
    yield
    health_watch.reset_probe_state()


class TestRecordTransitions:
    """按状态变化记录——这条规则是"不刷屏"的全部依据"""

    def test_first_failure_reports(self, caplog):
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            health_watch._record("LLM 服务", {"ok": False, "reason": "连接超时"})
        msgs = [r.getMessage() for r in caplog.records]
        assert any("依赖探测失败" in m and "LLM 服务" in m for m in msgs)

    def test_failure_marks_system_fault(self, caplog):
        """★ 必须以 system_error 记录：只有这样才会被算进红灯统计"""
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            health_watch._record("数据库", {"ok": False, "reason": "连不上"})
        assert any(getattr(r, "fault", False) for r in caplog.records), \
            "应带 fault 标记（否则不会被 SystemFaultFilter 加 system. 前缀、不点红灯）"

    def test_repeated_failure_not_reported(self, caplog):
        """★ 持续故障只报一次（每轮都记的话 60s 一条把日志淹掉）"""
        health_watch._record("LLM 服务", {"ok": False, "reason": "挂了"})
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(5):
                health_watch._record("LLM 服务", {"ok": False, "reason": "挂了"})
        assert not caplog.records, "持续故障不应重复记录"

    def test_recovery_reported(self, caplog):
        health_watch._record("LLM 服务", {"ok": False, "reason": "挂了"})
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            health_watch._record("LLM 服务", {"ok": True, "reason": "连接正常"})
        assert any("已恢复" in r.getMessage() for r in caplog.records)

    def test_repeated_ok_not_reported(self, caplog):
        """一直正常也不记（否则每轮一条"正常"同样是噪音）"""
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            for _ in range(5):
                health_watch._record("LLM 服务", {"ok": True, "reason": "正常"})
        assert not caplog.records

    def test_skipped_dependency_ignored(self, caplog):
        """未配置的可选依赖不算故障（用户不配 LLM 是正常选择）"""
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            health_watch._record("DeepDoc", {"ok": False, "reason": "服务地址未配置"})
        assert not caplog.records

    def test_each_dependency_tracked_separately(self, caplog):
        """不同依赖的状态互不影响"""
        health_watch._record("LLM 服务", {"ok": False, "reason": "挂了"})
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            health_watch._record("数据库", {"ok": False, "reason": "连不上"})
        assert any("数据库" in r.getMessage() for r in caplog.records)


class TestIsSkipped:

    def test_explicit_skipped_flag(self):
        assert health_watch._is_skipped({"ok": False, "skipped": True})

    def test_unconfigured_reason(self):
        for reason in ("服务地址未配置", "尚未启用 Rerank", "功能未启用"):
            assert health_watch._is_skipped({"ok": False, "reason": reason}), reason

    def test_real_failure_not_skipped(self):
        """真故障不能误判为 skipped（否则该报的没报）"""
        assert not health_watch._is_skipped(
            {"ok": False, "reason": "连接超时 (10.0.0.1:8000)"})
        assert not health_watch._is_skipped({"ok": True, "reason": "正常"})


class TestCheckOnce:
    """单轮编排：先探测、再复核灯色"""

    def test_probes_then_reports(self, monkeypatch):
        called: list = []

        async def _fake_probe():
            called.append("probe")

        def _fake_report():
            called.append("report")

        monkeypatch.setattr(health_watch, "_probe_dependencies", _fake_probe)
        monkeypatch.setattr(health_watch, "_report_health", _fake_report)
        asyncio.run(health_watch.check_once())
        assert called == ["probe", "report"]


class TestReportHealth:
    """上报口径必须与「日志查看」页面一致"""

    def test_uses_routers_logs_health_state(self, monkeypatch):
        """★ 复用 routers.logs._health_state

        自己另算一套的话，会出现"页面显示红灯但没告警"（或反之）的诡异现象。
        """
        captured: dict = {}
        monkeypatch.setattr("backend.routers.logs._health_state",
                            lambda w: {"level": "red", "summary": "有 2 条未确认故障"})
        monkeypatch.setattr(health_watch, "notify_health",
                            lambda **kw: captured.update(kw))
        health_watch._report_health()
        assert captured["is_red"] is True
        assert captured["summary"] == "有 2 条未确认故障"

    def test_green_reports_not_red(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr("backend.routers.logs._health_state",
                            lambda w: {"level": "green", "summary": "无故障"})
        monkeypatch.setattr(health_watch, "notify_health",
                            lambda **kw: captured.update(kw))
        health_watch._report_health()
        assert captured["is_red"] is False


class TestProbeDependencies:
    """并发探测 + 异常隔离"""

    def test_exception_isolated(self, monkeypatch):
        """★ 某项探测抛异常 → 归一为失败，不影响其他项、不中断整轮"""
        async def _boom(cfg, timeout=None):
            raise RuntimeError("探测内部炸了")

        async def _ok(cfg, timeout=None):
            return {"ok": True, "reason": "连接正常"}

        monkeypatch.setattr(health_watch, "_probe_specs", lambda: [
            ("坏依赖", (_boom, None)),
            ("好依赖", (_ok, None)),
        ])
        recorded: list = []
        monkeypatch.setattr(health_watch, "_record",
                            lambda name, r: recorded.append((name, r)))
        asyncio.run(health_watch._probe_dependencies())

        by_name = dict(recorded)
        assert by_name["坏依赖"]["ok"] is False
        assert "探测内部炸了" in by_name["坏依赖"]["reason"]
        assert by_name["好依赖"]["ok"] is True

    def test_all_dependencies_probed(self, monkeypatch):
        """默认探测项覆盖核心依赖"""
        probed: list = []

        async def _spy(cfg, timeout=None):
            probed.append(True)
            return {"ok": True, "reason": "ok"}

        specs = health_watch._probe_specs()
        monkeypatch.setattr(health_watch, "_probe_specs",
                            lambda: [(n, (_spy, c)) for n, (_, c) in specs])
        asyncio.run(health_watch._probe_dependencies())
        assert len(probed) == len(specs) >= 4, "应探测至少 4 个核心依赖"

    def test_specs_cover_core_services(self):
        """核心依赖必须在探测名单里（LLM/Embedding/数据库）"""
        names = [n for n, _ in health_watch._probe_specs()]
        joined = "".join(names)
        for kw in ("LLM", "Embedding", "数据库"):
            assert kw in joined, f"缺少核心依赖探测: {kw}"


class TestDisabledInTests:
    """★ 防回归：测试环境绝不启动自检协程"""

    def test_start_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(config_settings, "HEALTH_WATCH_ENABLED", False)

        async def _run():
            health_watch.start()
            assert health_watch._task is None, "开关关闭时不应创建 task"

        asyncio.run(_run())
        assert health_watch._task is None

    def test_conftest_disables_watch(self):
        """conftest 必须设 HEALTH_WATCH_ENABLED=false

        否则跑测试时自检会真去探测那些不可达地址、写系统级故障日志，污染
        logs 用例的灯色断言（"期望绿灯"的用例读到别处产生的故障）。
        """
        assert config_settings.HEALTH_WATCH_ENABLED is False, \
            "测试环境应关闭后台自检（检查 conftest 是否设置该环境变量）"


class TestLifecycle:
    """生命周期（测试环境默认关闭自检，这里显式打开）"""

    def test_start_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(config_settings, "HEALTH_WATCH_ENABLED", True)

        async def _run():
            health_watch.start()
            first = health_watch._task
            health_watch.start()                  # 第二次不应重建
            assert health_watch._task is first
            health_watch.stop()
            await asyncio.gather(first, return_exceptions=True)

        asyncio.run(_run())
        assert health_watch._task is None

    def test_stop_is_repeatable(self, monkeypatch):
        monkeypatch.setattr(config_settings, "HEALTH_WATCH_ENABLED", True)

        async def _run():
            health_watch.start()
            task = health_watch._task
            health_watch.stop()
            health_watch.stop()                   # 重复 stop 不应报错
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(_run())
        assert health_watch._task is None

    def test_loop_survives_single_round_exception(self, monkeypatch):
        """单轮炸了不能让自检停摆（下一轮继续）

        直接 `create_task(_loop())` 而非走 `start()`：测试要自己控制 task 的
        结束（若让被测代码调 `stop()`，等于让它 cancel 自己，`await task` 会
        抛 CancelledError）。
        """
        monkeypatch.setattr(health_watch, "STARTUP_DELAY", 0)
        monkeypatch.setattr(health_watch, "INTERVAL", 0.01)
        rounds: list = []

        async def _flaky():
            rounds.append(1)
            if len(rounds) < 3:
                raise RuntimeError("这一轮炸了")

        monkeypatch.setattr(health_watch, "check_once", _flaky)

        async def _run():
            task = asyncio.create_task(health_watch._loop())
            for _ in range(200):                # 最多等 4 秒
                if len(rounds) >= 3:
                    break
                await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(_run())
        assert len(rounds) >= 3, f"异常轮后应继续下一轮，实际只跑了 {len(rounds)} 轮"
