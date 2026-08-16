"""知识图谱检索增强（GraphRAG Local Search 思路）集成测试

覆盖：
- stream：开关默认开 + 有图谱 → meta.sources 含"知识图谱"引用、LLM system
  prompt 收到图谱上下文；无图谱文件 → 不调用抽实体 LLM（零成本跳过）问答
  正常；抽实体失败 → 无图谱引用查询照常；kg_enhance=false → 无图谱引用；
  普通检索无命中 + 图谱有内容 → 图谱引用兜底仍走 LLM
- retrieve：默认返回图谱引用（document_name="知识图谱"）；enable_kg=false
  关闭；enable_kg=true + 无匹配实体 → 不报错无引用
- 配置：GET/POST /api/settings/chat kg_enhance 读写、部门配置覆盖全局

全部离线：mock embedding + mock LLM（chat_service）+ 伪查询实体客户端
（knowledge_graph_service._get_client 替换）。图谱文件直接 save_graph 落盘。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from backend.services.knowledge_graph_service import save_graph
from conftest import _find_dept_id, create_kb, upload_and_ingest


def _write_graph(kb_id: str) -> None:
    """直接落盘测试图谱（绕过 LLM 构建）：图灵 →(相关) 专家系统"""
    graph = {
        "kb_id": kb_id,
        "updated_at": "2026-01-01 00:00:00",
        "docs": {"d1": {"name": "测试文档", "chunk_count": 2}},
        "entities": [
            {"id": "e1", "name": "图灵", "type": "人物",
             "description": "英国数学家、计算机科学奠基人", "count": 2,
             "chunk_refs": [
                 {"doc_id": "d1", "chunk_index": 0,
                  "char_start": 10, "char_end": 12},
                 {"doc_id": "d1", "chunk_index": 1,
                  "char_start": 5, "char_end": 7}]},
            {"id": "e2", "name": "专家系统", "type": "技术",
             "description": "基于规则的人工智能系统", "count": 1,
             "chunk_refs": [
                 {"doc_id": "d1", "chunk_index": 1,
                  "char_start": 30, "char_end": 34}]},
        ],
        "relations": [
            {"id": "r1", "source": "e1", "target": "e2", "type": "相关",
             "description": "同属人工智能领域", "weight": 1.0,
             "chunk_refs": [
                 {"doc_id": "d1", "chunk_index": 1,
                  "char_start": 30, "char_end": 34}]},
        ],
    }
    save_graph(kb_id, graph)


class _FakeQueryEntitiesClient:
    """伪查询实体 LLM 客户端（返回 JSON 数组；error/garbage 模拟失败）"""

    def __init__(self, mode: str = "ok",
                 payload: str = '["图灵","专家系统"]'):
        self.mode = mode
        self.payload = payload
        self.call_count = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.call_count += 1
        if self.mode == "error":
            raise RuntimeError("mock 查询实体 LLM 调用失败（测试构造）")
        if self.mode == "garbage":
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="这不是JSON"))])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.payload))])


def _patch_query_client(monkeypatch, mode: str = "ok",
                        payload: str = '["图灵","专家系统"]'):
    """替换 knowledge_graph_service 的模块级 _get_client 为伪客户端"""
    fake = _FakeQueryEntitiesClient(mode=mode, payload=payload)
    monkeypatch.setattr(
        "backend.services.knowledge_graph_service._get_client",
        lambda llm_cfg=None: fake)
    return fake


def _meta_sources(sse_text: str) -> list:
    """从 SSE 文本提取 meta 事件的 sources 数组"""
    meta_block = sse_text.split("event: meta", 1)[1].split("\n\n", 1)[0]
    data_line = meta_block.split("data: ", 1)[1].strip()
    return json.loads(data_line)["sources"]


class TestStreamKgEnhance:
    """stream 集成：图谱引用注入回答"""

    def test_stream_kg_source_in_meta_and_prompt(
            self, client, mock_embedding, mock_llm, admin_headers,
            monkeypatch):
        """开关默认开 + 有图谱 → meta.sources 含"知识图谱"、LLM 收到图谱上下文"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        _write_graph(kb["id"])
        fake = _patch_query_client(monkeypatch)
        state = mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "图灵和专家系统有什么关系？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert fake.call_count == 1, "有图谱时应调用抽实体 LLM"
        assert "event: done" in resp.text

        # meta.sources 含"知识图谱"条目（独立引用，结构完整）
        sources = _meta_sources(resp.text)
        names = [s["document_name"] for s in sources]
        assert "知识图谱" in names
        # 引用顺序规则：图谱引用（score=0）恒在列表末尾（补充引用），
        # 编号 = 列表顺序 1..N 连续（meta 顺序 = _build_refs 编号 =
        # 前端 sources[n-1] 行内 [n] = 面板 index+1 角标）
        assert names[-1] == "知识图谱", \
            f"图谱引用应恒在 sources 末尾，实际顺序: {names}"
        assert [s["score"] for s in sources[:-1]] == \
            sorted((s["score"] for s in sources[:-1]), reverse=True), \
            "图谱之前的普通引用应保持相关度降序"
        kg = next(s for s in sources if s["document_name"] == "知识图谱")
        assert kg["document_id"] == ""
        assert kg["score"] == 0.0
        assert kg["chunk_index"] == -1
        assert "【知识图谱实体】" in kg["text"]
        assert "图灵(人物|英国数学家" in kg["text"]
        assert "【知识图谱关系】" in kg["text"]
        assert "图灵|相关|专家系统|同属人工智能领域" in kg["text"]

        # LLM system prompt 收到图谱引用（[引用 N]（来源：知识图谱））
        inst = state.instances[0]
        system = inst.last_kwargs["messages"][0]["content"]
        assert "【知识图谱实体】" in system
        assert "来源：知识图谱" in system

    def test_stream_no_graph_skips_llm(self, client, mock_embedding,
                                       mock_llm, admin_headers, monkeypatch):
        """无图谱文件 → 不调用抽实体 LLM（零成本跳过），问答正常"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])  # 不写图谱文件
        fake = _patch_query_client(monkeypatch)
        mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert fake.call_count == 0, "无图谱不应调用抽实体 LLM"
        assert "event: done" in resp.text
        names = [s["document_name"] for s in _meta_sources(resp.text)]
        assert "知识图谱" not in names

    def test_stream_extract_failure_degrades(self, client, mock_embedding,
                                             mock_llm, admin_headers,
                                             monkeypatch):
        """抽实体 LLM 失败 → 无图谱引用，查询完全照常"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        _write_graph(kb["id"])
        fake = _patch_query_client(monkeypatch, mode="error")
        mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "图灵和专家系统有什么关系？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert fake.call_count == 1, "失败也算一次调用（不阻塞）"
        assert "event: delta" in resp.text and "event: done" in resp.text
        names = [s["document_name"] for s in _meta_sources(resp.text)]
        assert "知识图谱" not in names

    def test_stream_extract_garbage_degrades(self, client, mock_embedding,
                                             mock_llm, admin_headers,
                                             monkeypatch):
        """抽实体响应非 JSON → 同降级（无图谱引用）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        _write_graph(kb["id"])
        fake = _patch_query_client(monkeypatch, mode="garbage")
        mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "图灵和专家系统有什么关系？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert "event: done" in resp.text
        names = [s["document_name"] for s in _meta_sources(resp.text)]
        assert "知识图谱" not in names

    def test_stream_kg_disabled(self, client, mock_embedding, mock_llm,
                                admin_headers, monkeypatch):
        """配置 kg_enhance=false → 不调用抽实体 LLM、无图谱引用"""
        resp = client.post("/api/settings/chat", json={
            "chat": {"kg_enhance": False},
            "retrieval": {},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        _write_graph(kb["id"])
        fake = _patch_query_client(monkeypatch)
        mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "图灵和专家系统有什么关系？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert fake.call_count == 0, "开关关不应调用抽实体 LLM"
        assert "event: done" in resp.text
        names = [s["document_name"] for s in _meta_sources(resp.text)]
        assert "知识图谱" not in names

    def test_stream_kg_rescues_empty_retrieval(self, client, mock_embedding,
                                               mock_llm, admin_headers,
                                               monkeypatch):
        """普通检索无命中 + 图谱有内容 → 图谱引用兜底，仍走 LLM 回答"""
        kb = create_kb(client)
        _write_graph(kb["id"])  # 无文档（检索必空），但有图谱
        fake = _patch_query_client(monkeypatch)
        mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "图灵和专家系统有什么关系？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert "event: delta" in resp.text, "图谱引用兜底应走 LLM 回答"
        assert "event: done" in resp.text
        assert "未检索到相关内容" not in resp.text
        assert fake.call_count == 1


class TestRetrieveKgEnhance:
    """检索调试接口：同样返回图谱引用"""

    def test_retrieve_returns_kg_source(self, client, mock_embedding,
                                        admin_headers, monkeypatch):
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        _write_graph(kb["id"])
        fake = _patch_query_client(monkeypatch)
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "图灵和专家系统什么关系？",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        sources = resp.json()["sources"]
        assert fake.call_count == 1
        kg = [s for s in sources if s["document_name"] == "知识图谱"]
        assert kg, "检索结果应含图谱引用"
        assert "图灵|相关|专家系统|同属人工智能领域" in kg[0]["text"]

    def test_retrieve_multi_kb_kg_appended_last(self, client, mock_embedding,
                                                admin_headers, monkeypatch):
        """多库对比检索 + 图谱：图谱引用恒在末尾，普通引用按 score 降序，
        编号 = 列表顺序 1..N 连续（防多库拼接导致编号乱序/重复）"""
        kb_a = create_kb(client, name="知识库A")
        upload_and_ingest(client, kb_a["id"])
        kb_b = create_kb(client, name="知识库B")
        upload_and_ingest(client, kb_b["id"])
        _write_graph(kb_a["id"])  # 仅 A 库有图谱
        fake = _patch_query_client(monkeypatch)
        resp = client.post("/api/chat/retrieve", json={
            "kb_ids": [kb_a["id"], kb_b["id"]],
            "query": "Python 是什么语言？",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake.call_count == 1, "仅 A 库有图谱：只调用一次抽实体 LLM"
        sources = resp.json()["sources"]
        assert sources, "多库检索应至少命中普通引用"
        # 图谱恒在末尾（score=0 在排序截断后 append，不参与分数排序）
        assert sources[-1]["document_name"] == "知识图谱", \
            f"图谱引用应恒在 sources 末尾，实际: {[s['document_name'] for s in sources]}"
        # 图谱之前的普通引用按 score 降序（多库合并排序后截断）
        normal = sources[:-1]
        scores = [s["score"] for s in normal]
        assert scores == sorted(scores, reverse=True), \
            f"普通引用应保持 score 降序，实际: {scores}"
        # 编号 = 列表顺序 1..N 连续无重复（前端 [n] 按 sources[n-1] 映射）
        assert [i + 1 for i in range(len(sources))] == \
            list(range(1, len(sources) + 1))
        ids = [(s["document_id"], s["chunk_index"]) for s in normal]
        assert len(ids) == len(set(ids)), "普通引用不得重复"

    def test_retrieve_enable_kg_false(self, client, mock_embedding,
                                      admin_headers, monkeypatch):
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        _write_graph(kb["id"])
        fake = _patch_query_client(monkeypatch)
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "图灵和专家系统什么关系？",
            "enable_kg": False,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake.call_count == 0
        sources = resp.json()["sources"]
        assert all(s["document_name"] != "知识图谱" for s in sources)

    def test_retrieve_enable_kg_true_no_graph(self, client, mock_embedding,
                                              admin_headers, monkeypatch):
        """强制开启 + 无图谱 → 不报错、无图谱引用"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])  # 不写图谱
        fake = _patch_query_client(monkeypatch)
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
            "enable_kg": True,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake.call_count == 0, "无图谱不调用抽实体 LLM"
        assert all(s["document_name"] != "知识图谱"
                   for s in resp.json()["sources"])

    def test_retrieve_kg_no_match(self, client, mock_embedding,
                                  admin_headers, monkeypatch):
        """图谱存在但查询实体无匹配 → 无图谱引用不报错"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        _write_graph(kb["id"])
        fake = _patch_query_client(monkeypatch, payload='["爱丽丝"]')
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "爱丽丝漫游奇境",
            "enable_kg": True,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake.call_count == 1
        assert all(s["document_name"] != "知识图谱"
                   for s in resp.json()["sources"])


class TestKgEnhanceSettings:
    """配置：GET/POST /api/settings/chat kg_enhance 读写 + 部门覆盖"""

    def test_settings_default_on_and_read_write(self, client, admin_headers):
        resp = client.get("/api/settings/chat", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["chat"]["kg_enhance"] is True, "默认开启"

        resp = client.post("/api/settings/chat", json={
            "chat": {"kg_enhance": False},
            "retrieval": {},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        resp = client.get("/api/settings/chat", headers=admin_headers)
        assert resp.json()["chat"]["kg_enhance"] is False

        # 恢复默认
        resp = client.post("/api/settings/chat", json={
            "chat": {"kg_enhance": True},
            "retrieval": {},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text

    def test_dept_config_overrides_global(self, client, admin_headers,
                                          dept_admin_headers, user_headers,
                                          mock_embedding, mock_llm,
                                          monkeypatch):
        """部门配置 kg_enhance=false → 部门用户聊天无图谱引用（全局仍开）"""
        # 部门管理员关闭本部门图谱增强
        resp = client.post("/api/settings/chat", json={
            "chat": {"kg_enhance": False},
            "retrieval": {},
        }, headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        # 部门用户读取合并值 = false
        resp = client.get("/api/settings/chat", headers=user_headers)
        assert resp.json()["chat"]["kg_enhance"] is False

        # 部门知识库（user_headers 与 dept_admin_headers 同属"测试部门"）
        dept_id = _find_dept_id(client, admin_headers, "测试部门")
        kb = create_kb(client, "部门知识库", department_id=dept_id)
        upload_and_ingest(client, kb["id"])
        _write_graph(kb["id"])
        fake = _patch_query_client(monkeypatch)
        mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "图灵和专家系统有什么关系？",
        }, headers=user_headers)
        assert resp.status_code == 200
        assert fake.call_count == 0, "部门关闭后不应调用抽实体 LLM"
        names = [s["document_name"] for s in _meta_sources(resp.text)]
        assert "知识图谱" not in names
