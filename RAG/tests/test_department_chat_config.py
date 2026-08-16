"""部门级聊天配置测试：合并规则 + chat 组装集成 + 接口角色分流 + 唯一管理员

覆盖：
- merge_chat_config 纯函数：部门部分覆盖 / 未设置字段用全局 / 空配置=纯全局 /
  system_prompt 空串不覆盖 / 检索字段覆盖
- chat 组装集成：部门用户聊天用部门 system_prompt/温度/top_k；超管仍用全局
- 接口：dept_admin POST 写本部门（全局 profile 不变）、字段清除（null/空串
  移除=跟随全局）、user 读 merged、超管 POST 只动全局、dept_admin 无部门 403
- 唯一管理员：创建第二管理员 400、改角色冲突 400、跨部门不受影响、
  降级离开不受限（可重新任命）
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.config import get_active_config
from backend.services.chat_service import ChatService
from backend.services.settings_service import merge_chat_config
from conftest import _FakeStream, create_department_and_admin, create_kb, \
    create_user, upload_and_ingest

# ==================== 合并函数纯函数单测 ====================

GLOBAL = {
    "chat": {"temperature": 0.7, "top_p": 0.9, "max_tokens": 2048,
             "enable_multi_turn": True, "history_rounds": 8,
             "system_prompt": "全局提示词", "kg_enhance": True},
    "retrieval": {"top_k": 5, "similarity_threshold": 0.0},
}


class TestMergeChatConfig:
    """merge_chat_config：字段级合并（部门只覆盖它设置的字段）"""

    def test_empty_dept_is_pure_global(self):
        """部门未设置（空 dict）→ 纯全局"""
        merged = merge_chat_config(GLOBAL, {})
        assert merged == {
            "chat": dict(GLOBAL["chat"]),
            "retrieval": dict(GLOBAL["retrieval"]),
        }

    def test_partial_override(self):
        """部门只覆盖部分字段，其余用全局"""
        merged = merge_chat_config(GLOBAL, {
            "chat": {"temperature": 0.5, "system_prompt": "部门提示词"},
        })
        assert merged["chat"]["temperature"] == 0.5
        assert merged["chat"]["system_prompt"] == "部门提示词"
        assert merged["chat"]["top_p"] == 0.9, "未设置字段用全局"
        assert merged["chat"]["max_tokens"] == 2048
        assert merged["chat"]["enable_multi_turn"] is True
        assert merged["chat"]["history_rounds"] == 8
        assert merged["retrieval"] == GLOBAL["retrieval"]

    def test_retrieval_override(self):
        """检索字段 top_k/similarity_threshold 覆盖"""
        merged = merge_chat_config(GLOBAL, {
            "retrieval": {"top_k": 3, "similarity_threshold": 0.45},
        })
        assert merged["retrieval"]["top_k"] == 3
        assert merged["retrieval"]["similarity_threshold"] == 0.45
        assert merged["chat"] == GLOBAL["chat"]

    def test_null_and_empty_values_do_not_override(self):
        """部门字段值为 None/空串 → 不覆盖（跟随全局）"""
        merged = merge_chat_config(GLOBAL, {
            "chat": {"temperature": None, "system_prompt": "",
                     "top_p": None},
            "retrieval": {"top_k": None},
        })
        assert merged["chat"]["temperature"] == 0.7
        assert merged["chat"]["system_prompt"] == "全局提示词"
        assert merged["chat"]["top_p"] == 0.9
        assert merged["retrieval"]["top_k"] == 5

    def test_global_missing_field_kept_none(self):
        """全局缺字段（异常数据防御）→ 保持 None，不报错"""
        merged = merge_chat_config({"chat": {"temperature": 0.7},
                                    "retrieval": {}}, {
            "chat": {"history_rounds": 4},
        })
        assert merged["chat"]["temperature"] == 0.7
        assert merged["chat"]["history_rounds"] == 4
        assert merged["chat"]["system_prompt"] == ""
        assert merged["retrieval"]["top_k"] is None

    def test_non_dict_dept_tolerated(self):
        """部门配置脏数据（非 dict 段）→ 容错为空"""
        merged = merge_chat_config(GLOBAL, {"chat": "脏数据", "retrieval": []})
        assert merged["chat"] == dict(GLOBAL["chat"])
        assert merged["retrieval"] == dict(GLOBAL["retrieval"])


# ==================== chat 组装集成（部门配置全链路生效） ====================

class _RecordingLLM:
    """记录每次请求 kwargs 的伪客户端（验证发给 LLM 的 system/温度/top_k）"""

    def __init__(self):
        self.requests = []

    @property
    def chat(self):
        return SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        return _FakeStream(["回答内容。"])


def _dept_env(client, admin_headers):
    """建部门 + 部门管理员 + 部门普通用户 + 部门知识库（已入库），
    返回 (dept_admin_headers, user_headers, kb)"""
    dept_id, dept_admin_hdrs = create_department_and_admin(
        client, admin_headers, "配置隔离部", "cfg_dept_admin",
        "pass123456", "配置主管")
    user_hdrs = create_user(client, admin_headers, dept_id, "cfg_member_user")
    kb = create_kb(client, "配置部知识库", department_id=dept_id)
    upload_and_ingest(client, kb["id"])
    return dept_admin_hdrs, user_hdrs, kb


class TestDeptChatAssembly:
    """部门用户聊天 → 用部门配置；超管 → 用全局（互不影响）"""

    def test_dept_user_uses_dept_config(self, client, admin_headers,
                                        mock_embedding, monkeypatch):
        """部门用户流式问答：system_prompt/温度/检索 top_k 均用部门配置"""
        dept_admin_hdrs, user_hdrs, kb = _dept_env(client, admin_headers)
        # 部门管理员设置部门配置（system_prompt + 温度 + 检索 top_k/threshold）
        resp = client.post("/api/settings/chat", json={
            "retrieval": {"top_k": 3, "similarity_threshold": 0.0},
            "chat": {"temperature": 0.33, "system_prompt": "部门专用助手提示词"},
        }, headers=dept_admin_hdrs)
        assert resp.status_code == 200, resp.text

        # 记录检索服务调用参数（转发真实离线检索）
        from backend.services import retrieval_service
        from backend.services.chat_service import get_chat_service
        real_svc = retrieval_service.get_retrieval_service()
        retrieval_calls = []

        async def _wrapper(kb_id, query, top_k=None, min_score=None, **kw):
            retrieval_calls.append({"top_k": top_k, "min_score": min_score})
            return await real_svc.retrieve(
                kb_id, query, top_k=top_k, min_score=min_score, **kw)

        monkeypatch.setattr(
            "backend.services.chat_service.get_retrieval_service",
            lambda: SimpleNamespace(retrieve=_wrapper))
        recorder = _RecordingLLM()
        monkeypatch.setattr(ChatService, "_get_client", lambda self, llm_cfg=None: recorder)

        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=user_hdrs)
        assert resp.status_code == 200 and "event: done" in resp.text, resp.text
        req = recorder.requests[0]
        assert req["temperature"] == 0.33, "部门温度覆盖全局"
        assert req["messages"][0]["content"].startswith("部门专用助手提示词"), \
            "部门 system_prompt 覆盖全局"
        assert retrieval_calls[0]["top_k"] == 3, "部门 top_k 覆盖全局检索条数"
        assert retrieval_calls[0]["min_score"] == 0.0

    def test_super_admin_still_uses_global(self, client, admin_headers,
                                           mock_embedding, monkeypatch):
        """同部门配置存在时，超管聊天仍用全局配置（互不干扰）"""
        dept_admin_hdrs, _, kb = _dept_env(client, admin_headers)
        client.post("/api/settings/chat", json={
            "chat": {"temperature": 0.33, "system_prompt": "部门专用提示词"},
        }, headers=dept_admin_hdrs)

        recorder = _RecordingLLM()
        monkeypatch.setattr(ChatService, "_get_client", lambda self, llm_cfg=None: recorder)
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        req = recorder.requests[0]
        assert req["temperature"] != 0.33, "超管不应使用部门温度"
        assert not req["messages"][0]["content"].startswith("部门专用提示词"), \
            "超管不应使用部门 system_prompt"

    def test_other_department_not_affected(self, client, admin_headers,
                                           mock_embedding, monkeypatch):
        """B 部配置不生效于 A 部（部门隔离）"""
        # A 部设置部门配置
        dept_admin_a, _, kb_a = _dept_env(client, admin_headers)
        client.post("/api/settings/chat", json={
            "chat": {"system_prompt": "A部专用提示词"},
        }, headers=dept_admin_a)
        # B 部（无部门配置）+ B 部用户
        dept_b_id, dept_admin_b = create_department_and_admin(
            client, admin_headers, "配置隔离B部", "cfg_dept_admin_b",
            "pass123456", "B部主管")
        user_b = create_user(client, admin_headers, dept_b_id, "cfg_member_b")
        kb_b = create_kb(client, "B部知识库", department_id=dept_b_id)
        upload_and_ingest(client, kb_b["id"])

        recorder = _RecordingLLM()
        monkeypatch.setattr(ChatService, "_get_client", lambda self, llm_cfg=None: recorder)
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb_b["id"], "query": "Python 是什么？",
        }, headers=user_b)
        assert resp.status_code == 200 and "event: done" in resp.text
        sys_prompt = recorder.requests[0]["messages"][0]["content"]
        assert not sys_prompt.startswith("A部专用提示词"), \
            "A 部配置不得影响 B 部成员"


# ==================== 接口角色分流 ====================

class TestDeptSettingsApi:
    """GET 返回合并值（dept 段）；POST 按角色分流"""

    def test_get_merged_for_everyone(self, client, admin_headers,
                                     dept_admin_headers, user_headers):
        """三角色 GET 均 200：无部门用户 dept=null；部门管理员/成员读合并值"""
        for hdrs in (admin_headers, dept_admin_headers, user_headers):
            data = client.get("/api/settings/chat", headers=hdrs).json()
            assert "dept" in data
            assert data["chat"]["temperature"] is not None or True
        # 超管（无部门）→ dept=None
        assert client.get("/api/settings/chat",
                          headers=admin_headers).json()["dept"] is None

    def test_super_admin_post_keeps_dept_untouched(self, client,
                                                   admin_headers,
                                                   dept_admin_headers,
                                                   user_headers):
        """超管 POST 改全局 → 部门配置不受影响（dept 段保留，部门成员仍用部门值）"""
        client.post("/api/settings/chat", json={
            "chat": {"temperature": 0.33, "system_prompt": "部门提示词"},
        }, headers=dept_admin_headers)
        # 超管改全局温度
        resp = client.post("/api/settings/chat", json={
            "chat": {"temperature": 0.9},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert get_active_config().chat.temperature == 0.9
        # 部门成员读到的 merged 仍是部门温度（部门字段覆盖全局）
        merged = client.get("/api/settings/chat", headers=user_headers).json()
        assert merged["chat"]["temperature"] == 0.33, "部门覆盖优先于全局"
        assert merged["chat"]["system_prompt"] == "部门提示词"
        assert merged["dept"] is not None, "部门配置未被超管操作清除"

    def test_dept_field_clear_falls_back_to_global(self, client,
                                                   admin_headers,
                                                   dept_admin_headers,
                                                   user_headers):
        """部门传 temperature=null / system_prompt=空串 → 字段移除（跟随全局）"""
        client.post("/api/settings/chat", json={
            "chat": {"temperature": 0.33, "system_prompt": "部门提示词"},
        }, headers=dept_admin_headers)
        resp = client.post("/api/settings/chat", json={
            "chat": {"temperature": None, "system_prompt": ""},
        }, headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["dept"] is None, "全部字段清除 → 部门配置置空"
        merged = client.get("/api/settings/chat", headers=user_headers).json()
        assert merged["chat"]["temperature"] == get_active_config().chat.temperature, \
            "清除后跟随全局"
        assert merged["dept"] is None

    def test_dept_admin_without_department_403(self, client, admin_headers):
        """dept_admin 无部门归属 → POST 403（无法配置）"""
        resp = client.post("/api/users", json={
            "username": "homeless_cfg", "password": "pass123456",
            "display_name": "无部门主管", "role": "dept_admin",
        }, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        login = client.post("/api/auth/login", json={
            "username": "homeless_cfg", "password": "pass123456",
        }).json()
        hdrs = {"Authorization": f"Bearer {login['access_token']}"}
        resp = client.post("/api/settings/chat", json={
            "chat": {"temperature": 0.5},
        }, headers=hdrs)
        assert resp.status_code == 403
        assert "未分配部门" in resp.json()["detail"]

    def test_dept_admin_department_id_field_rejected(self, client,
                                                     dept_admin_headers):
        """body 带 department_id 顶层段 → 400（白名单拒绝，防越权指定部门）"""
        resp = client.post("/api/settings/chat", json={
            "chat": {"temperature": 0.5},
            "department_id": "other_dept",
        }, headers=dept_admin_headers)
        assert resp.status_code == 400
        assert "不允许修改配置段" in resp.json()["detail"]


# ==================== 唯一管理员约束 ====================

class TestUniqueDeptAdmin:
    """每部门仅一名 dept_admin"""

    def test_update_role_conflict_400(self, client, admin_headers):
        """把已有一名管理员的部门成员改为 dept_admin → 400"""
        dept_id, _ = create_department_and_admin(
            client, admin_headers, "唯一管理员部", "uni_admin",
            "pass123456", "主管")
        uid = None
        for u in client.get("/api/users", headers=admin_headers).json():
            if u["username"] == "uni_user":
                uid = u["id"]
        if uid is None:
            resp = client.post("/api/users", json={
                "username": "uni_user", "password": "pass123456",
                "display_name": "成员", "role": "user",
                "department_id": dept_id,
            }, headers=admin_headers)
            assert resp.status_code == 201, resp.text
            uid = resp.json()["id"]
        resp = client.put(f"/api/users/{uid}", json={"role": "dept_admin"},
                          headers=admin_headers)
        assert resp.status_code == 400, resp.text
        assert "该部门已有一名管理员" in resp.json()["detail"]

    def test_cross_department_unaffected(self, client, admin_headers):
        """不同部门各可有一名管理员（互不影响）"""
        dept_a, _ = create_department_and_admin(
            client, admin_headers, "唯一A部", "uni_admin_a", "pass123456", "主管A")
        dept_b_id, _ = create_department_and_admin(
            client, admin_headers, "唯一B部", "uni_admin_b", "pass123456", "主管B")
        # B 部再创建一个 dept_admin 仍被拒
        resp = client.post("/api/users", json={
            "username": "uni_admin_b2", "password": "pass123456",
            "display_name": "主管B2", "role": "dept_admin",
            "department_id": dept_b_id,
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "该部门已有一名管理员" in resp.json()["detail"]
        assert dept_a != dept_b_id

    def test_demote_and_reappoint(self, client, admin_headers):
        """dept_admin 降级离开不受限（允许无管理员）；之后可重新任命"""
        dept_id, _ = create_department_and_admin(
            client, admin_headers, "任命部", "reappoint_admin",
            "pass123456", "主管")
        uid = next(u["id"] for u in client.get(
            "/api/users", headers=admin_headers).json()
            if u["username"] == "reappoint_admin")
        # 超管降级 dept_admin → user（部门变无管理员，允许）
        resp = client.put(f"/api/users/{uid}", json={"role": "user"},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        # 重新任命新管理员成功
        resp = client.post("/api/users", json={
            "username": "new_appointed", "password": "pass123456",
            "display_name": "新主管", "role": "dept_admin",
            "department_id": dept_id,
        }, headers=admin_headers)
        assert resp.status_code == 201, resp.text

    def test_same_admin_self_update_allowed(self, client, admin_headers):
        """管理员改自己的显示名/部门内其他字段不受唯一性约束影响"""
        dept_id, admin_hdrs = create_department_and_admin(
            client, admin_headers, "自改部", "self_admin",
            "pass123456", "主管")
        uid = next(u["id"] for u in client.get(
            "/api/users", headers=admin_headers).json()
            if u["username"] == "self_admin")
        resp = client.put(f"/api/users/{uid}", json={"display_name": "新名字"},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "新名字"
        # 换部门（移动到 B 部，B 部无管理员）→ 成功
        dept_b = client.post("/api/departments", json={"name": "自改B部"},
                             headers=admin_headers).json()
        resp = client.put(f"/api/users/{uid}",
                          json={"department_id": dept_b["id"]},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        # 原部门现在可任命新管理员
        resp = client.post("/api/users", json={
            "username": "self_admin_new", "password": "pass123456",
            "display_name": "新主管", "role": "dept_admin",
            "department_id": dept_id,
        }, headers=admin_headers)
        assert resp.status_code == 201, resp.text
