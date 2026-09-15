"""系统级故障上报口子：红灯亮起 / 恢复时各报一次。

当前实现只落一条 `[ALERT]` 日志；后续接 webhook / 邮件 / 钉钉时改 `_deliver()`
即可，调用方（`routers/logs.py` 的 overview / health / ack 接口）不用动。
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

_alert_logger = logging.getLogger("backend.alert")

_lock = threading.Lock()
_last_red: Optional[bool] = None   # 进程内上一次上报的灯色（None = 还没记录过）


def notify_health(*, is_red: bool, summary: str) -> bool:
    """灯色变化时上报一次（绿→红、红→绿），返回本次是否真的上报了

    **只在变化时报**，不是每次轮询都报——故障持续期间每 60s 报一条，很快就
    没人看了。去重状态存进程内，重启后可能重复报一次（可接受）。

    调用点：`routers/logs.py` 的 overview / health / ack 接口（都会算灯色）。
    """
    global _last_red
    with _lock:
        if _last_red == is_red:
            return False
        if _last_red is None and not is_red:
            # 进程内首次且为绿：记下但不报（避免每次重启都报一条"平安"）
            _last_red = False
            return False
        _last_red = is_red
    _deliver(is_red, summary)
    return True


def _deliver(is_red: bool, summary: str) -> None:
    """上报出口：当前只落一条日志（后续在此外接 webhook / 邮件 / 钉钉）

    用 INFO 级别：它是一条「通知」，本身不是新故障，别去污染 WARNING 统计；
    logger 名 backend.alert 不带 system. 前缀，也不会被算进系统级故障。
    """
    _alert_logger.info("[ALERT] %s: %s",
                       "红灯（系统级故障）" if is_red else "恢复绿灯", summary)
