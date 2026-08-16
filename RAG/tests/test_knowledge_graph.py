"""知识图谱功能测试

覆盖：
- 纯函数单测：名称规范化（trim/全半角/数字间空格压缩）、JSON 兜底解析
  （```json 代码块/直接 JSON/平衡括号/截断/非 JSON）、图合并（同名实体
  count/chunk_refs/描述合并截断、关系 weight 累加、同块去重、位置定位）、
  幂等（重入库清旧引用、无重复实体）、图谱文件读写（缺失/损坏兜底）
- 服务层 build_graph_for_doc：开关关不调 LLM、成功构建文件、失败/超时跳过
  不抛异常、部分失败保留成功块、LLM 未配置跳过
- ingestion 集成（TestClient + mock embedding + 伪抽取 LLM 客户端）：
  开关关不生成图谱文件；开关开生成图谱文件 + parser_config 持久化 +
  实体/关系落盘；LLM 失败不阻塞入库；重解析沿用开关且幂等不翻倍
- 接口：图谱不存在 404"该知识库暂无知识图谱"、正常返回、doc_id 过滤、
  跨部门 404 伪装、未认证 401
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import (char_vector, create_department_and_admin, create_kb,
                      create_user, upload_doc, wait_for_status)
from backend.chunking.splitter import Chunk
from backend.services.knowledge_graph_service import (
    GRAPH_DIR, _empty_graph, build_graph_context, build_graph_for_doc,
    build_thinking_extra_body, expand_neighbors, extract_query_entities,
    graph_path, load_graph, match_entities, merge_into_graph, normalize_name,
    parse_extraction_response, parse_query_entities, remove_doc_refs,
    save_graph)

# 固定抽取结果（测试文档三元组，与 conftest.SAMPLE_TEXT 语义无关——
# 伪客户端固定返回，用于断言图谱合并与持久化）
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


# ==================== 伪抽取 LLM 客户端 ====================

class _FakeExtractionClient:
    """伪 OpenAI 客户端：非流式 chat.completions.create 返回固定抽取 JSON

    mode: ok=固定 JSON / bad=非 JSON / empty=空 / error=抛异常；
    delay 模拟耗时（并发与超时测试用）；create 记录调用次数与并发峰值。
    """

    def __init__(self, mode: str = "ok", payload: str = _FIXED_EXTRACTION_JSON,
                 delay: float = 0.0):
        self.mode = mode
        self.payload = payload
        self.delay = delay
        self.call_count = 0
        self.active = 0
        self.peak = 0
        # 每次调用透传的 extra_body 记录（thinking 模式透传断言用）
        self.extra_bodies: list = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.call_count += 1
        self.extra_bodies.append(kwargs.get("extra_body"))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.mode == "error":
                raise RuntimeError("mock 抽取 LLM 调用失败（测试构造）")
            if self.mode == "bad":
                content = "抱歉，我无法完成该任务（非 JSON）"
            elif self.mode == "empty":
                content = ""
            else:
                content = self.payload
            return SimpleNamespace(choices=[
                SimpleNamespace(message=SimpleNamespace(content=content))])
        finally:
            self.active -= 1


def _patch_kg_client(monkeypatch, fake: _FakeExtractionClient) -> _FakeExtractionClient:
    """替换 knowledge_graph_service 的客户端工厂（build_graph_for_doc 内部经它取）"""
    monkeypatch.setattr(
        "backend.services.knowledge_graph_service._get_client",
        lambda llm_cfg=None: fake)
    return fake


def _patch_active_llm_online(monkeypatch):
    """激活 LLM 配置改为在线 base_url（DeepSeek 路径：ExtraBody 策略生效）

    测试默认激活配置 base_url=http://127.0.0.1:1234（本地 LM Studio）→
    thinking disabled 时思考关闭走 QwenPrefill（messages 注入 prefill、
    无 extra_body）；本 helper 用于需要断言"在线 DeepSeek 行为不变"
    （extra_body 透传）的用例。
    """
    from backend.config import LLMConfig
    online = SimpleNamespace(
        llm=LLMConfig(base_url="https://api.deepseek.com/v1",
                      api_key="test-key", model="deepseek-chat",
                      temperature=0.3, max_tokens=8192, timeout=60.0))
    monkeypatch.setattr(
        "backend.services.knowledge_graph_service.get_active_config",
        lambda: online)


def _mk_chunks(texts) -> list:
    """构造简单 Chunk 列表（偏移连续）"""
    out = []
    pos = 0
    for t in texts:
        out.append(Chunk(text=t, char_start=pos, char_end=pos + len(t)))
        pos += len(t) + 1
    return out


# ==================== 思考模式 extra_body 组装（纯函数） ====================

class TestThinkingExtraBody:
    """build_thinking_extra_body：disabled/enabled_low/high/max 四种 extra_body
    正确；disabled 不带 reasoning_effort（DeepSeek 文档约束：思考强度仅思考开启
    时传）；未知值防御返回空（不干预服务端默认）"""

    def test_disabled(self):
        assert build_thinking_extra_body("disabled") == {"thinking": {"type": "disabled"}}
        assert build_thinking_extra_body(None) == {"thinking": {"type": "disabled"}}
        assert build_thinking_extra_body("") == {"thinking": {"type": "disabled"}}

    def test_enabled_levels(self):
        assert build_thinking_extra_body("enabled_low") == {
            "thinking": {"type": "enabled"}, "reasoning_effort": "low"}
        assert build_thinking_extra_body("enabled_high") == {
            "thinking": {"type": "enabled"}, "reasoning_effort": "high"}
        assert build_thinking_extra_body("enabled_max") == {
            "thinking": {"type": "enabled"}, "reasoning_effort": "max"}

    def test_unknown_returns_empty(self):
        """未知值 → {}（不传 extra_body，跟随服务端默认，防御脏数据）"""
        assert build_thinking_extra_body("enabled_ultra") == {}


# ==================== 名称规范化 ====================

class TestNormalizeName:

    def test_trim(self):
        assert normalize_name("  艾伦·图灵  ") == "艾伦·图灵"
        assert normalize_name("\t图灵\t") == "图灵"

    def test_fullwidth_to_halfwidth(self):
        assert normalize_name("ＡＩ") == "AI"
        assert normalize_name("ＧＢＤＴ") == "GBDT"
        assert normalize_name("１２３") == "123"
        assert normalize_name("　图灵　") == "图灵"  # 全角空格

    def test_digit_space_compress(self):
        assert normalize_name("1 9 4 3") == "1943"
        assert normalize_name("15 亿参数") == "15 亿参数"  # 数字与单位间空格保留
        assert normalize_name("GPT 3") == "GPT 3"  # 非数字字符与数字间空格保留

    def test_mixed(self):
        assert normalize_name("  ＬＬＭ 模 型 ") == "LLM 模 型"


# ==================== JSON 兜底解析 ====================

class TestParseExtractionResponse:

    def test_json_block_with_prefix(self):
        """模型包 ```json 代码块且带前后缀文字 → 提取代码块解析成功"""
        content = ("好的，抽取结果如下：```json\n"
                   + _FIXED_EXTRACTION_JSON + "\n```（完）")
        r = parse_extraction_response(content)
        assert r is not None
        assert len(r["entities"]) == 2
        assert r["entities"][0]["name"] == "Python"
        assert r["relations"][0]["type"] == "开发"

    def test_direct_json(self):
        r = parse_extraction_response(_FIXED_EXTRACTION_JSON)
        assert r is not None and len(r["entities"]) == 2

    def test_balanced_braces_with_trailing(self):
        """带前后缀且无代码块 → 平衡括号截取"""
        content = "这是前缀 {" + _FIXED_EXTRACTION_JSON + "} 这是后缀废话"
        r = parse_extraction_response(content)
        assert r is not None and len(r["entities"]) == 2

    def test_not_json_returns_none(self):
        assert parse_extraction_response("抱歉，我无法完成") is None
        assert parse_extraction_response("") is None
        assert parse_extraction_response(None) is None
        assert parse_extraction_response("```json\n不是JSON\n```") is None

    def test_invalid_entity_type_dropped(self):
        """实体类型不在白名单 → 该实体丢弃；全部丢弃 → 合法空结果（非解析失败）"""
        content = json.dumps({
            "entities": [{"name": "X", "type": "非法类型"}],
            "relations": []}, ensure_ascii=False)
        r = parse_extraction_response(content)
        assert r == {"entities": [], "relations": []}
        # 部分合法部分非法：合法保留
        content = json.dumps({
            "entities": [{"name": "图灵", "type": "人物"},
                         {"name": "X", "type": "非法"}],
            "relations": []}, ensure_ascii=False)
        r = parse_extraction_response(content)
        assert r is not None and len(r["entities"]) == 1

    def test_relation_unknown_entity_dropped(self):
        """关系两端必须引用本次抽取的实体 → 非法关系丢弃"""
        content = json.dumps({
            "entities": [{"name": "图灵", "type": "人物"}],
            "relations": [{"source": "图灵", "target": "不存在的实体",
                           "type": "提出"}]}, ensure_ascii=False)
        r = parse_extraction_response(content)
        assert r is not None and r["relations"] == []

    def test_relation_self_loop_dropped(self):
        content = json.dumps({
            "entities": [{"name": "图灵", "type": "人物"}],
            "relations": [{"source": "图灵", "target": "图灵", "type": "相关"}]},
            ensure_ascii=False)
        r = parse_extraction_response(content)
        assert r is not None and r["relations"] == []

    def test_relation_type_whitelist(self):
        content = json.dumps({
            "entities": [{"name": "图灵", "type": "人物"},
                         {"name": "图灵测试", "type": "概念"}],
            "relations": [{"source": "图灵", "target": "图灵测试",
                           "type": "祖传"}]}, ensure_ascii=False)
        r = parse_extraction_response(content)
        assert r is not None and r["relations"] == []

    def test_duplicate_entity_dedup(self):
        """同块重复实体（同名）去重"""
        content = json.dumps({
            "entities": [{"name": "图灵", "type": "人物"},
                         {"name": "图灵", "type": "人物"}],
            "relations": []}, ensure_ascii=False)
        r = parse_extraction_response(content)
        assert r is not None and len(r["entities"]) == 1


