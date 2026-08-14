"""系统配置档案 API 测试：多档案 CRUD / api_key 脱敏 / 激活即时生效 / 连接测试

覆盖：创建（空名 400）、脱敏回传保留原值、activate 后
config.get_active_config() 立即变化、删除活跃档案后自动激活剩余、
连接测试接口（mock 网络：LLM/Embedding 成功与失败路径、MinerU 失败路径，
均返回 HTTP 200 而非异常）。全部离线。
权限：写操作仅 super_admin 可访问（全部带 admin_headers）；读取类接口对
super_admin / dept_admin 放开（见 TestSettingsReadOnlyForDeptAdmin）。
"""
from __future__ import annotations

import httpx
import pytest
from types import SimpleNamespace

from backend.config import get_active_config


def create_profile(client, name="测试档案", headers=None, **sections):
    """创建配置档案，返回 public dict（默认 admin 登录态）"""
    body = {"name": name}
    body.update(sections)
    resp = client.post("/api/settings/profiles", json=body,
                       headers=headers if headers is not None else {})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestProfileCRUD:
    """配置档案增删改查"""

    def test_create_and_list(self, client, admin_headers):
        profile = create_profile(client, name="我的档案", headers=admin_headers)
        assert profile["id"]
        assert profile["name"] == "我的档案"
        # 缺省字段已用 .env 出厂值补齐；llm 段为模型列表（单条目 + active=0）
        assert profile["llm"]["models"][0]["base_url"]
        assert profile["llm"]["models"][0]["model"]
        assert profile["llm"]["active"] == 0
        assert profile["embedding"]["model"]
        assert profile["retrieval"]["top_k"] >= 1
        # 列表：新档案非活跃，且始终存在一个活跃档案
        items = client.get("/api/settings/profiles",
                           headers=admin_headers).json()
        mine = next(p for p in items if p["id"] == profile["id"])
        assert mine["name"] == "我的档案"
        assert mine["active"] is False
        assert any(p["active"] for p in items), "应存在活跃档案"

    def test_create_empty_name_400(self, client, admin_headers):
        resp = client.post("/api/settings/profiles", json={"name": "   "},
                           headers=admin_headers)
        assert resp.status_code == 400

    def test_get_active_default(self, client, admin_headers):
        """默认配置档案在系统初始化时自动创建并激活"""
        resp = client.get("/api/settings/profiles/active",
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "默认配置"

    def test_update_profile(self, client, admin_headers):
        profile = create_profile(client, name="旧名", headers=admin_headers)
        resp = client.put(
            f"/api/settings/profiles/{profile['id']}",
            json={"name": "新名", "llm": {"model": "updated-model"}},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "新名"
        # 旧标量提交 = 修改激活模型条目（models[0]）
        assert data["llm"]["models"][0]["model"] == "updated-model"

    def test_update_unknown_404(self, client, admin_headers):
        resp = client.put("/api/settings/profiles/nonexist",
                          json={"name": "x"}, headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_profile(self, client, admin_headers):
        profile = create_profile(client, headers=admin_headers)
        assert client.delete(
            f"/api/settings/profiles/{profile['id']}",
            headers=admin_headers).status_code == 200
        assert client.delete(
            f"/api/settings/profiles/{profile['id']}",
            headers=admin_headers).status_code == 404


class TestApiKeyMasking:
    """api_key 脱敏与保留"""

    def test_create_masks_api_key(self, client, admin_headers):
        """创建含 api_key 的档案，回传必须脱敏（前4****后4）"""
        profile = create_profile(
            client, name="带密钥", headers=admin_headers,
            llm={"base_url": "http://example.com/v1",
                 "api_key": "sk-abcdef1234567890"},
        )
        masked = profile["llm"]["models"][0]["api_key"]
        assert masked == "sk-a****7890"
        assert "****" in masked
        assert "abcdef" not in masked

    def test_update_masked_key_keeps_original(self, client, admin_headers):
        """PUT 回传脱敏值 → 原值保留；激活后 get_active_config 可见原值"""
        profile = create_profile(
            client, name="密钥档案", headers=admin_headers,
            llm={"base_url": "http://example.com/v1",
                 "api_key": "sk-abcdef1234567890", "model": "m1"},
        )
        resp = client.put(
            f"/api/settings/profiles/{profile['id']}",
            json={"llm": {"api_key": "sk-a****7890", "model": "m2"}},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        client.post(f"/api/settings/profiles/{profile['id']}/activate",
                    headers=admin_headers)
        cfg = get_active_config()
        assert cfg.llm.api_key == "sk-abcdef1234567890", "脱敏回传不应覆盖原值"
        assert cfg.llm.model == "m2", "非密钥字段正常更新"


class TestActivate:
    """激活即时生效"""

    def test_activate_immediate_effect(self, client, admin_headers):
        """activate 后 get_active_config() 立即变化（无需重启）"""
        profile = create_profile(client, name="即时生效", headers=admin_headers,
                                 llm={"model": "test-model-xyz"})
        resp = client.post(f"/api/settings/profiles/{profile['id']}/activate",
                           headers=admin_headers)
        assert resp.status_code == 200
        assert get_active_config().llm.model == "test-model-xyz"
        # active 接口同步
        active = client.get("/api/settings/profiles/active",
                            headers=admin_headers).json()
        assert active["id"] == profile["id"]

    def test_activate_unknown_404(self, client, admin_headers):
        resp = client.post("/api/settings/profiles/nonexist/activate",
                           headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_active_activates_remaining(self, client, admin_headers):
        """删除活跃档案后自动激活剩余第一个（本系统即默认配置）"""
        items = client.get("/api/settings/profiles",
                           headers=admin_headers).json()
        default_id = next(p["id"] for p in items if p["active"])
        p1 = create_profile(client, name="档案A", headers=admin_headers)
        create_profile(client, name="档案B", headers=admin_headers)
        client.post(f"/api/settings/profiles/{p1['id']}/activate",
                    headers=admin_headers)
        assert client.get("/api/settings/profiles/active",
                          headers=admin_headers).json()["id"] == p1["id"]

        resp = client.delete(f"/api/settings/profiles/{p1['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200
        active = client.get("/api/settings/profiles/active",
                            headers=admin_headers)
        assert active.status_code == 200
        assert active.json()["id"] == default_id, "应自动激活剩余第一个档案"

    def test_delete_all_profiles_no_active(self, client, admin_headers):
        """全部档案删除后 active 接口 404"""
        items = client.get("/api/settings/profiles",
                           headers=admin_headers).json()
        assert items, "至少应有默认配置档案"
        for p in items:
            assert client.delete(
                f"/api/settings/profiles/{p['id']}",
                headers=admin_headers).status_code == 200
        assert client.get("/api/settings/profiles/active",
                          headers=admin_headers).status_code == 404
        assert client.get("/api/settings/profiles",
                          headers=admin_headers).json() == []


# ==================== 连接测试（离线 mock 网络） ====================

class _FakeOpenAI:
    """伪 OpenAI 同步客户端：base_url 含 'bad' 时调用失败，否则成功

    兼容 settings_service 的两种调用形态：
    client.chat.completions.create(messages=[...]) / client.embeddings.create(input=...)
    """

    def __init__(self, base_url="", api_key="", timeout=5.0):
        self._base_url = base_url or ""
        self._api_key = api_key or ""
        self._timeout = timeout

    def _check(self, what):
        if "bad" in self._base_url:
            raise ConnectionError(f"mock: {what} 连接失败")

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    @property
    def embeddings(self):
        return self

    def create(self, **kwargs):
        if "input" in kwargs:
            self._check("Embedding")
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])
        self._check("LLM")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="hi"))])


