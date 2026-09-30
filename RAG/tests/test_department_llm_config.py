"""部门级 LLM 配置测试：按条目选 + 微调 / chat 组装生效 / 接口白名单与角色分流

**部门的 llm 段存的是"条目名"**（超管在「LLM 模型管理」里配的 name），不是模型名
——部门只"选"，连接信息与密钥不下放。原先让部门自由填 8 个字段，想换个模型就得
整份抄一遍，抄漏一个就静默漂移（实测软件部漏了 thinking_control，用着思考模型
apex-quality 却继承了激活条目的 'none'，思考根本没关）。改成按条目取整份配置后，
这类漂移从根上消失——本文件用 `test_no_leak_from_active_entry` 专门锁住它。

覆盖：
- merge_department_llm 纯函数：按名取整份条目 / 名字失效回退全局 / 温度与
  Token 微调 / 未选=纯全局 / 脏数据容错 / 全局缺字段补 None / 条目名不进配置
- _merge_legacy_config 纯函数：旧 chat_config 列回退（读取兼容，不搬迁数据）
- chat 组装集成：部门选的条目生效于部门成员；未选 → 全局；超管不受影响
- 接口：dept_admin 选条目（全局 profile 不变）/ llm_options 下发但**不含
  连接信息与密钥** / 白名单外字段 400 / user 读 200 写 403
"""
from __future__ import annotations

import json

from backend.config import get_active_config
from backend.services.department_service import _merge_legacy_config
from backend.services.settings.service import merge_department_llm
from conftest import create_department_and_admin, create_kb, create_user, \
    upload_and_ingest

# ==================== 合并函数纯函数单测 ====================

# 全局模型列表里的两个条目：字段刻意配得**处处不同**，任何"从激活条目继承"
# 都会被抓出来（甲是激活的，乙是部门要选的）
MODEL_A = {"name": "模型甲", "base_url": "http://a.example/v1",
           "api_key": "sk-entry-a", "model": "qwen-a", "temperature": 0.7,
           "max_tokens": 4096, "timeout": 60.0, "top_p": 0.9,
           "thinking_control": "api"}
MODEL_B = {"name": "模型乙", "base_url": "http://b.example/v1",
           "api_key": "sk-entry-b", "model": "qwen-b", "temperature": 0.3,
           "max_tokens": 8192, "timeout": 120.0, "top_p": 0.8,
           "thinking_control": "prefill"}
GLOBAL_MODELS = [MODEL_A, MODEL_B]
# 全局激活条目（甲）——不带 name（它是显示名，不进 LLM 配置）
GLOBAL_LLM = {k: v for k, v in MODEL_A.items() if k != "name"}

_ENTRY_FIELDS = ("base_url", "api_key", "model", "temperature", "max_tokens",
                 "timeout", "top_p", "thinking_control")


