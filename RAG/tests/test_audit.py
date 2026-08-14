"""审计操作日志 API 测试：/api/audit + 全路由埋点

覆盖：
- 登录成功/失败/修改密码记录（登录失败 detail 记尝试用户名，统一 401 文案不变）
- 用户/部门/知识库 CRUD + 标签更新 + 重建向量启动 记录
- 文档 上传/重命名/URL导入/解析/软删/恢复/彻底删/清空回收站 记录
- 会话删除/导出记录
- 配置档案 CRUD/激活/连接测试（成功与否）记录
- 权限：非 super_admin 查审计 403
- 查询：分页/action/username/target_type/时间范围过滤/倒序
- 审计失败不阻塞业务（monkeypatch 审计内部抛错，主操作仍成功）

全部离线：embedding/LLM 用 conftest mock；URL 抓取用假 httpx；
连接测试 monkeypatch SettingsService 各项 _test_* 秒回。
"""
from __future__ import annotations

import json

import pytest

from conftest import create_kb, upload_and_ingest, upload_doc


# ==================== 工具函数 ====================

def _logs(client, headers, **params):
    """GET /api/audit/logs，返回完整响应体"""
    resp = client.get("/api/audit/logs", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _find_log(client, headers, action, username=None, status=None, **params):
    """在结果（可选过滤条件下）中找指定 action 的第一条记录"""
    data = _logs(client, headers, action=action, **params)
    for item in data["items"]:
        if username is not None and item["username"] != username:
            continue
        if status is not None and item["status"] != status:
            continue
        return item
    return None


def _actions(client, headers):
    resp = client.get("/api/audit/actions", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["actions"]


# ---------------- 假 httpx（URL 导入用，与 test_document_rename 同构） ----------------

class _FakeResp:
    def __init__(self, status_code: int, body: bytes = b""):
        self.status_code = status_code
        self.headers = {}          # P0-2: SSRF 逐跳校验读取
        self.is_redirect = False   # P0-2: 手动跟随重定向标记
        self._chunks = [body] if body else []

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url):
        return _FakeStream(self._handler(url))


def _monkeypatch_httpx(monkeypatch, handler):
    from backend.services import web_importer
    monkeypatch.setattr(
        web_importer.httpx, "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(handler))
    # P0-2: SSRF 校验的 DNS 解析一并 mock（离线稳定）
    monkeypatch.setattr(
        web_importer.socket, "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", port))])


def _import_url(client, kb_id, url, headers):
    return client.post(f"/api/kbs/{kb_id}/documents/from-url",
                       json={"url": url}, headers=headers)


