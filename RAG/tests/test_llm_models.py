"""LLM 多模型管理测试：存量迁移 / 模型 CRUD 校验 / 激活语义 / 连接测试 / 部门兼容

覆盖：
- 存量迁移：旧单对象 llm 段 → models[0]+active=0（_coerce 与真实加载路径，
  缺失字段补 .env 默认；timeout 进入条目并生效）
- 模型 CRUD：添加（整体替换）/编辑/删除（至少保留 1 个）/
  校验（name 唯一、base_url/model 必填、active 索引合法、models 必须数组）
- 激活语义：active 切换 → get_active_config().llm 即时变为新模型
  （问答等所有 LLM 场景用激活模型，通过 chat 流式 llm_cfg 断言）
- 连接测试：POST /api/settings/llm/test（GET {base_url}/models 成功/失败，
  返回 {ok, reason, latency_ms}）；档案级连接测试取激活条目
- 部门兼容：部门 llm 字段覆盖基于全局**激活模型**；未设置跟随全局激活模型
- 兼容：/api/settings/chat 超管标量提交 = 修改激活模型条目（多模型场景）
全部离线（httpx/OpenAI 客户端 mock）。
"""
from __future__ import annotations

import json

import httpx
import pytest
from types import SimpleNamespace

from backend.config import build_default_config, get_active_config
from backend.services import settings_service as ss
from backend.services.settings_service import SettingsService, \
    active_llm_item


def _item(name="模型A", model="qwen-a", base_url="http://m.example/v1",
          api_key="sk-abc1234567890", temperature=0.3, max_tokens=1024,
          timeout=60.0):
    """完整模型条目（7 字段）"""
    return {"name": name, "base_url": base_url, "api_key": api_key,
            "model": model, "temperature": temperature,
            "max_tokens": max_tokens, "timeout": timeout}