# ==================== 图合并 / 幂等 ====================

class TestMergeGraph:

    def _graph(self):
        return {"kb_id": "kb1", "updated_at": "", "docs": {},
                "entities": [], "relations": []}

    def test_new_entities_and_ids(self):
        g = self._graph()
        merge_into_graph(g, "d1", 0, "图灵提出了图灵测试", 0, 12, {
            "entities": [{"name": "图灵", "type": "人物", "description": "数学家"},
                         {"name": "图灵测试", "type": "概念", "description": "测试"}],
            "relations": [{"source": "图灵", "target": "图灵测试", "type": "提出",
                           "description": "提出"}]})
        assert [e["id"] for e in g["entities"]] == ["e1", "e2"]
        assert [r["id"] for r in g["relations"]] == ["r1"]
        # 引用定位：实体名在块内 find → 全文偏移
        e = g["entities"][0]
        assert e["count"] == 1
        assert e["chunk_refs"][0] == {"doc_id": "d1", "chunk_index": 0,
                                      "char_start": 0, "char_end": 2}
        r = g["relations"][0]
        assert r["source"] == "e1" and r["target"] == "e2"
        assert r["weight"] == 1.0

    def test_merge_same_entity_accumulates(self):
        """同名实体跨文档合并：count 累加、chunk_refs 追加、描述拼接截断"""
        g = self._graph()
        merge_into_graph(g, "d1", 0, "图灵是数学家", 0, 7, {
            "entities": [{"name": "图灵", "type": "人物", "description": "英国数学家"}],
            "relations": []})
        merge_into_graph(g, "d2", 3, "图灵提出测试", 20, 28, {
            "entities": [{"name": "图灵", "type": "人物", "description": "人工智能之父"}],
            "relations": []})
        e = g["entities"][0]
        assert len(g["entities"]) == 1  # 合并为同一实体，id 稳定
        assert e["count"] == 2
        assert len(e["chunk_refs"]) == 2
        assert e["chunk_refs"][1]["doc_id"] == "d2"
        assert "英国数学家" in e["description"]
        assert "人工智能之父" in e["description"]
        assert len(e["description"]) <= 200

    def test_description_truncated(self):
        g = self._graph()
        long_desc = "长" * 150
        merge_into_graph(g, "d1", 0, "图灵", 0, 2, {
            "entities": [{"name": "图灵", "type": "人物", "description": long_desc}],
            "relations": []})
        merge_into_graph(g, "d2", 0, "图灵", 0, 2, {
            "entities": [{"name": "图灵", "type": "人物", "description": long_desc}],
            "relations": []})
        assert len(g["entities"][0]["description"]) <= 200

    def test_same_chunk_ref_dedup(self):
        """同 doc+chunk 重复引用 → 覆盖式去重（同块只保留一条）"""
        g = self._graph()
        merge_into_graph(g, "d1", 0, "图灵", 0, 2, {
            "entities": [{"name": "图灵", "type": "人物", "description": ""}],
            "relations": []})
        # 模拟重入库：整篇前清一次旧引用，再合并同块
        remove_doc_refs(g, "d1")
        merge_into_graph(g, "d1", 0, "图灵", 0, 2, {
            "entities": [{"name": "图灵", "type": "人物", "description": ""}],
            "relations": []})
        e = g["entities"][0]
        assert e["count"] == 1
        assert len(e["chunk_refs"]) == 1

    def test_relation_weight_accumulates(self):
        g = self._graph()
        for doc, text, off in (("d1", "图灵提出测试", 0), ("d2", "图灵提出测试", 10)):
            merge_into_graph(g, doc, 0, text, off, off + 6, {
                "entities": [{"name": "图灵", "type": "人物", "description": ""},
                             {"name": "图灵测试", "type": "概念", "description": ""}],
                "relations": [{"source": "图灵", "target": "图灵测试",
                               "type": "提出", "description": ""}]})
        assert len(g["relations"]) == 1
        assert g["relations"][0]["weight"] == 2.0
        assert len(g["relations"][0]["chunk_refs"]) == 2

    def test_locator_fallback_to_chunk_range(self):
        """实体名在块文本中找不到（LLM 输出与原文表述有差异）→ 回退整块区间"""
        g = self._graph()
        merge_into_graph(g, "d1", 0, "这里没有该实体名", 5, 20, {
            "entities": [{"name": "完全不同的名字", "type": "技术", "description": ""}],
            "relations": []})
        ref = g["entities"][0]["chunk_refs"][0]
        assert ref["char_start"] == 5 and ref["char_end"] == 20

    def test_remove_doc_refs(self):
        g = self._graph()
        merge_into_graph(g, "d1", 0, "图灵", 0, 2, {
            "entities": [{"name": "图灵", "type": "人物", "description": ""}],
            "relations": []})
        merge_into_graph(g, "d2", 0, "图灵", 0, 2, {
            "entities": [{"name": "图灵", "type": "人物", "description": ""}],
            "relations": []})
        n = remove_doc_refs(g, "d2")
        assert n == 1
        e = g["entities"][0]
        assert e["count"] == 1 and e["chunk_refs"][0]["doc_id"] == "d1"
        # 全部清空后实体删除
        remove_doc_refs(g, "d1")
        assert g["entities"] == []

    def test_remove_doc_refs_cleans_relations(self):
        """关系引用被清/两端实体消失 → 关系删除"""
        g = self._graph()
        merge_into_graph(g, "d1", 0, "图灵提出测试", 0, 9, {
            "entities": [{"name": "图灵", "type": "人物", "description": ""},
                         {"name": "图灵测试", "type": "概念", "description": ""}],
            "relations": [{"source": "图灵", "target": "图灵测试",
                           "type": "提出", "description": ""}]})
        remove_doc_refs(g, "d1")
        assert g["relations"] == []

    def test_reingest_idempotent(self):
        """重入库幂等：整篇前清一次旧引用再合并（build_graph_for_doc 语义），
        实体/关系不翻倍、引用跨块累积"""
        g = self._graph()
        payload = {
            "entities": [{"name": "图灵", "type": "人物", "description": "a"},
                         {"name": "图灵测试", "type": "概念", "description": "b"}],
            "relations": [{"source": "图灵", "target": "图灵测试",
                           "type": "提出", "description": "c"}]}
        # 第一次入库：块 0 + 块 1 合并（不清除）
        merge_into_graph(g, "d1", 0, "图灵提出测试", 0, 9, payload)
        merge_into_graph(g, "d1", 1, "测试由图灵提出", 10, 19, payload)
        assert len(g["entities"]) == 2
        assert all(len(e["chunk_refs"]) == 2 for e in g["entities"])  # 两块引用累积
        assert g["relations"][0]["weight"] == 2.0
        # 重入库：先清一次旧引用，再合并块 0/1（引用只留本次）
        remove_doc_refs(g, "d1")
        merge_into_graph(g, "d1", 0, "图灵提出测试", 0, 9, payload)
        merge_into_graph(g, "d1", 1, "测试由图灵提出", 10, 19, payload)
        assert len(g["entities"]) == 2  # 不翻倍
        assert len(g["relations"]) == 1
        assert all(len(e["chunk_refs"]) == 2 for e in g["entities"])


