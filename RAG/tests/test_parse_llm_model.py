"""解析配置 LLM 模型选择与切换测试

覆盖：
- find_llm_item / llm_cfg_for_parser：按 name/model 查激活档案模型列表、
  未找到/空标识/无档案 → None（回退激活模型语义）
- 上下文摘要（enrich_chunks）：parse_llm_model 指定模型 → 摘要客户端用指定
  模型配置（base_url/model/api_key 全来自档案条目）；未指定/查不到 → 激活模型
- 知识图谱（build_graph_for_doc）：同上
- 接口：GET /api/settings/llm/models（登录即可读、仅 {name,model}+active、
  不含 api_key/base_url、401）；POST /api/settings/llm/test-model
  （按 name 从档案查完整配置探测：成功/失败/404/400、user 可调用、401）
全部离线（httpx / OpenAI 客户端 mock，不依赖外部网络）。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.chunking.splitter import Chunk
from backend.services import settings_service as ss
from backend.services.contextual_retriever import enrich_chunks
from backend.services.knowledge_graph_service import build_graph_for_doc
from backend.services.settings_service import (find_llm_item,
                                               llm_cfg_for_parser)


def _item(name="模型A", model="m-a", base_url="http://a.example/v1",
          api_key="sk-aaa", temperature=0.3, max_tokens=1024, timeout=60.0):
    """完整模型条目（7 字段，与 test_llm_models._item 同构）"""
    return {"name": name, "base_url": base_url, "api_key": api_key,
            "model": model, "temperature": temperature,
            "max_tokens": max_tokens, "timeout": timeout}


def _setup_multi_models(active=0):
    """把当前激活档案的 llm 段替换为双模型并应用全局配置（测试内直接改服务
    状态，隔离由 conftest 的 reset_services 每测试重建保证）"""
    svc = ss.get_settings_service()
    p = svc.get_active()
    p["llm"] = {"models": [_item("模型A", "m-a", "http://a.example/v1",
                                 "sk-aaa"),
                           _item("模型B", "m-b", "http://b.example/v1",
                                 "sk-bbb")],
                "active": active}
    svc._profiles[p["id"]] = p
    svc._save()
    svc._apply_active()
    return p


def _create_profile(client, headers, **sections):
    body = {"name": "解析模型档案"}
    body.update(sections)
    resp = client.post("/api/settings/profiles", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _mk_chunks(texts) -> list:
    """构造简单 Chunk 列表（偏移连续）"""
    out = []
    pos = 0
    for t in texts:
        out.append(Chunk(text=t, char_start=pos, char_end=pos + len(t)))
        pos += len(t) + 1
    return out


# ==================== 1. find_llm_item / llm_cfg_for_parser ====================

class TestFindLlmItem:

    def test_by_name(self):
        """按 name 命中：返回完整条目（含 api_key，内部使用）"""
        _setup_multi_models(active=0)
        item = find_llm_item("模型B")
        assert item is not None
        assert item["model"] == "m-b"
        assert item["base_url"] == "http://b.example/v1"
        assert item["api_key"] == "sk-bbb"

    def test_by_model_fallback(self):
        """name 未命中 → 按 model 次之匹配"""
        _setup_multi_models()
        item = find_llm_item("m-b")
        assert item is not None and item["name"] == "模型B"

    def test_missing(self):
        _setup_multi_models()
        assert find_llm_item("不存在的模型") is None

    def test_empty_ident(self):
        """空标识（None/空串）→ None（=未指定，调用方回退激活模型）"""
        _setup_multi_models()
        assert find_llm_item("") is None
        assert find_llm_item(None) is None

    def test_no_active_profile(self, monkeypatch):
        """无激活档案 → None（不抛）"""
        _setup_multi_models()
        svc = ss.get_settings_service()
        monkeypatch.setattr(svc, "_active_id", "")
        assert find_llm_item("模型A") is None

    def test_llm_cfg_for_parser(self):
        """解析配置辅助：未指定/查不到 → None；命中 → 完整配置"""
        _setup_multi_models()
        assert llm_cfg_for_parser("") is None
        assert llm_cfg_for_parser(None) is None
        assert llm_cfg_for_parser("不存在") is None
        cfg = llm_cfg_for_parser("模型B")
        assert cfg["model"] == "m-b"
        assert llm_cfg_for_parser("m-b")["name"] == "模型B"

    def test_parser_config_default_field(self):
        """ingestion 默认解析配置含 parse_llm_model（空=激活模型）"""
        from backend.services.ingestion_service import _DEFAULT_PARSER_CONFIG
        assert _DEFAULT_PARSER_CONFIG["parse_llm_model"] == ""

    def test_ingest_request_passes_field_through(self):
        """IngestRequest 模型含 parse_llm_model：resolve 时参数不被 pydantic 丢弃"""
        from backend.models.rag_models import IngestRequest
        req = IngestRequest(contextual_retrieval=True, knowledge_graph=True,
                            parse_llm_model="模型B")
        params = req.model_dump(exclude_none=True)
        assert params["parse_llm_model"] == "模型B"
        assert params["contextual_retrieval"] is True


# ==================== 2. 服务层：指定模型覆盖 / 回退 ====================

_FIXED_EXTRACTION = {
    "entities": [{"name": "Python", "type": "技术", "description": "语言"}],
    "relations": [],
}
_FIXED_EXTRACTION_JSON = json.dumps(_FIXED_EXTRACTION, ensure_ascii=False)


class _RecorderClient:
    """可调用客户端工厂：记录每次 _get_client 收到的 llm_cfg，返回成功响应

    payload: 固定 chat 响应内容（摘要文本或抽取 JSON）"""
    def __init__(self, payload: str):
        self.llm_cfgs = []
        self.payload = payload
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def __call__(self, llm_cfg=None):
        self.llm_cfgs.append(dict(llm_cfg) if llm_cfg else None)
        return self

    async def _create(self, **kwargs):
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=self.payload))])


def _patch_recorder(monkeypatch, module_path, payload):
    """替换模块的 _get_client 为记录器（能捕获 llm_cfg 参数）"""
    fake = _RecorderClient(payload)
    monkeypatch.setattr(f"{module_path}._get_client", fake)
    return fake


class TestEnrichChunksSpecifiedModel:

    def test_specified_model_used(self, monkeypatch):
        """parse_llm_model=模型B → 摘要客户端配置全来自模型B条目"""
        _setup_multi_models(active=0)
        fake = _patch_recorder(monkeypatch,
                               "backend.services.contextual_retriever",
                               "该片段位于文档第二章")
        result = asyncio.run(enrich_chunks(
            _mk_chunks(["片段一", "片段二"]), "文档背景文本",
            {"contextual_retrieval": True, "parse_llm_model": "模型B"}))
        assert len(result) == 2, "指定模型调用成功"
        assert fake.llm_cfgs, "应调用摘要客户端"
        for cfg in fake.llm_cfgs:
            assert cfg["model"] == "m-b"
            assert cfg["base_url"] == "http://b.example/v1"
            assert cfg["api_key"] == "sk-bbb"

    def test_default_active_model(self, monkeypatch):
        """未指定 parse_llm_model → 激活模型（active=0 → 模型A）"""
        _setup_multi_models(active=0)
        fake = _patch_recorder(monkeypatch,
                               "backend.services.contextual_retriever",
                               "该片段位于文档第二章")
        asyncio.run(enrich_chunks(
            _mk_chunks(["片段一"]), "文档背景文本",
            {"contextual_retrieval": True}))
        assert fake.llm_cfgs
        for cfg in fake.llm_cfgs:
            assert cfg["model"] == "m-a"

    def test_missing_falls_back_active(self, monkeypatch):
        """查不到指定模型 → 回退激活模型（active=1 → 模型B），不抛"""
        _setup_multi_models(active=1)
        fake = _patch_recorder(monkeypatch,
                               "backend.services.contextual_retriever",
                               "该片段位于文档第二章")
        result = asyncio.run(enrich_chunks(
            _mk_chunks(["片段一"]), "文档背景文本",
            {"contextual_retrieval": True, "parse_llm_model": "不存在的模型"}))
        assert len(result) == 1
        for cfg in fake.llm_cfgs:
            assert cfg["model"] == "m-b"

    def test_empty_uses_active(self, monkeypatch):
        """空串指定（=未指定）→ 激活模型"""
        _setup_multi_models(active=0)
        fake = _patch_recorder(monkeypatch,
                               "backend.services.contextual_retriever",
                               "该片段位于文档第二章")
        asyncio.run(enrich_chunks(
            _mk_chunks(["片段一"]), "文档背景文本",
            {"contextual_retrieval": True, "parse_llm_model": ""}))
        for cfg in fake.llm_cfgs:
            assert cfg["model"] == "m-a"


class TestBuildGraphSpecifiedModel:

    def test_specified_model_used(self, monkeypatch):
        """parse_llm_model=模型B → 图谱抽取客户端用模型B配置"""
        _setup_multi_models(active=0)
        fake = _patch_recorder(monkeypatch,
                               "backend.services.knowledge_graph_service",
                               _FIXED_EXTRACTION_JSON)
        stats = asyncio.run(build_graph_for_doc(
            "kb-test-model", "doc-1", "测试文档",
            _mk_chunks(["第一块", "第二块"]),
            cfg={"knowledge_graph": True, "parse_llm_model": "模型B"}))
        assert stats["extracted"] == 2
        assert fake.llm_cfgs
        for cfg in fake.llm_cfgs:
            assert cfg["model"] == "m-b"
            assert cfg["base_url"] == "http://b.example/v1"

    def test_default_active_model(self, monkeypatch):
        """未指定 → 激活模型（active=0 → 模型A）"""
        _setup_multi_models(active=0)
        fake = _patch_recorder(monkeypatch,
                               "backend.services.knowledge_graph_service",
                               _FIXED_EXTRACTION_JSON)
        stats = asyncio.run(build_graph_for_doc(
            "kb-test-model-2", "doc-1", "测试文档",
            _mk_chunks(["第一块"]),
            cfg={"knowledge_graph": True}))
        assert stats["extracted"] == 1
        for cfg in fake.llm_cfgs:
            assert cfg["model"] == "m-a"

    def test_missing_falls_back_active(self, monkeypatch):
        """查不到 → 回退激活模型，不抛"""
        _setup_multi_models(active=1)
        fake = _patch_recorder(monkeypatch,
                               "backend.services.knowledge_graph_service",
                               _FIXED_EXTRACTION_JSON)
        stats = asyncio.run(build_graph_for_doc(
            "kb-test-model-3", "doc-1", "测试文档",
            _mk_chunks(["第一块"]),
            cfg={"knowledge_graph": True, "parse_llm_model": "不存在"}))
        assert stats["extracted"] == 1
        for cfg in fake.llm_cfgs:
            assert cfg["model"] == "m-b"


# ==================== 3. 接口：模型列表 + 按名测连接 ====================

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


class TestLlmModelsApi:

    def test_list_models_ok_for_user(self, client, admin_headers,
                                     user_headers):
        """普通用户可读模型列表：仅 {name, model} + active，无敏感字段"""
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("模型A", "m-a"),
                                            _item("模型B", "m-b")],
                                 "active": 1})
        client.post(f"/api/settings/profiles/{p['id']}/activate",
                    headers=admin_headers)
        resp = client.get("/api/settings/llm/models", headers=user_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["active"] == 1
        assert [m["name"] for m in data["models"]] == ["模型A", "模型B"]
        for m in data["models"]:
            assert set(m.keys()) == {"name", "model"}, \
                f"仅 name/model，实际: {sorted(m.keys())}"

    def test_list_models_requires_login(self, client, admin_headers):
        """未登录 → 401"""
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item()], "active": 0})
        client.post(f"/api/settings/profiles/{p['id']}/activate",
                    headers=admin_headers)
        resp = client.get("/api/settings/llm/models")
        assert resp.status_code == 401

    def test_list_models_no_active_404(self, client, user_headers, monkeypatch):
        """无激活档案 → 404"""
        svc = ss.get_settings_service()
        monkeypatch.setattr(svc, "_active_id", "")
        resp = client.get("/api/settings/llm/models", headers=user_headers)
        assert resp.status_code == 404


class TestLlmTestModelApi:

    def _activate_multi(self, client, admin_headers):
        p = _create_profile(client, admin_headers,
                            llm={"models": [_item("模型A", "m-a",
                                                  "http://a.example/v1",
                                                  "sk-aaa"),
                                            _item("模型B", "m-b",
                                                  "http://bad.example/v1",
                                                  "sk-bbb")],
                                 "active": 0})
        client.post(f"/api/settings/profiles/{p['id']}/activate",
                    headers=admin_headers)
        return p

    def test_user_can_test_ok(self, client, admin_headers, user_headers,
                              monkeypatch):
        """user 按名测连接：后端从档案查完整条目（含 api_key）→ 探测成功"""
        fake = _patch_llm_http(monkeypatch)
        self._activate_multi(client, admin_headers)
        resp = client.post("/api/settings/llm/test-model",
                           json={"name": "模型A"}, headers=user_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        # 探测 URL = 模型条目 base_url/models，且携带档案 api_key（前端无明文也能测）
        assert fake.calls and fake.calls[0][1] == "http://a.example/v1/models"
        assert fake.calls[0][2]["headers"].get("Authorization") == \
            "Bearer sk-aaa"

    def test_conn_fail(self, client, admin_headers, user_headers, monkeypatch):
        """连接失败 → ok=False + 原因（前端提示并保持原模型）"""
        _patch_llm_http(monkeypatch)
        self._activate_multi(client, admin_headers)
        resp = client.post("/api/settings/llm/test-model",
                           json={"name": "模型B"}, headers=user_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "连接失败" in data["reason"]

    def test_model_not_found_404(self, client, admin_headers, user_headers,
                                 monkeypatch):
        _patch_llm_http(monkeypatch)
        self._activate_multi(client, admin_headers)
        resp = client.post("/api/settings/llm/test-model",
                           json={"name": "不存在的模型"},
                           headers=user_headers)
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_missing_name_400(self, client, admin_headers, user_headers,
                              monkeypatch):
        _patch_llm_http(monkeypatch)
        self._activate_multi(client, admin_headers)
        resp = client.post("/api/settings/llm/test-model",
                           json={}, headers=user_headers)
        assert resp.status_code == 400
        assert "name" in resp.json()["detail"]

    def test_requires_login(self, client, admin_headers, monkeypatch):
        """未登录 → 401"""
        _patch_llm_http(monkeypatch)
        self._activate_multi(client, admin_headers)
        resp = client.post("/api/settings/llm/test-model",
                           json={"name": "模型A"})
        assert resp.status_code == 401