def _write_session_file(session_id, title="审计测试会话", messages=None):
    """直接落盘会话 JSON（绕过 API，构造会话归属场景）"""
    from backend.config import CHAT_DIR
    data = {
        "id": session_id,
        "kb_id": "kb_audit",
        "user_id": None,
        "title": title,
        "messages": messages or [],
        "created_at": "2026-08-10 10:00:00",
        "updated_at": "2026-08-10 10:00:00",
    }
    CHAT_DIR.joinpath(f"{session_id}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _mock_conn_tests(monkeypatch, ok_map: dict):
    """monkeypatch SettingsService 各项连接测试秒回（ok_map: {section: ok}）"""
    from backend.services import settings_service as ss

    def _ok(section):
        return {"ok": ok_map.get(section, True), "latency_ms": 1,
                "message": "mock"}

    def _sync(self, section, *a, **kw):
        return _ok(section)

    def _async(self, section, *a, **kw):
        return _ok(section)

    def _test_llm(self, llm): return _ok("llm")
    def _test_embedding(self, emb): return _ok("embedding")
    def _test_mineru(self, mineru): return _ok("mineru")
    async def _test_deepdoc(self, deepdoc): return _ok("deepdoc")
    async def _test_mysql(self, mysql): return _ok("mysql")
    async def _test_minio(self, minio): return _ok("minio")
    monkeypatch.setattr(ss.SettingsService, "_test_llm", _test_llm)
    monkeypatch.setattr(ss.SettingsService, "_test_embedding", _test_embedding)
    monkeypatch.setattr(ss.SettingsService, "_test_mineru", _test_mineru)
    monkeypatch.setattr(ss.SettingsService, "_test_deepdoc", _test_deepdoc)
    monkeypatch.setattr(ss.SettingsService, "_test_mysql", _test_mysql)
    monkeypatch.setattr(ss.SettingsService, "_test_minio", _test_minio)


# ==================== 认证埋点 ====================

class TestLoginAudit:
    """登录成功/失败/改密记录"""

    def test_login_success_recorded(self, client, admin_headers):
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "auth.login",
                         username="admin", status="success")
        assert item is not None
        assert item["target_type"] == "user"
        assert item["target_name"] == "admin"
        assert item["role"] == "super_admin"
        assert item["ip"]  # testclient 环境 IP 非空

    def test_login_failed_recorded(self, client, admin_headers):
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "wrong-pass"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"
        item = _find_log(client, admin_headers, "auth.login", status="failed")
        assert item is not None
        # 失败时无已认证用户：username 留空，操作对象记在 target_name
        assert item["username"] == ""
        assert item["target_name"] == "admin"
        assert "admin" in (item["detail"] or "")

    def test_login_unknown_user_recorded(self, client, admin_headers):
        resp = client.post("/api/auth/login", json={
            "username": "nobody", "password": "whatever"})
        assert resp.status_code == 401
        item = _find_log(client, admin_headers, "auth.login", status="failed")
        assert item is not None
        assert item["target_name"] == "nobody"

    def test_change_password_recorded(self, client, admin_headers):
        resp = client.post("/api/auth/change-password", json={
            "old_password": "admin123", "new_password": "newpass888"},
            headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "auth.change-password",
                         username="admin")
        assert item is not None
        assert item["target_type"] == "user"
        assert item["target_name"] == "admin"
        # detail 不落密码
        assert "newpass888" not in (item["detail"] or "")


# ==================== 用户/部门/知识库埋点 ====================

