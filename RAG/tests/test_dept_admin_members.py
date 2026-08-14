"""部门管理员管理本部门成员权限测试：/api/users + /api/departments

覆盖（权限矩阵部门管理员列）：
- 列表仅本部门（不含其他部门成员/全局 super_admin）
- 创建：本部门成功；强塞其他部门 → 强制本部门；role=super_admin → 400
- 编辑：本部门成员成功；跨部门成员 → 404；改别人为 super_admin → 400；
  跨部门调整 → 400；改自己角色/禁自己 → 400；改 super_admin → 404
- 删除：本部门成员成功；删自己 → 409；删跨部门成员/超管 → 404
- 重置密码：本部门成员成功（新密码可登录）；跨部门 → 404
- 部门：列表仅本部门；编辑本部门成功；编辑其他部门 404；创建/删除 403
- user 角色：用户/部门全部接口 403（现状保持）
"""
from __future__ import annotations

import pytest

from conftest import create_department_and_admin, create_user


@pytest.fixture()
def multi_dept_env(client, admin_headers):
    """两部门环境：A 部（dept_admin + user）、B 部（user + dept_admin）"""
    dept_a_id, dept_admin_a = create_department_and_admin(
        client, admin_headers, "成员权限A部", "member_admin_a",
        "pass123456", "A部主管")
    dept_b_id, dept_admin_b = create_department_and_admin(
        client, admin_headers, "成员权限B部", "member_admin_b",
        "pass123456", "B部主管")
    user_a = create_user(client, admin_headers, dept_a_id, "member_user_a")
    user_b = create_user(client, admin_headers, dept_b_id, "member_user_b")
    return {
        "dept_a_id": dept_a_id,
        "dept_b_id": dept_b_id,
        "dept_admin_a": dept_admin_a,
        "dept_admin_b": dept_admin_b,
        "user_a": user_a,
        "user_b": user_b,
    }


def _user_id(client, admin_headers, username: str) -> str:
    """按用户名查用户 id（admin 全量列表）"""
    users = client.get("/api/users", headers=admin_headers).json()
    return next(u["id"] for u in users if u["username"] == username)


class TestDeptAdminUserList:
    """dept_admin 用户列表仅本部门"""

    def test_list_only_own_department(self, client, admin_headers,
                                      multi_dept_env):
        env = multi_dept_env
        users = client.get("/api/users", headers=env["dept_admin_a"]).json()
        names = {u["username"] for u in users}
        assert "member_admin_a" in names and "member_user_a" in names
        assert "member_user_b" not in names, "不得出现其他部门成员"
        assert "admin" not in names, "不得出现全局 super_admin"
        # 全部成员部门号均为本部门
        assert all(u["department_id"] == env["dept_a_id"] for u in users)
        # 传 department_id 过滤参数也被忽略（强制本部门）
        forced = client.get("/api/users?department_id=" + env["dept_b_id"],
                            headers=env["dept_admin_a"]).json()
        assert {u["username"] for u in forced} == names

    def test_list_own_department_name_joined(self, client, admin_headers,
                                             multi_dept_env):
        """列表响应带 department_name"""
        users = client.get("/api/users", headers=multi_dept_env["dept_admin_a"]).json()
        assert all(u["department_name"] == "成员权限A部" for u in users)