# ==================== 图谱文件读写 ====================

class TestPersistence:

    def test_save_load_roundtrip(self, monkeypatch):
        g = {"kb_id": "kb1", "updated_at": "2026-01-01 00:00:00", "docs": {},
             "entities": [{"id": "e1", "name": "图灵", "type": "人物",
                           "description": "数学家", "count": 1,
                           "chunk_refs": [{"doc_id": "d1", "chunk_index": 0,
                                           "char_start": 0, "char_end": 2}]}],
             "relations": []}
        path = save_graph("kb1", g)
        assert path == graph_path("kb1")
        assert path.exists() and path.parent == GRAPH_DIR  # 目录自动创建
        g2 = load_graph("kb1")
        assert g2["kb_id"] == "kb1"
        assert g2["entities"][0]["name"] == "图灵"
        assert g2["entities"][0]["chunk_refs"][0]["doc_id"] == "d1"

    def test_load_missing_returns_empty(self):
        g = load_graph("不存在的kb")
        assert g["entities"] == [] and g["relations"] == []
        assert g["kb_id"] == "不存在的kb"

    def test_load_corrupted_returns_empty(self, tmp_path):
        path = graph_path("corrupted")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{这不是JSON", encoding="utf-8")
        try:
            g = load_graph("corrupted")
            assert g["entities"] == []
        finally:
            path.unlink()

    def test_save_updates_updated_at(self):
        g = load_graph("kb-ts")
        g["docs"]["d1"] = {"name": "doc1", "chunk_count": 1}
        save_graph("kb-ts", g)
        g2 = load_graph("kb-ts")
        assert g2["docs"]["d1"]["name"] == "doc1"
        assert g2["updated_at"]
        Path(graph_path("kb-ts")).unlink()


# ==================== 服务层 build_graph_for_doc ====================

class TestBuildGraphForDoc:

    def test_switch_off_no_llm_call(self, monkeypatch):
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        stats = asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", _mk_chunks(["片段一", "片段二"]),
            cfg={"knowledge_graph": False}))
        assert stats["extracted"] == 0
        assert fake.call_count == 0

    def test_switch_missing_no_llm_call(self, monkeypatch):
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        stats = asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", _mk_chunks(["片段"]), cfg={}))
        assert stats["extracted"] == 0 and fake.call_count == 0

    def test_empty_chunks_no_call(self, monkeypatch):
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        stats = asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", [], cfg={"knowledge_graph": True}))
        assert stats["extracted"] == 0 and fake.call_count == 0

    def test_success_builds_graph_file(self, monkeypatch):
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        chunks = _mk_chunks(["Python 由 Guido van Rossum 开发", "第二块"])
        stats = asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", chunks, cfg={"knowledge_graph": True}))
        assert fake.call_count == 2
        assert fake.peak <= 3  # 并发上限
        assert stats["chunks"] == 2
        assert stats["extracted"] == 2
        assert stats["entities"] == 2
        assert stats["relations"] == 1
        # 文件落盘且内容正确
        g = load_graph("kb1")
        assert g["docs"]["d1"]["name"] == "测试.txt"
        assert g["docs"]["d1"]["chunk_count"] == 2
        assert g["entities"][0]["name"] == "Python"
        assert g["entities"][0]["type"] == "技术"
        # 关系 source=Guido van Rossum（实体顺序：Python=e1、Guido=e2）
        assert g["relations"][0]["source"] == "e2"
        assert g["relations"][0]["target"] == "e1"
        Path(graph_path("kb1")).unlink()

    def test_failure_skips_without_raise(self, monkeypatch):
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient(mode="error"))
        stats = asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", _mk_chunks(["一", "二", "三"]),
            cfg={"knowledge_graph": True}))
        assert stats["extracted"] == 0
        assert fake.call_count == 3
        # 抽取全失败：图谱中该文档无实体（但 docs 记录已更新）
        g = load_graph("kb1")
        assert g["docs"]["d1"]["chunk_count"] == 3
        assert g["entities"] == []
        Path(graph_path("kb1")).unlink()

    def test_timeout_skips(self, monkeypatch):
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient(delay=0.05))
        stats = asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", _mk_chunks(["一", "二"]),
            cfg={"knowledge_graph": True}, timeout=0.01))
        assert stats["extracted"] == 0
        assert fake.call_count == 2

    def test_partial_failure_keeps_success(self, monkeypatch):
        """部分块失败：成功块仍入库，失败块跳过"""
        fake = _FakeExtractionClient()
        calls = {"n": 0}

        async def _flaky_create(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("第二个块失败")
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=_FIXED_EXTRACTION_JSON))])

        fake.chat.completions.create = _flaky_create
        monkeypatch.setattr(
            "backend.services.knowledge_graph_service._get_client",
            lambda llm_cfg=None: fake)
        stats = asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", _mk_chunks(["一", "二", "三"]),
            cfg={"knowledge_graph": True}))
        assert stats["extracted"] == 2
        g = load_graph("kb1")
        assert len(g["entities"]) == 2  # 成功块的实体已合并
        Path(graph_path("kb1")).unlink()

    def test_bad_json_skips(self, monkeypatch):
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient(mode="bad"))
        stats = asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", _mk_chunks(["片段"]),
            cfg={"knowledge_graph": True}))
        assert stats["extracted"] == 0
        assert fake.call_count == 1

    def test_thinking_extra_body_default_disabled(self, monkeypatch):
        """cfg 不带 thinking_mode → 在线模型（DeepSeek）默认关闭思考
        extra_body 透传（本地 LM Studio 走 QwenPrefill prefill 注入，见
        test_thinking_strategy）"""
        _patch_active_llm_online(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", _mk_chunks(["Python 开发"]),
            cfg={"knowledge_graph": True}))
        assert fake.call_count == 1
        assert fake.extra_bodies == [{"thinking": {"type": "disabled"}}]
        Path(graph_path("kb1")).unlink()

    def test_thinking_extra_body_enabled_high(self, monkeypatch):
        """cfg.thinking_mode=enabled_high → thinking.enabled + reasoning_effort=high"""
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", _mk_chunks(["Python 开发"]),
            cfg={"knowledge_graph": True, "thinking_mode": "enabled_high"}))
        assert fake.extra_bodies == [{"thinking": {"type": "enabled"},
                                      "reasoning_effort": "high"}]
        Path(graph_path("kb1")).unlink()

    def test_llm_not_configured_skips(self, monkeypatch):
        """LLM 未配置（base_url/model 为空）→ 跳过不调用"""
        from backend.config import LLMConfig
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        bare = SimpleNamespace(
            llm=LLMConfig(base_url="", api_key="", model="",
                          temperature=0.3, max_tokens=1024, timeout=30.0))
        # knowledge_graph_service 顶部 from-import 复制了 get_active_config 引用
        monkeypatch.setattr(
            "backend.services.knowledge_graph_service.get_active_config",
            lambda: bare)
        stats = asyncio.run(build_graph_for_doc(
            "kb1", "d1", "测试.txt", _mk_chunks(["片段"]),
            cfg={"knowledge_graph": True}))
        assert stats["extracted"] == 0
        assert fake.call_count == 0


