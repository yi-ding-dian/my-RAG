"""聊天设置 API 权限测试：GET 登录可读 / POST super_admin+dept_admin / 白名单校验

背景：部门管理员（dept_admin）需要配置会话参数（聊天设置）；/api/settings 其余
接口读取已对管理员放开（写仍仅 super_admin，见 test_settings.py
TestSettingsReadOnlyForDeptAdmin）。本文件覆盖 /api/settings/chat：

- GET：admin / dept_admin / user 均 200（user 只读，聊天设置弹窗数据源）
- POST：admin / dept_admin 200 且 get_active_config() 即时生效；user 403
- POST 白名单：携带 llm 等基础设施段 → 400 且配置未变；chat/retrieval 段内
  未知字段 → 400
- 无活跃档案：GET/POST → 404 明确中文错误
- chat 段 null 语义：temperature/max_tokens 显式传 null = 用 LLM 配置默认
"""
from __future__ import annotations

from backend.config import get_active_config


def _delete_all_profiles(client, admin_headers):
    """删除全部配置档案（与 test_settings 同法：制造无活跃档案场景）"""
    items = client.get("/api/settings/profiles",
                       headers=admin_headers).json()
    for p in items:
        assert client.delete(f"/api/settings/profiles/{p['id']}",
                             headers=admin_headers).status_code == 200


