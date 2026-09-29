"""日志过滤器：标记系统级故障 / 丢弃第三方 HTTP 噪音

`AppLog.system_error()` 会给记录带上 `extra={"fault": True}`，`SystemFaultFilter`
据此把 logger 名加上 `system.` 前缀，落成：

    2026-09-15 11:47:48,884 [ERROR] system.backend.services.chat_service: LLM 流式调用失败

`routers/logs.py` 的统计接口按这个前缀区分「系统级 / 用户级」，决定要不要点红灯。
不改日志格式（格式串里的 %(name)s 本来就带模块名），所以行解析正则、前端解析、
既有测试都不受影响。

`HttpNoiseFilter` 则是在**写入侧**丢弃第三方 HTTP 库的正常请求回显。
"""
from __future__ import annotations

import logging
import re

# 系统级 logger 名前缀（过滤器注入；统计接口按它识别域）
SYSTEM_PREFIX = "system."

# httpx 请求回显里的响应状态码（如 'HTTP/1.1" 200 OK'）
_HTTP_STATUS_RE = re.compile(r'"HTTP/\d\.\d (\d{3})')


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


class HttpNoiseFilter(logging.Filter):
    """丢弃第三方 HTTP 库的「正常」日志（在写入侧，而非读取时过滤）

    实测单日日志 55% 是 httpx 请求回显（图片摘要并发时一次刷几百行），把业务
    日志整个淹没。原先靠 `/api/logs/tail` 的 `hide_http` 参数在**读取时**跳过：
    日志照样落盘、照样占磁盘、概览统计的口径照样被污染。这里从源头不产生。

    - **httpx**：丢弃 INFO 级的 2xx/3xx 回显，**保留 4xx/5xx**——httpx 对失败
      请求同样打 INFO（见 `httpx/_client.py`），按级别过滤抓不到 HTTP 异常，
      而"依赖服务返回 4xx/5xx"恰恰是排障重点（LLM/Embedding 服务不可用等）。
      WARNING 及以上一律保留。
    - **httpcore / urllib3.connectionpool**：连接层提示，整类（INFO 及以下）丢弃。

    **必须挂在 handler 上**（同 SystemFaultFilter）：记录来自 `httpx._client`
    等子 logger，挂在父 logger 自身的 filter 上不生效。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        if name.startswith("httpx"):
            if record.levelno >= logging.WARNING:
                return True
            status = _HTTP_STATUS_RE.search(record.getMessage())
            return bool(status) and status.group(1)[0] in "45"
        if name.startswith(("httpcore", "urllib3")):
            return record.levelno >= logging.WARNING
        return True