# ==================== ingestion 集成 ====================

class _RecordingEmbedding:
    """记录输入文本的伪 embedding（字符直方图向量，离线可跑）"""

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


# 多块测试文档（>800 字，naive 默认 chunk_size 800 才能切多块）
_MULTI_CHUNK_DOC = (
    "# Python 发展史\n\n"
    + "\n\n".join(
        f"## 第{i}节\n\nPython 语言由 Guido van Rossum 于 1991 年首次发布，"
        f"这是一种强调可读性的高级编程语言，被广泛用于第 {i} 个应用场景中。"
        for i in range(1, 6))
    + "\n")


class TestIngestGraph:
    """入库链路：开关关不生成 / 开关开生成图谱文件 / 失败不阻塞 / 重解析幂等"""

    def _ingest(self, client, kb_id, doc_id, body=None, headers=None):
        return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                           json=body, headers=headers)

    def test_switch_off_no_graph_file(self, client, monkeypatch, admin_headers):
        """默认关：不调用抽取 LLM、不生成图谱文件、parser_config 持久化 False"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="图谱测试.txt",
                         content=_MULTI_CHUNK_DOC)
        resp = self._ingest(client, kb["id"], doc["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_config"]["knowledge_graph"] is False
        assert fake.call_count == 0
        assert not graph_path(kb["id"]).exists()

    def test_switch_on_generates_graph(self, client, monkeypatch, admin_headers):
        """开关开：每块调用一次抽取 LLM，图谱文件生成，实体/关系落盘"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="图谱测试.txt",
                         content=_MULTI_CHUNK_DOC)
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"knowledge_graph": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_config"]["knowledge_graph"] is True
        assert fake.call_count == final["chunk_count"]  # 每块一次 LLM
        # 图谱文件生成
        path = graph_path(kb["id"])
        assert path.exists()
        g = load_graph(kb["id"])
        assert g["docs"][doc["id"]]["name"] == "图谱测试.txt"
        assert g["docs"][doc["id"]]["chunk_count"] == final["chunk_count"]
        names = {e["name"] for e in g["entities"]}
        assert {"Python", "Guido van Rossum"} <= names
        assert any(r["type"] == "开发" for r in g["relations"])
        # 实体 count = 出现块数（文档多块，每块都返回固定实体）
        e = next(x for x in g["entities"] if x["name"] == "Python")
        assert e["count"] == final["chunk_count"]
        assert len(e["chunk_refs"]) == final["chunk_count"]

    def test_llm_failure_does_not_block(self, client, monkeypatch, admin_headers):
        """抽取 LLM 全部失败：不阻塞入库，图谱文件仍生成（docs 记录，无实体）"""
        _patch_rec_embedding(monkeypatch)
        _patch_kg_client(monkeypatch, _FakeExtractionClient(mode="error"))
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="图谱测试.txt",
                         content=_MULTI_CHUNK_DOC)
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"knowledge_graph": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        g = load_graph(kb["id"])
        assert g["entities"] == []
        assert g["docs"][doc["id"]]["chunk_count"] == final["chunk_count"]

    def test_reingest_idempotent(self, client, monkeypatch, admin_headers):
        """重解析：沿用开关，实体/关系不翻倍（幂等）"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="图谱测试.txt",
                         content=_MULTI_CHUNK_DOC)
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"knowledge_graph": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        first_calls = fake.call_count
        # 无 body 重解析：沿用 parser_config（开关保持开启）
        resp = self._ingest(client, kb["id"], doc["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final2 = wait_for_status(client, kb["id"], doc["id"])
        assert final2["parser_config"]["knowledge_graph"] is True
        assert fake.call_count == first_calls + final2["chunk_count"]
        g = load_graph(kb["id"])
        assert len(g["entities"]) == 2  # 不翻倍
        assert len(g["relations"]) == 1
        e = next(x for x in g["entities"] if x["name"] == "Python")
        assert e["count"] == final2["chunk_count"]
        # 描述来自本次抽取（幂等清除后重新累积）
        assert "高级编程语言" in e["description"]

    def test_thinking_mode_invalid_400(self, client, admin_headers):
        """thinking_mode 非法值 → 同步 400（任务不启动）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"thinking_mode": "enabled_ultra"},
                            headers=admin_headers)
        assert resp.status_code == 400
        assert "thinking_mode" in resp.json()["detail"]

    def test_thinking_mode_default_disabled_persisted(
            self, client, monkeypatch, admin_headers):
        """默认：thinking_mode=disabled 持久化到 parser_config（关闭思考）"""
        _patch_rec_embedding(monkeypatch)
        _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="图谱测试.txt",
                         content=_MULTI_CHUNK_DOC)
        resp = self._ingest(client, kb["id"], doc["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_config"]["thinking_mode"] == "disabled"

    def test_thinking_mode_passthrough_ingest(
            self, client, monkeypatch, admin_headers):
        """入库传 thinking_mode=enabled_max → 抽取调用 extra_body 全部带
        reasoning_effort=max，且持久化"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="图谱测试.txt",
                         content=_MULTI_CHUNK_DOC)
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"knowledge_graph": True,
                                  "thinking_mode": "enabled_max"},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["parser_config"]["thinking_mode"] == "enabled_max"
        assert fake.extra_bodies
        assert all(b == {"thinking": {"type": "enabled"},
                         "reasoning_effort": "max"}
                   for b in fake.extra_bodies)


# ==================== 图谱查询接口 ====================

class TestGraphAPI:

    def _build_graph(self, client, monkeypatch, admin_headers):
        """建库 + 上传 + 入库（开关开），返回 (kb, doc, graph_path)"""
        _patch_rec_embedding(monkeypatch)
        _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="图谱测试.txt",
                         content=_MULTI_CHUNK_DOC)
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"knowledge_graph": True},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_status(client, kb["id"], doc["id"])
        return kb, doc

    def test_no_graph_404(self, client, admin_headers):
        kb = create_kb(client)
        resp = client.get(f"/api/kbs/{kb['id']}/graph", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "该知识库暂无知识图谱"

    def test_get_graph_ok(self, client, monkeypatch, admin_headers):
        kb, doc = self._build_graph(client, monkeypatch, admin_headers)
        resp = client.get(f"/api/kbs/{kb['id']}/graph", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["kb_id"] == kb["id"]
        assert data["updated_at"]
        assert doc["id"] in data["docs"]
        assert data["docs"][doc["id"]]["name"] == "图谱测试.txt"
        names = {e["name"] for e in data["entities"]}
        assert "Python" in names
        assert any(r["type"] == "开发" for r in data["relations"])
        # 引用含偏移契约字段
        e = next(x for x in data["entities"] if x["name"] == "Python")
        ref = e["chunk_refs"][0]
        assert {"doc_id", "chunk_index", "char_start", "char_end"} <= set(ref)

    def test_get_graph_filter_by_doc(self, client, monkeypatch, admin_headers):
        """doc_id 过滤：只返回该文档的实体/关系"""
        kb, doc = self._build_graph(client, monkeypatch, admin_headers)
        resp = client.get(f"/api/kbs/{kb['id']}/graph",
                          params={"doc_id": doc["id"]}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert set(data["docs"].keys()) == {doc["id"]}
        assert len(data["entities"]) == 2
        # 过滤不存在的文档 → 空结构
        resp = client.get(f"/api/kbs/{kb['id']}/graph",
                          params={"doc_id": "不存在"}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["entities"] == []
        assert resp.json()["relations"] == []

    def test_cross_department_404(self, client, admin_headers):
        """跨部门访问 → 404 伪装（与知识库不存在同文案）"""
        _, dept_admin_a = create_department_and_admin(
            client, admin_headers, "图谱A部", "kg_admin_a",
            "pass123456", "A部管理员")
        dept_b_id, _ = create_department_and_admin(
            client, admin_headers, "图谱B部", "kg_admin_b",
            "pass123456", "B部管理员")
        user_b = create_user(client, admin_headers, dept_b_id, "kg_user_b")
        kb = client.post("/api/kbs", json={"name": "A部图谱库"},
                         headers=dept_admin_a).json()
        # 无权限 → 404（不暴露存在性）
        resp = client.get(f"/api/kbs/{kb['id']}/graph", headers=user_b)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "知识库不存在"

    def test_unauthorized_401(self, client, admin_headers):
        kb = create_kb(client)
        resp = client.get(f"/api/kbs/{kb['id']}/graph")
        assert resp.status_code == 401


# ==================== 删除清理（purge 文档 / 删知识库） ====================

class TestGraphCleanup:
    """删除时图谱清理：
    - purge 文档 → 图谱中该文档实体/关系引用移除（chunk_refs 清空、count 重算、
      无引用实体/关系删除）、docs 条目移除
    - 软删除（回收站）不清图谱（恢复免重建，与 MinIO 对象清理逻辑一致）
    - 删知识库 → 图谱文件 data/storage/graphs/{kb_id}.json 删除（不存在静默）
    - 清理失败仅 warning：不阻塞删除主流程（purge 仍删文档、删库仍 200）
    """

    def _build_graph(self, client, monkeypatch, admin_headers):
        """建库 + 上传 + 入库（knowledge_graph 开），返回 (kb, doc)"""
        _patch_rec_embedding(monkeypatch)
        _patch_kg_client(monkeypatch, _FakeExtractionClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="图谱测试.txt",
                         content=_MULTI_CHUNK_DOC)
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           json={"knowledge_graph": True},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_status(client, kb["id"], doc["id"])
        assert graph_path(kb["id"]).exists()
        return kb, doc

    def test_purge_doc_removes_graph_refs(self, client, monkeypatch,
                                          admin_headers):
        """purge 文档：软删除不清、purge 后实体/关系引用移除且 docs 条目移除
        （唯一文档时实体/关系清空）"""
        kb, doc = self._build_graph(client, monkeypatch, admin_headers)
        g = load_graph(kb["id"])
        assert g["entities"] and g["relations"]
        assert g["docs"][doc["id"]]["name"] == "图谱测试.txt"
        # 软删除（回收站）→ 图谱保留（恢复免重建）
        resp = client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200, resp.text
        g = load_graph(kb["id"])
        assert doc["id"] in g["docs"]
        assert g["entities"] and g["relations"]
        # purge 彻底删除 → 图谱引用移除
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/purge",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        g = load_graph(kb["id"])
        assert doc["id"] not in g["docs"]
        assert g["entities"] == [] and g["relations"] == []

    def test_purge_doc_refs_removed_partial(self, client, monkeypatch,
                                            admin_headers):
        """多文档共享实体：purge 一个文档后仅移除该文档引用，其余文档实体保留
        （count 重算、chunk_refs 过滤）"""
        kb, doc1 = self._build_graph(client, monkeypatch, admin_headers)
        doc2 = upload_doc(client, kb["id"], filename="图谱测试2.txt",
                          content=_MULTI_CHUNK_DOC)
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc2['id']}/ingest",
                           json={"knowledge_graph": True},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_status(client, kb["id"], doc2["id"])
        g = load_graph(kb["id"])
        python_e = next(e for e in g["entities"] if e["name"] == "Python")
        assert python_e["count"] == 2  # 两文档都引用
        # purge doc1 → Python 实体只剩 doc2 引用
        resp = client.delete(f"/api/kbs/{kb['id']}/documents/{doc1['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc1['id']}/purge",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        g = load_graph(kb["id"])
        assert doc1["id"] not in g["docs"]
        assert doc2["id"] in g["docs"]
        python_e = next(e for e in g["entities"] if e["name"] == "Python")
        assert python_e["count"] == 1
        assert all(r["doc_id"] == doc2["id"] for r in python_e["chunk_refs"])
        assert g["entities"]  # 实体保留（doc2 仍引用）

    def test_purge_cleanup_failure_does_not_block(
            self, client, monkeypatch, admin_headers):
        """图谱清理失败仅 warning：purge 仍成功、文档元数据仍删除"""
        kb, doc = self._build_graph(client, monkeypatch, admin_headers)

        def _boom_save(kb_id, graph):
            raise RuntimeError("图谱落盘失败（测试构造）")

        # from-import 后名字绑定在 documents 模块，须在此 patch（_purge_graph_refs
        # 内部 try/except 会吞掉该异常 → warning 不阻塞 purge 主流程）
        monkeypatch.setattr("backend.routers.documents.save_graph",
                            _boom_save)
        resp = client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/purge",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        assert all(d["id"] != doc["id"] for d in docs)

    def test_purge_no_graph_file_no_effect(self, client, admin_headers):
        """无图谱文件时 purge 文档：不报错、不创建图谱文件（remove 0 条不落盘）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/purge",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert not graph_path(kb["id"]).exists()

    def test_delete_kb_removes_graph_file(self, client, monkeypatch,
                                          admin_headers):
        """删知识库 → 图谱文件删除"""
        kb, doc = self._build_graph(client, monkeypatch, admin_headers)
        path = graph_path(kb["id"])
        assert path.exists()
        resp = client.delete(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert not path.exists()

    def test_delete_kb_no_graph_file_ok(self, client, admin_headers):
        """无图谱文件删库：正常 200（不存在静默）"""
        kb = create_kb(client)
        resp = client.delete(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 200, resp.text

    def test_delete_kb_cleanup_failure_does_not_block(
            self, client, monkeypatch, admin_headers):
        """图谱文件删除失败仅 warning：删库仍成功"""
        kb, doc = self._build_graph(client, monkeypatch, admin_headers)

        class _FakePath:
            def exists(self):
                return True

            def unlink(self):
                raise OSError("删除图谱文件失败（测试构造）")

        monkeypatch.setattr("backend.routers.knowledge_bases.graph_path",
                            lambda kb_id: _FakePath())
        resp = client.delete(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        # 知识库已删除
        resp = client.get(f"/api/kbs/{kb['id']}", headers=admin_headers)
        assert resp.status_code == 404


class _FakeQueryEntitiesClient:
    """伪 OpenAI 客户端：查询实体抽取（返回 JSON 数组）

    mode: ok=固定数组 / garbage=非 JSON / error=抛异常 / slow=慢响应（超时测试）
    """

    def __init__(self, mode="ok", payload='["图灵","专家系统"]', delay=0.0):
        self.mode = mode
        self.payload = payload
        self.delay = delay
        self.call_count = 0
        self.last_extra_body = None
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.call_count += 1
        self.last_extra_body = kwargs.get("extra_body")
        if self.mode == "error":
            raise RuntimeError("mock 查询实体 LLM 调用失败（测试构造）")
        if self.mode == "garbage":
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="这不是JSON"))])
        if self.mode == "slow":
            await asyncio.sleep(self.delay or 10.0)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.payload))])