class TestGetChatSettings:
    """GET /api/settings/chat：所有登录用户可读"""

    def test_all_roles_200(self, client, admin_headers, dept_admin_headers,
                           user_headers):
        """admin / dept_admin / user 均可读，返回弹窗所需字段"""
        for hdrs in (admin_headers, dept_admin_headers, user_headers):
            resp = client.get("/api/settings/chat", headers=hdrs)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["retrieval"]["top_k"] >= 1
            assert 0 <= data["retrieval"]["similarity_threshold"] <= 1
            assert data["chat"]["enable_multi_turn"] in (True, False)
            assert data["chat"]["history_rounds"] >= 1
            assert "temperature" in data["chat"]
            assert "top_p" in data["chat"]
            assert "max_tokens" in data["chat"]
            assert "system_prompt" in data["chat"]
            # llm 段返回合并视图但 api_key 已脱敏（绝不返回明文）
            assert "llm" in data
            assert data["llm"]["api_key"] != get_active_config().llm.api_key or \
                "****" in (data["llm"]["api_key"] or "")
            # 聊天设置接口绝不暴露基础设施配置
            assert "mysql" not in data and "embedding" not in data

    def test_unauthorized_401(self, client):
        """未登录 → 401"""
        assert client.get("/api/settings/chat").status_code == 401

    def test_no_active_profile_404(self, client, admin_headers):
        """无活跃档案 → 404 明确中文错误（前端透传展示，不再误判文案）"""
        _delete_all_profiles(client, admin_headers)
        resp = client.get("/api/settings/chat", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "没有激活的配置档案"


class TestPostChatSettings:
    """POST /api/settings/chat：super_admin / dept_admin 可写，user 403"""

    def test_dept_admin_saves_to_department(self, client,
                                            dept_admin_headers,
                                            user_headers):
        """dept_admin 保存聊天设置 → 写入本部门（不碰全局 profile）

        响应返回本部门视角合并值（dept 段 = 部门原始配置）；get_active_config
        与全局档案均不变；同部门普通用户 GET 读到部门覆盖值。
        """
        global_top_k = get_active_config().retrieval.top_k
        resp = client.post("/api/settings/chat", json={
            "retrieval": {"top_k": 7, "similarity_threshold": 0.35},
            "chat": {"temperature": 0.5, "top_p": 0.8,
                     "max_tokens": 4096, "enable_multi_turn": False,
                     "history_rounds": 4, "system_prompt": "测试提示词"},
        }, headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["retrieval"]["top_k"] == 7
        assert data["retrieval"]["similarity_threshold"] == 0.35
        assert data["chat"]["temperature"] == 0.5
        assert data["chat"]["system_prompt"] == "测试提示词"
        # dept 段 = 部门原始配置（仅部门显式设置的字段）
        assert data["dept"] == {
            "retrieval": {"top_k": 7, "similarity_threshold": 0.35},
            "chat": {"temperature": 0.5, "top_p": 0.8, "max_tokens": 4096,
                     "enable_multi_turn": False, "history_rounds": 4,
                     "system_prompt": "测试提示词"},
        }
        # 全局 profile 不被部门配置触碰（get_active_config 不变）
        cfg = get_active_config()
        assert cfg.retrieval.top_k == global_top_k, "部门配置不应影响全局活跃档案"
        assert cfg.chat.temperature is None, "部门配置不应影响全局 chat 段"
        # 同部门普通用户读合并值（部门覆盖全局）
        merged = client.get("/api/settings/chat", headers=user_headers).json()
        assert merged["retrieval"]["top_k"] == 7
        assert merged["chat"]["temperature"] == 0.5
        assert merged["chat"]["system_prompt"] == "测试提示词"
        assert merged["dept"] == data["dept"]

    def test_super_admin_saves(self, client, admin_headers):
        """super_admin 保存聊天设置 → 200"""
        resp = client.post("/api/settings/chat", json={
            "chat": {"temperature": 0.9},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["chat"]["temperature"] == 0.9
        assert get_active_config().chat.temperature == 0.9

    def test_thinking_mode_default_and_save(self, client, admin_headers):
        """chat.thinking_mode 默认 disabled（关闭思考）；保存 enabled_low 即时生效"""
        resp = client.get("/api/settings/chat", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["chat"]["thinking_mode"] == "disabled", \
            "默认应为关闭思考（简单延迟敏感任务更快更省 token）"
        resp = client.post("/api/settings/chat", json={
            "chat": {"thinking_mode": "enabled_low"},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["chat"]["thinking_mode"] == "enabled_low"
        assert get_active_config().chat.thinking_mode == "enabled_low", \
            "保存后 get_active_config 应即时生效"

    def test_null_temperature_uses_llm_default(self, client, admin_headers):
        """chat 段显式传 null → 用 LLM 配置默认（先设值再清空）"""
        client.post("/api/settings/chat", json={"chat": {"temperature": 1.2}},
                    headers=admin_headers)
        assert get_active_config().chat.temperature == 1.2
        resp = client.post("/api/settings/chat",
                           json={"chat": {"temperature": None}},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["chat"]["temperature"] is None
        assert get_active_config().chat.temperature is None, \
            "null = 用 LLM 配置默认，不应再覆盖"

    def test_empty_system_prompt_resets(self, client, admin_headers):
        """system_prompt 传空串 = 恢复内置默认模板（"恢复默认"按钮路径）"""
        client.post("/api/settings/chat",
                    json={"chat": {"system_prompt": "自定义"}},
                    headers=admin_headers)
        assert get_active_config().chat.system_prompt == "自定义"
        resp = client.post("/api/settings/chat",
                           json={"chat": {"system_prompt": ""}},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["chat"]["system_prompt"] == ""
        assert get_active_config().chat.system_prompt == ""

    def test_user_403(self, client, user_headers, dept_admin_headers):
        """user 写聊天设置 → 403（配置仍为原值）"""
        before = get_active_config().chat.temperature
        resp = client.post("/api/settings/chat", json={
            "chat": {"temperature": 1.9},
        }, headers=user_headers)
        assert resp.status_code == 403
        assert get_active_config().chat.temperature == before, "配置不应被 user 修改"

    def test_unauthorized_401(self, client):
        assert client.post("/api/settings/chat",
                           json={"chat": {"temperature": 0.5}}).status_code == 401

    def test_no_active_profile_404(self, client, admin_headers,
                                   dept_admin_headers):
        """无活跃档案 → 404（先于白名单校验，任何 POST 都拒绝）"""
        _delete_all_profiles(client, admin_headers)
        resp = client.post("/api/settings/chat", json={
            "chat": {"temperature": 0.5},
        }, headers=dept_admin_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "没有激活的配置档案"


class TestPostWhitelist:
    """POST 白名单校验：防越权改基础设施配置"""

    def test_infra_section_rejected_400(self, client, admin_headers,
                                        dept_admin_headers):
        """携带 mineru 等基础设施段 → 400 且配置未变（防越权改基础设施配置）

        llm 段 6 字段已在白名单内（部门 LLM 配置），其余基础设施段
        （embedding/mineru/mysql/minio/...）仍一律拒绝。
        """
        before = get_active_config().llm.base_url
        before_temp = get_active_config().chat.temperature
        resp = client.post("/api/settings/chat", json={
            "chat": {"temperature": 0.3},
            "mineru": {"url": "http://evil.example:8001"},
        }, headers=dept_admin_headers)
        assert resp.status_code == 400
        assert "不允许修改配置段" in resp.json()["detail"]
        assert get_active_config().llm.base_url == before, \
            "llm.base_url 不应被修改"
        assert get_active_config().chat.temperature == before_temp, \
            "非法请求应整体拒绝，chat 段也不应生效"

    def test_llm_unknown_field_rejected_400(self, client, admin_headers,
                                            dept_admin_headers):
        """llm 段内未知字段（如 dimension）→ 400（白名单仅 6 字段）"""
        resp = client.post("/api/settings/chat", json={
            "llm": {"dimension": 1024},
        }, headers=dept_admin_headers)
        assert resp.status_code == 400
        assert "不允许修改 LLM 配置字段" in resp.json()["detail"]

    def test_llm_bad_type_400(self, client, dept_admin_headers):
        """llm 段数值字段类型错误 → 400 而非 500"""
        for payload in ({"llm": {"temperature": "烫"}},
                        {"llm": {"max_tokens": "许多"}},
                        {"llm": {"timeout": "立刻"}}):
            resp = client.post("/api/settings/chat", json=payload,
                               headers=dept_admin_headers)
            assert resp.status_code == 400, resp.text

    def test_unknown_chat_field_rejected_400(self, client, admin_headers,
                                             dept_admin_headers):
        """chat 段内未知字段（如 api_key）→ 400 且配置未变"""
        resp = client.post("/api/settings/chat", json={
            "chat": {"api_key": "sk-evil"},
        }, headers=dept_admin_headers)
        assert resp.status_code == 400
        assert "不允许修改聊天设置字段" in resp.json()["detail"]

    def test_retrieval_rerank_rejected_400(self, client, admin_headers,
                                           dept_admin_headers):
        """retrieval 段仅允许 top_k/similarity_threshold，rerank → 400"""
        resp = client.post("/api/settings/chat", json={
            "retrieval": {"rerank": {"enabled": True}},
        }, headers=dept_admin_headers)
        assert resp.status_code == 400
        assert "不允许修改检索字段" in resp.json()["detail"]

    def test_embedding_section_rejected_400(self, client, admin_headers):
        """embedding 段 → 400（基础设施仍仅超管档案管理可改）"""
        resp = client.post("/api/settings/chat", json={
            "embedding": {"model": "bge-m3"},
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_bad_type_400(self, client, dept_admin_headers):
        """数值字段类型错误 → 400 而非 500"""
        for payload in ({"chat": {"temperature": "烫"}},
                        {"retrieval": {"top_k": "五个"}}):
            resp = client.post("/api/settings/chat", json=payload,
                               headers=dept_admin_headers)
            assert resp.status_code == 400, resp.text

    def test_empty_body_400(self, client, dept_admin_headers):
        """空对象 → 400（无可更新字段）"""
        resp = client.post("/api/settings/chat", json={},
                           headers=dept_admin_headers)
        assert resp.status_code == 400
