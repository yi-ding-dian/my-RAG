"""认证 API 测试：/api/auth（契约第 1 章）

覆盖：登录成功（admin）、密码错误/用户不存在统一 401 防枚举、无 token/伪造
token 401（带 WWW-Authenticate）、me 返回当前用户、change-password（旧密码
错误 400 / 新旧相同 400 / 成功后新密码可登录、旧密码失效）、health 公开。
全部进程内 TestClient + sqlite 内存库（种子 admin/admin123）。
"""
from __future__ import annotations

import pytest


class TestLogin:
    """登录"""

    def test_login_admin_ok(self, client):
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123",
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["token_type"] == "bearer"
        assert data["access_token"]
        user = data["user"]
        assert user["username"] == "admin"
        assert user["role"] == "super_admin"
        assert user["status"] == "active"
        assert user["department_id"] is None
        assert user["department_name"] is None
        for field in ("id", "display_name", "created_at"):
            assert user[field], f"user 缺字段: {field}"
        assert "password" not in user, "UserPublic 不应包含密码"

    def test_login_wrong_password_401(self, client):
        """密码错误 → 401 统一文案（防枚举）"""
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "wrong-pass",
        })
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"

    def test_login_unknown_user_401(self, client):
        """用户不存在 → 401 同文案（不区分用户不存在/密码错）"""
        resp = client.post("/api/auth/login", json={
            "username": "nobody", "password": "whatever",
        })
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"


class TestTokenValidation:
    """token 校验"""

    def test_no_token_401(self, client):
        """无 token 访问受保护接口 → 401 + WWW-Authenticate 头"""
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate", "").startswith("Bearer")
        assert "登录已过期" in resp.json()["detail"]

    def test_fake_token_401(self, client):
        """伪造 token → 401"""
        resp = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401

    def test_me_returns_current_user(self, client, admin_headers):
        """me 返回当前登录用户（与 login.user 同构）"""
        resp = client.get("/api/auth/me", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        user = resp.json()
        assert user["username"] == "admin"
        assert user["role"] == "super_admin"

    def test_health_public(self, client):
        """health 公开（无需登录）"""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestChangePassword:
    """修改密码"""

    def test_change_password_wrong_old_400(self, client, admin_headers):
        """旧密码错误 → 400"""
        resp = client.post("/api/auth/change-password", json={
            "old_password": "wrong-old",
            "new_password": "newpass888",
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "旧密码不正确"

    def test_change_password_same_400(self, client, admin_headers):
        """新密码与旧密码相同 → 400"""
        resp = client.post("/api/auth/change-password", json={
            "old_password": "admin123",
            "new_password": "admin123",
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "新密码不能与旧密码相同" in resp.json()["detail"]

    def test_change_password_then_login_with_new(self, client, admin_headers):
        """改密成功 → 旧密码登录 401、新密码登录 200"""
        resp = client.post("/api/auth/change-password", json={
            "old_password": "admin123",
            "new_password": "newpass888",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "密码修改成功"
        # 旧密码失效
        old = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123",
        })
        assert old.status_code == 401
        # 新密码可登录（并正常通过鉴权）
        new = client.post("/api/auth/login", json={
            "username": "admin", "password": "newpass888",
        })
        assert new.status_code == 200
        headers = {"Authorization": f"Bearer {new.json()['access_token']}"}
        assert client.get("/api/auth/me", headers=headers).status_code == 200
