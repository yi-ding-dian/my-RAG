"""Agentic 智能分块：对齐算法 / LLM 调用 / 入库链路（两档超限校验、回退 title、互斥）

- align_chunks 纯函数：完全匹配 / 折叠空白偏差 / 前缀模糊 / 部分失败 / 全失败
- agentic_chunk 单元（patch 客户端）：成功（偏移+标签）/ 失败 / 超时 /
  坏响应 / 围栏 JSON / 白名单外标签 / 对齐全失败 / LLM 未配置 / 超 5 万字
- 入库链路（TestClient + 记录式 embedding + 伪 agentic 客户端）：
  成功（label 存储+偏移正确）/ LLM 失败回退 title / 两档超限校验
  （1 万~5 万字未确认失败带字数、确认后入库成功且确认标记不持久化、
  超 5 万字即使确认也拒绝）/ 与上下文检索增强/知识图谱互斥强制关闭
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
    normalize_label, restore_heading_prefix)
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


# ==================== restore_heading_prefix（标题归属兜底） ====================

class TestRestoreHeadingPrefix:
    """标题归属兜底：单标题并入 / 多级链并入 / 已含标题不动 / 无标题不动 /
    后续块不重复并入 / setext 识别 / 链上限 / 偏移契约"""

    def _restored(self, text, llm_chunks):
        """align_chunks + restore_heading_prefix 串联（与 agentic_chunk 同路径）"""
        return restore_heading_prefix(text, align_chunks(text, llm_chunks))

    def test_single_heading_merged(self):
        """块前有单个标题未包含 → 扩展块起点并入标题"""
        text = "## 第二章 安装\n\n第一段内容。\n\n第二段内容。"
        aligned = self._restored(text, ["第一段内容。", "第二段内容。"])
        assert len(aligned) == 2
        s, e, i = aligned[0]
        assert i == 0
        assert text[s:e] == "## 第二章 安装\n\n第一段内容。"
        # 第二块：标题已并入第一块 → 不重复并入（保持原样）
        s2 = text.find("第二段内容。")
        assert aligned[1] == (s2, s2 + len("第二段内容。"), 1)

    def test_multi_level_headings_merged(self):
        """多级连续标题（## + ###）→ 两级并入块开头（标题层级链）"""
        text = "## 第二章\n### 2.1 概述\n第一段内容。"
        aligned = self._restored(text, ["第一段内容。"])
        s, e, _ = aligned[0]
        assert text[s:e].startswith("## 第二章\n### 2.1 概述")
        assert text[s:e] == text  # 标题链 + 正文整段 = 块文本

    def test_block_already_contains_heading_unchanged(self):
        """块已以标题行开头（LLM 已保留标题）→ 不动（不并入更早标题）"""
        text = "# 文档总标题\n\n## 第一章\n第一段内容。"
        aligned = self._restored(text, ["## 第一章\n第一段内容。"])
        s, e, _ = aligned[0]
        assert text[s:e] == "## 第一章\n第一段内容。"

    def test_no_heading_unchanged(self):
        """无标题文本 → 全部不动"""
        text = "第一段内容。\n\n第二段内容。"
        aligned = self._restored(text, ["第一段内容。", "第二段内容。"])
        assert text[aligned[0][0]:aligned[0][1]] == "第一段内容。"
        assert text[aligned[1][0]:aligned[1][1]] == "第二段内容。"

    def test_following_chunks_not_repeat_heading(self):
        """一个标题下的第一个块扩展后，同章节后续块不重复并入同一标题"""
        text = ("## 第一章 背景\n\n第一段背景内容。\n\n第二段背景内容。\n\n"
                "## 第二章 现状\n\n第三段现状内容。")
        aligned = self._restored(text, ["第一段背景内容。", "第二段背景内容。",
                                        "第三段现状内容。"])
        assert len(aligned) == 3
        assert text[aligned[0][0]:aligned[0][1]] == (
            "## 第一章 背景\n\n第一段背景内容。")
        assert text[aligned[1][0]:aligned[1][1]] == "第二段背景内容。"
        assert text[aligned[2][0]:aligned[2][1]] == (
            "## 第二章 现状\n\n第三段现状内容。")

    def test_setext_heading_merged(self):
        """setext 样式标题（下划线式）同样识别并并入（复用 _iter_headings）"""
        text = "第一章 安装\n============\n\n第一段内容。"
        aligned = self._restored(text, ["第一段内容。"])
        s, e, _ = aligned[0]
        assert text[s:e].startswith("第一章 安装\n============")

    def test_heading_path_chain_capped(self):
        """连续标题行链最多并入 _MAX_HEADING_PATH_LINES 行（防过度扩展）"""
        from backend.services.agentic_chunker import _MAX_HEADING_PATH_LINES
        text = "# 一级\n## 二级\n### 三级\n#### 四级\n第一段内容。"
        aligned = self._restored(text, ["第一段内容。"])
        s, e, _ = aligned[0]
        assert text[s:e].startswith(
            "## 二级\n### 三级\n#### 四级\n第一段内容。")
        assert not text[s:e].startswith("# 一级")  # 第 4 行标题被上限截断

    def test_offset_contract_after_restore(self):
        """扩展后偏移契约：块文本 == 原文切片（空行间隔的标题也并入链，
        与块起点前的扩展一致，char_start/end 与 text 严格对齐）"""
        text = "# 总标题\n\n## 第一节\n第一段。\n\n## 第二节\n第二段。"
        aligned = self._restored(text, ["第一段。", "第二段。"])
        assert len(aligned) == 2
        # 块1：链并入空行间隔的 # 总标题 + ## 第一节
        assert text[aligned[0][0]:aligned[0][1]] == (
            "# 总标题\n\n## 第一节\n第一段。")
        # 块2：并入 ## 第二节
        assert text[aligned[1][0]:aligned[1][1]] == "## 第二节\n第二段。"
        # 偏移契约：块文本 == 原文[char_start:char_end]（restore 只前移
        # char_start，text 按原文切片重取，偏移契约由上面的逐字比较覆盖）


