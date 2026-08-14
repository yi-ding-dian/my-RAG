"""超管全局文档管理（GET /api/admin/documents）测试

- 仅 super_admin：user/dept_admin → 403
- 跨部门/未分配部门文档全量可见，items 组装 kb_name/department_id/department_name
- 过滤：department_id / kb_id / status / keyword（文件名模糊），先过滤后分页
- 分页：默认 50、上限 200、页码边界、total 为过滤后数量
- 删除/重命名复用现有 /kbs/{kb_id}/documents 接口：补超管跨部门软删/重命名确认
"""
from __future__ import annotations

from conftest import (create_department_and_admin, create_kb, create_user,
                      upload_and_ingest, upload_doc)

ENV = {}


class TestAdminDocuments:

    def _env(self, client, admin_headers, ingest=False):
        """两部门 + 无部门（全局）知识库各带文档；返回上下文 dict"""
        dept_a, dept_admin_a = create_department_and_admin(
            client, admin_headers, "A部门", "dept_a", "dept123456", "A部管理员")
        dept_b, dept_admin_b = create_department_and_admin(
            client, admin_headers, "B部门", "dept_b", "dept123456", "B部管理员")
        user_a = create_user(client, admin_headers, dept_a,
                             "user_a", "user123456", "普通用户")
        kb_a = create_kb(client, name="A部知识库", department_id=dept_a)
        kb_b = create_kb(client, name="B部知识库", department_id=dept_b)
        kb_g = create_kb(client, name="全局知识库")  # 无部门
        if ingest:
            doc_a = upload_and_ingest(client, kb_a["id"], filename="A部制度.txt")
        else:
            doc_a = upload_doc(client, kb_a["id"], filename="A部制度.txt")
        doc_b = upload_doc(client, kb_b["id"], filename="B部规范.md")
        doc_g = upload_doc(client, kb_g["id"], filename="全局说明.txt")
        return {"dept_a": dept_a, "dept_admin_a": dept_admin_a,
                "dept_b": dept_b, "dept_admin_b": dept_admin_b,
                "user_a": user_a,
                "kb_a": kb_a, "kb_b": kb_b, "kb_g": kb_g,
                "doc_a": doc_a, "doc_b": doc_b, "doc_g": doc_g}

    # ---------- 权限 ----------

    def test_super_admin_200_with_kb_and_department_info(
            self, client, admin_headers):
        """超管 200：跨部门 + 未分配部门文档全量返回，kb/department 组装正确"""
        env = self._env(client, admin_headers)
        resp = client.get("/api/admin/documents", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3 and len(data["items"]) == 3
        by_name = {d["original_name"]: d for d in data["items"]}

        item = by_name["A部制度.txt"]
        assert item["kb_id"] == env["kb_a"]["id"]
        assert item["kb_name"] == "A部知识库"
        assert item["department_id"] == env["dept_a"]
        assert item["department_name"] == "A部门"

        item = by_name["B部规范.md"]
        assert item["kb_name"] == "B部知识库"
        assert item["department_name"] == "B部门"

        # 未分配部门（department_id=null）：department_name 为 null
        item = by_name["全局说明.txt"]
        assert item["kb_name"] == "全局知识库"
        assert item["department_id"] is None
        assert item["department_name"] is None

    def test_dept_admin_403(self, client, admin_headers):
        """dept_admin → 403（仅超级管理员）"""
        env = self._env(client, admin_headers)
        resp = client.get("/api/admin/documents", headers=env["dept_admin_a"])
        assert resp.status_code == 403
        assert "超级管理员" in resp.json()["detail"]

    def test_user_403(self, client, admin_headers):
        """user → 403"""
        env = self._env(client, admin_headers)
        resp = client.get("/api/admin/documents", headers=env["user_a"])
        assert resp.status_code == 403

    def test_unauthorized_401(self, client):
        """未登录 → 401"""
        resp = client.get("/api/admin/documents")
        assert resp.status_code == 401

    # ---------- 过滤 ----------

    def test_filter_by_department_id(self, client, admin_headers):
        """department_id 过滤：仅返回该部门文档"""
        env = self._env(client, admin_headers)
        resp = client.get(f"/api/admin/documents?department_id={env['dept_a']}",
                          headers=admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["original_name"] == "A部制度.txt"
        # B 部门过滤
        resp = client.get(f"/api/admin/documents?department_id={env['dept_b']}",
                          headers=admin_headers)
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["original_name"] == "B部规范.md"

    def test_filter_by_kb_id(self, client, admin_headers):
        """kb_id 过滤：仅返回该知识库文档"""
        env = self._env(client, admin_headers)
        resp = client.get(f"/api/admin/documents?kb_id={env['kb_g']['id']}",
                          headers=admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["original_name"] == "全局说明.txt"

    def test_filter_by_status(self, client, admin_headers, mock_embedding):
        """status 过滤：uploaded / ingested / unparsed（映射 uploaded+parsed）"""
        env = self._env(client, admin_headers, ingest=True)
        # A部制度.txt 已入库，其余两个 uploaded
        resp = client.get("/api/admin/documents?status=ingested",
                          headers=admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["original_name"] == "A部制度.txt"
        assert data["items"][0]["status"] == "ingested"

        resp = client.get("/api/admin/documents?status=uploaded",
                          headers=admin_headers)
        assert resp.json()["total"] == 2

        # unparsed = uploaded + parsed（此处 2 个 uploaded）
        resp = client.get("/api/admin/documents?status=unparsed",
                          headers=admin_headers)
        assert resp.json()["total"] == 2

    def test_filter_by_keyword(self, client, admin_headers):
        """keyword 文件名模糊过滤（大小写不敏感）"""
        env = self._env(client, admin_headers)
        resp = client.get("/api/admin/documents?keyword=部",
                          headers=admin_headers)
        assert resp.json()["total"] == 2  # A部制度.txt / B部规范.md
        resp = client.get("/api/admin/documents?keyword=规范",
                          headers=admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["original_name"] == "B部规范.md"
        # 大小写不敏感
        resp = client.get("/api/admin/documents?keyword=GLOBAL",
                          headers=admin_headers)
        assert resp.json()["total"] == 0
        resp = client.get("/api/admin/documents?keyword=全局",
                          headers=admin_headers)
        assert resp.json()["total"] == 1

    def test_filter_combination(self, client, admin_headers):
        """多过滤条件组合（department_id + keyword）"""
        env = self._env(client, admin_headers)
        resp = client.get(
            f"/api/admin/documents?department_id={env['dept_a']}&keyword=规范",
            headers=admin_headers)
        assert resp.json()["total"] == 0
        resp = client.get(
            f"/api/admin/documents?department_id={env['dept_a']}&keyword=制度",
            headers=admin_headers)
        assert resp.json()["total"] == 1

    def test_invalid_status_400(self, client, admin_headers):
        """非法 status → 400"""
        resp = client.get("/api/admin/documents?status=unknown",
                          headers=admin_headers)
        assert resp.status_code == 400

    # ---------- 分页 ----------

    def test_pagination_default_and_bounds(self, client, admin_headers):
        """缺省 page_size=50；页码边界；total 为过滤后数量"""
        env = self._env(client, admin_headers)
        # 缺省：3 条全返回（page_size 默认 50）
        resp = client.get("/api/admin/documents", headers=admin_headers)
        data = resp.json()
        assert data["page"] == 1 and data["page_size"] == 50
        assert data["total"] == 3 and len(data["items"]) == 3

        # 分页：page_size=2 → 2+1
        r1 = client.get("/api/admin/documents?page=1&page_size=2",
                        headers=admin_headers).json()
        assert r1["total"] == 3 and len(r1["items"]) == 2
        r2 = client.get("/api/admin/documents?page=2&page_size=2",
                        headers=admin_headers).json()
        assert len(r2["items"]) == 1
        # 越界页 → 空 items，total 仍正确
        r99 = client.get("/api/admin/documents?page=99&page_size=2",
                         headers=admin_headers).json()
        assert r99["total"] == 3 and r99["items"] == []
        # page<1 → 按第 1 页
        r0 = client.get("/api/admin/documents?page=0&page_size=2",
                        headers=admin_headers).json()
        assert r0["page"] == 1 and len(r0["items"]) == 2
        # 无重叠无遗漏
        ids1 = {d["id"] for d in r1["items"]}
        ids2 = {d["id"] for d in r2["items"]}
        assert not ids1 & ids2 and len(ids1 | ids2) == 3

    def test_page_size_cap_200(self, client, admin_headers):
        """page_size 上限 200"""
        env = self._env(client, admin_headers)
        resp = client.get("/api/admin/documents?page=1&page_size=1000",
                          headers=admin_headers)
        assert resp.json()["page_size"] == 200

    def test_pagination_after_filter(self, client, admin_headers):
        """先过滤后分页：total 为过滤后数量"""
        env = self._env(client, admin_headers)
        resp = client.get(
            f"/api/admin/documents?department_id={env['dept_a']}"
            f"&page=2&page_size=1", headers=admin_headers)
        data = resp.json()
        assert data["total"] == 1 and data["items"] == []

    def test_empty_global(self, client, admin_headers):
        """系统无文档：total=0、items 空，不报错"""
        create_kb(client, name="空库")
        resp = client.get("/api/admin/documents", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0 and resp.json()["items"] == []

    # ---------- 跨部门删除 / 重命名（复用现有接口） ----------

    def test_super_admin_cross_department_soft_delete(
            self, client, admin_headers):
        """超管软删他部门文档：200 移入回收站，全局列表不再出现"""
        env = self._env(client, admin_headers)
        resp = client.delete(
            f"/api/kbs/{env['kb_b']['id']}/documents/{env['doc_b']['id']}",
            headers=admin_headers)
        assert resp.status_code == 200
        assert "回收站" in resp.json()["message"]
        # 全局列表排除回收站
        data = client.get("/api/admin/documents",
                          headers=admin_headers).json()
        names = [d["original_name"] for d in data["items"]]
        assert "B部规范.md" not in names and data["total"] == 2
        # 回收站内可查（部门内回收站接口）
        trash = client.get(
            f"/api/kbs/{env['kb_b']['id']}/documents/trash",
            headers=admin_headers).json()
        assert any(d["id"] == env["doc_b"]["id"] for d in trash)

    def test_super_admin_cross_department_rename(
            self, client, admin_headers):
        """超管重命名他部门文档：200 且全局列表同步新名"""
        env = self._env(client, admin_headers)
        resp = client.post(
            f"/api/kbs/{env['kb_b']['id']}/documents/{env['doc_b']['id']}/rename",
            json={"name": "B部规范v2.md"}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["original_name"] == "B部规范v2.md"
        data = client.get("/api/admin/documents",
                          headers=admin_headers).json()
        names = [d["original_name"] for d in data["items"]]
        assert "B部规范v2.md" in names and "B部规范.md" not in names
