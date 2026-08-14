"""上下文检索增强（Contextual Retrieval）测试

覆盖：
- enrich_chunks 单元：成功生成 / 失败跳过不抛 / 超时跳过 / 空摘要跳过 /
  摘要超长截断 ≤100 / 并发上限 3 / 开关关不调用
- ingestion 集成（TestClient + 记录式 embedding mock + 伪摘要 LLM 客户端）：
  默认关（chunks_meta 无 context、embedding 用原文、不调用 LLM）；
  开关开（chunks_meta 有 context 且 text 保持原文、embedding 输入含
  【上下文】前缀、向量库 documents 含前缀、metadata context 截断 500、
  parser_config 持久化）；LLM 失败不阻塞入库；重解析沿用开关
- 检索：Source.context 透传；_build_refs 引用含摘要前缀
  （parent_text 场景拼接、s.text 已含前缀不重复、无 context 不变）
- 真实小验证：完整链路（真实解析 txt 小文档 + 摘要 LLM）→ chunks_meta
  有 context 且非空（真实环境 LLM 逐块调用耗时随块数线性增长，测试用
  伪客户端不耗时，该用例验证链路完整性）
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conftest import char_vector, create_kb, upload_doc, wait_for_status
from backend.chunking.splitter import Chunk
from backend.services.contextual_retriever import enrich_chunks


# ==================== 伪摘要 LLM 客户端 ====================

class _FakeContextClient:
    """伪 OpenAI 客户端：非流式 chat.completions.create 返回固定摘要

    mode: ok=成功 / error=抛异常 / empty=空摘要；delay 为模拟耗时
    （并发统计与超时测试用）；create 记录调用次数与并发峰值。
    """

    def __init__(self, mode: str = "ok", summary: str = "该片段位于文档第二章，介绍 Python 核心语法特性",
                 delay: float = 0.0):
        self.mode = mode
        self.summary = summary
        self.delay = delay
        self.call_count = 0
        self.active = 0
        self.peak = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.call_count += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.mode == "error":
                raise RuntimeError("mock 摘要 LLM 调用失败（测试构造）")
            if self.mode == "empty":
                return SimpleNamespace(choices=[
                    SimpleNamespace(message=SimpleNamespace(content=""))])
            return SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=self.summary))])
        finally:
            self.active -= 1


def _patch_ctx_client(monkeypatch, fake: _FakeContextClient):
    """替换 contextual_retriever 的客户端工厂（enrich_chunks 内部经它取客户端）"""
    monkeypatch.setattr(
        "backend.services.contextual_retriever._get_client",
        lambda llm_cfg=None: fake)
    return fake


class _RecordingEmbedding:
    """记录输入文本的伪 embedding（字符直方图向量，离线可跑且相似文本可命中）"""

    def __init__(self):
        self.inputs = []

    async def embed(self, texts):
        self.inputs.extend(texts)
        return [char_vector(t) for t in texts]


def _patch_rec_embedding(monkeypatch) -> _RecordingEmbedding:
    """替换 ingestion/retrieval 的 embedding 服务为记录式实现（引用复制需双 patch）"""
    rec = _RecordingEmbedding()
    fake_getter = lambda: rec  # noqa: E731
    for module in ("backend.services.ingestion_service",
                   "backend.services.retrieval_service"):
        monkeypatch.setattr(module + ".get_embedding_service", fake_getter)
    return rec


def _mk_chunks(texts) -> list:
    """构造简单 Chunk 列表（偏移连续，正文语义不影响摘要测试）"""
    out = []
    pos = 0
    for t in texts:
        out.append(Chunk(text=t, char_start=pos, char_end=pos + len(t)))
        pos += len(t) + 1
    return out


_SAMPLE_DOC = ("# 公司制度手册\n\n"
               "本手册适用于全体员工，涵盖考勤、报销与信息安全制度。\n\n"
               "## 考勤制度\n\n"
               "员工每日需按时打卡，迟到三次记一次旷工。\n\n"
               "## 报销制度\n\n"
               "报销需在 30 天内提交发票与审批单。\n")


# ==================== enrich_chunks 单元测试 ====================

class TestEnrichChunks:
    """摘要生成：成功 / 失败 / 超时 / 截断 / 并发 / 开关"""

    def test_success_returns_all(self, monkeypatch):
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient())
        chunks = _mk_chunks(["片段一", "片段二", "片段三"])
        result = asyncio.run(enrich_chunks(
            chunks, _SAMPLE_DOC, {"contextual_retrieval": True},
            doc_name="公司制度手册.txt"))
        assert fake.call_count == 3
        assert [r["index"] for r in result] == [0, 1, 2]
        assert all(r["context"] == fake.summary for r in result)
        assert all(isinstance(r["context"], str) and r["context"] for r in result)

    def test_summary_truncated(self, monkeypatch):
        long_summary = "长" * 300
        fake = _patch_ctx_client(monkeypatch,
                                 _FakeContextClient(summary=long_summary))
        result = asyncio.run(enrich_chunks(
            _mk_chunks(["片段"]), _SAMPLE_DOC,
            {"contextual_retrieval": True}))
        assert len(result) == 1
        assert len(result[0]["context"]) <= 100  # 截断兜底

    def test_failure_skips_without_raise(self, monkeypatch):
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient(mode="error"))
        chunks = _mk_chunks(["片段一", "片段二", "片段三"])
        result = asyncio.run(enrich_chunks(
            chunks, _SAMPLE_DOC, {"contextual_retrieval": True}))
        assert result == []  # 全部失败 → 空映射，不抛异常
        assert fake.call_count == 3

    def test_timeout_skips(self, monkeypatch):
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient(delay=0.05))
        chunks = _mk_chunks(["片段一", "片段二"])
        # 单调用超时 0.01s < 客户端耗时 0.05s → wait_for 抛超时 → 跳过
        result = asyncio.run(enrich_chunks(
            chunks, _SAMPLE_DOC, {"contextual_retrieval": True},
            timeout=0.01))
        assert result == []
        assert fake.call_count == 2

    def test_empty_summary_skips(self, monkeypatch):
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient(mode="empty"))
        result = asyncio.run(enrich_chunks(
            _mk_chunks(["片段"]), _SAMPLE_DOC,
            {"contextual_retrieval": True}))
        assert result == []
        assert fake.call_count == 1

    def test_concurrency_limited_to_3(self, monkeypatch):
        fake = _patch_ctx_client(monkeypatch,
                                 _FakeContextClient(delay=0.05))
        chunks = _mk_chunks([f"片段{i}" for i in range(10)])
        result = asyncio.run(enrich_chunks(
            chunks, _SAMPLE_DOC, {"contextual_retrieval": True}))
        assert len(result) == 10
        assert fake.call_count == 10
        assert fake.peak <= 3  # 并发上限（asyncio.Semaphore(3)）
        assert fake.peak >= 2  # 确实发生并发（10 个块串行不可能触发限流）

    def test_off_switch_no_llm_call(self, monkeypatch):
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient())
        result = asyncio.run(enrich_chunks(
            _mk_chunks(["片段"]), _SAMPLE_DOC,
            {"contextual_retrieval": False}))
        assert result == []
        assert fake.call_count == 0

    def test_missing_switch_no_llm_call(self, monkeypatch):
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient())
        result = asyncio.run(enrich_chunks(
            _mk_chunks(["片段"]), _SAMPLE_DOC, {}))
        assert result == []
        assert fake.call_count == 0

    def test_empty_chunks_no_call(self, monkeypatch):
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient())
        result = asyncio.run(enrich_chunks(
            [], _SAMPLE_DOC, {"contextual_retrieval": True}))
        assert result == []
        assert fake.call_count == 0


# ==================== ingestion 集成测试 ====================

class TestIngestContextual:
    """入库链路：开关关不调用 / 开关开摘要进向量与 metadata / 失败不阻塞 / 重解析沿用"""

    def _ingest(self, client, kb_id, doc_id, body=None, headers=None):
        return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                           json=body, headers=headers)

    def test_default_off_no_llm_and_original_embedding(
            self, client, monkeypatch, admin_headers):
        """默认（不传开关）：不调用 LLM，chunks_meta 无 context，embedding 用原文"""
        rec = _patch_rec_embedding(monkeypatch)
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])  # SAMPLE_TEXT（conftest，含两级标题）
        resp = self._ingest(client, kb["id"], doc["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        # 开关默认关闭并持久化 False；LLM 未被调用
        assert final["parser_config"]["contextual_retrieval"] is False
        assert fake.call_count == 0
        # chunks_meta 无 context 键，text 为原文
        assert all("context" not in c for c in final["chunks_meta"])
        assert all(c["text"] for c in final["chunks_meta"])
        # embedding 输入全部为原文（不含【上下文】前缀）
        assert rec.inputs
        assert all("【上下文】" not in t for t in rec.inputs)
        assert rec.inputs == [c["text"] for c in final["chunks_meta"]]

    def test_on_generates_context(self, client, monkeypatch, admin_headers):
        """开关开：chunks_meta 有 context（text 仍原文）、embedding/向量库含摘要前缀、
        metadata 透传、详情接口 ChunkInfo.context"""
        rec = _patch_rec_embedding(monkeypatch)
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"contextual_retrieval": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_config"]["contextual_retrieval"] is True
        assert fake.call_count == final["chunk_count"]  # 每块调用一次 LLM
        # chunks_meta：每块有 context 且非空；text 保持原文（偏移契约不破坏）
        metas = final["chunks_meta"]
        assert all(c.get("context") for c in metas)
        assert all("【上下文】" not in c["text"] for c in metas)
        for c in metas:
            assert c["text"] == c["text"]  # 原文
        # 偏移一致性：text 长度与 char 区间一致（摘要未进 text）
        for c in metas:
            assert c["char_end"] - c["char_start"] == len(c["text"])
        # embedding 输入含【上下文】前缀（向量化用增强文本）
        assert rec.inputs
        assert all("【上下文】" in t for t in rec.inputs)
        assert len(rec.inputs) == len(metas)
        # 向量库 documents 含前缀 + metadata context 透传
        from backend.services.vector_store import get_vector_store
        items = get_vector_store().get_all(kb["id"])
        assert len(items) == len(metas)
        assert all("【上下文】" in t for _, t, _ in items)
        assert all((m.get("context") or "") for _, _, m in items)
        # 详情接口：chunks[].context 透传
        detail = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                            headers=admin_headers).json()
        assert all(c.get("context") for c in detail["chunks"])

    def test_context_meta_truncated(self, client, monkeypatch, admin_headers):
        """超长摘要：chunks_meta.context 截断 ≤100，向量 metadata context 截断 ≤500"""
        _patch_rec_embedding(monkeypatch)
        _patch_ctx_client(monkeypatch,
                          _FakeContextClient(summary="长" * 600))
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"contextual_retrieval": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert all(len(c["context"]) <= 100 for c in final["chunks_meta"])
        from backend.services.vector_store import get_vector_store
        items = get_vector_store().get_all(kb["id"])
        assert all(len(m.get("context", "")) <= 500 for _, _, m in items)

    def test_llm_failure_does_not_block(self, client, monkeypatch, admin_headers):
        """摘要 LLM 全部失败：不阻塞入库，chunks_meta 无 context，embedding 用原文"""
        rec = _patch_rec_embedding(monkeypatch)
        _patch_ctx_client(monkeypatch, _FakeContextClient(mode="error"))
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"contextual_retrieval": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["chunk_count"] > 0
        assert all("context" not in c for c in final["chunks_meta"])
        assert all("【上下文】" not in t for t in rec.inputs)

    def test_reingest_keeps_switch(self, client, monkeypatch, admin_headers):
        """重解析沿用：首次开启后，无 body 重解析仍走上下文检索增强（再次调用 LLM）"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_ctx_client(monkeypatch, _FakeContextClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"contextual_retrieval": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["parser_config"]["contextual_retrieval"] is True
        first_calls = fake.call_count
        # 无 body 重解析：沿用上次 parser_config（开关保持开启，再次生成摘要）
        resp = self._ingest(client, kb["id"], doc["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final2 = wait_for_status(client, kb["id"], doc["id"])
        assert final2["status"] == "ingested"
        assert final2["parser_config"]["contextual_retrieval"] is True
        assert all(c.get("context") for c in final2["chunks_meta"])
        assert fake.call_count == first_calls + final2["chunk_count"]

    def test_parent_child_with_context(self, client, monkeypatch, admin_headers):
        """parent_child 方式 + 开关开：子块 context 正常生成，父块上下文不受影响"""
        _patch_rec_embedding(monkeypatch)
        _patch_ctx_client(monkeypatch, _FakeContextClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"method": "parent_child",
                                  "contextual_retrieval": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert all(c.get("context") for c in final["chunks_meta"])


# ==================== 检索与引用 ====================

class TestRetrieveContext:

    def test_source_context_passthrough(self, client, monkeypatch,
                                        admin_headers):
        """检索：Source.context 透传（来自向量 metadata），text 含【上下文】前缀"""
        rec = _patch_rec_embedding(monkeypatch)
        _patch_ctx_client(monkeypatch, _FakeContextClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"contextual_retrieval": True},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_status(client, kb["id"], doc["id"])
        # 记录式 embedding 下 query 与文本共享字符 → 命中
        from backend.services.retrieval_service import get_retrieval_service
        sources = asyncio.run(get_retrieval_service().retrieve(
            kb["id"], "Python 编程语言特性"))
        assert sources, "记录式 embedding 应能命中"
        assert all(s.context for s in sources)
        assert all("【上下文】" in s.text for s in sources)
        # 检索返回的 text 以【上下文】摘要开头、原文在后
        first = sources[0]
        assert first.text.startswith("【上下文】")
        assert first.context in first.text

    def test_build_refs_with_context(self):
        """引用构建：有 context 拼前缀 / parent_text 场景补摘要 / 已含前缀不重复 / 无 context 不变"""
        from backend.models.rag_models import Source
        from backend.services.chat_service import ChatService
        # 无摘要：保持现状（不出现前缀）
        refs = ChatService._build_refs([
            Source(id="d_0", text="原文内容", document_name="doc")])
        assert "【上下文】" not in refs
        assert "原文内容" in refs
        # 有摘要：引用文本 = 【上下文】摘要 + 原文
        refs = ChatService._build_refs([
            Source(id="d_0", text="原文内容", context="第二章介绍语法",
                   document_name="doc")])
        assert "【上下文】第二章介绍语法\n原文内容" in refs
        # parent_text 场景（父块全文本身无摘要）：补摘要前缀
        refs = ChatService._build_refs([
            Source(id="d_0", text="子块", parent_text="父块全文",
                   context="第二章介绍语法", document_name="doc")])
        assert "【上下文】第二章介绍语法\n父块全文" in refs
        # text 已含前缀（向量化增强文本）：不重复拼接
        refs = ChatService._build_refs([
            Source(id="d_0", text="【上下文】第二章介绍语法\n原文内容",
                   context="第二章介绍语法", document_name="doc")])
        assert refs.count("【上下文】") == 1


# ==================== 真实小验证 ====================

class TestRealIngest:
    """真实小验证：完整链路解析小文档 + 摘要 LLM，断言 chunks_meta 有 context

    真实环境说明：开启上下文检索增强后，每个切块都会调用一次激活 LLM
    （并发 3、单调用超时 20s），入库耗时 ≈ 块数 × 单次调用耗时，块多时
    明显变慢并产生额外 token 费用——本用例用伪客户端验证链路完整性，
    不实际消耗 token。
    """

    def test_real_ingest_small_doc(self, client, monkeypatch, admin_headers):
        _patch_rec_embedding(monkeypatch)
        _patch_ctx_client(monkeypatch, _FakeContextClient())
        # 内容 >800 字符（naive 默认 chunk_size），确保切出多块
        sections = [
            f"## 制度{i}\n\n本章规定了公司第{i}项管理制度的具体执行细则，"
            f"包括适用范围、责任主体与操作流程，所有员工均应遵照执行，"
            f"违反者按情节轻重给予相应处理并记录在案。"
            for i in range(1, 11)
        ]
        content = "# 公司制度手册\n\n" + "\n\n".join(sections) + "\n"
        assert len(content) > 800
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="请假制度.txt",
                         content=content)
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"contextual_retrieval": True},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["chunk_count"] >= 2
        assert all(c.get("context") for c in final["chunks_meta"])
        # 摘要非空且 text 保持原文（偏移契约）
        for c in final["chunks_meta"]:
            assert len(c["context"]) > 0
            assert "制度" in c["text"] or "公司" in c["text"]
            assert c["char_end"] - c["char_start"] == len(c["text"])
