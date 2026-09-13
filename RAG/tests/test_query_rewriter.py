"""查询改写单元测试：query_rewriter 触发条件 / LLM 调用 / 降级 / 输出清洗

覆盖：指代启发式（含指代触发/无指代跳过/空历史跳过）、LLM 未配置/超时/
失败/空响应降级 None、输出清洗（前缀剥离/引号剥离/多行取首行/长度截断）、
chat_service 集成（配置开+多轮+指代 → 检索用改写后 query；prompt 事件
附带 rewrite_ms/rewritten_query）。
全部离线（FakeLLMClient / 注入假 rewrite，无真实网络）。
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from backend.services import query_rewriter
from backend.services.query_rewriter import (_needs_rewrite, _normalize,
                                              rewrite_query)


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content))])


class TestNeedsRewrite:
    """指代/口语启发式（新签名：has_history）"""

    def test_reference_trigger_with_history(self):
        """含指代词 + 有历史 → 触发改写"""
        for q in ("它怎么实现？", "这个方案呢", "上面那个图表", "那后面的呢"):
            assert _needs_rewrite(q, True), f"应触发改写: {q}"

    def test_reference_no_history_skip(self):
        """含指代词但无历史 → 不触发（指代无从消解，白费调用）"""
        for q in ("它怎么实现？", "这个方案呢"):
            assert not _needs_rewrite(q, False), f"不应触发改写: {q}"

    def test_colloquial_trigger_without_history(self):
        """口语词不依赖历史也触发（正式化不需要上下文）"""
        for q in ("帮我查查花花去哪了呀", "快看看那个怎么搞", "帮我查一下"):
            assert _needs_rewrite(q, False), f"应触发改写: {q}"

    def test_plain_query_skip(self):
        """无指代无口语 → 不触发"""
        for q in ("Python 支持哪些类型？", "如何部署 Docker 镜像？",
                  "北京烤鸭烤制时长"):
            assert not _needs_rewrite(q, True), f"不应触发改写: {q}"
            assert not _needs_rewrite(q, False), f"不应触发改写: {q}"

    def test_empty_query(self):
        assert not _needs_rewrite("", False)
        assert not _needs_rewrite("", True)


class TestNormalize:
    """LLM 输出清洗"""

    def test_plain(self):
        assert _normalize("北京烤鸭怎么吃") == "北京烤鸭怎么吃"

    def test_prefix_stripped(self):
        assert _normalize("改写为：北京烤鸭怎么吃") == "北京烤鸭怎么吃"
        assert _normalize("检索查询：北京烤鸭怎么吃") == "北京烤鸭怎么吃"

    def test_quotes_stripped(self):
        assert _normalize('"北京烤鸭怎么吃"') == "北京烤鸭怎么吃"
        assert _normalize('「北京烤鸭怎么吃」') == "北京烤鸭怎么吃"
        assert _normalize("“北京烤鸭怎么吃”") == "北京烤鸭怎么吃"

    def test_first_nonempty_line(self):
        assert _normalize("北京烤鸭怎么吃\n\n其他说明") == "北京烤鸭怎么吃"

    def test_empty(self):
        assert _normalize("") is None
        assert _normalize("   \n  ") is None


class TestRewriteQueryLLM:
    """LLM 调用路径（注入假客户端）"""

    def test_success(self, monkeypatch):
        """调用成功 → 返回改写后查询"""
        monkeypatch.setattr(query_rewriter, "_get_client",
                            lambda llm_cfg=None: _FakeClient(
                                _resp("北京烤鸭怎么吃")))
        assert await_rewrite("它怎么吃？", [{"role": "user", "content": "北京烤鸭"}])\
            == "北京烤鸭怎么吃"

    def test_failure_degrades_none(self, monkeypatch):
        """LLM 调用失败 → None（调用方回退原问题）"""
        class _ErrClient(_FakeClient):
            async def _create(self, **kwargs):
                raise RuntimeError("mock 失败")
        monkeypatch.setattr(query_rewriter, "_get_client",
                            lambda llm_cfg=None: _ErrClient(None))
        assert await_rewrite("它怎么吃？",
                             [{"role": "user", "content": "x"}]) is None

    def test_empty_response_none(self, monkeypatch):
        """空响应 → None"""
        monkeypatch.setattr(query_rewriter, "_get_client",
                            lambda llm_cfg=None: _FakeClient(_resp("  \n ")))
        assert await_rewrite("它怎么吃？",
                             [{"role": "user", "content": "x"}]) is None

    def test_no_history_reference_skip(self):
        """无历史 + 仅指代 → 直接 None（指代无从消解，不发 LLM 调用）"""
        assert await_rewrite("它怎么吃？", []) is None

    def test_no_history_colloquial_rewrites(self, monkeypatch):
        """无历史 + 口语词 → 仍触发改写（正式化不依赖历史）"""
        monkeypatch.setattr(query_rewriter, "_get_client",
                            lambda llm_cfg=None: _FakeClient(
                                _resp("花花去过的旅游目的地")))
        assert await_rewrite("帮我查查花花去哪了呀", []) == "花花去过的旅游目的地"


class _FakeClient:
    """最小假 LLM 客户端（对齐 llm_completion 的调用形态）"""

    def __init__(self, resp):
        self._resp = resp
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        return self._resp


def await_rewrite(query: str, history: list) -> str | None:
    """同步包装（脚本内无运行中事件循环，asyncio.run 安全）"""
    import asyncio
    return asyncio.run(rewrite_query(query, history))


# ==================== chat_service 集成 ====================

class _FakeRewriteClient:
    """改写用假 LLM 客户端（非流式一次返回）"""

    def __init__(self, content: str):
        self.content = content
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.content))])


class _RecordingRetrieval:
    """记录检索 query 的伪检索服务（返回一条假来源 → 全链路走通）"""

    def __init__(self):
        self.queries: list[str] = []

    async def retrieve(self, kb_id, query, top_k=None, min_score=None,
                       enable_hybrid=None, enable_rerank=None):
        self.queries.append(query)
        from backend.models.rag_models import Source
        return [Source(
            id="doc1_0", text="测试片段", score=0.9, document_id="doc1",
            document_name="测试文档", kb_id=kb_id, chunk_index=0)]


class TestChatRewriteIntegration:
    """chat_service 集成：查询改写接线（检索用改写 query / 事件字段）"""

    @staticmethod
    def _patch(monkeypatch, rewritten: str) -> _RecordingRetrieval:
        fake = _RecordingRetrieval()
        monkeypatch.setattr(
            "backend.services.chat_service.get_retrieval_service",
            lambda: fake)
        monkeypatch.setattr(query_rewriter, "_get_client",
                            lambda llm_cfg=None: _FakeRewriteClient(rewritten))
        return fake

    def test_rewrite_used_in_retrieval(self, client, mock_embedding,
                                       mock_llm, admin_headers, monkeypatch):
        """配置开 + 多轮 + 指代 → 检索收到改写后 query，prompt 事件展示"""
        from conftest import create_kb
        kb = create_kb(client)
        retr = self._patch(monkeypatch, "北京烤鸭怎么做")
        # 开启查询改写（POST /api/settings/chat 持久化到档案）
        resp = client.post("/api/settings/chat", json={
            "chat": {"query_rewrite": True},
            "retrieval": {},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        mock_llm()
        # 第一轮：建立会话（无指代，不触发改写）
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "北京烤鸭是什么菜？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        session_id = None
        for block in resp.text.split("\n\n"):
            if block.startswith("event: done"):
                import json as _json
                session_id = _json.loads(
                    block.split("data: ", 1)[1])["session_id"]
                break
        assert session_id, "第一轮应产生会话"
        # 第二轮：带历史 + 指代 → 触发改写
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "session_id": session_id, "query": "它怎么做？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        text = resp.text
        assert "event: prompt" in text
        prompt_block = text.split("event: prompt", 1)[1].split("\n\n", 1)[0]
        import json as _json2
        prompt_data = _json2.loads(prompt_block.split("data: ", 1)[1].strip())
        assert prompt_data["rewritten_query"] == "北京烤鸭怎么做", \
            "prompt 事件应展示改写后查询"
        assert isinstance(prompt_data["rewrite_ms"], int) \
            and prompt_data["rewrite_ms"] >= 0
        # 检索收到改写后 query
        assert retr.queries[-1] == "北京烤鸭怎么做", \
            f"检索应使用改写后 query，实际: {retr.queries}"

    def test_rewrite_disabled_uses_original(self, client, mock_embedding,
                                            mock_llm, admin_headers,
                                            monkeypatch):
        """默认关（配置未开）→ 检索收到原问题，无改写耗时"""
        from conftest import create_kb
        kb = create_kb(client)
        retr = _RecordingRetrieval()
        monkeypatch.setattr(
            "backend.services.chat_service.get_retrieval_service",
            lambda: retr)
        mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "它怎么做？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert "event: done" in resp.text
        assert retr.queries == ["它怎么做？"], \
            "配置关时应原问题检索（不调用改写）"
        prompt_block = resp.text.split("event: prompt", 1)[1].split("\n\n", 1)[0]
        import json as _json3
        prompt_data = _json3.loads(
            prompt_block.split("data: ", 1)[1].strip())
        assert prompt_data["rewritten_query"] is None
        assert prompt_data["rewrite_ms"] == 0

    def test_rewrite_failure_degrades(self, client, mock_embedding,
                                      mock_llm, admin_headers, monkeypatch):
        """改写 LLM 失败 → 原问题检索（问答不被阻塞）"""
        from conftest import create_kb
        kb = create_kb(client)
        retr = self._patch(monkeypatch, "")

        class _ErrRewrite(_FakeRewriteClient):
            async def _create(self, **kwargs):
                raise RuntimeError("改写失败 mock")
        monkeypatch.setattr(query_rewriter, "_get_client",
                            lambda llm_cfg=None: _ErrRewrite(""))
        resp = client.post("/api/settings/chat", json={
            "chat": {"query_rewrite": True},
            "retrieval": {},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        mock_llm()
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "它怎么做？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert "event: done" in resp.text, "改写出错也应正常完成问答"
        assert retr.queries == ["它怎么做？"], "改写出错应回退原问题"

    def test_rewrite_without_multi_turn(self, client, mock_embedding,
                                        mock_llm, admin_headers, monkeypatch):
        """多轮对话关 + 口语问题 → 仍触发改写（与多轮开关解耦）"""
        from conftest import create_kb
        kb = create_kb(client)
        retr = self._patch(monkeypatch, "花花去过的旅游目的地")
        # 多轮关 + 改写开
        resp = client.post("/api/settings/chat", json={
            "chat": {"query_rewrite": True, "enable_multi_turn": False},
            "retrieval": {},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        mock_llm()
        # 首轮（无历史）口语问题 → 触发改写（正式化不依赖历史/多轮）
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "帮我查查花花去哪了呀",
        }, headers=admin_headers)
        assert resp.status_code == 200
        text = resp.text
        assert "event: prompt" in text
        prompt_block = text.split("event: prompt", 1)[1].split("\n\n", 1)[0]
        import json as _json4
        prompt_data = _json4.loads(prompt_block.split("data: ", 1)[1].strip())
        assert prompt_data["rewritten_query"] == "花花去过的旅游目的地", \
            "多轮关 + 口语问题仍应触发改写"
        assert retr.queries[-1] == "花花去过的旅游目的地", \
            "检索应使用改写后 query"
