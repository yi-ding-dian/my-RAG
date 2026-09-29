"""登录限速测试：IP 维度失败计数窗口（**落库实现**）

覆盖：
- 开关关（测试默认）→ 不锁定、不写库
- 5 次失败触发锁定；未达上限放行；成功清零
- 窗口外旧记录被清理（旧内存实现的泄漏点：只清当前 IP，一次性 IP 永久残留）
- 锁定到期即解锁（修正旧实现 `max(1, ...)` 在 LOCK < WINDOW 时永远返回
  "剩余 1 秒"却不解锁的问题）
- **真·多进程**：两个独立 Python 解释器共享同一份限流状态——这是本次从
  内存迁到数据库的核心保证，旧实现下进程 B 必然读到空状态（限流被 worker
  数放大）
- X-Forwarded-For 取首个 IP
- 集成：POST /api/auth/login 真实触发 429

隔离方式：独立 sqlite 文件库（NullPool），并把 `rate_limit.get_session`
替换为指向该库——避免与 TestClient 全局 engine 的 event loop 冲突
（同 test_db_migrations.py 的思路）。NullPool 保证每次操作新建连接，
`asyncio.run()` 多次调用不会复用跨 loop 的连接。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from backend.config import settings as config_settings
from backend.models.user_models import LoginAttemptORM
from backend.services import rate_limit

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _request(ip: str = "1.2.3.4", xff: str = "") -> SimpleNamespace:
    """模拟 starlette Request（限速模块只用 client/headers）"""
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=SimpleNamespace(host=ip),
                           headers=headers)


@pytest.fixture
def rl_db(tmp_path, monkeypatch):
    """独立 sqlite 文件库（只建 login_attempts 表）+ 替换 rate_limit 会话工厂"""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ratelimit.db'}", poolclass=NullPool)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(LoginAttemptORM.__table__.create)

    asyncio.run(_create())
    monkeypatch.setattr(
        rate_limit, "get_session",
        lambda: AsyncSession(engine, expire_on_commit=False))
    yield engine
    asyncio.run(engine.dispose())


@pytest.fixture
def enabled(monkeypatch):
    """开启限速（测试环境 conftest 默认关）：窗口 60s / 上限 5 / 锁定 60s"""
    monkeypatch.setattr(config_settings, "LOGIN_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config_settings, "LOGIN_RATE_WINDOW", 60)
    monkeypatch.setattr(config_settings, "LOGIN_MAX_FAILURES", 5)
    monkeypatch.setattr(config_settings, "LOGIN_LOCK_SECONDS", 60)


def _count(engine, where: str = "1=1") -> int:
    """直接查表计数（不经过 rate_limit，用于断言落库结果）"""
    async def _run():
        async with engine.connect() as conn:
            return (await conn.execute(text(
                f"SELECT COUNT(*) FROM login_attempts WHERE {where}"))).scalar()
    return asyncio.run(_run())


def _insert_failures(engine, ip: str, count: int, seconds_ago: int = 0) -> None:
    """直接插入失败记录（构造历史现场，如"窗口外的旧记录"）"""
    ts = (datetime.now() - timedelta(seconds=seconds_ago)).strftime(_TS_FMT)
    async def _run():
        async with engine.begin() as conn:
            for _ in range(count):
                await conn.execute(LoginAttemptORM.__table__.insert().values(
                    id=uuid.uuid4().hex, ip=ip, attempted_at=ts))
    asyncio.run(_run())


class TestDisabled:
    """开关关闭（测试默认）→ 所有函数无操作，且不写库"""

    def test_check_returns_none(self, rl_db):
        assert asyncio.run(rate_limit.check(_request())) is None

    def test_records_noop(self, rl_db):
        asyncio.run(rate_limit.record_failure(_request()))
        asyncio.run(rate_limit.record_success(_request()))
        assert _count(rl_db) == 0, "开关关闭时不应写库"


class TestEnabled:
    def test_five_failures_lock(self, enabled, rl_db):
        """5 次失败 → 锁定；第 6 次 check 返回剩余秒数"""
        req = _request()
        async def _run():
            for _ in range(5):
                await rate_limit.record_failure(req)
            return await rate_limit.check(req)
        remaining = asyncio.run(_run())
        assert remaining is not None
        assert 1 <= remaining <= 60

    def test_under_limit_free(self, enabled, rl_db):
        """失败 < 上限 → 不锁定"""
        req = _request()
        async def _run():
            for _ in range(4):
                await rate_limit.record_failure(req)
            return await rate_limit.check(req)
        assert asyncio.run(_run()) is None

    def test_success_resets(self, enabled, rl_db):
        """成功清零：失败 4 次 + 成功 → 再失败重新从 0 计"""
        req = _request()
        async def _run():
            for _ in range(4):
                await rate_limit.record_failure(req)
            await rate_limit.record_success(req)
            assert await rate_limit.check(req) is None
            for _ in range(4):
                await rate_limit.record_failure(req)
            return await rate_limit.check(req)
        assert asyncio.run(_run()) is None, "成功应清零失败记录"
        assert _count(rl_db, "ip='1.2.3.4'") == 4, "清零后应只剩新一轮 4 条"

    def test_different_ips_isolated(self, enabled, rl_db):
        """不同 IP 互不影响"""
        async def _run():
            for _ in range(5):
                await rate_limit.record_failure(_request(ip="1.1.1.1"))
            return await rate_limit.check(_request(ip="2.2.2.2"))
        assert asyncio.run(_run()) is None


class TestWindowCleanup:
    """窗口清理：旧内存实现只清当前 IP，一次性 IP 永久残留（泄漏点）"""

    def test_old_failures_not_counted(self, enabled, rl_db):
        """窗口外的旧记录不参与锁定判定"""
        _insert_failures(rl_db, "1.2.3.4", count=5, seconds_ago=300)
        assert asyncio.run(rate_limit.check(_request())) is None, \
            "窗口外记录不应触发锁定"

    def test_cleanup_removes_other_ips(self, enabled, rl_db):
        """★ record_failure 全表清理：**别的 IP** 的超窗记录也要删掉

        旧内存实现只 prune 当前 IP，被扫描时产生的一次性 IP 永远留在字典里。
        """
        _insert_failures(rl_db, "9.9.9.9", count=3, seconds_ago=300)
        assert _count(rl_db, "ip='9.9.9.9'") == 3
        asyncio.run(rate_limit.record_failure(_request(ip="1.2.3.4")))
        assert _count(rl_db, "ip='9.9.9.9'") == 0, "他 IP 的超窗记录应被清理"
        assert _count(rl_db, "ip='1.2.3.4'") == 1, "本次记录应保留"

    def test_window_records_kept(self, enabled, rl_db):
        """窗口内的记录不被清理"""
        _insert_failures(rl_db, "9.9.9.9", count=3, seconds_ago=10)
        asyncio.run(rate_limit.record_failure(_request(ip="1.2.3.4")))
        assert _count(rl_db, "ip='9.9.9.9'") == 3


class TestLockExpiry:
    """锁定到期语义（修正项）"""

    def test_unlock_after_lock_expires(self, enabled, rl_db, monkeypatch):
        """★ LOCK_SECONDS < WINDOW 时，锁定到期应**解锁**

        旧实现 `max(1, int(lock - elapsed))` 在 elapsed > lock 后恒返回 1，
        用户反复看到"请 1 秒后再试"却始终登不上（要等记录被窗口清掉）。
        """
        monkeypatch.setattr(config_settings, "LOGIN_RATE_WINDOW", 600)
        monkeypatch.setattr(config_settings, "LOGIN_LOCK_SECONDS", 60)
        # 5 条失败在 120 秒前：窗口内（600s），但已超锁定时间（60s）
        _insert_failures(rl_db, "1.2.3.4", count=5, seconds_ago=120)
        assert asyncio.run(rate_limit.check(_request())) is None, \
            "锁定已到期应解锁，而不是卡在'剩余 1 秒'"

    def test_still_locked_within_lock(self, enabled, rl_db, monkeypatch):
        """锁定时间内的失败仍锁定"""
        monkeypatch.setattr(config_settings, "LOGIN_RATE_WINDOW", 600)
        monkeypatch.setattr(config_settings, "LOGIN_LOCK_SECONDS", 60)
        _insert_failures(rl_db, "1.2.3.4", count=5, seconds_ago=10)
        remaining = asyncio.run(rate_limit.check(_request()))
        assert remaining is not None
        assert 1 <= remaining <= 50, f"应剩约 50 秒: {remaining}"


class TestCrossProcess:
    """★ 多 worker 核心保证：状态由数据库承载，与进程无关"""

    def test_state_lives_in_database(self, enabled, rl_db):
        """失败记录落在数据库表（而非模块内存）"""
        asyncio.run(rate_limit.record_failure(_request(ip="7.7.7.7")))
        assert _count(rl_db, "ip='7.7.7.7'") == 1

    def test_no_module_level_state(self):
        """模块内不得再有跨请求的进程级状态（多 worker 失效的根因）

        防回归：若有人改回内存字典，此断言会失败。
        """
        assert not hasattr(rate_limit, "_failures"), "不应再有内存计数字典"
        assert not hasattr(rate_limit, "_lock"), "不应再有进程内锁"

    def test_two_processes_share_state(self, tmp_path):
        """**真·多进程**：进程 A 记 5 次失败 → 进程 B（全新解释器）应看到锁定

        旧内存实现下，进程 B 的 check 必然返回 None（它自己的字典是空的），
        限流阈值被放大 N 倍（N=worker 数）。
        """
        env = {
            **os.environ,
            "DATA_DIR": str(tmp_path),
            "MYSQL_URL": f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}",
            "JWT_SECRET": "test-secret-test-secret",
            "LOGIN_RATE_LIMIT_ENABLED": "true",
            "LOGIN_RATE_WINDOW": "60",
            "LOGIN_MAX_FAILURES": "5",
            "LOGIN_LOCK_SECONDS": "60",
        }
        proj = str(Path(__file__).resolve().parents[1])
        script = (
            "import asyncio, sys\n"
            "from types import SimpleNamespace\n"
            "from backend.db import Base, get_engine\n"
            "from backend.services import rate_limit\n"
            "class Req:\n"
            "    def __init__(self, ip):\n"
            "        self.client = SimpleNamespace(host=ip)\n"
            "        self.headers = {}\n"
            "async def main():\n"
            "    mode, ip = sys.argv[1], sys.argv[2]\n"
            "    req = Req(ip)\n"
            "    if mode == 'init':\n"
            "        async with get_engine().begin() as conn:\n"
            "            await conn.run_sync(Base.metadata.create_all)\n"
            "        print('ready')\n"
            "    elif mode == 'fail':\n"
            "        for _ in range(5):\n"
            "            await rate_limit.record_failure(req)\n"
            "        print('recorded')\n"
            "    else:\n"
            "        r = await rate_limit.check(req)\n"
            "        print('locked' if r is not None else 'free')\n"
            "asyncio.run(main())\n"
        )

        def _run(mode: str, ip: str) -> str:
            r = subprocess.run(
                [sys.executable, "-c", script, mode, ip],
                cwd=proj, env=env, capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, f"子进程失败({mode}):\n{r.stderr}"
            return r.stdout.strip().splitlines()[-1]

        assert _run("init", "127.0.0.1") == "ready"
        assert _run("fail", "9.9.9.9") == "recorded"
        # 全新解释器，内存中没有任何上面的记录 → 只能从库里读到
        assert _run("check", "9.9.9.9") == "locked", \
            "另一进程应能看到失败计数并锁定（落库的意义所在）"
        # 未失败过的 IP 在该进程里仍应放行
        assert _run("check", "8.8.8.8") == "free"


class TestIpExtraction:
    def test_forwarded_for_first(self):
        """X-Forwarded-For 取首个（nginx 后真实 IP）"""
        r = _request(ip="127.0.0.1", xff="203.0.113.9, 10.0.0.2")
        assert rate_limit.ip_of(r) == "203.0.113.9"

    def test_client_default(self):
        assert rate_limit.ip_of(_request(ip="8.8.8.8")) == "8.8.8.8"

    def test_no_client_unknown(self):
        r = SimpleNamespace(client=None, headers={})
        assert rate_limit.ip_of(r) == "unknown"


class TestLoginEndpoint:
    """集成：POST /api/auth/login 真实触发（走真实全局 engine）"""

    def test_lock_after_failures_429(self, client, monkeypatch):
        """开启限速：连续 5 次错密码 → 第 6 次 429"""
        monkeypatch.setattr(config_settings, "LOGIN_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config_settings, "LOGIN_MAX_FAILURES", 5)
        for i in range(5):
            resp = client.post("/api/auth/login", json={
                "username": "admin", "password": "wrong-password"})
            assert resp.status_code == 401, f"第 {i+1} 次应 401"
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "wrong-password"})
        assert resp.status_code == 429, "第 6 次应 429 锁定"
        assert "过于频繁" in resp.json()["detail"]

    def test_locked_even_correct_password(self, client, monkeypatch):
        """锁定期内正确密码也 429（锁定不分对错，防爆破窗口）"""
        monkeypatch.setattr(config_settings, "LOGIN_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config_settings, "LOGIN_MAX_FAILURES", 5)
        for _ in range(5):
            client.post("/api/auth/login", json={
                "username": "admin", "password": "wrong-password"})
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})
        assert resp.status_code == 429, "锁定期正确密码也应 429"

    def test_success_clears_and_allows_login(self, client, monkeypatch):
        """成功登录清零：失败 4 次后正确登录应成功，且计数清空"""
        monkeypatch.setattr(config_settings, "LOGIN_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config_settings, "LOGIN_MAX_FAILURES", 5)
        for _ in range(4):
            client.post("/api/auth/login", json={
                "username": "admin", "password": "wrong-password"})
        ok = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})
        assert ok.status_code == 200, "未达上限时正确密码应能登录"
        # 清零后再失败 4 次仍不锁定（若未清零，累计 8 次早该 429）
        for _ in range(4):
            client.post("/api/auth/login", json={
                "username": "admin", "password": "wrong-password"})
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "wrong-password"})
        assert resp.status_code == 401, "成功登录应已清零计数"
