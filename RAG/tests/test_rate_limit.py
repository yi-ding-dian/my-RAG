"""登录限速单元测试：IP 维度失败计数窗口

覆盖：开关关（测试默认）→ 不锁定；开启后 5 次失败触发锁定 → 429、
第 6 次仍锁、剩余秒数递减；成功登录清零；窗口过期记录清理；
X-Forwarded-For 取首个 IP；内存裁剪上限。

全部离线（纯逻辑，不依赖网络）。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.config import settings as config_settings
from backend.services import rate_limit


def _request(ip: str = "1.2.3.4", xff: str = "") -> SimpleNamespace:
    """模拟 starlette Request（限速模块只用 client/headers）"""
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=SimpleNamespace(host=ip),
                           headers=headers)


@pytest.fixture
def enabled(monkeypatch):
    """开启限速窗口配置（测试环境 conftest 默认关）：小窗口便于测"""
    monkeypatch.setattr(config_settings, "LOGIN_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config_settings, "LOGIN_RATE_WINDOW", 60)
    monkeypatch.setattr(config_settings, "LOGIN_MAX_FAILURES", 5)
    monkeypatch.setattr(config_settings, "LOGIN_LOCK_SECONDS", 60)
    # 重置内存状态（各测试独立）
    rate_limit._failures.clear()
    yield
    rate_limit._failures.clear()


class TestDisabled:
    """开关关闭（测试默认）→ 所有函数无操作"""

    def test_check_returns_none(self):
        assert rate_limit.check(_request()) is None

    def test_records_noop(self):
        rate_limit.record_failure(_request())
        rate_limit.record_success(_request())
        assert rate_limit._failures == {}


class TestEnabled:
    def test_five_failures_lock(self, enabled):
        """5 次失败 → 锁定；第 6 次 check 返回剩余秒数"""
        req = _request()
        for _ in range(5):
            rate_limit.record_failure(req)
        remaining = rate_limit.check(req)
        assert remaining is not None
        assert 1 <= remaining <= 60

    def test_under_limit_free(self, enabled):
        """失败 < 上限 → 不锁定"""
        req = _request()
        for _ in range(4):
            rate_limit.record_failure(req)
        assert rate_limit.check(req) is None

    def test_success_resets(self, enabled):
        """成功清零：失败 4 次 + 成功 → 再失败重新从 0 计"""
        req = _request()
        for _ in range(4):
            rate_limit.record_failure(req)
        rate_limit.record_success(req)
        assert rate_limit.check(req) is None
        for _ in range(4):
            rate_limit.record_failure(req)
        assert rate_limit.check(req) is None, "成功应清零失败记录"


class TestWindowPrune:
    def test_old_failures_pruned(self, enabled):
        """窗口外旧失败记录被清理（模拟窗口过期 → 不锁定）"""
        req = _request()
        with rate_limit._lock:
            # 直接塞窗口外的旧记录（time.time() 当前 - 300s）
            rate_limit._failures[req.client.host] = [
                time.time() - 300, time.time() - 300, time.time() - 300,
                time.time() - 300, time.time() - 300,
            ]
        assert rate_limit.check(req) is None, "窗口外记录应被清理不触发锁定"

    def test_cap_list_growth(self, enabled):
        """记录列表超上限裁剪（内存防膨胀）"""
        req = _request()
        for _ in range(50):
            rate_limit.record_failure(req)
        with rate_limit._lock:
            ts = rate_limit._failures[req.client.host]
        assert len(ts) <= 6, f"记录应裁剪到上限附近: {len(ts)}"


class TestIpExtraction:
    def test_forwarded_for_first(self, enabled):
        """X-Forwarded-For 取首个（nginx 后真实 IP）"""
        r = _request(ip="127.0.0.1", xff="203.0.113.9, 10.0.0.2")
        assert rate_limit.ip_of(r) == "203.0.113.9"

    def test_client_default(self, enabled):
        assert rate_limit.ip_of(_request(ip="8.8.8.8")) == "8.8.8.8"

    def test_no_client_unknown(self, enabled):
        r = SimpleNamespace(client=None, headers={})
        assert rate_limit.ip_of(r) == "unknown"


class TestLoginEndpoint:
    """集成：POST /api/auth/login 真实触发（开启限速配置）"""

    def test_lock_after_failures_429(self, client, monkeypatch):
        """开启限速：连续 5 次错密码 → 第 6 次 429"""
        monkeypatch.setattr(config_settings, "LOGIN_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config_settings, "LOGIN_MAX_FAILURES", 5)
        rate_limit._failures.clear()
        for i in range(5):
            resp = client.post("/api/auth/login", json={
                "username": "admin", "password": "wrong-password"})
            assert resp.status_code == 401, f"第 {i+1} 次应 401"
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "wrong-password"})
        assert resp.status_code == 429, "第 6 次应 429 锁定"
        assert "过于频繁" in resp.json()["detail"]
        rate_limit._failures.clear()

    def test_locked_even_correct_password(self, client, monkeypatch):
        """锁定期内正确密码也 429（锁定不分对错，防爆破窗口）"""
        monkeypatch.setattr(config_settings, "LOGIN_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config_settings, "LOGIN_MAX_FAILURES", 5)
        rate_limit._failures.clear()
        for _ in range(5):
            client.post("/api/auth/login", json={
                "username": "admin", "password": "wrong-password"})
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})
        assert resp.status_code == 429, "锁定期正确密码也应 429"
        rate_limit._failures.clear()
