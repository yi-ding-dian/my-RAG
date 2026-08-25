"""登录限速：IP 维度失败计数窗口（内存实现，防爆破）

设计说明（为什么不引入 slowapi）：
- slowapi 只支持"窗口内**总请求数**"限流，无法区分成功/失败——会误伤
  正常登录（同 NAT 多设备、测试环境同 IP 高频请求）
- 本模块只对**登录失败**计数：成功即清零，失败达到上限触发锁定
- 内存实现：单 uvicorn worker 足够（多 worker 部署时各自独立计数，仍可
  显著降低爆破面，但不跨进程准确）；重启清零（破解者可重启后重试，
  但公网场景不能重启服务端，足够）

用法：
    from backend.services.rate_limit import login_rate
    if login_rate.check(request) is not None: raise 429
    ...
    login_rate.record_failure(request) / login_rate.record_success(request)

配置（config.py）：LOGIN_RATE_LIMIT_ENABLED 开关 / LOGIN_RATE_WINDOW 窗口秒数 /
LOGIN_MAX_FAILURES 失败上限 / LOGIN_LOCK_SECONDS 锁定时长，均可在 .env 覆盖。
测试环境由 conftest 关闭开关（同 IP 多测试会误锁）。
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Dict, List, Optional

from fastapi import Request

from backend.config import settings as config_settings

# key = IP；value = 窗口内失败时间戳列表（按插入序，越早越靠前）
_failures: Dict[str, List[float]] = {}
_lock = Lock()


def ip_of(request: Request) -> str:
    """取客户端 IP：优先 X-Forwarded-For 首个（nginx 反代后真实 IP），
    否则 request.client；无则 "unknown"（恶意者直连时也计入，不区分）"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    client = request.client
    return client.host if client else "unknown"


def _enabled() -> bool:
    return bool(config_settings.LOGIN_RATE_LIMIT_ENABLED)


def _window() -> int:
    return max(1, int(config_settings.LOGIN_RATE_WINDOW))


def _max_failures() -> int:
    return max(1, int(config_settings.LOGIN_MAX_FAILURES))


def _lock_seconds() -> int:
    return max(1, int(config_settings.LOGIN_LOCK_SECONDS))


def _prune(ip: str, now: float):
    """清理该 IP 超出窗口的失败记录（保留窗口内）"""
    ts = _failures.get(ip)
    if not ts:
        return
    cutoff = now - _window()
    while ts and ts[0] <= cutoff:
        ts.pop(0)
    if not ts:
        _failures.pop(ip, None)


def check(request: Request) -> Optional[int]:
    """当前 IP 是否已锁定。返回剩余锁定秒数（未锁定 → None）

    锁定判定触发即持续 LOGIN_LOCK_SECONDS，这期间无论是否再失败都不解锁；
    因为失败时间戳会不断推进，窗口后失败仍 >= 上限则继续锁定（实践简单可靠）。
    """
    if not _enabled():
        return None
    ip = ip_of(request)
    now = time.time()
    with _lock:
        _prune(ip, now)
        ts = _failures.get(ip)
        if ts and len(ts) >= _max_failures():
            # 最后一次失败后锁定 LOGIN_LOCK_SECONDS；窗口内失败数超过时每次
            # 都刷新锁起点（持续有攻击 → 持续锁定，需求符合）
            ref = ts[-1]
            return max(1, int(_lock_seconds() - (now - ref)))
    return None


def record_failure(request: Request):
    """记录一次登录失败（超窗的旧记录先清）"""
    if not _enabled():
        return
    ip = ip_of(request)
    now = time.time()
    with _lock:
        _prune(ip, now)
        _failures.setdefault(ip, []).append(now)
        # 只保窗口 + 上限内的记录（列表过长时裁剪，防内存膨胀）
        ts = _failures[ip]
        keep = _max_failures() + 1
        if len(ts) > keep:
            _failures[ip] = ts[-keep:]


def record_success(request: Request):
    """登录成功 → 清零该 IP 失败记录（防锁定正常用户）"""
    if not _enabled():
        return
    ip = ip_of(request)
    with _lock:
        _failures.pop(ip, None)
