"""登录限速：IP 维度失败计数窗口（**落库实现**，防爆破）

设计说明：
- 为什么不引入 slowapi：slowapi 只支持"窗口内**总请求数**"限流，无法区分
  成功/失败——会误伤正常登录（同 NAT 多设备、测试环境同 IP 高频请求）
- 本模块只对**登录失败**计数：成功即清零，失败达到上限触发锁定
- 为什么落库而非进程内存：内存实现（旧版 `_failures` 字典）在单 worker 下
  够用，但多 worker 部署时各进程独立计数——攻击者打到不同 worker 即重置，
  限流阈值被放大 N 倍（N=worker 数）；进程重启还会清零。落库后与进程数
  无关，重启也不丢。
- 为什么用独立 session（同 `audit_service.record_action`）：与业务事务分离，
  登录流程的任何回滚都不影响限流记录，调用方也无需传 db 会话。

用法（**异步**）::

    from backend.services.rate_limit import check, record_failure, record_success

    if (remaining := await check(request)) is not None:
        raise HTTPException(429, ...)
    ...
    await record_failure(request)   # 密码错误时
    await record_success(request)   # 登录成功时清零

配置（config.py）：LOGIN_RATE_LIMIT_ENABLED 开关 / LOGIN_RATE_WINDOW 窗口秒数 /
LOGIN_MAX_FAILURES 失败上限 / LOGIN_LOCK_SECONDS 锁定时长，均可在 .env 覆盖。
测试环境由 conftest 关闭开关（同 IP 多测试会误锁）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request
from sqlalchemy import delete, func, select

from backend.config import settings as config_settings
from backend.db import get_session
from backend.models.user_models import LoginAttemptORM

logger = logging.getLogger(__name__)

# 时间格式与全库一致（"%Y-%m-%d %H:%M:%S" 字符串，字典序即时间序，
# 范围过滤直接字符串比较；秒级精度对本模块 60s 级窗口足够）
_TS_FMT = "%Y-%m-%d %H:%M:%S"


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


def _fmt(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


def _cutoff(now: datetime) -> str:
    """窗口起点（秒级字符串）：**早于等于**它的记录视为已过期

    判定保留用 `attempted_at > cutoff`，清理用 `attempted_at <= cutoff`，
    两侧互补，不会漏删或多删。
    """
    return _fmt(now - timedelta(seconds=_window()))


async def check(request: Request) -> Optional[int]:
    """当前 IP 是否已锁定。返回剩余锁定秒数（未锁定 → None）

    锁定起点 = 窗口内**最后一次失败**时间；距其不足 LOGIN_LOCK_SECONDS 则
    继续锁定，超出即解锁。

    **修正旧实现的边界问题**：旧版 `max(1, int(lock - elapsed))` 在
    LOCK_SECONDS < WINDOW 时，锁定超时后仍返回"剩余 1 秒"而非解锁，用户会
    反复看到"请 1 秒后再试"却始终登不上，直到记录被窗口清掉（最长 WINDOW
    秒）。这里改为剩余 ≤ 0 直接返回 None（解锁）。

    数据库异常 → **放行**（fail-open）：限流是防爆破的辅助手段，不该因为它
    自己出问题而锁死所有人的登录（何况登录本身还要查库，库挂了登录必然失败）；
    异常记 warning 留痕。
    """
    if not _enabled():
        return None
    ip = ip_of(request)
    now = datetime.now()
    try:
        async with get_session() as session:
            row = await session.execute(
                select(func.count(), func.max(LoginAttemptORM.attempted_at))
                .where(LoginAttemptORM.ip == ip,
                       LoginAttemptORM.attempted_at > _cutoff(now)))
            count, latest = row.one()
    except Exception as e:  # noqa: BLE001
        logger.warning("登录限流检查失败（本次放行）: ip=%s err=%s",
                       ip, str(e)[:150])
        return None
    if not count or count < _max_failures() or not latest:
        return None
    try:
        elapsed = int((now - datetime.strptime(latest, _TS_FMT)).total_seconds())
    except (TypeError, ValueError):
        elapsed = 0
    remaining = _lock_seconds() - max(0, elapsed)   # 时钟回拨时 elapsed 可能为负
    return remaining if remaining > 0 else None


async def record_failure(request: Request) -> None:
    """记录一次登录失败（顺带清理超窗记录，防表膨胀）

    清理是**全表**的：被公网扫描时会产生大量一次性 IP（每个 IP 只失败一两次
    就不再出现），只清当前 IP 的话那些记录永远留着——这正是旧内存实现的
    泄漏点。表本身很小（只在登录失败时写），带索引的 DELETE 开销可忽略。
    """
    if not _enabled():
        return
    ip = ip_of(request)
    now = datetime.now()
    try:
        async with get_session() as session:
            session.add(LoginAttemptORM(
                id=uuid.uuid4().hex, ip=ip, attempted_at=_fmt(now)))
            await session.execute(
                delete(LoginAttemptORM).where(
                    LoginAttemptORM.attempted_at <= _cutoff(now)))
            await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("登录失败记录写入失败（不影响登录流程）: ip=%s err=%s",
                       ip, str(e)[:150])


async def record_success(request: Request) -> None:
    """登录成功 → 清零该 IP 失败记录（防锁定正常用户）"""
    if not _enabled():
        return
    ip = ip_of(request)
    try:
        async with get_session() as session:
            await session.execute(
                delete(LoginAttemptORM).where(LoginAttemptORM.ip == ip))
            await session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("登录失败记录清零失败（不影响登录流程）: ip=%s err=%s",
                       ip, str(e)[:150])