class TestMergeDepartmentLlm:
    """merge_department_llm：按条目名取整份配置（不再逐字段覆盖）"""

    def test_picks_whole_entry_by_name(self):
        """★ 部门选了乙 → 拿到乙的**完整**配置"""
        merged = merge_department_llm(GLOBAL_LLM, {"model": "模型乙"},
                                      GLOBAL_MODELS)
        for k in _ENTRY_FIELDS:
            assert merged[k] == MODEL_B[k], f"{k} 应来自选中的条目乙"

    def test_no_leak_from_active_entry(self):
        """★ 回归：漏配的模型级参数不再从**激活条目**继承（原漂移 bug）

        曾经的情形：部门手抄了 base_url/model/温度/Token/超时，但没抄
        thinking_control 与 top_p——这两个就被激活条目（甲，api/0.9）填了，
        而部门用的其实是乙（prefill/0.8）。思考控制错了，模型性能直接受影响。
        """
        merged = merge_department_llm(GLOBAL_LLM, {"model": "模型乙"},
                                      GLOBAL_MODELS)
        assert merged["thinking_control"] == "prefill", \
            "乙是思考模型要 prefill，绝不能继承激活条目的 'api'"
        assert merged["top_p"] == 0.8, "也不能继承激活条目的 0.9"

    def test_same_as_entry_even_when_dept_lists_some_fields(self):
        """部门只写条目名时，结果就等于那条目本身（不多不少）"""
        merged = merge_department_llm(GLOBAL_LLM, {"model": "模型乙"},
                                      GLOBAL_MODELS)
        assert merged == {k: v for k, v in MODEL_B.items() if k != "name"}

    def test_tunable_fields_override(self):
        """温度 / 最大 Token 可在选中条目之上微调（其余仍来自条目）"""
        merged = merge_department_llm(
            GLOBAL_LLM,
            {"model": "模型乙", "temperature": 0.05, "max_tokens": 256},
            GLOBAL_MODELS)
        assert merged["temperature"] == 0.05
        assert merged["max_tokens"] == 256
        assert merged["base_url"] == MODEL_B["base_url"], "其余不跟着变"
        assert merged["thinking_control"] == "prefill"

    def test_tunable_null_follows_entry(self):
        """微调字段传 None/空串 → 跟随条目"""
        merged = merge_department_llm(
            GLOBAL_LLM, {"model": "模型乙", "temperature": None,
                         "max_tokens": ""}, GLOBAL_MODELS)
        assert merged["temperature"] == MODEL_B["temperature"]
        assert merged["max_tokens"] == MODEL_B["max_tokens"]

    def test_entry_name_not_leaked_into_config(self):
        """条目名（显示名）只给部门看，不进 LLM 配置"""
        merged = merge_department_llm(GLOBAL_LLM, {"model": "模型乙"},
                                      GLOBAL_MODELS)
        assert "name" not in merged

    def test_deleted_entry_falls_back_to_global(self):
        """★ 选的条目被超管删掉/改名 → 回退全局激活条目（不断服务）"""
        merged = merge_department_llm(GLOBAL_LLM, {"model": "已被删掉的条目"},
                                      GLOBAL_MODELS)
        assert merged == dict(GLOBAL_LLM)

    def test_empty_dept_is_pure_global(self):
        """部门未设置（空 dict）→ 纯全局"""
        assert merge_department_llm(GLOBAL_LLM, {}, GLOBAL_MODELS) == \
            dict(GLOBAL_LLM)

    def test_empty_or_none_model_follows_global(self):
        """model 空串/None → 跟随全局（清空选择 = 取消本部门覆盖）"""
        assert merge_department_llm(GLOBAL_LLM, {"model": ""},
                                    GLOBAL_MODELS) == dict(GLOBAL_LLM)
        assert merge_department_llm(GLOBAL_LLM, {"model": None},
                                    GLOBAL_MODELS) == dict(GLOBAL_LLM)

    def test_no_models_list_falls_back(self):
        """调用方没给模型列表 → 安全回退全局（不抛错）"""
        assert merge_department_llm(GLOBAL_LLM, {"model": "模型乙"}) == \
            dict(GLOBAL_LLM)
        assert merge_department_llm(GLOBAL_LLM, {"model": "模型乙"}, []) == \
            dict(GLOBAL_LLM)

    def test_non_dict_dept_tolerated(self):
        """部门配置脏数据（非 dict）→ 容错为纯全局"""
        assert merge_department_llm(GLOBAL_LLM, "脏数据", GLOBAL_MODELS) == \
            dict(GLOBAL_LLM)
        assert merge_department_llm(GLOBAL_LLM, None, GLOBAL_MODELS) == \
            dict(GLOBAL_LLM)

    def test_global_missing_field_kept_none(self):
        """全局缺字段（异常数据防御）→ 保持 None，不报错"""
        merged = merge_department_llm({"base_url": "http://x/v1"}, {},
                                      GLOBAL_MODELS)
        assert merged["base_url"] == "http://x/v1"
        assert merged["api_key"] is None

    def test_global_extra_field_passthrough(self):
        """全局侧的非白名单字段原样透传（不被合并悄悄丢掉）"""
        merged = merge_department_llm(
            {**GLOBAL_LLM, "custom_field": "keep-me"}, {}, GLOBAL_MODELS)
        assert merged["custom_field"] == "keep-me"


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
            {"llm": {"model": "条目甲"}},
            {"chat": {"system_prompt": "旧提示词"}})
        assert merged["llm"]["model"] == "条目甲"
        assert merged["chat"]["system_prompt"] == "旧提示词"


# ==================== chat 组装集成（部门选中条目全链路生效） ====================

