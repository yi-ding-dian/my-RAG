"""统一日志门面 AppLog 测试：级别、域（点灯标记）、级别覆盖、通用透传

域 → 点灯的链路：`system_error()` 给记录注入 `extra={"fault": True}`，由挂在
handler 上的 SystemFaultFilter 加 `system.` 前缀，`routers/logs.py` 统计时按前缀
识别。这里测前半段（门面注入标记），过滤器本身在 test_logs_overview 里测。
"""
from __future__ import annotations

import logging

import pytest

from backend.logger import AppLog, LogLevel

LOG_NAME = "backend.tests.applog"


def _last(caplog) -> logging.LogRecord:
    return caplog.records[-1]


class TestAppLog:

    def test_system_error_is_error_and_marked(self, caplog):
        """系统级 → ERROR + fault 标记（有标记才会被加 system. 前缀、点红灯）"""
        log = AppLog(LOG_NAME)
        with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
            log.system_error("LLM 崩溃: %s", "boom")
        rec = _last(caplog)
        assert rec.levelname == "ERROR"
        assert rec.getMessage() == "LLM 崩溃: boom"
        assert getattr(rec, "fault", False) is True

    def test_user_error_is_error_without_marking(self, caplog):
        """用户级 → ERROR，但**不打标记**（不点灯）"""
        log = AppLog(LOG_NAME)
        with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
            log.user_error("入库失败: %s（文档超阈值）", "doc1")
        rec = _last(caplog)
        assert rec.levelname == "ERROR"
        assert getattr(rec, "fault", False) is False

    def test_skipped_is_warning_without_marking(self, caplog):
        """跳过/回退 → WARNING，不打标记"""
        log = AppLog(LOG_NAME)
        with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
            log.skipped("知识图谱抽取超时，跳过: chunk#%d", 3)
        rec = _last(caplog)
        assert rec.levelname == "WARNING"
        assert getattr(rec, "fault", False) is False

    def test_level_override_keeps_marking(self, caplog):
        """级别可覆盖，但**不影响点灯**——fault 标记照打（灯只看域）"""
        log = AppLog(LOG_NAME)
        with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
            log.system_error("这条只算警告", level=LogLevel.WARNING)
        rec = _last(caplog)
        assert rec.levelname == "WARNING"
        assert getattr(rec, "fault", False) is True

    def test_level_accepts_enum_only(self):
        """level 只吃 LogLevel 枚举：传裸字符串应直接报错，不静默接受"""
        log = AppLog(LOG_NAME)
        with pytest.raises(AttributeError):
            log.system_error("x", level="WARNING")  # type: ignore[arg-type]

    def test_common_methods_passthrough(self, caplog):
        """通用方法（info/warning/debug/error）级别原样、不打标记"""
        log = AppLog(LOG_NAME)
        with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
            log.debug("d")
            log.info("i %d", 1)
            log.warning("w")
            log.error("e")
        levels = [r.levelname for r in caplog.records]
        assert levels == ["DEBUG", "INFO", "WARNING", "ERROR"]
        assert all(getattr(r, "fault", False) is False for r in caplog.records)
        assert caplog.records[1].getMessage() == "i 1"

    def test_exc_info_records_stack(self, caplog):
        """system_error 传 exc_info=True 时带堆栈（原来 logger.exception 的用法）"""
        log = AppLog(LOG_NAME)
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
                log.system_error("崩了", exc_info=True)
        rec = _last(caplog)
        assert rec.exc_info is not None
        assert rec.levelname == "ERROR"
        assert getattr(rec, "fault", False) is True

    def test_logger_name_is_module(self, caplog):
        """logger 名 = 传入的模块名（日志里的来源不因门面而改变）"""
        log = AppLog(LOG_NAME)
        with caplog.at_level(logging.DEBUG, logger=LOG_NAME):
            log.info("x")
        assert _last(caplog).name == LOG_NAME
