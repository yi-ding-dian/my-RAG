"""部门级 LLM 配置测试：merge_department_llm 合并规则 + chat 组装部门 LLM 生效 + 接口脱敏与角色分流

覆盖：
- merge_department_llm 纯函数：字段覆盖 / 空串跟随全局（api_key 空串=故意
  清空用全局）/ None 跟随全局 / 未设置=纯全局 / 非 dict 容错
- _merge_legacy_config 纯函数：旧 chat_config 列回退（读取兼容，不搬迁数据）
- chat 组装集成：部门 llm（base_url/model/api_key/温度）生效于部门成员；
  部门未设置 → 全局；超管不受部门 llm 影响
- 接口：dept_admin POST 写本部门 llm（全局 profile 不变）/ GET 合并 +
  api_key 脱敏 / POST 传 "****" 保留原值 / 空串跟随全局 / 清除字段跟随
  全局 / super_admin 写全局 / user 读 200、写 403
"""
from __future__ import annotations

from backend.config import get_active_config
from backend.services.department_service import _merge_legacy_config
from backend.services.settings_service import mask_api_key, \
    merge_department_llm
from conftest import create_department_and_admin, create_kb, create_user, \
    upload_and_ingest

# ==================== 合并函数纯函数单测 ====================

GLOBAL_LLM = {
    "base_url": "http://global.example/v1",
    "api_key": "sk-global-1234567890",
    "model": "global-model",
    "temperature": 0.7,
    "max_tokens": 4096,
    "timeout": 60.0,
}


class TestMergeDepartmentLlm:
    """merge_department_llm：字段级覆盖（部门只覆盖它显式设置的字段）"""

    def test_empty_dept_is_pure_global(self):
        """部门未设置（空 dict）→ 纯全局"""
        assert merge_department_llm(GLOBAL_LLM, {}) == dict(GLOBAL_LLM)

    def test_partial_override(self):
        """部门只覆盖部分字段，其余用全局"""
        merged = merge_department_llm(GLOBAL_LLM, {
            "base_url": "http://dept.example/v1",
            "api_key": "sk-dept-abcdefgh",
            "model": "dept-model",
            "temperature": 0.3,
        })
        assert merged["base_url"] == "http://dept.example/v1"
        assert merged["api_key"] == "sk-dept-abcdefgh"
        assert merged["model"] == "dept-model"
        assert merged["temperature"] == 0.3
        assert merged["max_tokens"] == 4096, "未设置字段用全局"
        assert merged["timeout"] == 60.0

    def test_empty_string_follows_global(self):
        """空串字段（含 api_key 空串=故意清空用全局）→ 跟随全局"""
        merged = merge_department_llm(GLOBAL_LLM, {
            "base_url": "", "api_key": "", "model": "",
        })
        assert merged == dict(GLOBAL_LLM)

    def test_none_follows_global(self):
        """None 字段 → 跟随全局"""
        merged = merge_department_llm(GLOBAL_LLM, {
            "base_url": None, "temperature": None, "max_tokens": None,
            "timeout": None,
        })
        assert merged == dict(GLOBAL_LLM)

    def test_non_dict_dept_tolerated(self):
        """部门配置脏数据（非 dict）→ 容错为纯全局"""
        assert merge_department_llm(GLOBAL_LLM, "脏数据") == dict(GLOBAL_LLM)
        assert merge_department_llm(GLOBAL_LLM, None) == dict(GLOBAL_LLM)

    def test_global_missing_field_kept_none(self):
        """全局缺字段（异常数据防御）→ 保持 None，不报错"""
        merged = merge_department_llm({"base_url": "http://x/v1"}, {})
        assert merged["base_url"] == "http://x/v1"
        assert merged["api_key"] is None
        assert merged["timeout"] is None


class TestMergeLegacyConfig:
    """_merge_legacy_config：旧 chat_config 列回退（读取兼容，不强制搬迁数据）"""

    def test_legacy_only(self):
        """仅旧列有数据（存量部门升级后未保存）→ 全部回退"""
        merged = _merge_legacy_config(
            {}, {"chat": {"system_prompt": "旧提示词"},
                 "retrieval": {"top_k": 3}})
        assert merged["chat"]["system_prompt"] == "旧提示词"
        assert merged["retrieval"]["top_k"] == 3

    def test_new_wins_legacy_fallback(self):
        """新列字段优先；新列缺失字段回退旧列"""
        merged = _merge_legacy_config(
            {"chat": {"temperature": 0.5}},
            {"chat": {"temperature": 0.9, "system_prompt": "旧提示词"}})
        assert merged["chat"]["temperature"] == 0.5, "新列显式设置优先"
        assert merged["chat"]["system_prompt"] == "旧提示词", "缺失字段回退旧列"

    def test_llm_only_from_new(self):
        """llm 段只来自新列（旧列无 llm）；chat 段照常回退"""
        merged = _merge_legacy_config(
            {"llm": {"model": "dept-model"}},
            {"chat": {"system_prompt": "旧提示词"}})
        assert merged["llm"]["model"] == "dept-model"
        assert merged["chat"]["system_prompt"] == "旧提示词"


