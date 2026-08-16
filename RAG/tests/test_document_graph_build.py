"""文档图谱补建/重建接口 + graph_status 生命周期测试

覆盖：
- 接口校验：未认证 401 / 普通用户 403（can_manage_kb）/
  未入库 400"请先入库后再构建图谱" / building 中 409"图谱正在构建中，请稍候"
- graph_status 生命周期：入库（开关开）→ ready；默认（开关关）→ none；
  补建 none → building → ready；抽取全失败 → failed + graph_error
- 重建清旧引用：补建后实体 count/引用正确，重建不翻倍
- 列表/详情接口返回 graph_status
- 入库抽取全失败（mock 抛错）不阻塞入库：graph_status 仍 ready（文档级语义，
  与"失败/超时跳过不阻塞入库"设计一致）
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from conftest import create_kb, upload_doc, wait_for_status
from backend.services.knowledge_graph_service import graph_path, load_graph

# 固定抽取结果（与 test_knowledge_graph 同款语义：Python + Guido van Rossum）
_FIXED_EXTRACTION = {
    "entities": [
        {"name": "Python", "type": "技术", "description": "高级编程语言"},
        {"name": "Guido van Rossum", "type": "人物",
         "description": "Python 语言的创始人"},
    ],
    "relations": [
        {"source": "Guido van Rossum", "target": "Python", "type": "开发",
         "description": "开发了 Python 语言"},
    ],
}
_FIXED_EXTRACTION_JSON = json.dumps(_FIXED_EXTRACTION, ensure_ascii=False)

# 多块测试文档（>800 字，naive 默认 chunk_size 800 才能切多块）
_MULTI_CHUNK_DOC = (
    "# Python 发展史\n\n"
    + "\n\n".join(
        f"## 第{i}节\n\nPython 语言由 Guido van Rossum 于 1991 年首次发布，"
        f"这是一种强调可读性的高级编程语言，被广泛用于第 {i} 个应用场景中。"
        for i in range(1, 6))
    + "\n")


class _FakeExtractionClient:
    """伪 OpenAI 客户端：非流式 chat.completions.create 返回固定抽取 JSON

    mode: ok=固定 JSON / error=抛异常（模拟 LLM 调用全失败）
    """

    def __init__(self, mode: str = "ok", payload: str = _FIXED_EXTRACTION_JSON,
                 delay: float = 0.0):
        self.mode = mode
        self.payload = payload
        self.delay = delay
        self.call_count = 0
        self.last_llm_cfg = None
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    def set_cfg(self, llm_cfg: dict) -> "_FakeExtractionClient":
        """记录客户端注入的 LLM 配置（模型覆盖断言用）并返回自身"""
        self.last_llm_cfg = llm_cfg
        return self

    async def _create(self, **kwargs):
        self.call_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "error":
            raise RuntimeError("mock 抽取 LLM 调用失败（测试构造）")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.payload))])


def _patch_kg_client(monkeypatch, fake: _FakeExtractionClient) -> _FakeExtractionClient:
    """替换 knowledge_graph_service 的客户端工厂（抽取调用经它取；
    注入的 LLM 配置记到 fake.last_llm_cfg 供模型覆盖断言）"""
    monkeypatch.setattr(
        "backend.services.knowledge_graph_service._get_client",
        lambda llm_cfg=None: fake.set_cfg(llm_cfg))
    return fake


class _RecordingEmbedding:
    """记录输入文本的伪 embedding（字符直方图向量，离线可跑）"""

    def __init__(self):
        self.inputs = []

    async def embed(self, texts):
        self.inputs.extend(texts)
        from conftest import char_vector
        return [char_vector(t) for t in texts]


def _patch_rec_embedding(monkeypatch):
    """替换 ingestion/retrieval 的 embedding 服务为记录式实现（引用复制需双 patch）"""
    rec = _RecordingEmbedding()
    fake_getter = lambda: rec  # noqa: E731
    for module in ("backend.services.ingestion_service",
                   "backend.services.retrieval_service"):
        monkeypatch.setattr(module + ".get_embedding_service", fake_getter)
    return rec


def wait_for_graph_status(client, kb_id, doc_id, status, timeout=30.0,
                          headers=None):
    """轮询文档 graph_status 直到指定状态；failed 且目标非 failed → 断言失败"""
    deadline = time.monotonic() + timeout
    while True:
        doc = client.get(f"/api/kbs/{kb_id}/documents/{doc_id}",
                         headers=headers).json()
        cur = doc.get("graph_status")
        if cur == status:
            return doc
        if cur == "failed" and status != "failed":
            raise AssertionError(f"图谱构建失败: {doc.get('graph_error')}")
        if time.monotonic() > deadline:
            raise AssertionError(
                f"等待图谱状态 {status} 超时（{timeout}s），当前: {cur}")
        time.sleep(0.2)


def _upload_and_ingest(client, kb_id, filename="图谱测试.txt", headers=None,
                       ingest_body=None):
    """上传 + 入库（等待 ingested），返回最终 DocumentItem dict"""
    doc = upload_doc(client, kb_id, filename=filename,
                     content=_MULTI_CHUNK_DOC)
    resp = client.post(f"/api/kbs/{kb_id}/documents/{doc['id']}/ingest",
                       json=ingest_body, headers=headers)
    assert resp.status_code == 200, resp.text
    return wait_for_status(client, kb_id, doc["id"], headers=headers)


def _post_graph_build(client, kb_id, doc_id, headers=None):
    return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/graph-build",
                       headers=headers)


# ==================== 接口校验 ====================

class TestGraphBuildAPI:

    def test_unauthorized_401(self, client, admin_headers):
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _post_graph_build(client, kb["id"], doc["id"])
        assert resp.status_code == 401

    def test_non_manager_403(self, client, admin_headers):
        """普通用户（无 can_manage_kb）→ 403（与上传/入库/删除同权限矩阵）"""
        from conftest import create_department_and_admin, create_user
        dept_id, dept_admin = create_department_and_admin(
            client, admin_headers, "图谱构建部", "gb_admin",
            "pass123456", "图谱构建管理员")
        kb = create_kb(client, headers=dept_admin)
        doc = upload_doc(client, kb["id"])
        user = create_user(client, admin_headers, dept_id, "gb_user")
        resp = _post_graph_build(client, kb["id"], doc["id"], headers=user)
        assert resp.status_code == 403

    def test_doc_not_found_404(self, client, admin_headers):
        kb = create_kb(client)
        resp = _post_graph_build(client, kb["id"], "不存在的文档",
                                 headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "文档不存在"

    def test_not_ingested_400(self, client, admin_headers):
        """未入库（无切块）→ 400"请先入库后再构建图谱" """
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _post_graph_build(client, kb["id"], doc["id"],
                                 headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "请先入库后再构建图谱"

    def test_building_409(self, client, monkeypatch, admin_headers):
        """构建中（graph_status=building）→ 409"图谱正在构建中，请稍候" """
        _patch_rec_embedding(monkeypatch)
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        # 直接持久化 building 状态（模拟任务运行中），路由应拒绝重复触发
        from backend.services.document_service import get_document_service
        get_document_service().update_graph_status(final["id"], "building")
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "图谱正在构建中，请稍候"

    def test_cross_department_403(self, client, monkeypatch, admin_headers):
        """跨部门（无 can_manage_kb）→ 403（与上传/入库写接口一致，
        不泄露知识库存在性——403 文案不含库信息）"""
        _patch_rec_embedding(monkeypatch)
        from conftest import create_department_and_admin, create_user
        _, dept_admin_a = create_department_and_admin(
            client, admin_headers, "图谱构建A部", "gb_admin_a",
            "pass123456", "A部管理员")
        dept_b_id, _ = create_department_and_admin(
            client, admin_headers, "图谱构建B部", "gb_admin_b",
            "pass123456", "B部管理员")
        kb = create_kb(client, headers=dept_admin_a)
        doc = upload_doc(client, kb["id"])
        user_b = create_user(client, admin_headers, dept_b_id, "gb_user_b")
        resp = _post_graph_build(client, kb["id"], doc["id"], headers=user_b)
        assert resp.status_code == 403
        assert "仅超级管理员或本部门管理员" in resp.json()["detail"]


# ==================== graph_status 生命周期 ====================

class TestGraphStatusLifecycle:

    def test_default_none(self, client, monkeypatch, admin_headers):
        """入库（开关关）→ graph_status=none，列表/详情返回该字段"""
        _patch_rec_embedding(monkeypatch)
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        assert final["graph_status"] == "none"
        assert not graph_path(kb["id"]).exists()
        # 列表返回 graph_status
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        item = next(d for d in docs if d["id"] == final["id"])
        assert item["graph_status"] == "none"

    def test_ingest_switch_on_ready(self, client, monkeypatch, admin_headers):
        """入库（knowledge_graph 开）→ 构建成功 → graph_status=ready"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers,
                                   ingest_body={"knowledge_graph": True})
        assert final["graph_status"] == "ready"
        assert fake.call_count == final["chunk_count"]
        g = load_graph(kb["id"])
        assert g["docs"][final["id"]]["chunk_count"] == final["chunk_count"]
        assert {e["name"] for e in g["entities"]} >= {"Python", "Guido van Rossum"}

    def test_ingest_llm_failure_still_ready(self, client, monkeypatch,
                                            admin_headers):
        """入库抽取全失败：不阻塞入库，graph_status 仍 ready（文档级语义：
        图谱文件已生成、docs 条目已记录；失败仅跳过对应块不阻塞——既有设计）"""
        _patch_rec_embedding(monkeypatch)
        _patch_kg_client(monkeypatch, _FakeExtractionClient(mode="error"))
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers,
                                   ingest_body={"knowledge_graph": True})
        assert final["graph_status"] == "ready"
        g = load_graph(kb["id"])
        assert g["entities"] == []
        assert g["docs"][final["id"]]["chunk_count"] == final["chunk_count"]

    def test_reingest_switch_off_keeps_status(self, client, monkeypatch,
                                              admin_headers):
        """重入库（开关关）：kg_status=None → 保持原 graph_status 不清空"""
        _patch_rec_embedding(monkeypatch)
        _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], content=_MULTI_CHUNK_DOC)
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"knowledge_graph": True},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["graph_status"] == "ready"
        # 重入库不传参（沿用 parser_config 开关=开）→ 仍 ready
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final2 = wait_for_status(client, kb["id"], doc["id"])
        assert final2["graph_status"] == "ready"