class TestDeptAdminCreateUser:
    """dept_admin 创建用户：强制本部门、角色受限"""

    def test_create_user_in_own_department(self, client, admin_headers,
                                           multi_dept_env):
        """创建本部门 user 成功 → 201"""
        resp = client.post("/api/users", json={
            "username": "new_member", "password": "pass123456",
            "display_name": "新成员", "role": "user",
        }, headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 201, resp.text
        assert resp.json()["department_id"] == multi_dept_env["dept_a_id"]

    def test_create_second_dept_admin_400(self, client, admin_headers,
                                          multi_dept_env):
        """每部门唯一管理员：创建本部门第二个 dept_admin → 400"""
        resp = client.post("/api/users", json={
            "username": "new_sub_admin", "password": "pass123456",
            "display_name": "副主管", "role": "dept_admin",
        }, headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 400, resp.text
        assert "该部门已有一名管理员" in resp.json()["detail"]
        # 超管创建同部门第二个 dept_admin 同样被拒（约束对部门本身生效）
        resp = client.post("/api/users", json={
            "username": "admin_dup", "password": "pass123456",
            "display_name": "重复主管", "role": "dept_admin",
            "department_id": multi_dept_env["dept_a_id"],
        }, headers=admin_headers)
        assert resp.status_code == 400, resp.text
        assert "该部门已有一名管理员" in resp.json()["detail"]

    def test_create_forced_own_department(self, client, admin_headers,
                                          multi_dept_env):
        """body 强塞其他部门 → 强制本部门（与 kb 创建同模式）"""
        env = multi_dept_env
        resp = client.post("/api/users", json={
            "username": "forced_member", "password": "pass123456",
            "display_name": "强制归属", "role": "user",
            "department_id": env["dept_b_id"],
        }, headers=env["dept_admin_a"])
        assert resp.status_code == 201, resp.text
        assert resp.json()["department_id"] == env["dept_a_id"]

    def test_create_super_admin_role_400(self, client, admin_headers,
                                         multi_dept_env):
        """创建 super_admin 角色 → 400"""
        resp = client.post("/api/users", json={
            "username": "wannabe_root", "password": "pass123456",
            "display_name": "越权", "role": "super_admin",
        }, headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 400
        assert "不能创建超级管理员" in resp.json()["detail"]


class TestDeptAdminUpdateUser:
    """dept_admin 编辑用户：仅本部门成员 + 约束"""

    def test_update_own_member_success(self, client, admin_headers,
                                       multi_dept_env):
        """编辑本部门成员（显示名）成功"""
        env = multi_dept_env
        uid = _user_id(client, admin_headers, "member_user_a")
        resp = client.put(f"/api/users/{uid}", json={"display_name": "改名成员"},
                          headers=env["dept_admin_a"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "改名成员"

    def test_update_cross_department_404(self, client, admin_headers,
                                         multi_dept_env):
        """编辑跨部门成员 → 404 伪装"""
        env = multi_dept_env
        uid = _user_id(client, admin_headers, "member_user_b")
        resp = client.put(f"/api/users/{uid}", json={"display_name": "x"},
                          headers=env["dept_admin_a"])
        assert resp.status_code == 404
        assert resp.json()["detail"] == "用户不存在"

    def test_update_super_admin_404(self, client, admin_headers,
                                    multi_dept_env):
        """编辑 super_admin → 404 伪装"""
        uid = _user_id(client, admin_headers, "admin")
        resp = client.put(f"/api/users/{uid}", json={"display_name": "x"},
                          headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 404

    def test_update_role_to_super_admin_400(self, client, admin_headers,
                                            multi_dept_env):
        """把本部门成员改为 super_admin → 400"""
        env = multi_dept_env
        uid = _user_id(client, admin_headers, "member_user_a")
        resp = client.put(f"/api/users/{uid}", json={"role": "super_admin"},
                          headers=env["dept_admin_a"])
        assert resp.status_code == 400
        assert "不能将用户设置为超级管理员" in resp.json()["detail"]

    def test_update_move_to_other_department_400(self, client, admin_headers,
                                                 multi_dept_env):
        """把本部门成员移到其他部门 → 400"""
        env = multi_dept_env
        uid = _user_id(client, admin_headers, "member_user_a")
        resp = client.put(f"/api/users/{uid}",
                          json={"department_id": env["dept_b_id"]},
                          headers=env["dept_admin_a"])
        assert resp.status_code == 400
        assert "不能将用户分配到其他部门" in resp.json()["detail"]

    def test_update_self_role_400(self, client, admin_headers, multi_dept_env):
        """修改自己的角色 → 400"""
        uid = _user_id(client, admin_headers, "member_admin_a")
        resp = client.put(f"/api/users/{uid}", json={"role": "user"},
                          headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 400
        assert "不能修改自己的角色" in resp.json()["detail"]

    def test_update_self_disabled_400(self, client, admin_headers,
                                      multi_dept_env):
        """禁用自己 → 400"""
        uid = _user_id(client, admin_headers, "member_admin_a")
        resp = client.put(f"/api/users/{uid}", json={"status": "disabled"},
                          headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 400
        assert "不能禁用当前登录账号" in resp.json()["detail"]


class TestDeptAdminDeleteUser:
    """dept_admin 删除用户：仅本部门成员 + 保护"""

    def test_delete_own_member_success(self, client, admin_headers,
                                       multi_dept_env):
        """删除本部门成员成功 → 200；其后登录 401"""
        env = multi_dept_env
        uid = _user_id(client, admin_headers, "member_user_a")
        resp = client.delete(f"/api/users/{uid}", headers=env["dept_admin_a"])
        assert resp.status_code == 200
        assert resp.json()["message"] == "用户已删除"
        assert client.post("/api/auth/login", json={
            "username": "member_user_a", "password": "user123456",
        }).status_code == 401

    def test_delete_self_409(self, client, admin_headers, multi_dept_env):
        """删除自己 → 409（与现有「不能删除当前登录账号」逻辑一致）"""
        uid = _user_id(client, admin_headers, "member_admin_a")
        resp = client.delete(f"/api/users/{uid}",
                             headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 409
        assert resp.json()["detail"] == "不能删除当前登录账号"

    def test_delete_cross_department_404(self, client, admin_headers,
                                         multi_dept_env):
        """删除跨部门成员 → 404 伪装"""
        env = multi_dept_env
        uid = _user_id(client, admin_headers, "member_user_b")
        resp = client.delete(f"/api/users/{uid}", headers=env["dept_admin_a"])
        assert resp.status_code == 404

    def test_delete_super_admin_404(self, client, admin_headers,
                                    multi_dept_env):
        """删除 super_admin → 404 伪装"""
        uid = _user_id(client, admin_headers, "admin")
        resp = client.delete(f"/api/users/{uid}",
                             headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 404


class TestDeptAdminResetPassword:
    """dept_admin 重置本部门成员密码（PUT /users/{id} 传 password）"""

    def test_reset_own_member_password(self, client, admin_headers,
                                       multi_dept_env):
        """重置本部门成员密码 → 新密码可登录、旧密码失效"""
        env = multi_dept_env
        uid = _user_id(client, admin_headers, "member_user_a")
        resp = client.put(f"/api/users/{uid}", json={"password": "newpass789"},
                          headers=env["dept_admin_a"])
        assert resp.status_code == 200
        assert client.post("/api/auth/login", json={
            "username": "member_user_a", "password": "newpass789",
        }).status_code == 200
        assert client.post("/api/auth/login", json={
            "username": "member_user_a", "password": "user123456",
        }).status_code == 401

    def test_reset_cross_department_404(self, client, admin_headers,
                                        multi_dept_env):
        """重置跨部门成员密码 → 404 伪装"""
        env = multi_dept_env
        uid = _user_id(client, admin_headers, "member_user_b")
        resp = client.put(f"/api/users/{uid}", json={"password": "x123456"},
                          headers=env["dept_admin_a"])
        assert resp.status_code == 404


class TestDeptAdminDepartments:
    """部门：dept_admin 列表仅本部门、可编辑本部门、不可创建/删除"""

    def test_list_only_own_department(self, client, admin_headers,
                                      multi_dept_env):
        """部门列表仅返回本部门（不含默认部门与其他部门）"""
        env = multi_dept_env
        depts = client.get("/api/departments",
                           headers=env["dept_admin_a"]).json()
        assert [d["id"] for d in depts] == [env["dept_a_id"]]
        assert depts[0]["name"] == "成员权限A部"

    def test_update_own_department_success(self, client, admin_headers,
                                           multi_dept_env):
        """编辑本部门名称/描述 → 200"""
        env = multi_dept_env
        resp = client.put(f"/api/departments/{env['dept_a_id']}", json={
            "name": "成员权限A部-改名", "description": "新描述",
        }, headers=env["dept_admin_a"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "成员权限A部-改名"
        assert resp.json()["description"] == "新描述"

    def test_update_other_department_404(self, client, admin_headers,
                                         multi_dept_env):
        """编辑其他部门 → 404 伪装"""
        env = multi_dept_env
        resp = client.put(f"/api/departments/{env['dept_b_id']}",
                          json={"name": "越权改名"},
                          headers=env["dept_admin_a"])
        assert resp.status_code == 404
        assert resp.json()["detail"] == "部门不存在"

    def test_create_department_403(self, client, admin_headers,
                                   multi_dept_env):
        """创建部门 → 403（仅 super_admin）"""
        resp = client.post("/api/departments", json={"name": "越权新部"},
                           headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 403

    def test_delete_department_403(self, client, admin_headers,
                                   multi_dept_env):
        """删除部门 → 403（仅 super_admin）"""
        resp = client.delete(f"/api/departments/{multi_dept_env['dept_a_id']}",
                             headers=multi_dept_env["dept_admin_a"])
        assert resp.status_code == 403


class TestDeptAdminWithoutDepartment:
    """dept_admin 无部门归属（异常配置防御）：不能管理任何用户"""

    def test_no_department_dept_admin_isolated(self, client, admin_headers):
        """无部门 dept_admin：列表为空、编辑/删除任意用户 404、创建 403"""
        # 超管创建一个无部门的 dept_admin
        resp = client.post("/api/users", json={
            "username": "homeless_admin", "password": "pass123456",
            "display_name": "无部门主管", "role": "dept_admin",
        }, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        login = client.post("/api/auth/login", json={
            "username": "homeless_admin", "password": "pass123456",
        }).json()
        hdrs = {"Authorization": f"Bearer {login['access_token']}"}
        # 列表为空（不泄露全局用户）
        assert client.get("/api/users", headers=hdrs).json() == []
        # 编辑/删除任意用户（含未分配用户）→ 404 伪装
        uid = _user_id(client, admin_headers, "homeless_admin")
        assert client.put(f"/api/users/{uid}", json={"display_name": "x"},
                          headers=hdrs).status_code == 404
        assert client.delete(f"/api/users/{uid}",
                             headers=hdrs).status_code == 404
        # 创建用户 → 403
        resp = client.post("/api/users", json={
            "username": "x_new", "password": "pass123456",
            "display_name": "越权", "role": "user",
        }, headers=hdrs)
        assert resp.status_code == 403


class TestUserRoleStill403:
    """user 角色：用户/部门全部接口 403（现状保持）"""

    def test_user_users_endpoints_403(self, client, admin_headers,
                                      multi_dept_env):
        """user 访问用户管理全部接口 → 403"""
        env = multi_dept_env
        uid = _user_id(client, admin_headers, "member_user_a")
        assert client.get("/api/users", headers=env["user_a"]).status_code == 403
        resp = client.post("/api/users", json={
            "username": "x_user", "password": "pass123456",
            "display_name": "越权", "role": "user",
        }, headers=env["user_a"])
        assert resp.status_code == 403
        assert client.put(f"/api/users/{uid}", json={"display_name": "x"},
                          headers=env["user_a"]).status_code == 403
        assert client.delete(f"/api/users/{uid}",
                             headers=env["user_a"]).status_code == 403

    def test_user_departments_endpoints_403(self, client, admin_headers,
                                            multi_dept_env):
        """user 访问部门全部接口 → 403"""
        env = multi_dept_env
        assert client.get("/api/departments",
                          headers=env["user_a"]).status_code == 403
        resp = client.post("/api/departments", json={"name": "越权部"},
                           headers=env["user_a"])
        assert resp.status_code == 403
        assert client.put(f"/api/departments/{env['dept_a_id']}",
                          json={"name": "x"},
                          headers=env["user_a"]).status_code == 403
        assert client.delete(f"/api/departments/{env['dept_a_id']}",
                             headers=env["user_a"]).status_code == 403