def _create_profile(client, headers, **sections):
    body = {"name": "多模型档案"}
    body.update(sections)
    resp = client.post("/api/settings/profiles", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ==================== 1. 存量迁移（旧单对象 → models[0]+active=0） ====================

class TestLegacyMigration:

    def test_coerce_legacy_single_object(self):
        """旧单对象 → models[0]（name="默认"）+ active=0；缺失字段补 .env 默认"""
        out = SettingsService._coerce({
            "llm": {"base_url": "http://old.example/v1", "api_key": "sk-old",
                    "model": "old-model", "temperature": 0.5,
                    "max_tokens": 2048},
        })
        assert out["llm"]["active"] == 0
        m = out["llm"]["models"][0]
        assert m["name"] == "默认"
        assert m["base_url"] == "http://old.example/v1"
        assert m["api_key"] == "sk-old"
        assert m["model"] == "old-model"
        assert m["temperature"] == 0.5
        assert m["max_tokens"] == 2048
        # 旧档案无 timeout → 补 .env 出厂默认（条目是完整配置，timeout 生效）
        assert m["timeout"] == build_default_config().llm.timeout

    def test_coerce_legacy_minimal_fields(self):
        """旧结构只传部分字段 → 其余字段补默认，条目始终完整"""
        out = SettingsService._coerce({"llm": {"model": "m1"}})
        m = out["llm"]["models"][0]
        assert m["model"] == "m1"
        assert m["base_url"] == build_default_config().llm.base_url
        assert m["api_key"] == build_default_config().llm.api_key

    def test_coerce_models_structure_normalizes(self):
        """新结构输入：条目类型规范化 + 缺失字段补默认，不迁移"""
        out = SettingsService._coerce({
            "llm": {"models": [{"name": "本地", "model": "m1",
                                "temperature": "0.6", "max_tokens": "512"}],
                    "active": "0"},
        })
        assert out["llm"]["active"] == 0
        m = out["llm"]["models"][0]
        assert m["name"] == "本地"
        assert m["temperature"] == 0.6
        assert m["max_tokens"] == 512
        assert m["base_url"] == build_default_config().llm.base_url

    def test_legacy_load_migrates_and_applies(self, monkeypatch):
        """真实加载路径：旧格式 settings.json → 服务启动自动迁移并生效"""
        old = {"profiles": [{"id": "p1", "name": "旧档案", "active": True,
                             "llm": {"base_url": "http://old/v1",
                                     "api_key": "sk-old-key",
                                     "model": "legacy-model",
                                     "temperature": 0.7,
                                     "max_tokens": 2048}}],
               "active_id": "p1"}
        ss.SETTINGS_FILE.write_text(json.dumps(old, ensure_ascii=False))
        svc = SettingsService()
        active = svc.get_active()
        assert active["llm"]["models"][0]["model"] == "legacy-model"
        assert active["llm"]["active"] == 0
        # 迁移后立即应用到全局活跃配置（get_active_config().llm = 迁移模型）
        assert get_active_config().llm.model == "legacy-model"
        assert get_active_config().llm.timeout == \
            build_default_config().llm.timeout
        # 迁移结果写回磁盘（存量文件升级）
        saved = json.loads(ss.SETTINGS_FILE.read_text(encoding="utf-8"))
        assert saved["profiles"][0]["llm"]["models"][0]["model"] == \
            "legacy-model"

    def test_active_llm_item_helper(self):
        """active_llm_item：取激活条目；异常数据 → {}"""
        llm = {"models": [_item("A", "m-a"), _item("B", "m-b")], "active": 1}
        assert active_llm_item(llm)["model"] == "m-b"
        assert active_llm_item({"models": []}) == {}
        assert active_llm_item({"models": [_item("A")], "active": 9}) == {}
        assert active_llm_item(None) == {}
        assert active_llm_item("脏数据") == {}


# ==================== 2. 模型 CRUD（添加/编辑/删除/校验） ====================

class TestModelCrud:

    def test_create_profile_with_models(self, client, admin_headers):
        """创建档案：models 数组 + active 索引 → 保存并脱敏"""
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("A", "m-a"),
                                            _item("B", "m-b")], "active": 1})
        llm = p["llm"]
        assert len(llm["models"]) == 2
        assert llm["active"] == 1
        assert llm["models"][0]["api_key"] == "sk-a****7890", "条目 api_key 脱敏"

    def test_update_replaces_models(self, client, admin_headers):
        """PUT 整体替换 models（新列表覆盖旧列表）"""
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("A", "m-a"),
                                            _item("B", "m-b")], "active": 1})
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"models": [_item("C", "m-c"),
                                                   _item("D", "m-d")],
                                        "active": 1}},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        llm = resp.json()["llm"]
        assert [m["model"] for m in llm["models"]] == ["m-c", "m-d"]
        assert llm["active"] == 1

    def test_update_edit_single_field_keeps_others(self, client,
                                                   admin_headers):
        """编辑条目单个字段（同索引脱敏回传）→ 其余字段继承原条目"""
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("A", "m-a")], "active": 0})
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"models": [
                              {"name": "A", "base_url": "http://m.example/v1",
                               "model": "m-a-new",
                               "api_key": "sk-a****7890"}]}},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        raw = client.get("/api/settings/profiles/active",
                         headers=admin_headers)
        # 用档案原值断言（脱敏回传保留原 key、未提交字段继承）
        svc = ss.get_settings_service()
        saved = svc.get_profile(p["id"])
        m = saved["llm"]["models"][0]
        assert m["model"] == "m-a-new"
        assert m["api_key"] == "sk-abc1234567890", "脱敏回传保留原值"
        assert m["temperature"] == 0.3, "未提交字段继承原条目"
        assert m["timeout"] == 60.0

    def test_switch_active_only(self, client, admin_headers):
        """只提交 active → 列表不变，仅切换激活"""
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("A", "m-a"),
                                            _item("B", "m-b")], "active": 0})
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"active": 1}}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["llm"]["active"] == 1
        assert len(resp.json()["llm"]["models"]) == 2

    # ---- 校验：非法输入一律 400（不落库） ----

    def test_models_must_be_list(self, client, admin_headers):
        p = _create_profile(client, admin_headers)
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"models": "not-a-list"}},
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "模型列表格式错误" in resp.json()["detail"]

    def test_empty_models_rejected(self, client, admin_headers):
        """至少保留 1 个模型（删除最后一个被拒绝）"""
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("A", "m-a")], "active": 0})
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"models": []}},
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "至少保留一个模型" in resp.json()["detail"]
        # 列表未被破坏
        assert len(client.get("/api/settings/profiles/active",
                              headers=admin_headers).json()["llm"]["models"]) \
            == 1

    def test_duplicate_name_rejected(self, client, admin_headers):
        p = _create_profile(client, admin_headers)
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"models": [_item("同", "m1"),
                                                   _item("同", "m2")]}},
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "模型名称不能重复" in resp.json()["detail"]

    def test_missing_name_rejected(self, client, admin_headers):
        p = _create_profile(client, admin_headers)
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"models": [{"model": "m1"}]}},
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "模型名称不能为空" in resp.json()["detail"]

    def test_missing_base_url_rejected(self, client, admin_headers):
        p = _create_profile(client, admin_headers)
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"models": [{"name": "A",
                                                    "model": "m1",
                                                    "base_url": "  "}]}},
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "API 地址不能为空" in resp.json()["detail"]

    def test_missing_model_id_rejected(self, client, admin_headers):
        p = _create_profile(client, admin_headers)
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"models": [
                              {"name": "A", "base_url": "http://x/v1"}]}},
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "模型标识不能为空" in resp.json()["detail"]

    @pytest.mark.parametrize("active", [-1, 5, "abc", None])
    def test_invalid_active_rejected(self, client, admin_headers, active):
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("A", "m-a")], "active": 0})
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"active": active}},
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "激活" in resp.json()["detail"]
        # 原激活索引未被破坏
        assert client.get("/api/settings/profiles/active",
                          headers=admin_headers).json()["llm"]["active"] == 0