# ==================== 补建 / 重建 ====================

class TestGraphBuildTask:

    def test_build_success_lifecycle(self, client, monkeypatch, admin_headers):
        """补建：none → 触发（building）→ ready；图谱文件生成、实体合理；
        列表显示 graph_status=ready"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        assert final["graph_status"] == "none"
        # 触发补建
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "图谱构建任务已启动"
        done = wait_for_graph_status(client, kb["id"], final["id"], "ready",
                                     headers=admin_headers)
        assert done["graph_status"] == "ready"
        # 每块一次抽取（复用现有切块，不重新入库）
        assert fake.call_count == final["chunk_count"]
        # 图谱文件生成 + 实体/关系合理
        path = graph_path(kb["id"])
        assert path.exists()
        g = load_graph(kb["id"])
        assert g["docs"][final["id"]]["name"] == "图谱测试.txt"
        assert g["docs"][final["id"]]["chunk_count"] == final["chunk_count"]
        names = {e["name"] for e in g["entities"]}
        assert {"Python", "Guido van Rossum"} <= names
        assert any(r["type"] == "开发" for r in g["relations"])
        python_e = next(e for e in g["entities"] if e["name"] == "Python")
        assert python_e["count"] == final["chunk_count"]
        # 列表返回 ready
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        item = next(d for d in docs if d["id"] == final["id"])
        assert item["graph_status"] == "ready"
        # 详情接口也返回
        detail = client.get(f"/api/kbs/{kb['id']}/documents/{final['id']}",
                            headers=admin_headers).json()
        assert detail["graph_status"] == "ready"

    def test_rebuild_clears_old_refs(self, client, monkeypatch, admin_headers):
        """重建清旧引用：先补建（实体 count=块数），改抽取结果后重建 →
        实体数不翻倍、引用/描述来自本次抽取（覆盖式）"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_graph_status(client, kb["id"], final["id"], "ready",
                              headers=admin_headers)
        g = load_graph(kb["id"])
        python_e = next(e for e in g["entities"] if e["name"] == "Python")
        assert python_e["count"] == final["chunk_count"]
        first_calls = fake.call_count
        # 重建：抽取结果换成另一套实体（模拟文档更新后重新抽取）
        new_payload = {
            "entities": [
                {"name": "Python", "type": "技术",
                 "description": "解释型语言（新版描述）"},
                {"name": "Guido van Rossum", "type": "人物",
                 "description": "荷兰程序员"},
                {"name": "AI", "type": "技术", "description": "人工智能"},
            ],
            "relations": [
                {"source": "Guido van Rossum", "target": "Python",
                 "type": "开发", "description": "开发了 Python"},
            ],
        }
        fake.payload = json.dumps(new_payload, ensure_ascii=False)
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text
        done = wait_for_graph_status(client, kb["id"], final["id"], "ready",
                                     headers=admin_headers)
        assert done["graph_status"] == "ready"
        assert fake.call_count == first_calls + final["chunk_count"]
        g = load_graph(kb["id"])
        names = {e["name"] for e in g["entities"]}
        # 清旧后重建：新实体 AI 加入，总数 = 3（不翻倍）
        assert names == {"Python", "Guido van Rossum", "AI"}
        assert len(g["relations"]) == 1
        python_e = next(e for e in g["entities"] if e["name"] == "Python")
        assert python_e["count"] == final["chunk_count"]
        assert "新版描述" in python_e["description"]

    def test_build_all_failed_failed_status(self, client, monkeypatch,
                                            admin_headers):
        """抽取全失败（LLM 抛错）→ graph_status=failed + graph_error 有原因"""
        _patch_rec_embedding(monkeypatch)
        _patch_kg_client(monkeypatch, _FakeExtractionClient(mode="error"))
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text
        done = wait_for_graph_status(client, kb["id"], final["id"], "failed",
                                     headers=admin_headers)
        assert done["graph_status"] == "failed"
        assert done["graph_error"], "失败原因应写回 graph_error"
        assert "实体抽取全部失败" in done["graph_error"]
        # failed 后可再次触发重建（不卡死）
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text

    def test_build_task_exception_failed(self, client, monkeypatch,
                                         admin_headers):
        """任务内 build_graph_for_doc 抛异常 → failed + graph_error（原因截断）"""
        _patch_rec_embedding(monkeypatch)
        import backend.routers.documents as documents_mod

        async def _boom(*args, **kwargs):
            raise RuntimeError("图谱落盘失败（测试构造）")

        monkeypatch.setattr(documents_mod, "build_graph_for_doc", _boom)
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text
        done = wait_for_graph_status(client, kb["id"], final["id"], "failed",
                                     headers=admin_headers)
        assert done["graph_status"] == "failed"
        assert "图谱落盘失败" in done["graph_error"]
        # 列表返回 failed + 原因
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        item = next(d for d in docs if d["id"] == final["id"])
        assert item["graph_status"] == "failed"
        assert item["graph_error"]

    def test_rebuild_no_chunks_400(self, client, admin_headers):
        """未入库文档（无切块）→ 400 未入库语义（uploaded 状态且 chunks_meta 空）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _post_graph_build(client, kb["id"], doc["id"],
                                 headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "请先入库后再构建图谱"


def _setup_multi_models(active=0):
    """把当前激活档案的 llm 段替换为双模型（模型A=激活 m-a / 模型B=m-b），
    测试内直接改服务单例（conftest 每测试重建，隔离安全；
    与 test_parse_llm_model._setup_multi_models 同款模式）"""
    from backend.services.settings_service import get_settings_service
    svc = get_settings_service()
    p = svc.get_active()
    p["llm"] = {
        "models": [
            {"name": "模型A", "base_url": "http://a.example/v1",
             "api_key": "sk-aaa", "model": "m-a", "temperature": 0.1,
             "max_tokens": 2048, "timeout": 60},
            {"name": "模型B", "base_url": "http://b.example/v1",
             "api_key": "sk-bbb", "model": "m-b", "temperature": 0.2,
             "max_tokens": 4096, "timeout": 90},
        ],
        "active": active,
    }
    svc._profiles[p["id"]] = p
    svc._save()
    svc._apply_active()


# ==================== 构建模型选择（GraphBuildRequest.llm_model） ====================

class TestGraphBuildLlmModel:
    """补建/重建指定 LLM 模型：本次构建覆盖、不写回文档配置、回退激活"""

    def _post_build(self, client, kb_id, doc_id, headers, body=None):
        return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/graph-build",
                           json=body, headers=headers)

    def test_build_llm_model_override_this_run(self, client, monkeypatch,
                                                admin_headers):
        """带 llm_model=模型B → 本次构建用模型B（m-b/base_url b）覆盖激活；
        「本次构建生效」：不写回文档 parser_config（详情无 parse_llm_model）"""
        _setup_multi_models(active=0)
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        resp = self._post_build(client, kb["id"], final["id"],
                                admin_headers, body={"llm_model": "模型B"})
        assert resp.status_code == 200, resp.text
        done = wait_for_graph_status(client, kb["id"], final["id"], "ready",
                                     headers=admin_headers)
        assert done["graph_status"] == "ready"
        assert fake.last_llm_cfg, "抽取客户端应记录注入的 LLM 配置"
        assert fake.last_llm_cfg["model"] == "m-b"
        assert fake.last_llm_cfg["base_url"] == "http://b.example/v1"
        # 本次构建生效：文档持久化配置不被修改（默认 parser_config 中
        # parse_llm_model 为空串，仍应保持为空而非写入"模型B"）
        detail = client.get(f"/api/kbs/{kb['id']}/documents/{final['id']}",
                            headers=admin_headers).json()
        cfg = detail.get("parser_config") or {}
        assert not cfg.get("parse_llm_model")

    def test_build_without_llm_model_uses_doc_config(self, client, monkeypatch,
                                                      admin_headers):
        """不传 llm_model + 文档入库时配置 parse_llm_model=模型A（持久化）
        → 本次构建沿用文档配置（m-a）"""
        _setup_multi_models(active=0)
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers,
                                   ingest_body={"parse_llm_model": "模型A"})
        assert (final.get("parser_config") or {}).get("parse_llm_model") == "模型A"
        resp = self._post_build(client, kb["id"], final["id"], admin_headers)
        assert resp.status_code == 200, resp.text
        done = wait_for_graph_status(client, kb["id"], final["id"], "ready",
                                     headers=admin_headers)
        assert done["graph_status"] == "ready"
        assert fake.last_llm_cfg and fake.last_llm_cfg["model"] == "m-a"

    def test_build_without_llm_model_uses_active(self, client, monkeypatch,
                                                  admin_headers):
        """不传 llm_model + 文档无配置 → 默认激活模型（active=0 → m-a）"""
        _setup_multi_models(active=0)
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        resp = self._post_build(client, kb["id"], final["id"], admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_graph_status(client, kb["id"], final["id"], "ready",
                              headers=admin_headers)
        assert fake.last_llm_cfg and fake.last_llm_cfg["model"] == "m-a"

    def test_build_unknown_llm_model_fallback_active(self, client, monkeypatch,
                                                      admin_headers):
        """llm_model 不在激活档案 → 不失败：回退激活模型（m-a），ready"""
        _setup_multi_models(active=0)
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        resp = self._post_build(client, kb["id"], final["id"], admin_headers,
                                body={"llm_model": "不存在的模型"})
        assert resp.status_code == 200, resp.text
        done = wait_for_graph_status(client, kb["id"], final["id"], "ready",
                                     headers=admin_headers)
        assert done["graph_status"] == "ready"
        assert fake.last_llm_cfg and fake.last_llm_cfg["model"] == "m-a"

    def test_build_llm_model_non_string_422(self, client, admin_headers):
        """llm_model 非字符串 → 422（pydantic 请求体校验，路由前拦截）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = self._post_build(client, kb["id"], doc["id"], admin_headers,
                                body={"llm_model": 123})
        assert resp.status_code == 422


