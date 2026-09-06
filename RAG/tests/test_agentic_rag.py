"""Agentic 检索增强测试：配置白名单 / LangGraph 决策图 / chat 集成

- TestAgenticConfig：/api/settings/chat 对 agentic 段的白名单+范围校验
  （schema 驱动，改一处全联动）
- TestAgenticDecisionGraph：图单元测试（桩检索分数 + 桩改写 LLM）：
  分档 answer/rewrite/abstain、重试用尽、改写失败降级、max_retries=0、
  纯 BM25 命中（vector_score=None）视为通过
- TestAgenticChatIntegration：chat_service 集成（默认关闭零影响、
  高分直接答、拒答不调 LLM；mock_embedding + mock_llm 离线）
- TestAgenticMerge：settings_merge 部门字段级覆盖

全部离线（mock_embedding / mock_llm / 桩 retrieve / 桩 llm_completion）。
项目无 pytest-asyncio：async 用例统一 asyncio.run()。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from backend.config import AgenticConfig
from backend.models.rag_models import Source

from conftest import create_kb, upload_and_ingest


def _source(vscore: float) -> Source:
    """构造带 vector_score 的检索命中桩（分档基准 = vector_score）"""
    return Source(
        id=f"doc0_{int(vscore * 100)}",
        text=f"片段（相似度 {vscore}）",
        score=vscore,
        document_id="doc0",
        document_name="测试文档.docx",
        kb_id="kb_x",
        chunk_index=0,
        vector_score=vscore,
    )


def _stub_retrieve(scores, monkeypatch):
    """桩检索：按调用顺序返回固定 vector_score 列表（每轮消费一个）"""
    calls = []

    class _StubRetrieval:
        async def retrieve(self, kb_id, query, top_k=None, min_score=None):
            calls.append(query)
            idx = len(calls) - 1
            score = scores[min(idx, len(scores) - 1)]
            return [_source(score)]

    monkeypatch.setattr("backend.services.agentic_service.get_retrieval_service",
                        lambda: _StubRetrieval())
    return calls


def _stub_rewrite(monkeypatch, rewritten: str = "改写后的问题", fail: bool = False):
    """桩 LLM 改写：返回 rewritten 文本；fail=True 时抛 LLMRequestError"""
    async def fake_completion(client, *, model, messages, max_tokens,
                              temperature, extra_body=None, timeout=None):
        if fail:
            from backend.services.llm_client import LLMRequestError
            raise LLMRequestError("改写服务不可用")
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=rewritten))])

    monkeypatch.setattr("backend.services.agentic_service.llm_completion",
                        fake_completion)
    monkeypatch.setattr("backend.services.agentic_service.get_llm_client",
                        lambda llm_cfg=None, timeout=None: SimpleNamespace())


def _stub_active_config(monkeypatch, max_retries=1, recheck=0.55,
                        abstain=0.25):
    """桩活跃配置：monkeypatch agentic_service 模块引用的 get_active_config"""
    monkeypatch.setattr("backend.services.agentic_service.get_active_config",
                        lambda: SimpleNamespace(
                            agentic=AgenticConfig(
                                enabled=True, max_retries=max_retries,
                                recheck_threshold=recheck,
                                abstain_threshold=abstain),
                            llm=SimpleNamespace(model="test-llm")))


def _run(monkeypatch, scores, query="问题", rewritten="改写后的问题",
         rewrite_fail=False, **kw):
    """组合全部桩 + asyncio.run 执行一次 agentic 决策

    返回 (result, calls, active_cfg)：calls = 每轮检索 query 列表。
    """
    calls = _stub_retrieve(scores, monkeypatch)
    _stub_rewrite(monkeypatch, rewritten=rewritten, fail=rewrite_fail)
    _stub_active_config(monkeypatch, **kw)
    from backend.services.agentic_service import get_agentic_service
    result = asyncio.run(get_agentic_service().run("kb_x", query))
    return result, calls


class TestAgenticConfig:
    """/api/settings/chat 的 agentic 段白名单校验"""

    def test_save_and_get_agentic(self, client, admin_headers):
        """super_admin 保存 agentic 配置成功，GET 返回合并值"""
        resp = client.post("/api/settings/chat", json={
            "agentic": {
                "enabled": True,
                "max_retries": 2,
                "recheck_threshold": 0.6,
                "abstain_threshold": 0.3,
            },
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["agentic"]["enabled"] is True
        g = client.get("/api/settings/chat", headers=admin_headers)
        assert g.status_code == 200
        assert g.json()["agentic"] == {
            "enabled": True,
            "max_retries": 2,
            "recheck_threshold": 0.6,
            "abstain_threshold": 0.3,
        }

    def test_agentic_unknown_field_400(self, client, admin_headers):
        """段内未知字段 → 400（白名单 = schema whitelist）"""
        resp = client.post("/api/settings/chat", json={
            "agentic": {"foo": 1},
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "Agentic" in resp.json()["detail"]

    def test_agentic_max_retries_range_400(self, client, admin_headers):
        """max_retries 越界 → 400（range 校验，文案含中文标签）"""
        resp = client.post("/api/settings/chat", json={
            "agentic": {"max_retries": 9},
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "改写重试次数需为 0~5" in resp.json()["detail"]


class TestAgenticDecisionGraph:
    """LangGraph 图单元测试：分档/改写/拒答/降级（纯桩，无网络）"""

    def test_high_score_answer_no_rewrite(self, monkeypatch):
        """0.8 分 → answer：不改写、trace 1 轮"""
        result, calls = _run(monkeypatch, [0.8])
        assert result.decision == "answer"
        assert result.query == "问题"
        assert len(calls) == 1
        assert result.trace == [{
            "attempt": 1, "query": "问题", "best_score": 0.8,
            "sources_count": 1, "from_rewrite": False,
            "rewrite_failed": False,
        }]

    def test_mid_score_rewrite_then_answer(self, monkeypatch):
        """0.4 分 → 改写 → 0.7 分 → answer（trace 2 轮，改写命中）"""
        result, calls = _run(monkeypatch, [0.4, 0.7], query="口语化的问题")
        assert result.decision == "answer"
        assert result.query == "改写后的问题"
        assert calls == ["口语化的问题", "改写后的问题"]
        assert len(result.trace) == 2
        assert result.trace[1]["from_rewrite"] is True
        assert result.trace[1]["rewrite_failed"] is False

    def test_low_score_abstain_no_rewrite(self, monkeypatch):
        """0.1 分 < abstain → abstain：不改写、trace 1 轮"""
        result, calls = _run(monkeypatch, [0.1], query="乱问的问题")
        assert result.decision == "abstain"
        assert len(calls) == 1  # 不被拒答触发改写（零额外成本）

    def test_retry_exhausted_abstain(self, monkeypatch):
        """中档改写后仍中档 → 重试用尽 abstain（不无限循环）"""
        result, calls = _run(monkeypatch, [0.4, 0.4], max_retries=1)
        assert result.decision == "abstain"
        assert len(result.trace) == 2
        assert len(calls) == 2  # 恰好 1 次改写检索，无多余循环

    def test_rewrite_failure_fallback(self, monkeypatch):
        """改写失败（LLM 异常）→ 降级原查询重检，trace 标记 rewrite_failed"""
        result, calls = _run(monkeypatch, [0.4, 0.4], rewrite_fail=True)
        assert result.decision == "abstain"
        assert calls[1] == "问题"  # 原查询重检
        assert result.trace[1]["rewrite_failed"] is True

    def test_max_retries_zero_abstain(self, monkeypatch):
        """max_retries=0 → 中档直接 abstain（不改写）"""
        result, calls = _run(monkeypatch, [0.4], max_retries=0)
        assert result.decision == "abstain"
        assert len(calls) == 1

    def test_no_vector_score_answer(self, monkeypatch):
        """纯 BM25 命中（vector_score=None）→ 视为通过（answer）"""
        class _StubRetrieval:
            async def retrieve(self, kb_id, query, top_k=None, min_score=None):
                s = _source(0.0)
                s.vector_score = None  # BM25-only 命中
                return [s]

        monkeypatch.setattr("backend.services.agentic_service.get_retrieval_service",
                            lambda: _StubRetrieval())
        _stub_active_config(monkeypatch)
        from backend.services.agentic_service import get_agentic_service
        result = asyncio.run(get_agentic_service().run("kb_x", "关键词问题"))
        assert result.decision == "answer"

    def test_run_iter_phase_sequence_rewrite(self, monkeypatch):
        """run_iter 阶段事件序列：retrieving → rewriting → rechecking → result"""
        _stub_retrieve([0.4, 0.7], monkeypatch)
        _stub_rewrite(monkeypatch, "改写后的问题")
        _stub_active_config(monkeypatch)
        from backend.services.agentic_service import get_agentic_service
        events: list = []

        async def collect():
            async for kind, payload in get_agentic_service().run_iter(
                    "kb_x", "口语化的问题"):
                events.append((kind, payload))

        asyncio.run(collect())
        phases = [p["stage"] for k, p in events if k == "phase"]
        assert phases == ["retrieving", "rewriting", "rechecking"]
        result = [p for k, p in events if k == "result"][0]
        assert result.decision == "answer"
        assert len(result.trace) == 2

    def test_run_iter_phase_sequence_abstain(self, monkeypatch):
        """拒答路径阶段：仅 retrieving（不进入改写，无额外事件）"""
        _stub_retrieve([0.1], monkeypatch)
        _stub_rewrite(monkeypatch)
        _stub_active_config(monkeypatch)
        from backend.services.agentic_service import get_agentic_service
        events: list = []

        async def collect():
            async for kind, payload in get_agentic_service().run_iter(
                    "kb_x", "乱问的问题"):
                events.append((kind, payload))

        asyncio.run(collect())
        assert [p["stage"] for k, p in events if k == "phase"] == ["retrieving"]
        assert [p for k, p in events if k == "result"][0].decision == "abstain"


class TestAgenticChatIntegration:
    """chat_service 集成（真实检索 + mock_llm；agentic 经设置接口开启）"""

    @staticmethod
    def _enable(client, admin_headers):
        resp = client.post("/api/settings/chat", json={
            "agentic": {"enabled": True},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text

    def test_disabled_by_default_no_agentic_event(self, client, mock_embedding,
                                                  mock_llm, admin_headers):
        """默认关闭：SSE 无 agentic 事件（行为与未接入一致）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert "event: agentic" not in resp.text
        assert "event: prompt" in resp.text

    def test_enabled_answer_path(self, client, mock_embedding,
                                 mock_llm, admin_headers):
        """开启后高分直接答：agentic 事件（trace 1 轮无改写）+ 正常生成"""
        self._enable(client, admin_headers)
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        mock = mock_llm(parts=["这是测试回答。"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么语言？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        text = resp.text
        assert "event: agentic" in text
        # 决策阶段进度事件同样下发（检索中/改写中/重检中，前端换提示）
        assert "event: agentic_status" in text
        # 定位 event: agentic 后的 data 行（其前有 agentic_status，不能取首个 data）
        lines = text.splitlines()
        ag_idx = next(i for i, ln in enumerate(lines) if ln == "event: agentic")
        agentic_data = json.loads(
            next(ln[6:] for ln in lines[ag_idx:] if ln.startswith("data: ")))
        assert agentic_data["trace"][0]["from_rewrite"] is False
        assert "event: delta" in text
        assert "event: done" in text
        assert len(mock.instances) == 1  # 仅生成一次（无多余 LLM 调用）

    def test_enabled_abstain_no_llm(self, client, mock_embedding,
                                    mock_llm, admin_headers):
        """拒答路径：agentic 事件 + 未检索提示 delta，不调 LLM 生成"""
        self._enable(client, admin_headers)
        kb = create_kb(client)  # 空库 → 检索无命中 → 拒答
        mock = mock_llm(parts=["不应出现"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "库中不存在的内容",
        }, headers=admin_headers)
        assert resp.status_code == 200
        text = resp.text
        assert "event: agentic" in text
        assert "未检索到相关内容" in text
        assert "event: prompt" not in text  # 拒答不进入生成
        assert mock.instances == []  # LLM 零调用（分数过低也不改写）


class TestAgenticMerge:
    """settings_merge 部门字段级覆盖（纯函数）"""

    def test_dept_overrides_global(self):
        from backend.services.settings_merge import merge_chat_config
        profile = {"agentic": {"enabled": False, "max_retries": 1,
                               "recheck_threshold": 0.55,
                               "abstain_threshold": 0.25},
                   "retrieval": {}, "chat": {}}
        merged = merge_chat_config(profile, {"agentic": {"enabled": True}})
        assert merged["agentic"]["enabled"] is True
        assert merged["agentic"]["max_retries"] == 1  # 未覆盖字段沿用全局

    def test_global_defaults(self):
        from backend.services.settings_merge import chat_payload
        payload = chat_payload({"agentic": {}})
        assert payload["agentic"] == {
            "enabled": False, "max_retries": 1,
            "recheck_threshold": 0.55, "abstain_threshold": 0.25,
        }