# ==================== chat 组装集成（部门 LLM 全链路生效） ====================

def _dept_env(client, admin_headers):
    """建部门 + 部门管理员 + 部门普通用户 + 部门知识库（已入库）"""
    dept_id, dept_admin_hdrs = create_department_and_admin(
        client, admin_headers, "部门LLM部", "dept_llm_admin",
        "pass123456", "LLM主管")
    user_hdrs = create_user(client, admin_headers, dept_id, "dept_llm_member")
    kb = create_kb(client, "部门LLM知识库", department_id=dept_id)
    upload_and_ingest(client, kb["id"])
    return dept_admin_hdrs, user_hdrs, kb


class TestDeptLlmAssembly:
    """部门成员聊天 → 用部门 LLM 配置；未设置/超管 → 全局（互不影响）"""

    def test_dept_user_uses_dept_llm(self, client, admin_headers,
                                     mock_embedding, mock_llm):
        """部门成员流式问答：_get_client 收到合并后的部门 LLM 配置
        （base_url/api_key/model/温度），请求参数用部门 model/温度"""
        dept_admin_hdrs, user_hdrs, kb = _dept_env(client, admin_headers)
        resp = client.post("/api/settings/chat", json={
            "llm": {"base_url": "http://dept-llm.example/v1",
                    "api_key": "sk-dept-llm-key",
                    "model": "dept-qwen",
                    "temperature": 0.25},
        }, headers=dept_admin_hdrs)
        assert resp.status_code == 200, resp.text

        state = mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=user_hdrs)
        assert resp.status_code == 200 and "event: done" in resp.text
        inst = state.instances[0]
        assert inst.llm_cfg["base_url"] == "http://dept-llm.example/v1", \
            "部门 base_url 覆盖全局"
        assert inst.llm_cfg["api_key"] == "sk-dept-llm-key", \
            "部门 api_key 覆盖全局"
        assert inst.llm_cfg["model"] == "dept-qwen"
        assert inst.llm_cfg["temperature"] == 0.25
        assert inst.last_kwargs["model"] == "dept-qwen", \
            "请求 model 用部门值"
        assert inst.last_kwargs["temperature"] == 0.25, \
            "请求温度用部门 llm 值"

    def test_dept_unset_uses_global(self, client, admin_headers,
                                    mock_embedding, mock_llm):
        """部门未设置 llm → 合并配置 = 全局活跃 LLM 配置"""
        _, user_hdrs, kb = _dept_env(client, admin_headers)
        state = mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=user_hdrs)
        assert resp.status_code == 200 and "event: done" in resp.text
        cfg = get_active_config().llm
        assert state.instances[0].llm_cfg["base_url"] == cfg.base_url
        assert state.instances[0].llm_cfg["api_key"] == cfg.api_key
        assert state.instances[0].llm_cfg["model"] == cfg.model
        assert state.instances[0].llm_cfg["timeout"] == cfg.timeout

    def test_super_admin_still_uses_global(self, client, admin_headers,
                                           mock_embedding, mock_llm):
        """部门 llm 存在时，超管聊天仍用全局 LLM（互不干扰）"""
        dept_admin_hdrs, _, kb = _dept_env(client, admin_headers)
        client.post("/api/settings/chat", json={
            "llm": {"model": "dept-qwen", "base_url": "http://dept-llm/v1"},
        }, headers=dept_admin_hdrs)
        state = mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        cfg = get_active_config().llm
        assert state.instances[0].llm_cfg["model"] == cfg.model, \
            "超管不应使用部门 model"


# ==================== 接口角色分流 + 脱敏语义 ====================

DEPT_LLM = {"base_url": "http://dept-llm.example/v1",
            "api_key": "sk-dept-llm-key",
            "model": "dept-qwen",
            "temperature": 0.25}