# ==================== 图谱查询（GraphRAG 检索增强） ====================

def _make_query_graph():
    """测试图谱：图灵 → 图灵测试 → 专家系统 链 + 邻居艾伦·图灵"""
    return {
        "kb_id": "kb-test",
        "updated_at": "2026-01-01 00:00:00",
        "docs": {"d1": {"name": "测试文档", "chunk_count": 3}},
        "entities": [
            {"id": "e1", "name": "图灵", "type": "人物",
             "description": "英国数学家", "count": 2,
             "chunk_refs": [
                 {"doc_id": "d1", "chunk_index": 0, "char_start": 10, "char_end": 12},
                 {"doc_id": "d1", "chunk_index": 1, "char_start": 5, "char_end": 7}]},
            {"id": "e2", "name": "艾伦·图灵", "type": "人物",
             "description": "计算机科学奠基人", "count": 1,
             "chunk_refs": [
                 {"doc_id": "d1", "chunk_index": 1, "char_start": 5, "char_end": 10}]},
            {"id": "e3", "name": "图灵测试", "type": "概念",
             "description": "判断机器智能的测试", "count": 1,
             "chunk_refs": [
                 {"doc_id": "d1", "chunk_index": 1, "char_start": 30, "char_end": 34}]},
            {"id": "e4", "name": "专家系统", "type": "技术",
             "description": "基于规则的智能系统", "count": 1,
             "chunk_refs": [
                 {"doc_id": "d1", "chunk_index": 2, "char_start": 0, "char_end": 4}]},
        ],
        "relations": [
            {"id": "r1", "source": "e1", "target": "e3", "type": "提出",
             "description": "1950年论文提出", "weight": 1.0,
             "chunk_refs": [
                 {"doc_id": "d1", "chunk_index": 1, "char_start": 30, "char_end": 34}]},
            {"id": "r2", "source": "e3", "target": "e4", "type": "相关",
             "description": "同属人工智能领域", "weight": 1.0,
             "chunk_refs": [
                 {"doc_id": "d1", "chunk_index": 2, "char_start": 0, "char_end": 4}]},
        ],
    }


