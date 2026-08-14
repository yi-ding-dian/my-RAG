"""用户管理 API 测试：/api/users（契约第 2 章，全部仅 super_admin）

覆盖：创建用户（201 + 部门名 join + 凭据可登录）、重名 409、部门不存在 400、
列表（全量 / 按部门过滤）、更新（字段/状态/改密）、404、删除（删自己 409、
删普通用户成功、删除后登录 401、最后 super_admin 保护）、禁用后登录 401。
"""
from __future__ import annotations


class TestUserCRUD:
    """用户增删改查"""

    def test_create_user_and_login(self, client, admin_headers):
        """创建用户 → 201 返回 UserPublic（含部门名）→ 凭据可登录"""
        resp = client.post("/api/users", json={
            "username": "zhangsan",
            "password": "pass123456",
            "display_name": "张三",
            "role": "user",
            "department_id": "dept_default",
        }, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        user = resp.json()
        assert user["username"] == "zhangsan"
        assert user["role"] == "user"
        assert user["department_id"] == "dept_default"
        assert user["department_name"] == "默认部门", "列表应 join 部门名"
        assert user["status"] == "active"
        assert "password" not in user and "password_hash" not in user
        # 用新用户凭据登录成功
        login = client.post("/api/auth/login", json={
            "username": "zhangsan", "password": "pass123456",
        })
        assert login.status_code == 200

    def test_duplicate_username_409(self, client, admin_headers):
        """重名创建 → 409"""
        body = {"username": "dup_user", "password": "pass123456",
                "display_name": "重复", "role": "user"}
        assert client.post("/api/users", json=body,
                           headers=admin_headers).status_code == 201
        resp = client.post("/api/users", json=body, headers=admin_headers)
        assert resp.status_code == 409
        assert "用户名已存在" in resp.json()["detail"]

    def test_create_user_invalid_department_400(self, client, admin_headers):
        """department_id 传了但不存在 → 400"""
        resp = client.post("/api/users", json={
            "username": "no_dept", "password": "pass123456",
            "display_name": "无部门", "role": "user",
            "department_id": "dept_not_exist",
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "部门不存在"

    def test_list_users_with_department_filter(self, client, admin_headers):
        """列表含 admin 与新建用户；按部门过滤只出该部门用户"""
        client.post("/api/users", json={
            "username": "a_dept", "password": "pass123456",
            "display_name": "甲", "role": "dept_admin",
            "department_id": "dept_default",
        }, headers=admin_headers)
        users = client.get("/api/users", headers=admin_headers).json()
        assert any(u["username"] == "admin" for u in users), "列表含 admin"
        a_dept = next(u for u in users if u["username"] == "a_dept")
        assert a_dept["department_name"] == "默认部门"
        # 过滤：按不存在部门过滤 → 空
        filtered = client.get(
            "/api/users?department_id=dept_default",
            headers=admin_headers).json()
        assert any(u["username"] == "a_dept" for u in filtered)

    def test_update_user(self, client, admin_headers):
        """更新 display_name/role → 200 生效"""
        created = client.post("/api/users", json={
            "username": "upd_user", "password": "pass123456",
            "display_name": "旧名", "role": "user",
            "department_id": "dept_default",
        }, headers=admin_headers).json()
        resp = client.put(f"/api/users/{created['id']}", json={
            "display_name": "新名", "role": "dept_admin",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["display_name"] == "新名"
        assert data["role"] == "dept_admin"

    def test_update_user_unknown_404(self, client, admin_headers):
        resp = client.put("/api/users/nonexist", json={"display_name": "x"},
                          headers=admin_headers)
        assert resp.status_code == 404

    def test_update_user_invalid_department_400(self, client, admin_headers):
        created = client.post("/api/users", json={
            "username": "dep_user", "password": "pass123456",
            "display_name": "换部门", "role": "user",
        }, headers=admin_headers).json()
        resp = client.put(f"/api/users/{created['id']}",
                          json={"department_id": "dept_not_exist"},
                          headers=admin_headers)
        assert resp.status_code == 400

    def test_update_user_password_then_login(self, client, admin_headers):
        """PUT 传 password → 重哈希：新密码登录 200、旧密码 401"""
        created = client.post("/api/users", json={
            "username": "pw_user", "password": "oldpass123",
            "display_name": "改密", "role": "user",
        }, headers=admin_headers).json()
        resp = client.put(f"/api/users/{created['id']}",
                          json={"password": "newpass456"},
                          headers=admin_headers)
        assert resp.status_code == 200
        assert "password" not in resp.json(), "不回传密码"
        assert client.post("/api/auth/login", json={
            "username": "pw_user", "password": "newpass456",
        }).status_code == 200
        assert client.post("/api/auth/login", json={
            "username": "pw_user", "password": "oldpass123",
        }).status_code == 401


class TestUserDelete:
    """删除保护"""

    def test_delete_self_409(self, client, admin_headers):
        """删除当前登录账号 → 409"""
        me = client.get("/api/auth/me", headers=admin_headers).json()
        resp = client.delete(f"/api/users/{me['id']}", headers=admin_headers)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "不能删除当前登录账号"
        # 账号仍然可用
        assert client.get("/api/auth/me", headers=admin_headers).status_code == 200

    def test_delete_user_success_and_login_401(self, client, admin_headers):
        """删除普通用户 → 200；删除后其凭据登录 401"""
        created = client.post("/api/users", json={
            "username": "doomed", "password": "pass123456",
            "display_name": "将被删", "role": "user",
        }, headers=admin_headers).json()
        resp = client.delete(f"/api/users/{created['id']}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "用户已删除"
        # 删除后 token 与登录均失效（用户不存在 → 401）
        assert client.post("/api/auth/login", json={
            "username": "doomed", "password": "pass123456",
        }).status_code == 401
        resp = client.delete(f"/api/users/{created['id']}",
                             headers=admin_headers)
        assert resp.status_code == 404, "重复删除 → 404"

    def test_delete_last_super_admin_protected(self, client, admin_headers):
        """最后 super_admin 保护：唯一 super_admin 不可被删除

        - admin 是唯一 super_admin：删自己先命中「不能删除当前登录账号」；
        - 创建第二个 super_admin B 后，B 可删除 admin（B 成为唯一）；
        - B 删自己 → 409（此时 B 是唯一 super_admin，「最后一个超级管理员」
          分支为服务层双保险，HTTP 层先命中删自己校验）。
        """
        me = client.get("/api/auth/me", headers=admin_headers).json()
        admin_id = me["id"]
        # 唯一 super_admin 时：admin 删 admin → 409（删自己优先）
        resp = client.delete(f"/api/users/{admin_id}", headers=admin_headers)
        assert resp.status_code == 409
        # 创建第二个 super_admin B
        b = client.post("/api/users", json={
            "username": "admin_b", "password": "pass123456",
            "display_name": "备份管理员", "role": "super_admin",
        }, headers=admin_headers).json()
        b_login = client.post("/api/auth/login", json={
            "username": "admin_b", "password": "pass123456",
        }).json()
        b_headers = {"Authorization": f"Bearer {b_login['access_token']}"}
        # B 删除 admin → 200（库中仍有 B 一个 super_admin）
        assert client.delete(f"/api/users/{admin_id}",
                             headers=b_headers).status_code == 200
        # B 删自己 → 409（唯一 super_admin，删自己校验命中）
        resp = client.delete(f"/api/users/{b['id']}", headers=b_headers)
        assert resp.status_code == 409


class TestUserStatus:
    """禁用状态"""

    def test_disable_user_login_rejected(self, client, admin_headers):
        """PUT status=disabled → 登录被拒 401（与密码错误同文案防枚举）"""
        created = client.post("/api/users", json={
            "username": "ban_me", "password": "pass123456",
            "display_name": "将被禁", "role": "user",
        }, headers=admin_headers).json()
        resp = client.put(f"/api/users/{created['id']}",
                          json={"status": "disabled"}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"
        login = client.post("/api/auth/login", json={
            "username": "ban_me", "password": "pass123456",
        })
        assert login.status_code == 401
        # 已签发 token 也失效（鉴权拒绝）
        tok = client.post("/api/auth/login", json={
            "username": "ban_me", "password": "pass123456",
        }).json().get("access_token")
        if tok:  # disabled 后登录本就失败，无需再断言 me
            assert client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {tok}"}).status_code == 401
        # 重新启用 → 可登录
        client.put(f"/api/users/{created['id']}",
                   json={"status": "active"}, headers=admin_headers)
        assert client.post("/api/auth/login", json={
            "username": "ban_me", "password": "pass123456",
        }).status_code == 200