class TestDeptLlmApi:
    """GET 返回合并 + 脱敏；POST 按角色分流"""

    def test_dept_admin_saves_llm_global_untouched(self, client,
                                                   admin_headers,
                                                   dept_admin_headers,
                                                   user_headers):
        """dept_admin POST llm → 写本部门（全局 profile 不变），响应/GET 合并"""
        global_model = get_active_config().llm.model
        resp = client.post("/api/settings/chat", json={
            "llm": dict(DEPT_LLM),
        }, headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["llm"]["model"] == "dept-qwen", "响应 llm 为部门合并值"
        assert data["llm"]["api_key"] != "sk-dept-llm-key", "绝不返回明文"
        assert "****" in data["llm"]["api_key"], "api_key 已脱敏"
        assert data["dept"]["llm"]["model"] == "dept-qwen"
        assert "****" in data["dept"]["llm"]["api_key"]
        # 全局 profile 不被部门配置触碰
        assert get_active_config().llm.model == global_model
        # 超管视角 GET → llm 仍全局（dept=None）
        gdata = client.get("/api/settings/chat", headers=admin_headers).json()
        assert gdata["llm"]["model"] == global_model
        assert gdata["dept"] is None
        # 同部门普通用户 GET → 部门合并值
        udata = client.get("/api/settings/chat", headers=user_headers).json()
        assert udata["llm"]["model"] == "dept-qwen"

    def test_masked_api_key_keeps_original(self, client,
                                           dept_admin_headers):
        """POST api_key="****"（脱敏回传）→ 保留部门原值不覆盖"""
        client.post("/api/settings/chat", json={
            "llm": dict(DEPT_LLM),
        }, headers=dept_admin_headers)
        resp = client.post("/api/settings/chat", json={
            "llm": {"api_key": "****", "model": "other-model"},
        }, headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        dept_llm = resp.json()["dept"]["llm"]
        assert dept_llm["model"] == "other-model", "非密钥字段照常更新"
        assert dept_llm["api_key"] == mask_api_key("sk-dept-llm-key"), \
            "脱敏回传不覆盖原值"
        # 明文新 key → 覆盖
        client.post("/api/settings/chat", json={
            "llm": {"api_key": "sk-new-key-12345"},
        }, headers=dept_admin_headers)
        data = client.get("/api/settings/chat",
                          headers=dept_admin_headers).json()
        assert data["dept"]["llm"]["api_key"] == mask_api_key(
            "sk-new-key-12345"), "明文新 key 已覆盖"

    def test_empty_api_key_follows_global(self, client,
                                          dept_admin_headers):
        """api_key 空串 = 故意清空 → 移除部门 key，合并用全局"""
        client.post("/api/settings/chat", json={
            "llm": dict(DEPT_LLM),
        }, headers=dept_admin_headers)
        resp = client.post("/api/settings/chat", json={
            "llm": {"api_key": ""},
        }, headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["dept"]["llm"].get("api_key") is None
        assert resp.json()["llm"]["api_key"] == mask_api_key(
            get_active_config().llm.api_key), "清空后跟随全局 key"

    def test_clear_llm_fields_follows_global(self, client,
                                             dept_admin_headers,
                                             user_headers):
        """llm 段全部字段清除 → 部门配置无 llm 段（dept=None，纯全局）"""
        client.post("/api/settings/chat", json={
            "llm": dict(DEPT_LLM),
        }, headers=dept_admin_headers)
        resp = client.post("/api/settings/chat", json={
            "llm": {"base_url": "", "api_key": "", "model": "",
                    "temperature": None, "max_tokens": None,
                    "timeout": None},
        }, headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["dept"] is None, "全部字段清除 → 部门配置置空"
        cfg = get_active_config().llm
        merged = client.get("/api/settings/chat", headers=user_headers).json()
        assert merged["llm"]["model"] == cfg.model, "清除后跟随全局"

    def test_super_admin_saves_global_llm(self, client, admin_headers):
        """super_admin POST llm → 写全局活跃档案并即时生效"""
        resp = client.post("/api/settings/chat", json={
            "llm": {"model": "global-v2", "temperature": 0.15},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["llm"]["model"] == "global-v2"
        assert resp.json()["dept"] is None
        assert get_active_config().llm.model == "global-v2"
        assert get_active_config().llm.temperature == 0.15

    def test_user_read_merged_post_403(self, client, admin_headers,
                                       dept_admin_headers, user_headers):
        """user GET 读合并值；user POST llm → 403（配置仍为原值）"""
        client.post("/api/settings/chat", json={
            "llm": dict(DEPT_LLM),
        }, headers=dept_admin_headers)
        data = client.get("/api/settings/chat", headers=user_headers).json()
        assert data["llm"]["model"] == "dept-qwen", "普通成员读部门合并值"
        global_model = get_active_config().llm.model
        resp = client.post("/api/settings/chat", json={
            "llm": {"model": "evil-model"},
        }, headers=user_headers)
        assert resp.status_code == 403
        assert get_active_config().llm.model == global_model