class TestMatchEntities:
    """match_entities：精确 / 包含 / 长度限制 / 去重"""

    def test_exact_match(self):
        graph = _make_query_graph()
        matched = match_entities(graph, ["图灵"])
        names = [e["name"] for e in matched]
        assert "图灵" in names

    def test_graph_name_contains_query(self):
        """图谱名含查询名（查询名≥2字）："图灵" 匹配 "艾伦·图灵"/"图灵测试" """
        matched = match_entities(_make_query_graph(), ["图灵"])
        names = [e["name"] for e in matched]
        assert set(names) == {"图灵", "艾伦·图灵", "图灵测试"}

    def test_query_contains_graph_name(self):
        """查询名含图谱名（图谱名≥2字）："图灵测试的提出者" 命中 "图灵" """
        matched = match_entities(_make_query_graph(), ["图灵测试的提出者"])
        names = [e["name"] for e in matched]
        assert "图灵" in names
        assert "图灵测试" in names

    def test_single_char_no_substring_match(self):
        """单字查询不参与包含匹配（防"图"误匹配"图灵"/"图谱"）"""
        matched = match_entities(_make_query_graph(), ["图"])
        assert matched == [], "单字只能精确匹配，图谱中无名为'图'的实体"

    def test_no_match_returns_empty(self):
        assert match_entities(_make_query_graph(), ["爱丽丝"]) == []
        assert match_entities(_make_query_graph(), []) == []
        assert match_entities(_make_query_graph(), ["  "]) == []

    def test_query_entities_dedup(self):
        matched = match_entities(_make_query_graph(), ["图灵", "图灵"])
        names = [e["name"] for e in matched]
        assert names.count("图灵") == 1

    def test_fullwidth_normalized(self):
        """全角字符规范化后仍可匹配（全角字母转半角再精确匹配）"""
        graph = _make_query_graph()
        graph["entities"].append(
            {"id": "e9", "name": "AI", "type": "技术",
             "description": "人工智能", "count": 1, "chunk_refs": []})
        matched = match_entities(graph, ["ＡＩ"])
        assert any(e["name"] == "AI" for e in matched)

    def test_empty_graph_returns_empty(self):
        assert match_entities(_empty_graph("kb"), ["图灵"]) == []


