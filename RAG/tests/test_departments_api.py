"""部门管理 API 测试：/api/departments（契约第 3 章，全部仅 super_admin）

覆盖：创建（201 + 列表含默认部门）、重名 409、更新（改名/描述、重名 409、
不存在 404）、删除（无引用部门 200、被用户引用的部门 409、被知识库引用的
部门 409）。
"""
from __future__ import annotations


class TestDepartmentCRUD:
    """部门增删改查"""

    def test_create_and_list(self, client, admin_headers):
        """创建部门 → 201 返回 DepartmentPublic；列表含默认部门"""
        resp = client.post("/api/departments", json={
            "name": "研发部", "description": "研发团队",
        }, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        dept = resp.json()
        assert dept["name"] == "研发部"
        assert dept["description"] == "研发团队"
        assert dept["id"] and dept["created_at"]
        # 列表含种子默认部门 + 新建部门
        depts = client.get("/api/departments", headers=admin_headers).json()
        names = [d["name"] for d in depts]
        assert "默认部门" in names
        assert "研发部" in names
        default = next(d for d in depts if d["name"] == "默认部门")
        assert default["id"] == "dept_default", "默认部门 id 固定"

    def test_duplicate_name_409(self, client, admin_headers):
        """重名创建 → 409"""
        body = {"name": "重复部门", "description": ""}
        assert client.post("/api/departments", json=body,
                           headers=admin_headers).status_code == 201
        resp = client.post("/api/departments", json=body,
                           headers=admin_headers)
        assert resp.status_code == 409
        assert "部门名称已存在" in resp.json()["detail"]

    def test_update_department(self, client, admin_headers):
        """更新名称与描述 → 200 生效"""
        created = client.post("/api/departments", json={
            "name": "旧部门", "description": "旧描述",
        }, headers=admin_headers).json()
        resp = client.put(f"/api/departments/{created['id']}", json={
            "name": "新部门", "description": "新描述",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "新部门"
        assert data["description"] == "新描述"

    def test_update_duplicate_name_409(self, client, admin_headers):
        """改名与已有部门重名 → 409"""
        first = client.post("/api/departments", json={"name": "甲部"},
                            headers=admin_headers).json()
        second = client.post("/api/departments", json={"name": "乙部"},
                             headers=admin_headers).json()
        resp = client.put(f"/api/departments/{second['id']}",
                          json={"name": "甲部"}, headers=admin_headers)
        assert resp.status_code == 409
        # 同名不改（更新自身同名）→ 允许
        resp = client.put(f"/api/departments/{first['id']}",
                          json={"name": "甲部"}, headers=admin_headers)
        assert resp.status_code == 200

    def test_update_unknown_404(self, client, admin_headers):
        resp = client.put("/api/departments/nonexist",
                          json={"name": "x"}, headers=admin_headers)
        assert resp.status_code == 404


class TestDepartmentDelete:
    """删除保护"""

    def test_delete_empty_department(self, client, admin_headers):
        """无引用的部门可删除 → 200"""
        created = client.post("/api/departments", json={"name": "临时部"},
                              headers=admin_headers).json()
        resp = client.delete(f"/api/departments/{created['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "部门已删除"
        assert client.delete(f"/api/departments/{created['id']}",
                             headers=admin_headers).status_code == 404

    def test_delete_department_with_users_409(self, client, admin_headers):
        """部门下存在用户 → 409"""
        created = client.post("/api/departments", json={"name": "有人部"},
                              headers=admin_headers).json()
        client.post("/api/users", json={
            "username": "in_dept", "password": "pass123456",
            "display_name": "部门成员", "role": "user",
            "department_id": created["id"],
        }, headers=admin_headers)
        resp = client.delete(f"/api/departments/{created['id']}",
                             headers=admin_headers)
        assert resp.status_code == 409
        assert "个用户" in resp.json()["detail"], resp.text

    def test_delete_department_with_kb_409(self, client, admin_headers):
        """部门下存在知识库 → 409（需先建 kb 再尝试删部门）"""
        created = client.post("/api/departments", json={"name": "有库部"},
                              headers=admin_headers).json()
        # super_admin 建库时可指定部门
        resp = client.post("/api/kbs", json={
            "name": "部门库", "description": "",
            "department_id": created["id"],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["department_id"] == created["id"]
        # 删除部门 → 409（被知识库引用）
        resp = client.delete(f"/api/departments/{created['id']}",
                             headers=admin_headers)
        assert resp.status_code == 409
        assert "个知识库" in resp.json()["detail"], resp.text