def _dept_env(client, admin_headers):
    """建部门 + 部门管理员 + 部门普通用户 + 部门知识库（已入库）"""
    dept_id, dept_admin_hdrs = create_department_and_admin(
        client, admin_headers, "部门LLM部", "dept_llm_admin",
        "pass123456", "LLM主管")
    user_hdrs = create_user(client, admin_headers, dept_id, "dept_llm_member")
    kb = create_kb(client, "部门LLM知识库", department_id=dept_id)
    upload_and_ingest(client, kb["id"])
    return dept_admin_hdrs, user_hdrs, kb


def _set_global_models(client, admin_headers, models) -> None:
    """把全局活跃档案的 llm 段设成给定条目列表（部门要从里面选）"""
    pid = client.get("/api/settings/profiles/active",
                     headers=admin_headers).json()["id"]
    resp = client.put(f"/api/settings/profiles/{pid}",
                      json={"llm": {"models": models, "active": 0}},
                      headers=admin_headers)
    assert resp.status_code == 200, resp.text


class TestDeptLlmAssembly:
    """部门成员聊天 → 用**选中条目**的配置；未选/超管 → 全局（互不影响）"""

    def test_dept_user_uses_selected_entry(self, client, admin_headers,
                                           mock_embedding, mock_llm):
        """★ 部门成员流式问答：客户端拿到的是选中条目的完整配置"""
        _set_global_models(client, admin_headers,
                           [dict(MODEL_A), dict(MODEL_B)])
        dept_admin_hdrs, user_hdrs, kb = _dept_env(client, admin_headers)
        resp = client.post("/api/settings/chat", json={
            "llm": {"model": "模型乙"},
        }, headers=dept_admin_hdrs)
        assert resp.status_code == 200, resp.text

        state = mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=user_hdrs)
        assert resp.status_code == 200 and "event: done" in resp.text
        inst = state.instances[0]
        assert inst.llm_cfg["base_url"] == MODEL_B["base_url"]
        assert inst.llm_cfg["api_key"] == MODEL_B["api_key"]
        assert inst.llm_cfg["model"] == MODEL_B["model"]
        assert inst.llm_cfg["thinking_control"] == "prefill", \
            "模型级参数跟着条目走（原漂移 bug 的端到端回归）"
        assert inst.last_kwargs["model"] == MODEL_B["model"], "请求 model 用条目值"
        assert inst.last_kwargs["temperature"] == MODEL_B["temperature"]

    def test_dept_tunable_applied(self, client, admin_headers, mock_embedding,
                                  mock_llm):
        """部门微调温度 → 请求参数用微调值，其余仍来自条目"""
        _set_global_models(client, admin_headers,
                           [dict(MODEL_A), dict(MODEL_B)])
        dept_admin_hdrs, user_hdrs, kb = _dept_env(client, admin_headers)
        client.post("/api/settings/chat", json={
            "llm": {"model": "模型乙", "temperature": 0.11},
        }, headers=dept_admin_hdrs)
        state = mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=user_hdrs)
        assert resp.status_code == 200 and "event: done" in resp.text
        inst = state.instances[0]
        assert inst.last_kwargs["temperature"] == 0.11
        assert inst.llm_cfg["model"] == MODEL_B["model"], "模型仍来自条目"

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
        """部门选了条目时，超管聊天仍用全局（互不干扰）"""
        _set_global_models(client, admin_headers,
                           [dict(MODEL_A), dict(MODEL_B)])
        dept_admin_hdrs, _, kb = _dept_env(client, admin_headers)
        client.post("/api/settings/chat", json={"llm": {"model": "模型乙"}},
                    headers=dept_admin_hdrs)
        state = mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        assert state.instances[0].llm_cfg["model"] == \
            get_active_config().llm.model, "超管不应使用部门选的条目"


# ==================== 接口：白名单 + 角色分流 + llm_options 下发 ====================

