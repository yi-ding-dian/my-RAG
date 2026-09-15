"""系统日志总览 / 红绿灯测试：GET /api/logs/overview、GET /api/logs/health

覆盖：
- 权限：非 super_admin 404（路由统一挂 require_super_admin）
- overview 契约：days / totals / today / health / recent_faults，分级别与分域计数
- **级别与域正交**：用户级 ERROR（如「文档超阈值」）计入 error 但不算系统故障；
  系统级 WARNING（如「Embedding 服务不可用」）不计入 error 但算系统故障、点亮红灯
- 灯色：窗口内有系统级故障 → red；只有用户级 ERROR → green；窗口外的不算
- 上报去重：notify_health 只在灯色**变化**时报一次
- SystemFaultFilter：只有 extra={"fault": True} 的记录被打上 system. 前缀

隔离策略同 test_logs_tail：测试手工写「昨天/前天」的文件（logging handler 只写当天
文件，不冲突）；断言只看自己写的那些天，不依赖真实日志的绝对数字。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pytest

from backend.routers import logs as logs_mod
from backend.routers.logs import _log_path
from backend.logger import SystemFaultFilter
from backend.logger import alert as alert_mod
from backend.logger.alert import notify_health


def _d(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _write(date: str, content: str):
    p = _log_path(date)
    p.unlink(missing_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _line(ts: str, level: str, name: str, msg: str) -> str:
    """构造一条与 main.py logging 格式一致的标准行"""
    return f"{ts} [{level}] {name}: {msg}"


def _now_ts(offset_seconds: int = 0) -> str:
    """当前时间（可偏移）的日志时间戳，用于灯色窗口测试"""
    dt = datetime.now() + timedelta(seconds=offset_seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + ",000"


# SystemFaultFilter 注入前缀后的 logger 名（系统级）
SYS = "system.backend.services.chat_service"
# 普通 logger 名（用户级）
USER = "backend.services.ingestion.service"


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path, monkeypatch):
    """把日志目录隔离到临时目录 —— 本文件所有用例只看得见自己写的文件。

    不隔离会「单独跑全过、全量跑失败」：健康接口读的是真实 data/logs，
    而全量跑时前面的用例（ingestion/chat 等）触发系统级故障打点会落进
    **今天的真实日志**，于是「期望绿灯」的用例读到别处产生的故障而红灯。
    顺带清 _day_stat_cache：它以 (路径, mtime_ns, size) 为键，跨用例残留会串味。
    """
    monkeypatch.setattr(logs_mod, "LOG_DIR", tmp_path)
    logs_mod._day_stat_cache.clear()
    yield
    logs_mod._day_stat_cache.clear()


@pytest.fixture(autouse=True)
def _reset_alert_state():
    """重置上报去重状态，避免用例间互相污染（进程内全局）"""
    alert_mod._last_red = None
    yield
    alert_mod._last_red = None


# ==================== 权限 ====================

class TestOverviewPermission:

    def test_no_token_401(self, client):
        assert client.get("/api/logs/overview").status_code == 401
        assert client.get("/api/logs/health").status_code == 401

    def test_non_super_admin_forbidden(self, client, dept_admin_headers,
                                       user_headers):
        for headers in (dept_admin_headers, user_headers):
            assert client.get("/api/logs/overview",
                              headers=headers).status_code == 404
            assert client.get("/api/logs/health",
                              headers=headers).status_code == 404


# ==================== 计数：级别与域正交 ====================

class TestOverviewCounts:

    def test_counts_by_level_and_domain(self, client, admin_headers):
        """分级别计数与系统级故障数各算各的：用户级 ERROR 计入 error 不计 faults"""
        date = _d(1)
        _write(date,
               _line("2026-08-15 10:00:00,000", "INFO", "backend.main", "启动") + "\n"
               # 用户级 ERROR（文档超阈值这类：只影响这一个操作）
               + _line("2026-08-15 10:00:01,000", "ERROR", USER,
                       "入库失败: doc1 (文档超阈值)") + "\n"
               # 系统级 ERROR（LLM 崩了：全体受影响）
               + _line("2026-08-15 10:00:02,000", "ERROR", SYS,
                       "LLM 流式调用失败") + "\n"
               # 系统级 WARNING（依赖不可用，打的是 warning）
               + _line("2026-08-15 10:00:03,000", "WARNING",
                       "system.backend.services.embedding_service",
                       "embedding 调用失败（重试后）") + "\n")
        data = client.get("/api/logs/overview", params={"days": 7},
                          headers=admin_headers).json()
        day = next(d for d in data["days"] if d["date"] == date)
        assert day == {"date": date, "lines": 4, "info": 1, "warning": 1,
                       "error": 2, "faults": 2, "fault_errors": 1}
        # faults 含系统级 ERROR + 系统级 WARNING；fault_errors 只数前者，
        # 用户级 ERROR = error - fault_errors = 1（前端两个口径并排显示）

    def test_user_level_error_alone_is_not_fault(self, client, admin_headers):
        """用户级 ERROR：error 计数 +1，faults 仍为 0（红灯不该被它点亮）"""
        date = _d(2)
        _write(date,
               _line("2026-08-15 10:00:00,000", "ERROR", USER,
                     "入库失败: doc1 (文档超阈值)") + "\n")
        data = client.get("/api/logs/overview", params={"days": 7},
                          headers=admin_headers).json()
        day = next(d for d in data["days"] if d["date"] == date)
        assert day["error"] == 1
        assert day["faults"] == 0

    def test_days_excludes_missing_files(self, client, admin_headers):
        """只统计实际有日志的天（缺的天不占位），时间为正序"""
        dates = [d["date"] for d in client.get(
            "/api/logs/overview", params={"days": 7},
            headers=admin_headers).json()["days"]]
        assert dates == sorted(dates)

    def test_recent_faults_newest_first(self, client, admin_headers):
        """最近系统级故障列表：只含 system. 行，最新在前"""
        date = _d(1)
        _write(date,
               _line("2026-08-15 10:00:00,000", "ERROR", SYS, "第一条故障") + "\n"
               + _line("2026-08-15 10:00:01,000", "ERROR", USER,
                       "用户级失败不该出现") + "\n"
               + _line("2026-08-15 10:00:02,000", "ERROR", SYS, "第二条故障") + "\n")
        faults = client.get("/api/logs/overview", params={"days": 7},
                            headers=admin_headers).json()["recent_faults"]
        assert [f["message"] for f in faults[:2]] == [
            f"{SYS}: 第二条故障", f"{SYS}: 第一条故障"]
        assert all("用户级失败" not in f["message"] for f in faults)


# ==================== 灯色 ====================

class TestHealthLight:

    def test_green_when_only_user_level_error(self, client, admin_headers):
        """用户级 ERROR 不点灯（这是设计，不是 bug）"""
        _write(_d(1), _line(_now_ts(-60), "ERROR", USER, "入库失败: 超阈值") + "\n")
        data = client.get("/api/logs/health", headers=admin_headers).json()
        assert data["level"] == "green"
        assert data["fault_count"] == 0

    def test_red_when_system_fault_in_window(self, client, admin_headers):
        _write(_d(1), _line(_now_ts(-60), "ERROR", SYS, "LLM 流式调用失败") + "\n")
        data = client.get("/api/logs/health", headers=admin_headers).json()
        assert data["level"] == "red"
        assert data["fault_count"] == 1
        assert data["error_count"] == 1

    def test_red_on_system_warning_too(self, client, admin_headers):
        """系统级 WARNING 也点灯——「Embedding 服务不可用」打的就是 warning，
        按级别判会被骗（级别与域正交）"""
        _write(_d(1), _line(_now_ts(-60), "WARNING", SYS, "检索服务不可用") + "\n")
        data = client.get("/api/logs/health", headers=admin_headers).json()
        assert data["level"] == "red"
        assert data["warning_count"] == 1
        assert data["error_count"] == 0

    def test_fault_outside_window_is_green(self, client, admin_headers):
        """窗口外的故障不算（默认回看 60 分钟）"""
        _write(_d(1), _line(_now_ts(-2 * 3600), "ERROR", SYS, "两小时前的故障") + "\n")
        data = client.get("/api/logs/health",
                          params={"window_minutes": 60},
                          headers=admin_headers).json()
        assert data["level"] == "green"
        assert data["fault_count"] == 0

    def test_window_covers_fault(self, client, admin_headers):
        """放大窗口后同一条故障就算数"""
        _write(_d(1), _line(_now_ts(-2 * 3600), "ERROR", SYS, "两小时前的故障") + "\n")
        data = client.get("/api/logs/health",
                          params={"window_minutes": 1440},
                          headers=admin_headers).json()
        assert data["level"] == "red"

    def test_health_contract(self, client, admin_headers):
        data = client.get("/api/logs/health", headers=admin_headers).json()
        assert set(data) == {"level", "window_minutes", "fault_count",
                             "acked_count", "error_count",
                             "warning_count", "summary"}
        assert data["level"] in ("green", "red")


# ==================== 故障确认（按条 ACK 消警） ====================

class TestFaultAck:
    """按条确认：逐条标记已解决，**全部确认完**红灯才灭；新故障是新 id、重新亮灯"""

    def _seed_two(self):
        """写两条系统级故障（时间不同 → id 不同）"""
        _write(_d(1),
               _line(_now_ts(-60), "ERROR", SYS, "故障一") + "\n"
               + _line(_now_ts(-50), "ERROR", SYS, "故障二") + "\n")

    def _faults(self, client, admin_headers):
        """overview 的 recent_faults（最新在前）"""
        return client.get("/api/logs/overview", params={"days": 7},
                          headers=admin_headers).json()["recent_faults"]

    def _ack(self, client, admin_headers, ids, note=""):
        return client.post("/api/logs/ack", json={"ids": ids, "note": note},
                           headers=admin_headers).json()

    def test_faults_carry_id_and_acked_flag(self, client, admin_headers):
        self._seed_two()
        faults = self._faults(client, admin_headers)
        assert len(faults) == 2
        assert all(f["id"] and f["acked"] is False for f in faults)
        assert len({f["id"] for f in faults}) == 2       # id 互不相同

    def test_ack_one_of_two_still_red(self, client, admin_headers):
        """两条只确认一条 → 仍有未确认的，红灯不灭"""
        self._seed_two()
        faults = self._faults(client, admin_headers)
        res = self._ack(client, admin_headers, [faults[0]["id"]])
        assert res["level"] == "red"
        assert res["fault_count"] == 1
        assert res["acked_count"] == 1
        # overview 里那条已标 acked
        after = self._faults(client, admin_headers)
        assert sum(1 for f in after if f["acked"]) == 1

    def test_ack_all_turns_green(self, client, admin_headers):
        """全部确认完（未确认数为 0）→ 红灯灭"""
        self._seed_two()
        ids = [f["id"] for f in self._faults(client, admin_headers)]
        res = self._ack(client, admin_headers, ids)
        assert res["level"] == "green"
        assert res["fault_count"] == 0
        assert res["acked_count"] == 2
        again = client.get("/api/logs/health", headers=admin_headers).json()
        assert again["level"] == "green"
        assert again["acked_count"] == 2

    def test_new_fault_after_ack_lights_red_again(self, client, admin_headers):
        """确认后冒出**新**故障（新 id）→ 重新亮灯，不能一键按死"""
        _write(_d(1), _line(_now_ts(-60), "ERROR", SYS, "老故障") + "\n")
        ids = [f["id"] for f in self._faults(client, admin_headers)]
        self._ack(client, admin_headers, ids)

        with _log_path(_d(1)).open("a", encoding="utf-8") as f:
            f.write(_line(_now_ts(5), "ERROR", SYS, "又崩了") + "\n")

        data = client.get("/api/logs/health", headers=admin_headers).json()
        assert data["level"] == "red"
        assert data["fault_count"] == 1
        assert data["acked_count"] == 1

    def test_unknown_id_ignored(self, client, admin_headers):
        """未知 id 静默忽略（日志按天轮转后旧 id 自然失效）"""
        _write(_d(1), _line(_now_ts(-60), "ERROR", SYS, "故障") + "\n")
        res = self._ack(client, admin_headers, ["不存在的-id"])
        assert res["level"] == "red"
        assert res["fault_count"] == 1

    def test_ack_persisted_to_file(self, client, admin_headers):
        """确认记录落盘（进程重启后仍生效），确认文件不被当作日志文件列出"""
        self._seed_two()
        ids = [f["id"] for f in self._faults(client, admin_headers)]
        self._ack(client, admin_headers, ids)
        assert len(logs_mod._read_acked()) == 2
        files = client.get("/api/logs/files", headers=admin_headers).json()["files"]
        assert all(f["filename"].startswith("kb-") for f in files)

    def test_corrupted_ack_file_treated_as_none(self, client, admin_headers):
        """确认文件损坏 → 当一条都没确认过（不能让坏文件把红灯一直按灭）"""
        logs_mod._ack_path().write_text("{ 坏掉的 json", encoding="utf-8")
        _write(_d(1), _line(_now_ts(-60), "ERROR", SYS, "故障") + "\n")
        data = client.get("/api/logs/health", headers=admin_headers).json()
        assert data["level"] == "red"
        assert data["fault_count"] == 1

    def test_ack_note_round_trip(self, client, admin_headers):
        """确认备注随确认存下，overview 原样带回（供日后回溯「当时为什么确认掉」）"""
        self._seed_two()
        faults = self._faults(client, admin_headers)
        self._ack(client, admin_headers, [faults[0]["id"]], note="压测产生的，非真实故障")
        self._ack(client, admin_headers, [faults[1]["id"]])

        after = self._faults(client, admin_headers)
        assert after[0]["ack_note"] == "压测产生的，非真实故障"
        assert after[1]["ack_note"] == ""          # 不填备注就是空串

    def test_ack_note_trimmed_and_capped(self, client, admin_headers):
        """备注去首尾空白并限长（日志域不该存大段文本）"""
        self._seed_two()
        fault_id = self._faults(client, admin_headers)[0]["id"]
        self._ack(client, admin_headers, [fault_id], note="  " + "备" * 500 + "  ")
        note = self._faults(client, admin_headers)[0]["ack_note"]
        assert note == "备" * 200

    def test_legacy_ack_format_still_read(self, client, admin_headers):
        """兼容旧格式（值直接是确认时间字符串）：仍认作已确认"""
        self._seed_two()
        faults = self._faults(client, admin_headers)
        logs_mod._ack_path().write_text(
            json.dumps({"acked": {faults[0]["id"]: "2026-09-15 10:00:00"}},
                       ensure_ascii=False), encoding="utf-8")
        after = self._faults(client, admin_headers)
        assert after[0]["acked"] is True
        assert after[0]["ack_note"] == ""


# ==================== 上报口子 ====================

class TestNotifyHealth:

    def test_only_reports_on_change(self):
        """绿→红报一次；持续红不重报；红→绿报一次"""
        assert notify_health(is_red=True, summary="测试") is True
        assert notify_health(is_red=True, summary="测试") is False
        assert notify_health(is_red=True, summary="测试") is False
        assert notify_health(is_red=False, summary="测试") is True
        assert notify_health(is_red=False, summary="测试") is False

    def test_first_green_not_reported(self):
        """进程内首次就是绿灯：记下但不报（避免每次重启都报一条「平安」）"""
        assert notify_health(is_red=False, summary="测试") is False
        assert notify_health(is_red=True, summary="测试") is True

    def test_alert_line_written(self, caplog):
        """上报落地成一条 [ALERT] 日志（后续接 webhook 前的当前实现）"""
        with caplog.at_level(logging.INFO, logger="backend.alert"):
            notify_health(is_red=True, summary="LLM 不可用")
        assert any("[ALERT]" in r.message and "LLM 不可用" in r.getMessage()
                   for r in caplog.records)


# ==================== 全局异常处理器 ====================

class TestUnhandledException:
    """未捕获异常必须记为**系统级**并落盘

    uvicorn 把 ASGI 未捕获异常记到 uvicorn.error，而该 logger 的
    propagate=false（uvicorn 默认 LOGGING_CONFIG）→ 不进 root logger、不落盘。
    不补这一刀，红绿灯恰恰看不见最严重的故障。
    """

    def test_handler_registered(self):
        from backend.main import app
        assert Exception in app.exception_handlers

    def test_logs_as_system_fault_and_returns_500(self, caplog):
        import asyncio

        from starlette.requests import Request

        from backend.main import _unhandled_exception
        scope = {"type": "http", "method": "POST", "path": "/api/boom",
                 "headers": [], "query_string": b"", "scheme": "http",
                 "server": ("testserver", 80), "client": ("127.0.0.1", 1234)}
        with caplog.at_level(logging.ERROR, logger="backend.main"):
            resp = asyncio.run(
                _unhandled_exception(Request(scope), RuntimeError("boom")))
        assert resp.status_code == 500
        rec = next(r for r in caplog.records if "未捕获异常" in r.getMessage())
        # extra={"fault": True} → handler 上的 SystemFaultFilter 会加 system. 前缀
        assert getattr(rec, "fault", False) is True


# ==================== SystemFaultFilter ====================

class TestSystemFaultFilter:

    def _run(self, logger_name: str, fault: bool) -> str:
        """过一遍 filter，返回最终 logger 名"""
        record = logging.LogRecord(
            logger_name, logging.ERROR, __file__, 1, "msg", None, None)
        if fault:
            record.fault = True
        SystemFaultFilter().filter(record)
        return record.name

    def test_adds_prefix_when_marked(self):
        assert self._run("backend.services.chat_service", True) == \
            "system.backend.services.chat_service"

    def test_keeps_name_when_not_marked(self):
        assert self._run("backend.services.ingestion.service", False) == \
            "backend.services.ingestion.service"

    def test_idempotent(self):
        """已经带前缀的不重复加（多个 handler 各挂一份 filter）"""
        assert self._run("system.backend.services.x", True) == \
            "system.backend.services.x"
