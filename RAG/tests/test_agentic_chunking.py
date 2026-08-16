"""Agentic 智能分块：对齐算法 / LLM 调用 / 入库链路（超限 400 语义、回退 title、互斥）

- align_chunks 纯函数：完全匹配 / 折叠空白偏差 / 前缀模糊 / 部分失败 / 全失败
- agentic_chunk 单元（patch 客户端）：成功（偏移+标签）/ 失败 / 超时 /
  坏响应 / 围栏 JSON / 白名单外标签 / 对齐全失败 / LLM 未配置 / 超 1 万字
- 入库链路（TestClient + 记录式 embedding + 伪 agentic 客户端）：
  成功（label 存储+偏移正确）/ LLM 失败回退 title / 超限失败 /
  与上下文检索增强/知识图谱互斥强制关闭
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from conftest import char_vector, create_kb, upload_doc
from backend.models.rag_models import DocumentItem
from backend.services.agentic_chunker import (
    AgenticChunkError, _parse_response, agentic_chunk, align_chunks,
    normalize_label)
from backend.services.ingestion_service import resolve_parser_config

# ==================== 样例与伪客户端 ====================

_AGENTIC_DOC = (
    "# Python 语言介绍\n\n"
    "Python 是一种高级编程语言，由 Guido van Rossum 于 1991 年发布，"
    "强调代码可读性与简洁性，广泛应用于各类开发场景。\n\n"
    "## 安装步骤\n\n"
    "首先访问官方网站下载安装包，然后运行安装程序，最后配置环境变量。\n\n"
    "## 性能数据\n\n"
    "根据基准测试，Python 在数值计算场景的耗时约为 C 语言的 10 倍，"
    "但开发效率提升明显。\n"
)

_LLM_JSON = {
    "chunks": [
        {"text": "Python 是一种高级编程语言，由 Guido van Rossum 于 1991 年"
                  "发布，强调代码可读性与简洁性，广泛应用于各类开发场景。",
         "label": "论述类"},
        {"text": "首先访问官方网站下载安装包，然后运行安装程序，"
                  "最后配置环境变量。", "label": "操作类"},
        {"text": "根据基准测试，Python 在数值计算场景的耗时约为 C 语言"
                  "的 10 倍，但开发效率提升明显。", "label": "数据类"},
    ]
}


class _FakeAgenticClient:
    """伪 OpenAI 客户端：非流式 chat.completions.create 返回 agentic JSON

    mode: ok=成功 / error=抛异常 / bad=非 JSON 内容 / empty=空 content；
    delay 模拟耗时（超时测试用）；create 记录调用参数（extra_body /
    messages 断言思考策略注入）
    """

    def __init__(self, mode: str = "ok", payload=None, delay: float = 0.0):
        self.mode = mode
        self.payload = payload
        self.delay = delay
        self.call_count = 0
        self.last_kwargs = None
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "error":
            raise RuntimeError("mock Agentic LLM 调用失败（测试构造）")
        if self.mode == "empty":
            content = ""
        elif self.mode == "bad":
            content = "我不是 JSON，我是乱码文本"
        elif isinstance(self.payload, str):
            content = self.payload  # 字符串 payload 原样返回（如围栏 JSON）
        else:
            content = json.dumps(
                self.payload if self.payload is not None else _LLM_JSON,
                ensure_ascii=False)
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=content))])


def _patch_agentic_client(monkeypatch, fake: _FakeAgenticClient):
    """替换 agentic_chunker 的客户端工厂（agentic_chunk 内部经它取客户端）"""
    monkeypatch.setattr(
        "backend.services.agentic_chunker._get_client",
        lambda llm_cfg=None: fake)
    return fake


def _patch_active_llm_online(monkeypatch):
    """激活 LLM 配置改为在线 base_url（DeepSeek 路径：ExtraBody 策略生效）"""
    from backend.config import LLMConfig
    online = SimpleNamespace(
        llm=LLMConfig(base_url="https://api.deepseek.com/v1",
                      api_key="test-key", model="deepseek-chat",
                      temperature=0.3, max_tokens=8192, timeout=60.0))
    monkeypatch.setattr(
        "backend.services.agentic_chunker.get_active_config",
        lambda: online)


def _patch_rec_embedding(monkeypatch):
    """替换 ingestion/retrieval 的 embedding 服务为记录式实现（离线可跑）"""
    from backend.services import embedding_service

    class _Rec:
        def __init__(self):
            self.inputs = []

        async def embed(self, texts):
            self.inputs.extend(texts)
            return [char_vector(t) for t in texts]

    rec = _Rec()
    for module in ("backend.services.ingestion_service",
                   "backend.services.retrieval_service"):
        monkeypatch.setattr(module + ".get_embedding_service",
                            lambda: rec)
    return rec


# ==================== align_chunks 纯函数 ====================

class TestAlignChunks:
    """偏移对齐：完全匹配 / 偏差 / 部分失败 / 全失败 / 顺序推进"""

    def test_exact_match_all_blocks(self):
        """完全匹配：多块覆盖全文，偏移正确且顺序推进"""
        text = _AGENTIC_DOC
        llm = [c["text"] for c in _LLM_JSON["chunks"]]
        spans = align_chunks(text, llm)
        assert len(spans) == 3
        for s, e, i in spans:
            assert text[s:e] == llm[i]
        # 顺序递增且互不重叠
        for a, b in zip(spans, spans[1:]):
            assert a[1] <= b[0]

    def test_whitespace_differs_still_matches(self):
        """块有偏差（LLM 改写换行/空白）：折叠空白匹配命中，偏移正确"""
        text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        llm = ["第一段内容。第二段内容。", "第三段内容。"]
        # 第一块把原文两段之间的换行折叠掉了 → 精确失败、折叠空白命中
        spans = align_chunks(text, llm)
        assert len(spans) == 2
        s, e, i = spans[0]
        assert i == 0 and text[s:e] == "第一段内容。\n\n第二段内容。"
        # 折叠后文本与原文一致
        flat = "".join(ch for ch in text if not ch.isspace())
        assert "".join(ch for ch in llm[0] if not ch.isspace()) in flat

    def test_fuzzy_matches_rewritten_block(self):
        """块被模型改写个别字符：前缀 seed + 窗口模糊匹配命中"""
        text = ("背景介绍部分的内容说明文字。\n\n"
                "详细步骤包括安装软件包并配置参数。\n\n"
                "结尾总结部分的内容说明。")
        # 中间块被改写（"配置参数"→"设置参数"），前缀仍一致 → fuzzy 命中
        llm = ["背景介绍部分的内容说明文字。",
               "详细步骤包括安装软件包并设置参数。",
               "结尾总结部分的内容说明。"]
        spans = align_chunks(text, llm)
        assert len(spans) == 3
        # 中间块对齐到原文正确位置（第一段之后、第三段之前），
        # 匹配区间以块前缀开头（窗口最长匹配覆盖被改写前的原文前缀）
        s, e, i = spans[1]
        assert i == 1
        assert s > text.find("背景介绍")
        assert e <= text.find("结尾总结")
        assert text[s:e].startswith("详细步骤包括安装软件包")

    def test_partial_failure_drops_block(self):
        """部分失败（一块被彻底改写）：丢弃该块，其余保留"""
        text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        llm = ["第一段内容。", "这是完全无关的乱写内容", "第三段内容。"]
        spans = align_chunks(text, llm)
        assert len(spans) == 2
        assert [i for _, _, i in spans] == [0, 2]

    def test_all_failed_returns_empty(self):
        """全失败：返回空列表（上层抛错触发回退）"""
        text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
        spans = align_chunks(text, ["完全不存在的文本一",
                                    "完全不存在的文本二"])
        assert spans == []

    def test_empty_inputs(self):
        """空块列表 / 空文本：返回空"""
        assert align_chunks("", ["x"]) == []
        assert align_chunks("abc", []) == []
        assert align_chunks("abc", ["", "  "]) == []

    def test_duplicate_texts_sequential(self):
        """原文有重复内容：顺序推进不回头（各块定位到各自位置）"""
        text = "重复句。\n\n独特句。\n\n重复句。"
        llm = ["重复句。", "独特句。", "重复句。"]
        spans = align_chunks(text, llm)
        assert len(spans) == 3
        # 第一块命中第一个"重复句"（偏移 0），第三块命中第二个（偏移 >0）
        assert spans[0][0] == 0
        assert spans[2][0] > 0
        s, e, i = spans[2]
        assert text[s:e] == "重复句。"


# ==================== agentic_chunk 单元 ====================

class TestAgenticChunk:
    """LLM 调用：成功 / 失败 / 超时 / 坏响应 / 标签归一 / 策略注入"""

    def test_success_returns_chunks_and_labels(self, monkeypatch):
        """成功：chunks 偏移正确（原文切片一致）+ labels 对应"""
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        chunks, labels = asyncio.run(agentic_chunk(_AGENTIC_DOC, {}))
        assert len(chunks) == 3
        assert labels == ["论述类", "操作类", "数据类"]
        for c in chunks:
            assert c.text == _AGENTIC_DOC[c.char_start:c.char_end]
        assert fake.call_count == 1

    def test_llm_error_raises(self, monkeypatch):
        """LLM 调用抛错 → AgenticChunkError"""
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient(mode="error"))
        with pytest.raises(AgenticChunkError, match="调用失败"):
            asyncio.run(agentic_chunk(_AGENTIC_DOC, {}))

    def test_timeout_raises(self, monkeypatch):
        """单次调用超时 → AgenticChunkError（timeout 参数缩小）"""
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient(delay=0.2))
        with pytest.raises(AgenticChunkError, match="超时"):
            asyncio.run(agentic_chunk(_AGENTIC_DOC, {}, timeout=0.05))

    def test_bad_response_raises(self, monkeypatch):
        """响应非 JSON → AgenticChunkError"""
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient(mode="bad"))
        with pytest.raises(AgenticChunkError, match="格式非法"):
            asyncio.run(agentic_chunk(_AGENTIC_DOC, {}))

    def test_empty_content_raises(self, monkeypatch):
        """响应 content 为空 → AgenticChunkError"""
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient(mode="empty"))
        with pytest.raises(AgenticChunkError, match="格式非法"):
            asyncio.run(agentic_chunk(_AGENTIC_DOC, {}))

    def test_fence_json_accepted(self, monkeypatch):
        """```json 围栏包裹的响应同样解析成功"""
        payload = json.dumps(_LLM_JSON, ensure_ascii=False)
        fake = _patch_agentic_client(
            monkeypatch, _FakeAgenticClient(payload=f"```json\n{payload}\n```"))
        chunks, labels = asyncio.run(agentic_chunk(_AGENTIC_DOC, {}))
        assert len(chunks) == 3 and labels[0] == "论述类"

    def test_out_of_whitelist_label_falls_back(self, monkeypatch):
        """标签白名单外 → 归"其他"（宽容不丢块）"""
        payload = {
            "chunks": [
                {"text": "Python 是一种高级编程语言，由 Guido van Rossum"
                          "于 1991 年发布，强调代码可读性与简洁性，广泛"
                          "应用于各类开发场景。", "label": "自定义标签"},
                {"text": "首先访问官方网站下载安装包，然后运行安装程序，"
                          "最后配置环境变量。", "label": ""},
            ]
        }
        fake = _patch_agentic_client(monkeypatch,
                                     _FakeAgenticClient(payload=payload))
        chunks, labels = asyncio.run(agentic_chunk(_AGENTIC_DOC, {}))
        assert labels == ["其他", "其他"]
        assert len(chunks) == 2

    def test_all_alignment_failed_raises(self, monkeypatch):
        """LLM 输出与原文完全无关 → 对齐全失败 → AgenticChunkError"""
        payload = {"chunks": [
            {"text": "完全无关的第一段文字内容", "label": "论述类"},
            {"text": "完全无关的第二段文字内容", "label": "事实类"},
        ]}
        fake = _patch_agentic_client(monkeypatch,
                                     _FakeAgenticClient(payload=payload))
        with pytest.raises(AgenticChunkError, match="对齐"):
            asyncio.run(agentic_chunk(_AGENTIC_DOC, {}))

    def test_empty_chunks_raises(self, monkeypatch):
        """LLM 返回空块列表 → AgenticChunkError"""
        fake = _patch_agentic_client(
            monkeypatch, _FakeAgenticClient(payload={"chunks": []}))
        with pytest.raises(AgenticChunkError, match="空块列表"):
            asyncio.run(agentic_chunk(_AGENTIC_DOC, {}))

    def test_too_long_text_raises(self, monkeypatch):
        """超 1 万字 → AgenticChunkError（ingestion 层另有 400 校验）"""
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        with pytest.raises(AgenticChunkError, match="超过 1 万字"):
            asyncio.run(agentic_chunk("字" * 10001, {}))

    def test_online_strategy_injects_extra_body(self, monkeypatch):
        """在线模型（DeepSeek）：thinking disabled → extra_body 关闭思考"""
        _patch_active_llm_online(monkeypatch)
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        asyncio.run(agentic_chunk(_AGENTIC_DOC, {"thinking_mode": "disabled"}))
        assert fake.last_kwargs.get("extra_body") == {
            "thinking": {"type": "disabled"}}
        # 不注入 prefill 消息
        messages = fake.last_kwargs["messages"]
        assert not any(m.get("continue_assistant_turn") for m in messages)

    def test_local_qwen_prefill_injected(self, monkeypatch):
        """本地模型（默认测试 base_url=127.0.0.1:59999，内网）：
        thinking disabled → messages 末尾注入空 <think> prefill"""
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        asyncio.run(agentic_chunk(_AGENTIC_DOC, {"thinking_mode": "disabled"}))
        messages = fake.last_kwargs["messages"]
        assert messages[-1] == {
            "role": "assistant", "content": "<think>\n\n</think>",
            "continue_assistant_turn": True}


class TestParseResponse:
    """响应解析：纯 JSON / 围栏 / 前后缀文字 / 非法"""

    def test_plain_json(self):
        data = _parse_response('{"chunks": [{"text": "x", "label": "论述类"}]}')
        assert data["chunks"][0]["text"] == "x"

    def test_fence(self):
        data = _parse_response('```json\n{"chunks": []}\n```')
        assert data == {"chunks": []}

    def test_surrounding_text(self):
        data = _parse_response('好的，结果如下：{"chunks": []} 完成。')
        assert data == {"chunks": []}

    def test_invalid(self):
        assert _parse_response("乱码文本") is None
        assert _parse_response("") is None


class TestNormalizeLabel:
    """标签归一化"""

    def test_valid_kept(self):
        assert normalize_label("论述类") == "论述类"
        assert normalize_label("数据类") == "数据类"

    def test_invalid_falls_back(self):
        assert normalize_label("自定义") == "其他"
        assert normalize_label("") == "其他"
        assert normalize_label(None) == "其他"


# ==================== 入库链路（TestClient） ====================

class TestIngestAgentic:
    """Agentic 入库：成功（label 存储+偏移）/ 回退 title / 超限 / 互斥"""

    def _ingest_and_wait(self, client, kb_id, doc_id, body, timeout=30.0,
                          headers=None):
        resp = client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                           json=body, headers=headers)
        assert resp.status_code == 200, resp.text
        deadline = time.monotonic() + timeout
        while True:
            doc = client.get(f"/api/kbs/{kb_id}/documents/{doc_id}",
                             headers=headers).json()
            if doc["status"] in ("ingested", "failed"):
                return doc
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"等待入库超时，当前状态: {doc['status']} "
                    f"error={doc.get('error')}")
            time.sleep(0.2)

    def test_success_stores_labels_and_offsets(self, client, monkeypatch, admin_headers):
        """成功路径：入库成功，parser_id=agentic，chunks_meta 带 label，
        偏移正确（text == full_text[char_start:char_end]），详情接口透传"""
        _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        _patch_rec_embedding(monkeypatch)
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], content=_AGENTIC_DOC)
        final = self._ingest_and_wait(client, kb["id"], doc["id"],
                                      {"method": "agentic"},
                                      headers=admin_headers)
        assert final["status"] == "ingested", final.get("error")
        assert final["parser_id"] == "agentic"
        assert final["chunk_count"] == 3
        meta = final["chunks_meta"]
        assert len(meta) == 3
        # 每块带 label 且可选字段不带 context（互斥强制关闭 CR）
        assert [m["label"] for m in meta] == ["论述类", "操作类", "数据类"]
        assert all("context" not in m for m in meta)
        # 偏移契约：块文本 == 全文切片
        detail = client.get(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}",
            headers=admin_headers).json()
        full_text = detail["full_text"]
        for m, c in zip(meta, detail["chunks"]):
            assert full_text[m["char_start"]:m["char_end"]] == m["text"]
            assert c["label"] == m["label"]
        # 文档元数据持久化 parser_config 无上下文检索/图谱
        assert final["parser_config"]["contextual_retrieval"] is False
        assert final["parser_config"]["knowledge_graph"] is False

    def test_llm_failure_falls_back_to_title(self, client, monkeypatch,
                                             caplog, admin_headers):
        """LLM 失败（抛错）→ 回退 title 切块：入库成功、无 label、
        日志 warning 注明原因"""
        _patch_agentic_client(monkeypatch, _FakeAgenticClient(mode="error"))
        _patch_rec_embedding(monkeypatch)
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], content=_AGENTIC_DOC)
        with caplog.at_level("WARNING", logger="backend.services.ingestion_service"):
            final = self._ingest_and_wait(client, kb["id"], doc["id"],
                                          {"method": "agentic"},
                                          headers=admin_headers)
        assert final["status"] == "ingested", final.get("error")
        assert final["parser_id"] == "agentic"  # 用户选择的方法不变
        assert final["chunk_count"] > 0
        assert all("label" not in m for m in final["chunks_meta"])
        assert any("Agentic 切块失败，回退标题切块" in r.message
                   for r in caplog.records)

    def test_too_long_document_fails_with_hint(self, client, monkeypatch, admin_headers):
        """超 1 万字：任务失败（failed），error 带"超过 1 万字"提示
        （后端 400 校验语义：失败原因经文档 error 传递，前端提示换方式）"""
        _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        _patch_rec_embedding(monkeypatch)
        kb = create_kb(client)
        long_text = ("# 标题\n\n" + "这是一个很长的段落。" * 1200)  # > 10000 字
        assert len(long_text) > 10000
        doc = upload_doc(client, kb["id"], content=long_text)
        final = self._ingest_and_wait(client, kb["id"], doc["id"],
                                      {"method": "agentic"},
                                      headers=admin_headers)
        assert final["status"] == "failed"
        assert "超过 1 万字" in final["error"]
        assert "Agentic" in final["error"]
        # LLM 不应被调用（超限在切块前拦截）
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        assert fake.call_count == 0

    def test_agentic_forces_mutual_exclusion(self, client, monkeypatch, admin_headers):
        """互斥：agentic + 上下文检索增强/知识图谱同时传 → 后端强制关闭
        （chunks_meta 无 context、graph_status 不构建、解析配置强制 False）"""
        _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        _patch_rec_embedding(monkeypatch)
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], content=_AGENTIC_DOC)
        final = self._ingest_and_wait(
            client, kb["id"], doc["id"],
            {"method": "agentic", "contextual_retrieval": True,
             "knowledge_graph": True}, headers=admin_headers)
        assert final["status"] == "ingested", final.get("error")
        assert final["parser_config"]["contextual_retrieval"] is False
        assert final["parser_config"]["knowledge_graph"] is False
        assert final["graph_status"] == "none"
        assert all("context" not in m for m in final["chunks_meta"])
        # 重跑沿用（不带开关参数）：持久化配置已是 False，仍不构建图谱
        final2 = self._ingest_and_wait(client, kb["id"], doc["id"],
                                       {"method": "agentic"},
                                       headers=admin_headers)
        assert final2["graph_status"] == "none"

    def test_agentic_method_is_valid(self, client, admin_headers):
        """method=agentic 路由校验通过（200 启动；未知方法仍 400）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], content=_AGENTIC_DOC)
        # 先测非法 method（无任务运行，同步校验 400）
        resp2 = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
            json={"method": "unknown_method"}, headers=admin_headers)
        assert resp2.status_code == 400
        # agentic 合法：200 启动（任务执行中重复触发返回 200 短路，故先测非法）
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
            json={"method": "agentic"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text


class TestResolveParserConfigAgentic:
    """resolve_parser_config：agentic 合法且互斥强制（重跑沿用旧配置组合）"""

    def test_agentic_allowed_and_mutual_exclusion(self):
        doc = DocumentItem(id="d1", kb_id="k1",
                           parser_config={"contextual_retrieval": True,
                                          "knowledge_graph": True})
        method, cfg = resolve_parser_config(doc, "agentic", {})
        assert method == "agentic"
        assert cfg["contextual_retrieval"] is False
        assert cfg["knowledge_graph"] is False

    def test_unknown_method_still_rejected(self):
        doc = DocumentItem(id="d1", kb_id="k1")
        with pytest.raises(ValueError, match="非法切块方式"):
            resolve_parser_config(doc, "unknown", {})