# ==================== 构建中断（graph-build/cancel） ====================

class TestGraphBuildCancel:

    def test_cancel_unauthorized_401(self, client):
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/graph-build/cancel")
        assert resp.status_code == 401

    def test_cancel_not_building_409(self, client, admin_headers):
        """未在构建（无取消信号）→ 409"当前不在图谱构建中，无法中断" """
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/graph-build/cancel",
            headers=admin_headers)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "当前不在图谱构建中，无法中断"

    def test_cancel_interrupt_restores_prev_status(self, client, monkeypatch,
                                                    admin_headers):
        """构建中 cancel → 200；任务停止、状态恢复 none（本次不落盘，图谱
        文件不存在）；_GRAPH_RUNNING 释放后可再次构建成功"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient(delay=3))
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_graph_status(client, kb["id"], final["id"], "building",
                              headers=admin_headers)
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{final['id']}/graph-build/cancel",
            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "图谱构建中断请求已发送"
        done = wait_for_graph_status(client, kb["id"], final["id"], "none",
                                     headers=admin_headers)
        assert done["graph_status"] == "none"
        assert not graph_path(kb["id"]).exists(), "中断不落盘"
        # 中断后再次构建成功
        fake.delay = 0
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text
        done = wait_for_graph_status(client, kb["id"], final["id"], "ready",
                                     headers=admin_headers)
        assert done["graph_status"] == "ready"

    def test_cancel_keeps_existing_graph(self, client, monkeypatch,
                                          admin_headers):
        """重建中 cancel → 状态恢复 ready，旧图谱原样保留（实体不丢不增）"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        final = _upload_and_ingest(client, kb["id"], headers=admin_headers)
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_graph_status(client, kb["id"], final["id"], "ready",
                              headers=admin_headers)
        names_before = {e["name"] for e in load_graph(kb["id"])["entities"]}
        assert {"Python", "Guido van Rossum"} <= names_before
        # 重建：慢抽取留出 cancel 窗口
        fake.delay = 3
        resp = _post_graph_build(client, kb["id"], final["id"],
                                 headers=admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_graph_status(client, kb["id"], final["id"], "building",
                              headers=admin_headers)
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{final['id']}/graph-build/cancel",
            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        done = wait_for_graph_status(client, kb["id"], final["id"], "ready",
                                     headers=admin_headers)
        assert done["graph_status"] == "ready"
        assert {e["name"] for e in load_graph(kb["id"])["entities"]
                } == names_before, "中断后旧图谱实体应原样保留"