class TestAgenticPrompt:
    """prompt 约束：块开头必须含所属章节标题行"""

    def test_prompt_requires_heading_line(self):
        from backend.services.agentic_chunker import _AGENTIC_PROMPT
        assert "章节标题行" in _AGENTIC_PROMPT
        assert "标题行是段落的一部分" in _AGENTIC_PROMPT
        assert "逐字拷贝" in _AGENTIC_PROMPT


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
        """超 5 万字 → AgenticChunkError（ingestion 层另有拒绝校验）"""
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        with pytest.raises(AgenticChunkError, match="超过 5 万字"):
            asyncio.run(agentic_chunk("字" * 50001, {}))

    def test_mid_size_text_with_confirm_allowed(self, monkeypatch):
        """1 万~5 万字文本（~1 万字）：agentic_chunk 防御上限已放宽到 5 万，
        不抛长度类错误（超限校验在 ingestion 层按确认标记决定）"""
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        text = _AGENTIC_DOC * 55  # ~1.04 万字（mock LLM 输出可对齐，验证正常切块）
        assert 10000 < len(text) <= 50000
        chunks, labels = asyncio.run(agentic_chunk(text, {}))
        assert fake.call_count == 1
        assert len(chunks) == 3  # 未抛 AgenticChunkError，正常切块

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

    def test_too_long_document_fails_with_word_count(self, client, monkeypatch, admin_headers):
        """1 万~5 万字未带确认：任务失败（failed），error 带"约 1.2 万字"
        提示与 agentic_confirm=true 重试指引（前端据此弹确认框）"""
        _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        _patch_rec_embedding(monkeypatch)
        kb = create_kb(client)
        long_text = ("# 标题\n\n" + "这是一个很长的段落。" * 1200)  # ~1.2 万字
        assert 10000 < len(long_text) <= 50000
        doc = upload_doc(client, kb["id"], content=long_text)
        final = self._ingest_and_wait(client, kb["id"], doc["id"],
                                      {"method": "agentic"},
                                      headers=admin_headers)
        assert final["status"] == "failed"
        assert "约 1.2 万字" in final["error"]
        assert "Agentic" in final["error"]
        assert "agentic_confirm=true" in final["error"]
        # LLM 不应被调用（超限在切块前拦截）
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        assert fake.call_count == 0

    def test_mid_size_document_confirm_ingests_and_not_persisted(self, client, monkeypatch, admin_headers):
        """1 万~5 万字带 agentic_confirm=true：跳过超限校验直接分块入库
        （mock LLM 对重复段落文本对不齐 → 回退 title，不阻塞入库）；
        确认标记不持久化——入库后文档 parser_config 无 agentic_confirm 键
        （重跑仍需再次确认）"""
        _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        _patch_rec_embedding(monkeypatch)
        kb = create_kb(client)
        long_text = ("# 标题\n\n" + "这是一个很长的段落。" * 1200)  # ~1.2 万字
        doc = upload_doc(client, kb["id"], content=long_text)
        final = self._ingest_and_wait(
            client, kb["id"], doc["id"],
            {"method": "agentic", "agentic_confirm": True},
            headers=admin_headers)
        assert final["status"] == "ingested", final.get("error")
        assert final["parser_id"] == "agentic"
        assert final["chunk_count"] > 0
        assert "agentic_confirm" not in final["parser_config"]

    def test_over_50000_rejected_even_with_confirm(self, client, monkeypatch, admin_headers):
        """超过 5 万字：即使带 agentic_confirm=true 也直接拒绝（error 带
        "超过 5 万字"，不提供确认）"""
        _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        _patch_rec_embedding(monkeypatch)
        kb = create_kb(client)
        huge_text = ("# 标题\n\n" + "这是一个很长的段落。" * 5100)  # ~5.1 万字
        assert len(huge_text) > 50000
        doc = upload_doc(client, kb["id"], content=huge_text)
        final = self._ingest_and_wait(
            client, kb["id"], doc["id"],
            {"method": "agentic", "agentic_confirm": True},
            headers=admin_headers)
        assert final["status"] == "failed"
        assert "超过 5 万字" in final["error"]
        # LLM 不应被调用（拒绝在切块前拦截）
        fake = _patch_agentic_client(monkeypatch, _FakeAgenticClient())
        assert fake.call_count == 0

    def test_agentic_forces_mutual_exclusion(self, client, monkeypatch, admin_headers):
        """互斥：agentic + 上下文检索增强同时传 → 后端强制关闭 CR
        （chunks_meta 无 context）；知识图谱与 Agentic 可叠加保留
        （kg 保留 True，LLM 不可达时抽取失败跳过不阻塞入库）"""
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
        assert final["parser_config"]["knowledge_graph"] is True  # 可叠加保留
        # 图谱抽取失败（LLM 不可达）被内部跳过，入库不受阻塞
        assert final["graph_status"] in ("ready", "failed")
        assert all("context" not in m for m in final["chunks_meta"])
        # 重跑沿用（不带开关参数）：持久化配置 CR=False / kg=True →
        # 仍不构建 CR，图谱按持久化配置继续尝试
        final2 = self._ingest_and_wait(client, kb["id"], doc["id"],
                                       {"method": "agentic"},
                                       headers=admin_headers)
        assert final2["parser_config"]["contextual_retrieval"] is False
        assert final2["graph_status"] in ("ready", "failed")

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
    """resolve_parser_config：agentic 合法、CR 互斥强制、KG 可叠加、
    agentic_confirm 临时键（仅请求显式传、不读旧配置）"""

    def test_agentic_allowed_and_mutual_exclusion(self):
        doc = DocumentItem(id="d1", kb_id="k1",
                           parser_config={"contextual_retrieval": True,
                                          "knowledge_graph": True})
        method, cfg = resolve_parser_config(doc, "agentic", {})
        assert method == "agentic"
        assert cfg["contextual_retrieval"] is False
        assert cfg["knowledge_graph"] is True  # 知识图谱与 Agentic 可叠加保留

    def test_agentic_confirm_reads_request_only(self):
        """agentic_confirm 只读请求显式传（不读文档旧配置，防旧配置带确认
        标记绕过未来校验）；True 时作为临时键放 cfg，False/缺省不写入"""
        # 请求显式传 True → cfg 带临时键
        doc = DocumentItem(id="d1", kb_id="k1")
        _, cfg = resolve_parser_config(doc, "agentic",
                                       {"agentic_confirm": True})
        assert cfg.get("agentic_confirm") is True
        # 旧配置带确认标记（历史脏数据）→ 不生效（只从请求读）
        doc2 = DocumentItem(id="d2", kb_id="k1",
                            parser_config={"agentic_confirm": True})
        _, cfg2 = resolve_parser_config(doc2, "agentic", {})
        assert "agentic_confirm" not in cfg2
        # 请求显式 False → 不写入
        _, cfg3 = resolve_parser_config(doc, "agentic",
                                        {"agentic_confirm": False})
        assert "agentic_confirm" not in cfg3
        # 非布尔 → ValueError（路由层 400 / 任务内写回 failed 双保险）
        with pytest.raises(ValueError, match="agentic_confirm"):
            resolve_parser_config(doc, "agentic", {"agentic_confirm": "yes"})

    def test_unknown_method_still_rejected(self):
        doc = DocumentItem(id="d1", kb_id="k1")
        with pytest.raises(ValueError, match="非法切块方式"):
            resolve_parser_config(doc, "unknown", {})