class TestUserDeptKbAudit:
    """用户/部门/知识库 CRUD 与标签、重建向量记录"""

    def test_user_create_update_delete_recorded(self, client, admin_headers):
        resp = client.post("/api/users", json={
            "username": "audit_user", "password": "user123456",
            "display_name": "审计用户", "role": "user"}, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        uid = resp.json()["id"]
        item = _find_log(client, admin_headers, "user.create",
                         username="admin")
        assert item is not None
        assert item["target_type"] == "user"
        assert item["target_name"] == "audit_user"
        assert "audit_user" not in (item["detail"] or "")  # detail 无密码
        assert "user" in (item["detail"] or "")  # role 摘要

        resp = client.put(f"/api/users/{uid}", json={"display_name": "改名"},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        item = _find_log(client, admin_headers, "user.update",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "audit_user"

        resp = client.delete(f"/api/users/{uid}", headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "user.delete",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "audit_user"  # 删除后仍记目标用户名

    def test_department_crud_recorded(self, client, admin_headers):
        resp = client.post("/api/departments", json={
            "name": "审计部门", "description": "审计测试"}, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        dept_id = resp.json()["id"]
        item = _find_log(client, admin_headers, "dept.create",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "审计部门"

        resp = client.put(f"/api/departments/{dept_id}", json={"name": "审计部门2"},
                          headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "dept.update",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "审计部门2"

        resp = client.delete(f"/api/departments/{dept_id}",
                             headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "dept.delete",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "审计部门2"

    def test_kb_crud_tags_rebuild_recorded(self, client, admin_headers,
                                           mock_embedding):
        kb = create_kb(client, name="审计知识库")
        item = _find_log(client, admin_headers, "kb.create",
                         username="admin")
        assert item is not None
        assert item["target_type"] == "kb"
        assert item["target_name"] == "审计知识库"

        resp = client.put(f"/api/kbs/{kb['id']}", json={"name": "审计知识库2"},
                          headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "kb.update",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "审计知识库2"

        resp = client.put(f"/api/kbs/{kb['id']}/tags",
                          json={"tags": ["制度", "审计"]}, headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "kb.tags-update",
                         username="admin")
        assert item is not None
        assert "审计" in (item["detail"] or "")

        resp = client.post(f"/api/kbs/{kb['id']}/rebuild-vectors",
                           headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "kb.rebuild-vectors",
                         username="admin")
        assert item is not None
        assert item["target_type"] == "kb"

        resp = client.delete(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "kb.delete",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "审计知识库2"


# ==================== 文档埋点 ====================

class TestDocumentAudit:
    """文档全生命周期记录"""

    def test_upload_and_ingest_recorded(self, client, admin_headers,
                                        mock_embedding):
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="审计文档.txt")
        item = _find_log(client, admin_headers, "doc.upload",
                         username="admin")
        assert item is not None
        assert item["target_type"] == "doc"
        assert item["target_name"] == "审计文档.txt"
        assert "size" in (item["detail"] or "")

        upload_and_ingest(client, kb["id"], filename="审计文档2.txt",
                          ingest_body={"method": "naive"})
        item = _find_log(client, admin_headers, "doc.ingest",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "审计文档2.txt"
        assert "naive" in (item["detail"] or "")

    def test_rename_from_url_recorded(self, client, admin_headers, monkeypatch):
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="旧名字.txt")
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/rename",
                           json={"name": "新名字.txt"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        item = _find_log(client, admin_headers, "doc.rename",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "新名字.txt"
        assert "旧名字" in (item["detail"] or "")

        _monkeypatch_httpx(
            monkeypatch, lambda url: _FakeResp(200, (
                "<html><head><title>网页标题</title></head>"
                "<body><h1>大标题</h1><p>正文内容</p></body></html>"
            ).encode("utf-8")))
        resp = _import_url(client, kb["id"], "https://example.com/page",
                           admin_headers)
        assert resp.status_code == 200, resp.text
        item = _find_log(client, admin_headers, "doc.from-url",
                         username="admin")
        assert item is not None
        assert "https://example.com/page" in (item["detail"] or "")

    def test_soft_delete_restore_purge_trash_empty_recorded(
            self, client, admin_headers, mock_embedding):
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"], filename="回收文档.txt")
        doc_id = doc["id"]

        resp = client.delete(f"/api/kbs/{kb['id']}/documents/{doc_id}",
                             headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "doc.delete",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "回收文档.txt"
        assert item["target_type"] == "doc"

        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc_id}/restore",
                           headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "doc.restore",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "回收文档.txt"

        # 再软删一次进回收站，然后 purge
        client.delete(f"/api/kbs/{kb['id']}/documents/{doc_id}",
                      headers=admin_headers)
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc_id}/purge",
                           headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "doc.purge",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "回收文档.txt"

        # 清空回收站：再造一个进回收站后清空
        doc2 = upload_and_ingest(client, kb["id"], filename="清空测试.txt")
        client.delete(f"/api/kbs/{kb['id']}/documents/{doc2['id']}",
                      headers=admin_headers)
        resp = client.post(f"/api/kbs/{kb['id']}/documents/trash/empty",
                           headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["count"] == 1
        item = _find_log(client, admin_headers, "doc.trash-empty",
                         username="admin")
        assert item is not None
        assert '"count": 1' in (item["detail"] or "")


# ==================== 会话埋点 ====================

class TestChatAudit:
    """会话删除/导出记录"""

    def test_delete_session_recorded(self, client, admin_headers):
        _write_session_file("sess_delete", title="删除测试会话")
        resp = client.delete("/api/chat/history/sess_delete",
                             headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "chat.delete",
                         username="admin")
        assert item is not None
        assert item["target_type"] == "chat"
        assert item["target_name"] == "删除测试会话"

    def test_export_session_recorded(self, client, admin_headers):
        _write_session_file("sess_export", title="导出测试会话",
                            messages=[{"role": "user", "content": "hi"}])
        resp = client.get("/api/chat/history/sess_export/export",
                          headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "chat.export",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "导出测试会话"
        assert '"message_count": 1' in (item["detail"] or "")


# ==================== 配置档案埋点 ====================

class TestSettingsAudit:
    """配置档案 CRUD/激活/连接测试记录"""

    def test_profile_crud_activate_recorded(self, client, admin_headers):
        resp = client.post("/api/settings/profiles",
                           json={"name": "审计档案"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        pid = resp.json()["id"]
        item = _find_log(client, admin_headers, "settings.create",
                         username="admin")
        assert item is not None
        assert item["target_type"] == "config"
        assert item["target_name"] == "审计档案"

        resp = client.put(f"/api/settings/profiles/{pid}",
                          json={"name": "审计档案2"}, headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "settings.update",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "审计档案2"

        resp = client.post(f"/api/settings/profiles/{pid}/activate",
                           headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "settings.activate",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "审计档案2"

        resp = client.delete(f"/api/settings/profiles/{pid}",
                             headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "settings.delete",
                         username="admin")
        assert item is not None
        assert item["target_name"] == "审计档案2"

    def test_connection_test_recorded_success(self, client, admin_headers,
                                              monkeypatch):
        resp = client.post("/api/settings/profiles",
                           json={"name": "测试档案"}, headers=admin_headers)
        pid = resp.json()["id"]
        _mock_conn_tests(monkeypatch, {})
        resp = client.post(f"/api/settings/profiles/{pid}/test",
                           json={}, headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "settings.test-connections",
                         status="success")
        assert item is not None
        assert item["target_name"] == "测试档案"
        detail = json.loads(item["detail"] or "{}")
        assert detail["llm"] is True and detail["minio"] is True

    def test_connection_test_recorded_failed(self, client, admin_headers,
                                             monkeypatch):
        resp = client.post("/api/settings/profiles",
                           json={"name": "测试档案"}, headers=admin_headers)
        pid = resp.json()["id"]
        _mock_conn_tests(monkeypatch, {"llm": False, "mysql": False})
        resp = client.post(f"/api/settings/profiles/{pid}/test",
                           json={}, headers=admin_headers)
        assert resp.status_code == 200
        item = _find_log(client, admin_headers, "settings.test-connections",
                         status="failed")
        assert item is not None
        detail = json.loads(item["detail"] or "{}")
        assert detail["llm"] is False and detail["embedding"] is True


# ==================== 权限 ====================

class TestAuditPermission:
    """仅 super_admin 可查审计"""

    def test_non_super_admin_forbidden(self, client, admin_headers,
                                       dept_admin_headers, user_headers):
        for headers in (dept_admin_headers, user_headers):
            resp = client.get("/api/audit/logs", headers=headers)
            assert resp.status_code == 403
            resp = client.get("/api/audit/actions", headers=headers)
            assert resp.status_code == 403

    def test_no_token_401(self, client):
        assert client.get("/api/audit/logs").status_code == 401

    def test_actions_list_has_chinese_labels(self, client, admin_headers):
        actions = _actions(client, admin_headers)
        mapping = {a["action"]: a["label"] for a in actions}
        assert mapping["auth.login"] == "登录"
        assert mapping["doc.upload"] == "上传文档"
        assert mapping["kb.create"] == "创建知识库"
        assert mapping["user.create"] == "创建用户"
        assert "auth.login" in mapping and "settings.update" in mapping


# ==================== 查询：分页/过滤/倒序 ====================

class TestAuditQuery:
    """分页与过滤"""

    def _seed(self, client, admin_headers):
        """造多条审计：登录(admin + audit_0) + 2 个用户 + 1 个部门"""
        for i in range(2):
            resp = client.post("/api/users", json={
                "username": f"audit_{i}", "password": "user123456",
                "display_name": f"用户{i}", "role": "user"},
                headers=admin_headers)
            assert resp.status_code == 201
        resp = client.post("/api/auth/login", json={
            "username": "audit_0", "password": "user123456"})
        assert resp.status_code == 200
        resp = client.post("/api/departments", json={"name": "查询部门"},
                           headers=admin_headers)
        assert resp.status_code == 201

    def test_pagination_and_desc_order(self, client, admin_headers):
        self._seed(client, admin_headers)
        data = _logs(client, admin_headers, page=1, page_size=2)
        assert data["total"] >= 4
        assert len(data["items"]) == 2
        assert data["page"] == 1 and data["page_size"] == 2
        # 倒序：时间降序（同一秒内按 id 降序兜底）
        assert data["items"][0]["created_at"] >= data["items"][1]["created_at"]
        page2 = _logs(client, admin_headers, page=2, page_size=2)
        ids1 = {i["id"] for i in data["items"]}
        ids2 = {i["id"] for i in page2["items"]}
        assert not (ids1 & ids2)  # 分页不重复

    def test_filter_by_action(self, client, admin_headers):
        self._seed(client, admin_headers)
        data = _logs(client, admin_headers, action="user.create")
        assert data["total"] == 2
        assert all(i["action"] == "user.create" for i in data["items"])
        # 大小写敏感精确匹配
        data = _logs(client, admin_headers, action="USER.CREATE")
        assert data["total"] == 0

    def test_filter_by_username(self, client, admin_headers):
        self._seed(client, admin_headers)
        data = _logs(client, admin_headers, username="audit")
        assert data["total"] >= 1  # audit_0 的登录记录
        assert all("audit" in i["username"] for i in data["items"])
        # 不存在的用户名 → 0 条
        data = _logs(client, admin_headers, username="nobody_xyz")
        assert data["total"] == 0

    def test_filter_by_target_type(self, client, admin_headers):
        self._seed(client, admin_headers)
        data = _logs(client, admin_headers, target_type="dept")
        assert data["total"] == 1
        assert data["items"][0]["action"] == "dept.create"

    def test_filter_by_time_range(self, client, admin_headers):
        self._seed(client, admin_headers)
        # 宽窗口全命中
        data = _logs(client, admin_headers, start_time="2000-01-01 00:00:00",
                     end_time="2099-01-01 00:00:00")
        assert data["total"] >= 4
        # 未来窗口：0 条
        data = _logs(client, admin_headers, start_time="2099-01-01 00:00:00")
        assert data["total"] == 0
        # 历史窗口（end 早于全部记录）：0 条
        data = _logs(client, admin_headers, end_time="2000-01-01 00:00:00")
        assert data["total"] == 0
        # 起止结合 action 过滤
        data = _logs(client, admin_headers, action="user.create",
                     start_time="2000-01-01 00:00:00",
                     end_time="2099-01-01 00:00:00")
        assert data["total"] == 2


# ==================== 审计失败不阻塞业务 ====================

class TestAuditFailureNotBlocking:
    """审计落库失败（内部抛错）绝不影响主流程"""

    def test_audit_failure_does_not_block_upload(self, client,
                                                 admin_headers, monkeypatch):
        from backend.services import audit_service

        def _boom(*args, **kwargs):
            raise RuntimeError("审计故障（测试构造）")

        monkeypatch.setattr(audit_service, "log_audit", _boom)
        kb = create_kb(client)  # kb.create 埋点同样失败但业务成功
        doc = upload_doc(client, kb["id"], filename="容错文档.txt")
        assert doc["status"] == "uploaded"
        assert doc["original_name"] == "容错文档.txt"

    def test_audit_failure_does_not_break_login(self, client, monkeypatch):
        from backend.services import audit_service

        def _boom(*args, **kwargs):
            raise RuntimeError("审计故障（测试构造）")

        monkeypatch.setattr(audit_service, "log_audit", _boom)
        # 登录失败：审计抛错不影响 401 响应
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "wrong-pass"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"
        # 登录成功也不受影响
        resp = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})
        assert resp.status_code == 200
        assert resp.json()["access_token"]

    def test_audit_failure_does_not_break_ingest(self, client,
                                                 admin_headers, monkeypatch,
                                                 mock_embedding):
        from backend.services import audit_service

        def _boom(*args, **kwargs):
            raise RuntimeError("审计故障（测试构造）")

        monkeypatch.setattr(audit_service, "log_audit", _boom)
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="容错解析.txt")
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"method": "naive"}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "parsing"
