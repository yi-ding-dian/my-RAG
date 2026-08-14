"""A1 P0 update_user 超管保护测试

问题：PUT /api/users/{id} 对 super_admin 无保护——可禁用/降级自己或
最后一个超管，之后系统无管理员、管理接口锁死（delete 有保护、update 漏了）。
修复：user_service.update 增加与 delete 同款校验——不能修改当前登录账号的
角色或状态（400）；不能把最后一个激活 super_admin 禁用或降级（400）。
"""
from __future__ import annotations


def _admin_id(client, admin_headers) -> str:
    """取种子账号 admin 的 id"""
    users = client.get("/api/users", headers=admin_headers).json()
    return next(u["id"] for u in users if u["username"] == "admin")


def _create_super_admin(client, admin_headers, username="sa2") -> dict:
    """建一个额外的 super_admin（默认部门），返回 UserPublic"""
    resp = client.post("/api/users", json={
        "username": username,
        "password": "pass123456",
        "display_name": "第二超管",
        "role": "super_admin",
        "department_id": "dept_default",
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestSelfProtection:
    """超管不能改自己的角色/状态（400）"""

    def test_disable_self_400(self, client, admin_headers):
        """admin 禁用自己的 status=disabled → 400"""
        admin_id = _admin_id(client, admin_headers)
        resp = client.put(f"/api/users/{admin_id}",
                          json={"status": "disabled"}, headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "不能修改当前登录账号的角色或状态"

    def test_demote_self_400(self, client, admin_headers):
        """admin 降级自己的 role → 400（消息一致）"""
        admin_id = _admin_id(client, admin_headers)
        resp = client.put(f"/api/users/{admin_id}",
                          json={"role": "user"}, headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "不能修改当前登录账号的角色或状态"

    def test_update_self_profile_ok(self, client, admin_headers):
        """自己改 display_name/密码等普通字段不受影响 → 200"""
        admin_id = _admin_id(client, admin_headers)
        resp = client.put(f"/api/users/{admin_id}",
                          json={"display_name": "管理员甲"},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "管理员甲"


class TestLastSuperAdminProtection:
    """最后一个激活 super_admin 不能被禁用/降级（400）"""

    def test_disable_last_super_admin_400(self, client, admin_headers):
        """先禁用第二超管（两超管时 OK），再禁它 → 此时只剩 admin 一个
        激活超管 → 400 保护"""
        sa2 = _create_super_admin(client, admin_headers)
        # 两个激活超管：禁一个 → OK
        resp = client.put(f"/api/users/{sa2['id']}",
                          json={"status": "disabled"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        # 只剩 admin 一个激活超管：再次禁目标 → 400（目标仍是 super_admin）
        resp = client.put(f"/api/users/{sa2['id']}",
                          json={"status": "disabled"}, headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "不能禁用或降级最后一个超级管理员"

    def test_demote_last_super_admin_400(self, client, admin_headers):
        """同上：先禁第二超管，再降级它 → 400"""
        sa2 = _create_super_admin(client, admin_headers)
        client.put(f"/api/users/{sa2['id']}",
                   json={"status": "disabled"}, headers=admin_headers)
        resp = client.put(f"/api/users/{sa2['id']}",
                          json={"role": "user"}, headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "不能禁用或降级最后一个超级管理员"

    def test_disable_one_of_two_ok(self, client, admin_headers):
        """有两个超管时禁一个 → 200，系统仍有管理员"""
        sa2 = _create_super_admin(client, admin_headers)
        resp = client.put(f"/api/users/{sa2['id']}",
                          json={"status": "disabled"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "disabled"
        # admin 仍可登录（未被锁死）
        login = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})
        assert login.status_code == 200

    def test_normal_update_unaffected(self, client, admin_headers):
        """普通用户（非超管）的普通更新不受保护影响 → 200"""
        resp = client.post("/api/users", json={
            "username": "normal_user", "password": "pass123456",
            "display_name": "普通", "role": "user",
            "department_id": "dept_default",
        }, headers=admin_headers)
        uid = resp.json()["id"]
        resp = client.put(f"/api/users/{uid}",
                          json={"display_name": "改名", "role": "dept_admin"},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "改名"
        assert resp.json()["role"] == "dept_admin"