@pytest.fixture()
def mock_openai(monkeypatch):
    """替换 settings_service 的 OpenAI 客户端（连接测试用，离线）"""
    monkeypatch.setattr("backend.services.settings_service.OpenAI",
                        _FakeOpenAI)


class TestConnectionTest:
    """连接测试接口（无论各服务可用与否，接口都应返回 HTTP 200）"""

    def test_all_ok(self, client, mock_openai, monkeypatch, admin_headers):
        """LLM/Embedding 成功（mock OpenAI）；MinerU 成功（mock httpx.get）；
        DeepDoc 成功（mock httpx.post 返回 HTTP_AUTHORIZATION 头）"""
        profile = create_profile(
            client, name="全通", headers=admin_headers,
            llm={"base_url": "http://good:1234/v1", "model": "m"},
            embedding={"base_url": "http://good:8300/v1", "model": "bge"},
            mineru={"url": "http://good:8001"},
            deepdoc={"base_url": "http://good:9380", "email": "a@b.c",
                     "password": "pw"},
        )
        monkeypatch.setattr(httpx, "get",
                            lambda url, **kw: SimpleNamespace(status_code=200))
        monkeypatch.setattr(
            httpx, "post",
            lambda url, **kw: SimpleNamespace(
                status_code=200,
                headers={"HTTP_AUTHORIZATION": "mock-login-token"}, text=""))
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["llm"]["ok"] is True
        assert data["embedding"]["ok"] is True
        assert data["mineru"]["ok"] is True
        assert data["deepdoc"]["ok"] is True
        assert data["embedding"]["message"].startswith("连接成功")

    def test_all_fail_still_200(self, client, mock_openai, monkeypatch,
                                admin_headers):
        """全部失败仍返回 HTTP 200，且各服务 ok=False、message 含失败信息"""
        profile = create_profile(
            client, name="全断", headers=admin_headers,
            llm={"base_url": "http://bad:1/v1", "model": "m"},
            embedding={"base_url": "http://bad:1/v1", "model": "bge"},
            mineru={"url": "http://bad:1"},
        )

        def fake_get(url, **kw):
            raise ConnectionError("mock: MinerU 不可达")

        def fake_post(url, **kw):
            raise ConnectionError("mock: DeepDoc 不可达")

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           headers=admin_headers)
        assert resp.status_code == 200, "连接测试接口应总是返回 200，而非异常"
        data = resp.json()
        assert data["llm"]["ok"] is False
        assert data["embedding"]["ok"] is False
        assert data["mineru"]["ok"] is False
        assert data["deepdoc"]["ok"] is False
        assert "连接失败" in data["llm"]["message"]
        assert "连接失败" in data["mineru"]["message"]

    def test_mixed_llm_ok_mineru_fail(self, client, mock_openai, monkeypatch,
                                      admin_headers):
        """LLM/Embedding 成功、MinerU/DeepDoc 失败（真实环境最常见形态）"""
        profile = create_profile(
            client, name="混合", headers=admin_headers,
            llm={"base_url": "http://good:1234/v1", "model": "m"},
            embedding={"base_url": "http://good:8300/v1", "model": "bge"},
            mineru={"url": "http://localhost:8001"},
        )

        def fake_get(url, **kw):
            raise ConnectionError("mock: MinerU 未启动")

        def fake_post(url, **kw):
            raise ConnectionError("mock: DeepDoc 未启动")

        monkeypatch.setattr(httpx, "get", fake_get)
        monkeypatch.setattr(httpx, "post", fake_post)
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["llm"]["ok"] is True
        assert data["embedding"]["ok"] is True
        assert data["mineru"]["ok"] is False
        assert data["deepdoc"]["ok"] is False

    def test_test_unknown_profile_404(self, client, admin_headers):
        resp = client.post("/api/settings/profiles/nonexist/test",
                           headers=admin_headers)
        assert resp.status_code == 404


