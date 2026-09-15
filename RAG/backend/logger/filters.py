"""日志过滤器：把「系统级故障」的日志记录标记出来。

`AppLog.system_error()` 会给记录带上 `extra={"fault": True}`，这里的过滤器
据此把 logger 名加上 `system.` 前缀，落成：

    2026-09-15 11:47:48,884 [ERROR] system.backend.services.chat_service: LLM 流式调用失败

`routers/logs.py` 的统计接口按这个前缀区分「系统级 / 用户级」，决定要不要点红灯。
不改日志格式（格式串里的 %(name)s 本来就带模块名），所以行解析正则、前端解析、
既有测试都不受影响。
"""
from __future__ import annotations

import logging

# 系统级 logger 名前缀（过滤器注入；统计接口按它识别域）
SYSTEM_PREFIX = "system."


class SystemFaultFilter(logging.Filter):
    """把 `extra={"fault": True}` 的记录标记为系统级（logger 名加前缀）

    **必须挂在 handler 上，不能挂 logger**：日志向上 propagate 时只检查各级
    handler 的 filter，不检查父 logger 自身的 filter——挂在 root logger 上对
    子 logger 发出的记录完全不生效。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "fault", False) and not record.name.startswith(SYSTEM_PREFIX):
            record.name = f"{SYSTEM_PREFIX}{record.name}"
        return True