class TestExpandNeighbors:
    """expand_neighbors：1-hop 邻接扩展 + chunk_refs 去重"""

    def test_one_hop_neighbors_and_relations(self):
        graph = _make_query_graph()
        matched = match_entities(graph, ["图灵"])
        expanded = expand_neighbors(graph, matched, hop=1)
        names = [e["name"] for e in expanded["entities"]]
        # 匹配实体：图灵/艾伦·图灵/图灵测试（包含匹配）；"图灵测试"的邻居
        # 专家系统（r2）随之 1-hop 进入——包含匹配导致的多实体邻域扩展，合规
        assert set(names) == {"图灵", "艾伦·图灵", "图灵测试", "专家系统"}
        # 子图内两端相连的关系：r1（图灵-图灵测试）+ r2（图灵测试-专家系统）
        rtypes = {r["id"]: r["type"] for r in expanded["relations"]}
        assert rtypes == {"r1": "提出", "r2": "相关"}

    def test_source_chunks_dedup(self):
        graph = _make_query_graph()
        matched = match_entities(graph, ["图灵"])
        expanded = expand_neighbors(graph, matched, hop=1)
        # e1 引 d1#0、d1#1；e2/e3/r1 引 d1#1；e4/r2 引 d1#2 → 去重后 3 条
        chunks = expanded["source_chunks"]
        keys = {(c["doc_id"], c["chunk_index"]) for c in chunks}
        assert keys == {("d1", 0), ("d1", 1), ("d1", 2)}
        assert all("char_start" in c and "char_end" in c for c in chunks)

    def test_no_matched_returns_empty(self):
        expanded = expand_neighbors(_make_query_graph(), [])
        assert expanded == {"entities": [], "relations": [],
                            "source_chunks": []}