# ==================== dept_admin 只读模式 ====================

class TestSettingsReadOnlyForDeptAdmin:
    """系统配置对 dept_admin 只读：读接口 200、写接口 403

    背景：系统设置菜单对部门管理员开放（查看权限；修改权限保持分级）。
    GET profiles / profiles/active / embedding-dim / 连接测试放开；
    POST/PUT/DELETE profiles、activate 仍仅 super_admin。
    """

    def test_dept_admin_read_profiles_200(self, client, admin_headers,
                                          dept_admin_headers):
        """dept_admin 可查看档案列表（含脱敏密钥）"""
        resp = client.get("/api/settings/profiles",
                          headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()
        assert items, "默认配置档案应可见"
        assert any(p["active"] for p in items)
        assert "****" in items[0]["llm"]["models"][0]["api_key"], "密钥仍需脱敏"

    def test_dept_admin_read_active_200(self, client, admin_headers,
                                        dept_admin_headers):
        """dept_admin 可查看当前活跃档案"""
        resp = client.get("/api/settings/profiles/active",
                          headers=dept_admin_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "默认配置"

    def test_dept_admin_embedding_dim_200(self, client, admin_headers,
                                          dept_admin_headers, mock_embedding):
        """dept_admin 可读 embedding 维度（实测只读探测）"""
        resp = client.get("/api/settings/embedding-dim",
                          headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["dimension"] == 64, "mock_embedding 输出 64 维"
        assert data["ok"] is True

    def test_dept_admin_test_connection_200(self, client, mock_openai,
                                            monkeypatch, admin_headers,
                                            dept_admin_headers):
        """dept_admin 可执行连接测试（只测不写，mock 网络离线）"""
        profile = create_profile(
            client, name="只读测试", headers=admin_headers,
            llm={"base_url": "http://good:1234/v1", "model": "m"},
            embedding={"base_url": "http://good:8300/v1", "model": "bge"},
        )
        monkeypatch.setattr(httpx, "get",
                            lambda url, **kw: SimpleNamespace(status_code=200))
        monkeypatch.setattr(
            httpx, "post",
            lambda url, **kw: SimpleNamespace(
                status_code=200,
                headers={"HTTP_AUTHORIZATION": "mock-login-token"}, text=""))
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["llm"]["ok"] is True

    def test_dept_admin_write_403(self, client, admin_headers,
                                  dept_admin_headers):
        """dept_admin 全部写操作 → 403（创建/更新/删除/激活）"""
        profile = create_profile(client, name="超管档案",
                                 headers=admin_headers)
        # POST 创建
        resp = client.post("/api/settings/profiles",
                           json={"name": "越权档案"}, headers=dept_admin_headers)
        assert resp.status_code == 403
        assert "仅超级管理员" in resp.json()["detail"]
        # PUT 更新
        resp = client.put(f"/api/settings/profiles/{profile['id']}",
                          json={"name": "越权改名"}, headers=dept_admin_headers)
        assert resp.status_code == 403
        # DELETE 删除
        resp = client.delete(f"/api/settings/profiles/{profile['id']}",
                             headers=dept_admin_headers)
        assert resp.status_code == 403
        # POST 激活
        resp = client.post(f"/api/settings/profiles/{profile['id']}/activate",
                           headers=dept_admin_headers)
        assert resp.status_code == 403
        # 配置未被越权改动（档案仍存在且未激活）
        items = client.get("/api/settings/profiles",
                           headers=admin_headers).json()
        assert any(p["id"] == profile["id"] for p in items)
        assert not any(p["id"] == profile["id"] and p["active"]
                       for p in items)

    def test_user_read_403(self, client, admin_headers, dept_admin_headers,
                           user_headers):
        """普通用户读系统配置 → 403（仅管理员开放）"""
        for path in ("/api/settings/profiles", "/api/settings/profiles/active",
                     "/api/settings/embedding-dim"):
            resp = client.get(path, headers=user_headers)
            assert resp.status_code == 403, f"{path} 应对 user 403"
        resp = client.post("/api/settings/profiles/active/test",
                           headers=user_headers)
        assert resp.status_code == 403