# ==================== 3. 激活语义（激活模型用于所有 LLM 场景） ====================

class TestActiveSemantics:

    def test_active_switch_changes_active_config(self, client, admin_headers):
        """同档案内切换 active → get_active_config().llm 即时变为新模型"""
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("本地", "qwen-local"),
                                            _item("云端", "deepseek-cloud")],
                                 "active": 0})
        client.post(f"/api/settings/profiles/{p['id']}/activate",
                    headers=admin_headers)
        assert get_active_config().llm.model == "qwen-local"
        resp = client.put(f"/api/settings/profiles/{p['id']}",
                          json={"llm": {"active": 1}},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert get_active_config().llm.model == "deepseek-cloud", \
            "active 切换即时生效（问答/摘要/Judge 等全部用激活模型）"
        assert get_active_config().llm.timeout == 60.0, "条目 timeout 生效"

    def test_chat_uses_active_model(self, client, admin_headers,
                                    mock_embedding, mock_llm):
        """流式问答：_get_client 收到激活模型配置，请求用激活模型"""
        from conftest import create_kb, upload_and_ingest
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("A", "m-a"),
                                            _item("B", "m-b",
                                                  api_key="sk-bbb2222222")],
                                 "active": 1})
        client.post(f"/api/settings/profiles/{p['id']}/activate",
                    headers=admin_headers)
        kb = create_kb(client, "激活模型知识库")
        upload_and_ingest(client, kb["id"])
        state = mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        inst = state.instances[0]
        assert inst.llm_cfg["model"] == "m-b", "激活模型 B 用于问答"
        assert inst.llm_cfg["api_key"] == "sk-bbb2222222"
        assert inst.last_kwargs["model"] == "m-b"

    def test_active_model_survives_reload(self, client, admin_headers):
        """激活选择持久化：重建服务后激活模型不变"""
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("A", "m-a"),
                                            _item("B", "m-b")], "active": 1})
        client.post(f"/api/settings/profiles/{p['id']}/activate",
                    headers=admin_headers)
        # 重建 SettingsService（模拟重启）→ 从磁盘加载
        svc = SettingsService()
        assert svc.get_active()["llm"]["active"] == 1
        assert get_active_config().llm.model == "m-b"


