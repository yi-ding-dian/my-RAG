"""权限矩阵集成测试（方案"权限矩阵"章节逐项验证）

覆盖：
- user 建 kb 403 / dept_admin 建 kb 强制本部门（body 指定其他部门也被覆盖）
- 列表按部门隔离（user/dept_admin 只见本部门，super_admin 全量）
- 跨部门 kb 详情 404 伪装（防探测）
- user 上传 403
- 管理端点矩阵：settings 读对 super_admin/dept_admin 放开（user 403）、写仅超管；
  /api/users、/api/departments 对 super_admin/dept_admin 开放（user 403）
- 会话隔离：他人会话列表不可见/详情 404/续聊 404/删除 404，super_admin 全量
- 图片代理权限：未登录 401 / 跨部门 404 / 同部门 200
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import (create_department_and_admin, create_user,
                      extract_session_id, upload_doc)


@pytest.fixture()
def multi_dept_env(client, admin_headers):
    """两部门环境：A 部（dept_admin + user）、B 部（user）

    返回 dict: dept_a_id / dept_b_id / dept_admin_a / user_a / user_b。
    """
    dept_a_id, dept_admin_a = create_department_and_admin(
        client, admin_headers, "权限测试A部", "perm_admin_a",
        "pass123456", "A部管理员")
    dept_b_id, _ = create_department_and_admin(
        client, admin_headers, "权限测试B部", "perm_admin_b",
        "pass123456", "B部管理员")
    user_a = create_user(client, admin_headers, dept_a_id, "perm_user_a")
    user_b = create_user(client, admin_headers, dept_b_id, "perm_user_b")
    return {
        "dept_a_id": dept_a_id,
        "dept_b_id": dept_b_id,
        "dept_admin_a": dept_admin_a,
        "user_a": user_a,
        "user_b": user_b,
    }


class TestKBLevelPermissions:
    """知识库级权限"""

    def test_user_create_kb_403(self, client, admin_headers, multi_dept_env):
        """普通用户建知识库 → 403"""
        resp = client.post("/api/kbs", json={"name": "越权库"},
                           headers=multi_dept_env["user_a"])
        assert resp.status_code == 403
        assert "仅超级管理员或部门管理员" in resp.json()["detail"]

    def test_dept_admin_create_kb_forced_department(
            self, client, admin_headers, multi_dept_env):
        """dept_admin 建库强制本部门：body 指定其他部门也被覆盖"""
        env = multi_dept_env
        # body 指定 B 部 → 响应仍为 A 部（强制覆盖，防越权）
        resp = client.post("/api/kbs", json={
            "name": "A部库-越权指定", "department_id": env["dept_b_id"],
        }, headers=env["dept_admin_a"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["department_id"] == env["dept_a_id"]
        # 不传 department_id → 缺省也是本部门
        resp = client.post("/api/kbs", json={"name": "A部库-缺省"},
                           headers=env["dept_admin_a"])
        assert resp.json()["department_id"] == env["dept_a_id"]
        assert resp.json()["owner_id"], "创建人记录 owner_id"

    def test_super_admin_create_kb_specified_department(
            self, client, admin_headers, multi_dept_env):
        """super_admin 建库可指定任意部门"""
        resp = client.post("/api/kbs", json={
            "name": "B部库", "department_id": multi_dept_env["dept_b_id"],
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["department_id"] == multi_dept_env["dept_b_id"]

    def test_kb_list_department_isolation(
            self, client, admin_headers, multi_dept_env):
        """列表隔离：user/dept_admin 只见本部门，super_admin 全量"""
        env = multi_dept_env
        kb_a = client.post("/api/kbs", json={"name": "A部库"},
                           headers=env["dept_admin_a"]).json()
        kb_b = client.post("/api/kbs", json={
            "name": "B部库", "department_id": env["dept_b_id"],
        }, headers=admin_headers).json()
        # user_a / dept_admin_a 只见 A 部
        for hdrs in (env["user_a"], env["dept_admin_a"]):
            ids = [k["id"] for k in client.get("/api/kbs", headers=hdrs).json()]
            assert kb_a["id"] in ids and kb_b["id"] not in ids, \
                f"部门隔离失效: {ids}"
        # user_b 只见 B 部
        ids_b = [k["id"] for k in client.get(
            "/api/kbs", headers=env["user_b"]).json()]
        assert kb_b["id"] in ids_b and kb_a["id"] not in ids_b
        # super_admin 全量
        ids_admin = [k["id"] for k in client.get(
            "/api/kbs", headers=admin_headers).json()]
        assert kb_a["id"] in ids_admin and kb_b["id"] in ids_admin

    def test_cross_department_kb_detail_404(
            self, client, admin_headers, multi_dept_env):
        """跨部门 kb 详情 → 404 伪装（不暴露存在性）"""
        env = multi_dept_env
        kb_a = client.post("/api/kbs", json={"name": "A部库"},
                           headers=env["dept_admin_a"]).json()
        # 无权限 → 404（与不存在同文案）
        for hdrs in (env["user_b"],):
            resp = client.get(f"/api/kbs/{kb_a['id']}", headers=hdrs)
            assert resp.status_code == 404
            assert resp.json()["detail"] == "知识库不存在"
        # 同部门可访问
        assert client.get(f"/api/kbs/{kb_a['id']}",
                          headers=env["user_a"]).status_code == 200
        assert client.get(f"/api/kbs/{kb_a['id']}",
                          headers=env["dept_admin_a"]).status_code == 200

    def test_user_upload_403(self, client, admin_headers, multi_dept_env):
        """user 对本部门 kb 上传文档 → 403（仅 dept_admin/super_admin）"""
        env = multi_dept_env
        kb_a = client.post("/api/kbs", json={"name": "A部库"},
                           headers=env["dept_admin_a"]).json()
        resp = client.post(
            f"/api/kbs/{kb_a['id']}/documents/upload",
            files={"file": ("a.txt", b"hi", "text/plain")},
            headers=env["user_a"],
        )
        assert resp.status_code == 403
        # dept_admin 本部门可上传
        assert upload_doc(client, kb_a["id"],
                          headers=env["dept_admin_a"])["status"] == "uploaded"


class TestAdminOnlyEndpoints:
    """仅 super_admin 的管理端点（settings 读已对 dept_admin 放开，写仍仅超管）"""

    def test_settings_read_write_matrix(self, client, admin_headers,
                                        multi_dept_env):
        """权限矩阵：user 读 403；dept_admin 读 200 / 写 403；super_admin 全通"""
        dept_h = multi_dept_env["dept_admin_a"]
        # user 读系统配置 → 403
        resp = client.get("/api/settings/profiles",
                          headers=multi_dept_env["user_a"])
        assert resp.status_code == 403
        # dept_admin 读 → 200（只读放开，写测试见 TestSettingsReadOnlyForDeptAdmin）
        assert client.get("/api/settings/profiles",
                          headers=dept_h).status_code == 200
        # dept_admin 写（新建档案）→ 403
        resp = client.post("/api/settings/profiles",
                           json={"name": "越权档案"}, headers=dept_h)
        assert resp.status_code == 403
        assert "仅超级管理员" in resp.json()["detail"]

    def test_user_users_403(self, client, admin_headers, multi_dept_env):
        """user 访问用户管理 → 403（dept_admin 已放开，见 test_dept_admin_members）"""
        resp = client.get("/api/users", headers=multi_dept_env["user_a"])
        assert resp.status_code == 403
        # dept_admin 可访问（仅本部门成员）
        assert client.get("/api/users",
                          headers=multi_dept_env["dept_admin_a"]).status_code == 200

    def test_user_departments_403(self, client, admin_headers,
                                  multi_dept_env):
        """user 访问部门管理 → 403（dept_admin 已放开，见 test_dept_admin_members）"""
        resp = client.get("/api/departments", headers=multi_dept_env["user_a"])
        assert resp.status_code == 403
        # dept_admin 可访问（仅本部门）
        assert client.get("/api/departments",
                          headers=multi_dept_env["dept_admin_a"]).status_code == 200

    def test_super_admin_access_all_ok(self, client, admin_headers):
        """super_admin 可访问 settings/users/departments"""
        assert client.get("/api/settings/profiles",
                          headers=admin_headers).status_code == 200
        assert client.get("/api/users", headers=admin_headers).status_code == 200
        assert client.get("/api/departments",
                          headers=admin_headers).status_code == 200


class TestSessionIsolation:
    """会话隔离（ChatSession.user_id）"""

    def test_session_ownership_isolation(
            self, client, admin_headers, multi_dept_env, mock_embedding,
            mock_llm):
        """user A 会话：user B 列表不可见/详情 404/续聊 404/删除 404；
        super_admin 全量可见"""
        env = multi_dept_env
        # A 部建库，user_a 对话产生会话
        kb_a = client.post("/api/kbs", json={"name": "A部库"},
                           headers=env["dept_admin_a"]).json()
        # B 部建库（供 user_b 续聊时通过 kb 权限校验，命中会话归属校验）
        kb_b = client.post("/api/kbs", json={
            "name": "B部库", "department_id": env["dept_b_id"],
        }, headers=admin_headers).json()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb_a["id"], "query": "用户A的私密问题",
        }, headers=env["user_a"])
        assert resp.status_code == 200
        session_id = extract_session_id(resp.text)
        assert session_id

        # 1) user_b 列表不可见
        history_b = client.get("/api/chat/history",
                               headers=env["user_b"]).json()
        assert all(h["id"] != session_id for h in history_b), \
            "他人会话不应出现在列表中"
        # 2) user_b 详情 → 404 伪装
        resp = client.get(f"/api/chat/history/{session_id}",
                          headers=env["user_b"])
        assert resp.status_code == 404
        # 3) user_b 续聊（传他人 session_id）→ 404（归属校验）
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb_b["id"], "query": "试图续聊他人会话",
            "session_id": session_id,
        }, headers=env["user_b"])
        assert resp.status_code == 404, "跨用户会话归属校验必须 404"
        # 4) user_b 删除他人会话 → 404
        assert client.delete(f"/api/chat/history/{session_id}",
                             headers=env["user_b"]).status_code == 404
        # 5) 会话未被误删（user_a 仍可见）
        assert client.get(f"/api/chat/history/{session_id}",
                          headers=env["user_a"]).status_code == 200
        # 6) super_admin 全量可见 + 可删除
        admin_history = client.get("/api/chat/history",
                                   headers=admin_headers).json()
        assert any(h["id"] == session_id for h in admin_history), \
            "super_admin 应看到全部会话"
        assert client.get(f"/api/chat/history/{session_id}",
                          headers=admin_headers).status_code == 200
        assert client.delete(f"/api/chat/history/{session_id}",
                             headers=admin_headers).status_code == 200


class TestImageProxyPermissions:
    """图片代理 /api/files/images/{doc_id}/{name} 权限"""

    def _prepare_image(self, client, env, admin_headers):
        """A 部建库 + 上传文档 + 直写存储对象，返回 (kb_id, doc_id, key)"""
        kb_a = client.post("/api/kbs", json={"name": "图片库"},
                           headers=env["dept_admin_a"]).json()
        doc = upload_doc(client, kb_a["id"], filename="有图文档.md",
                         content="# 有图文档\n\n![图](pic.png)\n",
                         headers=env["dept_admin_a"])
        # 模拟入库后图片已上传存储（local 后端直写对象）
        from backend.services.storage_service import get_storage_service
        storage = get_storage_service()
        asyncio.run(storage.upload_bytes(
            f"images/{doc['id']}/pic.png", b"PNGDATA-123"))
        return kb_a["id"], doc["id"]

    def test_image_proxy_permissions(self, client, admin_headers,
                                     multi_dept_env):
        """未登录 401 / 跨部门 404 / 同部门 200（内容一致）"""
        env = multi_dept_env
        kb_id, doc_id = self._prepare_image(client, env, admin_headers)
        url = f"/api/files/images/{doc_id}/pic.png"

        # 未登录 → 401
        assert client.get(url).status_code == 401
        # 跨部门（B 部）→ 404 伪装
        resp = client.get(url, headers=env["user_b"])
        assert resp.status_code == 404
        assert resp.json()["detail"] == "图片不存在"
        # 同部门（A 部 user / dept_admin）→ 200，内容一致
        for hdrs in (env["user_a"], env["dept_admin_a"], admin_headers):
            resp = client.get(url, headers=hdrs)
            assert resp.status_code == 200, resp.text
            assert resp.content == b"PNGDATA-123", \
                f"图片内容不一致（{resp.headers.get('content-type')}）"
        # 对象不存在（同部门）→ 404
        resp = client.get(f"/api/files/images/{doc_id}/missing.png",
                          headers=env["user_a"])
        assert resp.status_code == 404

    def test_image_proxy_unknown_doc_404(self, client, admin_headers):
        """文档不存在 → 404（即使对象存在）"""
        resp = client.get("/api/files/images/nonexist-doc/pic.png",
                          headers=admin_headers)
        assert resp.status_code == 404