class TestDeptLlmApi:
    """GET 下发 llm_options；POST 按角色分流；白名单收紧到条目名 + 两项微调"""

    def test_dept_admin_selects_entry_global_untouched(
            self, client, admin_headers, dept_admin_headers, user_headers):
        """★ dept_admin 选条目 → 只写本部门，全局 profile 不动"""
        _set_global_models(client, admin_headers,
                           [dict(MODEL_A), dict(MODEL_B)])
        global_model = get_active_config().llm.model
        resp = client.post("/api/settings/chat", json={"llm": {"model": "模型乙"}},
                           headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["dept"]["llm"]["model"] == "模型乙", "部门存的是条目名"
        assert data["llm"]["model"] == MODEL_B["model"], "合并值是条目里的模型名"
        assert get_active_config().llm.model == global_model, "全局未被触碰"
        # 超管视角 GET → 仍是全局（dept=None）
        gdata = client.get("/api/settings/chat", headers=admin_headers).json()
        assert gdata["dept"] is None
        # 同部门普通用户 GET → 合并值
        udata = client.get("/api/settings/chat", headers=user_headers).json()
        assert udata["llm"]["model"] == MODEL_B["model"]

    def test_llm_options_downloaded_without_secrets(self, client,
                                                    admin_headers,
                                                    dept_admin_headers):
        """★ 下发的 llm_options 只有名字与模型名——连接信息与密钥不下放"""
        _set_global_models(client, admin_headers,
                           [dict(MODEL_A), dict(MODEL_B)])
        data = client.get("/api/settings/chat",
                          headers=dept_admin_headers).json()
        opts = data["llm_options"]
        assert [o["name"] for o in opts] == ["模型甲", "模型乙"]
        assert opts[0]["model"] == "qwen-a"
        blob = json.dumps(opts, ensure_ascii=False)
        assert "sk-entry-a" not in blob, "密钥绝不下放"
        assert "http://a.example" not in blob, "连接地址不下放"

    def test_connection_fields_rejected(self, client, admin_headers,
                                        dept_admin_headers):
        """★ 白名单已收紧：部门再提交 base_url/api_key/timeout → 400"""
        _set_global_models(client, admin_headers,
                           [dict(MODEL_A), dict(MODEL_B)])
        for bad in ({"base_url": "http://evil.example/v1"},
                    {"api_key": "sk-evil"},
                    {"timeout": 5},
                    {"thinking_control": "none"},
                    {"top_p": 0.1}):
            resp = client.post("/api/settings/chat",
                               json={"llm": {**bad, "model": "模型乙"}},
                               headers=dept_admin_headers)
            assert resp.status_code == 400, \
                f"{list(bad)} 应被白名单拒绝，实际 {resp.status_code}"

    def test_clear_selection_follows_global(self, client, admin_headers,
                                            dept_admin_headers, user_headers):
        """清空选择 → 部门 llm 段移除（dept=None）→ 跟随全局"""
        _set_global_models(client, admin_headers,
                           [dict(MODEL_A), dict(MODEL_B)])
        client.post("/api/settings/chat", json={"llm": {"model": "模型乙"}},
                    headers=dept_admin_headers)
        resp = client.post("/api/settings/chat", json={
            "llm": {"model": "", "temperature": None, "max_tokens": None},
        }, headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["dept"] is None, "全部清空 → 部门配置置空"
        merged = client.get("/api/settings/chat", headers=user_headers).json()
        assert merged["llm"]["model"] == get_active_config().llm.model

    def test_deleted_entry_falls_back_end_to_end(self, client, admin_headers,
                                                 dept_admin_headers,
                                                 user_headers):
        """★ 部门选完条目后，超管把该条目删掉 → 该部门回退全局（不断服务）"""
        _set_global_models(client, admin_headers,
                           [dict(MODEL_A), dict(MODEL_B)])
        client.post("/api/settings/chat", json={"llm": {"model": "模型乙"}},
                    headers=dept_admin_headers)
        _set_global_models(client, admin_headers, [dict(MODEL_A)])  # 删掉乙
        merged = client.get("/api/settings/chat", headers=user_headers).json()
        assert merged["llm"]["model"] == MODEL_A["model"], "回退到全局激活条目"

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
        """user GET 读合并值；user POST llm → 404 伪装（配置仍为原值）"""
        _set_global_models(client, admin_headers,
                           [dict(MODEL_A), dict(MODEL_B)])
        client.post("/api/settings/chat", json={"llm": {"model": "模型乙"}},
                    headers=dept_admin_headers)
        data = client.get("/api/settings/chat", headers=user_headers).json()
        assert data["llm"]["model"] == MODEL_B["model"], "普通成员读合并值"
        global_model = get_active_config().llm.model
        resp = client.post("/api/settings/chat",
                           json={"llm": {"model": "模型甲"}},
                           headers=user_headers)
        assert resp.status_code == 404
        assert get_active_config().llm.model == global_model