# ==================== 4. 连接测试（勾选激活前置探测 + 档案级测试取激活条目） ====================

class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def json(self):
        return {}


class _FakeAsyncClient:
    """mock httpx.AsyncClient：base_url 含 'bad' → 抛连接异常；500 → 失败"""

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        if "bad" in url:
            raise httpx.ConnectError("mock: 连接失败")
        if "500" in url:
            return _FakeResponse(500)
        return _FakeResponse(200)


def _patch_llm_http(monkeypatch):
    fake = _FakeAsyncClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=None: fake)
    return fake


class TestLlmConnectionTest:

    def test_llm_test_endpoint_ok(self, client, admin_headers, monkeypatch):
        """POST /api/settings/llm/test：GET {base_url}/models 2xx → ok=True"""
        fake = _patch_llm_http(monkeypatch)
        resp = client.post("/api/settings/llm/test",
                           json=_item("A", "m-a"), headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert "连接成功" in data["reason"]
        assert data["latency_ms"] >= 0
        # 探测打到 {base_url}/models（OpenAI 兼容端点）并携带认证头
        assert fake.calls and fake.calls[0][0] == "get"
        assert fake.calls[0][1].endswith("/models")

    def test_llm_test_endpoint_http_error(self, client, admin_headers,
                                          monkeypatch):
        """GET /models 5xx → ok=False + 原因（服务异常）"""
        _patch_llm_http(monkeypatch)
        resp = client.post("/api/settings/llm/test",
                           json=_item("A", "m-a",
                                      base_url="http://500.example/v1"),
                           headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "服务异常" in data["reason"]

    def test_llm_test_endpoint_conn_error(self, client, admin_headers,
                                          monkeypatch):
        """连接失败 → ok=False + 原因（供前端勾选激活时提示）"""
        _patch_llm_http(monkeypatch)
        resp = client.post("/api/settings/llm/test",
                           json=_item("A", "m-a",
                                      base_url="http://bad.example/v1"),
                           headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "连接失败" in data["reason"]
        assert data["latency_ms"] >= 0

    def test_llm_test_endpoint_missing_base_url(self, client, admin_headers,
                                                monkeypatch):
        """未配置 base_url → ok=False（不发起网络请求）"""
        fake = _patch_llm_http(monkeypatch)
        resp = client.post("/api/settings/llm/test",
                           json={"model": "m-a"}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "未配置" in resp.json()["reason"]
        assert not fake.calls, "无 base_url 不应发起请求"

    def test_llm_test_requires_admin(self, client, admin_headers,
                                     user_headers):
        """user 调 /llm/test → 403（管理员专用）"""
        resp = client.post("/api/settings/llm/test",
                           json=_item("A"), headers=user_headers)
        assert resp.status_code == 403

    def test_profile_test_uses_active_item(self, client, admin_headers,
                                           monkeypatch):
        """档案级连接测试：测的是激活模型条目（非激活模型坏连接不影响）"""
        from backend.services.settings_service import OpenAI

        class _FakeOpenAI:
            def __init__(self, base_url="", api_key="", timeout=5.0):
                self._base_url = base_url or ""

            @property
            def chat(self):
                return self

            @property
            def completions(self):
                return self

            def create(self, **kw):
                if "bad" in self._base_url:
                    raise ConnectionError("mock: LLM 连接失败")
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="hi"))])

        monkeypatch.setattr("backend.services.settings_service.OpenAI",
                            _FakeOpenAI)
        p = _create_profile(
            client, admin_headers,
            llm={"models": [_item("坏", "m-bad", base_url="http://bad:1/v1"),
                            _item("好", "m-good",
                                  base_url="http://good:1234/v1")],
                 "active": 1})
        resp = client.post(f"/api/settings/profiles/{p['id']}/test",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["llm"]["ok"] is True, \
            "连接测试取激活条目（models[1] 好模型），坏模型不影响"
        # 切回激活 0（坏模型）→ 失败
        client.put(f"/api/settings/profiles/{p['id']}",
                   json={"llm": {"active": 0}}, headers=admin_headers)
        resp = client.post(f"/api/settings/profiles/{p['id']}/test",
                           headers=admin_headers)
        assert resp.json()["llm"]["ok"] is False


# ==================== 5. 部门兼容（字段覆盖基于全局激活模型） ====================

class TestDeptCompatibility:

    def _setup(self, client, admin_headers):
        """全局两模型 + 激活 B；返回 (p, dept_admin_headers, user_headers)"""
        from conftest import create_department_and_admin, create_user
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("A", "m-a"),
                                            _item("B", "m-b",
                                                  api_key="sk-bbb2222222")],
                                 "active": 1})
        client.post(f"/api/settings/profiles/{p['id']}/activate",
                    headers=admin_headers)
        dept_id, dept_admin_hdrs = create_department_and_admin(
            client, admin_headers, "LLM多模型部", "multi_admin",
            "pass123456", "多模型主管")
        user_hdrs = create_user(client, admin_headers, dept_id,
                                "multi_member")
        return dept_admin_hdrs, user_hdrs

    def test_dept_unset_follows_active_model(self, client, admin_headers,
                                             dept_admin_headers,
                                             user_headers):
        """部门未设置 → 合并配置 = 全局**激活模型**（B）"""
        _, user_hdrs = self._setup(client, admin_headers)
        data = client.get("/api/settings/chat", headers=user_hdrs).json()
        assert data["llm"]["model"] == "m-b", "部门跟随激活模型 B"
        assert data["dept"] is None

    def test_dept_override_active_model_fields(self, client, admin_headers,
                                               dept_admin_headers,
                                               user_headers):
        """部门字段覆盖基于激活模型：base_url 覆盖、model 仍为激活模型 B"""
        dept_admin_hdrs, user_hdrs = self._setup(client, admin_headers)
        resp = client.post("/api/settings/chat", json={
            "llm": {"base_url": "http://dept-llm.example/v1",
                    "api_key": "sk-dept-key-12345"},
        }, headers=dept_admin_hdrs)
        assert resp.status_code == 200, resp.text
        merged = client.get("/api/settings/chat", headers=user_hdrs).json()
        assert merged["llm"]["base_url"] == "http://dept-llm.example/v1"
        assert merged["llm"]["model"] == "m-b", "未覆盖字段=激活模型"
        assert merged["llm"]["api_key"] != "sk-dept-key-12345", "绝不返回明文"

    def test_super_admin_chat_llm_edits_active_item(self, client,
                                                    admin_headers):
        """/api/settings/chat 超管标量提交 = 修改**激活模型**条目（其余模型不变）"""
        _, _ = self._setup(client, admin_headers)
        resp = client.post("/api/settings/chat", json={
            "llm": {"model": "m-b-v2", "temperature": 0.15},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["llm"]["model"] == "m-b-v2"
        assert get_active_config().llm.model == "m-b-v2", "即时生效"
        # 列表其余模型未被触碰（models[0] 仍是 A）
        svc = ss.get_settings_service()
        saved = svc.get_active()
        assert saved["llm"]["models"][0]["model"] == "m-a"
        assert saved["llm"]["models"][1]["model"] == "m-b-v2"
        assert saved["llm"]["active"] == 1