class TestBuildGraphContext:
    """build_graph_context：CSV 文本组装 / 空结构 / 截断"""

    def test_context_text_format(self):
        graph = _make_query_graph()
        ctx = build_graph_context(graph, ["图灵"])
        assert ctx["context_text"].startswith("【知识图谱实体】")
        assert "图灵(人物|英国数学家|2)" in ctx["context_text"]
        assert "艾伦·图灵(人物|计算机科学奠基人|1)" in ctx["context_text"]
        assert "【知识图谱关系】" in ctx["context_text"]
        assert "图灵|提出|图灵测试|1950年论文提出" in ctx["context_text"]
        assert "图灵测试|相关|专家系统|同属人工智能领域" in ctx["context_text"]
        # 结构化字段与引用溯源（顺序受集合迭代影响，用集合断言）
        assert {e["name"] for e in ctx["entities"]} == \
            {"图灵", "艾伦·图灵", "图灵测试", "专家系统"}
        assert {r["id"] for r in ctx["relations"]} == {"r1", "r2"}
        assert ctx["source_chunks"]

    def test_no_match_empty_structure(self):
        ctx = build_graph_context(_make_query_graph(), ["爱丽丝"])
        assert ctx == {"entities": [], "relations": [],
                       "context_text": "", "source_chunks": []}

    def test_query_entities_empty(self):
        ctx = build_graph_context(_make_query_graph(), [])
        assert ctx["context_text"] == ""

    def test_desc_truncated(self):
        graph = _make_query_graph()
        graph["entities"][0]["description"] = "长" * 200
        ctx = build_graph_context(graph, ["图灵"])
        assert "长" * 61 not in ctx["context_text"]
        assert ctx["context_text"].count("长") <= 61  # 60 字 + 省略号


class TestParseQueryEntities:
    """parse_query_entities：数组解析多策略 + 校验"""

    def test_direct_array(self):
        assert parse_query_entities('["图灵","专家系统"]') == ["图灵", "专家系统"]

    def test_json_block_wrapped(self):
        content = '```json\n["图灵"]\n```'
        assert parse_query_entities(content) == ["图灵"]

    def test_trailing_text_balanced(self):
        content = '结果是：["图灵","专家系统"]，以上。'
        assert parse_query_entities(content) == ["图灵", "专家系统"]

    def test_garbage_returns_empty(self):
        assert parse_query_entities("不是JSON") == []
        assert parse_query_entities("") == []
        assert parse_query_entities(None) == []

    def test_max_five_and_filters(self):
        content = '["a","b","c","d","e","f"]'
        assert len(parse_query_entities(content)) == 5
        # 非字符串项/空白项过滤
        content2 = '[1, null, "  ", "图灵"]'
        assert parse_query_entities(content2) == ["图灵"]

    def test_dedup_normalized(self):
        assert parse_query_entities('["图灵","图灵","ＡＩ"]') == ["图灵", "AI"]


class TestExtractQueryEntities:
    """extract_query_entities：mock LLM 客户端（成功/失败/垃圾/空查询）"""

    def test_ok_returns_entities(self, monkeypatch):
        fake = _FakeQueryEntitiesClient()
        monkeypatch.setattr("backend.services.knowledge_graph_service."
                            "_get_client", lambda llm_cfg=None: fake)
        result = asyncio.run(extract_query_entities("图灵和专家系统什么关系？"))
        assert result == ["图灵", "专家系统"]
        assert fake.call_count == 1

    def test_thinking_disabled_extra_body(self, monkeypatch):
        """在线模型（DeepSeek）：查询实体抽取固定关闭思考 extra_body 透传
        （本地 LM Studio 走 QwenPrefill prefill 注入，见 test_thinking_strategy）"""
        _patch_active_llm_online(monkeypatch)
        fake = _FakeQueryEntitiesClient()
        monkeypatch.setattr("backend.services.knowledge_graph_service."
                            "_get_client", lambda llm_cfg=None: fake)
        asyncio.run(extract_query_entities("图灵测试是什么"))
        assert fake.last_extra_body == {"thinking": {"type": "disabled"}}

    def test_failure_returns_empty(self, monkeypatch):
        fake = _FakeQueryEntitiesClient(mode="error")
        monkeypatch.setattr("backend.services.knowledge_graph_service."
                            "_get_client", lambda llm_cfg=None: fake)
        assert asyncio.run(extract_query_entities("图灵测试是什么")) == []
        assert fake.call_count == 1

    def test_garbage_returns_empty(self, monkeypatch):
        fake = _FakeQueryEntitiesClient(mode="garbage")
        monkeypatch.setattr("backend.services.knowledge_graph_service."
                            "_get_client", lambda llm_cfg=None: fake)
        assert asyncio.run(extract_query_entities("图灵测试是什么")) == []

    def test_empty_query_no_llm_call(self, monkeypatch):
        fake = _FakeQueryEntitiesClient()
        monkeypatch.setattr("backend.services.knowledge_graph_service."
                            "_get_client", lambda llm_cfg=None: fake)
        assert asyncio.run(extract_query_entities("  ")) == []
        assert fake.call_count == 0, "空查询不应调用 LLM"

    def test_timeout_returns_empty(self, monkeypatch):
        fake = _FakeQueryEntitiesClient(mode="slow")
        monkeypatch.setattr("backend.services.knowledge_graph_service."
                            "_get_client", lambda llm_cfg=None: fake)
        # 超时 8s 太长不便等待，直接断言内部逻辑：slow 模式 sleep 10s 会超时
        # —— 通过把模块超时临时调小验证降级路径
        import backend.services.knowledge_graph_service as kgs
        monkeypatch.setattr(kgs, "_QUERY_TIMEOUT", 0.05)
        assert asyncio.run(extract_query_entities("图灵测试是什么")) == []
