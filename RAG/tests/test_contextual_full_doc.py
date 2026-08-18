"""上下文检索"完整文档视角"增强测试

覆盖：
- 配置字段：出厂/新档案默认 20000；SECTION_SCHEMA 声明（int cast、fill_missing、
  fill_section、range 校验）；档案 CRUD 修改即时生效（改配置 → 活跃配置变化，
  无需重启）；越界 400；非超管修改 403（仅超管可配）
- enrich_chunks 输入组装：≤阈值 → 完整文档全文作上下文（prompt user message
  含全文，替代旧 1500 字符截断）；>阈值 → 抛 DocTooLongError（信息带
  "约 X.X 万字"与"不建议采用该方式入库"）；阈值每次调用动态读取（改配置后
  同代码路径行为变化，不缓存）；开关关不做长度校验
- ingestion 链路：超限 → 任务 failed 带明确提示（整文档超限 = 任务失败，
  区别于单块失败跳过）；≤阈值 → 成功且 prompt 含完整文档全文
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conftest import char_vector, create_kb, upload_doc, wait_for_status
from backend.chunking.splitter import Chunk
from backend.config import ContextualRetrievalConfig, build_default_config
from backend.services import settings_service as ss
from backend.services.contextual_retriever import (DocTooLongError,
                                                   enrich_chunks)
from backend.services.settings_service import SECTION_SCHEMA


# ==================== 伪 LLM 客户端（记录 prompt 组装） ====================

class _MsgRecorderClient:
    """伪 OpenAI 客户端：记录每次调用的 messages（断言完整文档传入），
    返回固定摘要"""

    def __init__(self, summary: str = "该片段位于文档第二章，介绍 Python 核心语法特性"):
        self.summary = summary
        self.call_count = 0
        self.messages_list: list = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.call_count += 1
        self.messages_list.append(kwargs.get("messages") or [])
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=self.summary))])


def _patch_ctx_client(monkeypatch, fake):
    """替换 contextual_retriever 的客户端工厂"""
    monkeypatch.setattr(
        "backend.services.contextual_retriever._get_client",
        lambda llm_cfg=None: fake)
    return fake


class _RecordingEmbedding:
    """记录输入文本的伪 embedding（字符直方图向量，离线可跑）"""

    def __init__(self):
        self.inputs = []

    async def embed(self, texts):
        self.inputs.extend(texts)
        return [char_vector(t) for t in texts]


def _patch_rec_embedding(monkeypatch) -> _RecordingEmbedding:
    rec = _RecordingEmbedding()
    monkeypatch.setattr(
        "backend.services.ingestion_service.get_embedding_service",
        lambda: rec)
    return rec


def _user_msg(msgs) -> str:
    """从 LLM 请求 messages 中取 user 消息 content
    （本地路径 QwenPrefill 策略会在末尾注入 assistant prefill 消息，
    须按 role 定位 user 消息）"""
    return next(m["content"] for m in msgs if m.get("role") == "user")


def _mk_chunks(texts) -> list:
    out = []
    pos = 0
    for t in texts:
        out.append(Chunk(text=t, char_start=pos, char_end=pos + len(t)))
        pos += len(t) + 1
    return out


def _set_threshold(value: int):
    """直接改活跃档案阈值并应用全局配置（模拟超管改配置，即时生效）"""
    svc = ss.get_settings_service()
    p = svc.get_active()
    p.setdefault("contextual_retrieval", {})["max_full_doc_chars"] = value
    svc._profiles[p["id"]] = p
    svc._save()
    svc._apply_active()


# ==================== 配置字段（默认/可改/超管/校验） ====================

class TestConfigField:

    def test_default_20000(self):
        """出厂配置与活跃配置默认 20000"""
        assert build_default_config().contextual_retrieval.max_full_doc_chars \
            == 20000
        from backend.config import get_active_config
        assert get_active_config().contextual_retrieval.max_full_doc_chars \
            == 20000

    def test_schema_declared(self):
        """SECTION_SCHEMA 字段声明：int cast / fill_missing / fill_section"""
        spec = SECTION_SCHEMA["contextual_retrieval"]
        f = spec.fields["max_full_doc_chars"]
        assert f.cast == "int"
        assert f.fill_missing is True
        assert f.condition == "truthy"
        assert spec.fill_section is True
        assert ContextualRetrievalConfig().max_full_doc_chars == 20000

    def test_old_profile_auto_fill_section(self):
        """旧档案缺段：_coerce 自动补整段默认（fill_section，存量升级）"""
        svc = ss.get_settings_service()
        coerced = svc._coerce({"id": "x", "name": "旧档案", "llm": {}})
        assert coerced["contextual_retrieval"]["max_full_doc_chars"] == 20000

    def test_update_via_profile_api(self, client, admin_headers):
        """API 改阈值：活跃配置即时生效（无需重启）"""
        pid = client.get("/api/settings/profiles/active",
                         headers=admin_headers).json()["id"]
        resp = client.put(
            f"/api/settings/profiles/{pid}",
            json={"contextual_retrieval": {"max_full_doc_chars": 5000}},
            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["contextual_retrieval"]["max_full_doc_chars"] == 5000
        from backend.config import get_active_config
        assert get_active_config().contextual_retrieval.max_full_doc_chars == 5000

    def test_out_of_range_400(self, client, admin_headers):
        """阈值越界（<1000 或 >1000000）→ 400（range 校验）"""
        pid = client.get("/api/settings/profiles/active",
                         headers=admin_headers).json()["id"]
        for bad in (100, 2000000):
            resp = client.put(
                f"/api/settings/profiles/{pid}",
                json={"contextual_retrieval": {"max_full_doc_chars": bad}},
                headers=admin_headers)
            assert resp.status_code == 400, resp.text

    def test_only_super_admin(self, client, user_headers, admin_headers):
        """非超管修改配置 → 403（仅超管可配）"""
        pid = client.get("/api/settings/profiles/active",
                         headers=admin_headers).json()["id"]
        resp = client.put(
            f"/api/settings/profiles/{pid}",
            json={"contextual_retrieval": {"max_full_doc_chars": 5000}},
            headers=user_headers)
        assert resp.status_code == 403, resp.text


# ==================== enrich_chunks 输入组装（完整文档 / 超限抛错） ====================

class TestEnrichFullDocInput:

    def test_full_doc_passed_within_threshold(self, monkeypatch):
        """≤阈值：完整文档全文作上下文（不再 1500 字符截断），
        prompt 含文档名首行 + 全文 + 当前片段"""
        fake = _patch_ctx_client(monkeypatch, _MsgRecorderClient())
        # 约 4000+ 字符，远超旧截断 1500——全量传入才能证明未被截断
        long_doc = "# 长文档\n\n" + "背景段落内容。" * 400
        assert len(long_doc) > 1500
        result = asyncio.run(enrich_chunks(
            _mk_chunks(["片段A", "片段B"]), long_doc,
            {"contextual_retrieval": True}, doc_name="长文档.txt"))
        assert len(result) == 2
        assert fake.call_count == 2
        for msgs in fake.messages_list:
            user_msg = _user_msg(msgs)
            assert "文档名称：长文档.txt" in user_msg
            assert long_doc in user_msg          # 完整文档全文
            assert long_doc[-30:] in user_msg    # 文档结尾也在（无截断）
            assert "片段A" in user_msg or "片段B" in user_msg

    def test_doc_too_long_raises(self, monkeypatch):
        """>阈值：抛 DocTooLongError，信息带万字与建议（任务失败提示）"""
        _patch_ctx_client(monkeypatch, _MsgRecorderClient())
        _set_threshold(1000)
        doc = "内容" * 600  # 1200 字符 > 1000
        with pytest.raises(DocTooLongError) as ei:
            asyncio.run(enrich_chunks(
                _mk_chunks(["片段"]), doc, {"contextual_retrieval": True}))
        msg = str(ei.value)
        assert "超过上下文检索完整文档阈值" in msg
        assert "万字" in msg
        assert "不建议采用该方式入库" in msg
        assert "请换用其他切块方式或关闭上下文检索增强" in msg

    def test_threshold_dynamic_read(self, monkeypatch):
        """阈值动态读取：改配置后同代码路径行为变化（每次调用实时读，不缓存）"""
        _patch_ctx_client(monkeypatch, _MsgRecorderClient())
        doc = "内容" * 600  # 1200 字符
        # 默认 20000：不超限
        assert len(asyncio.run(enrich_chunks(
            _mk_chunks(["片段"]), doc, {"contextual_retrieval": True}))) == 1
        # 改阈值 1000：立即超限
        _set_threshold(1000)
        with pytest.raises(DocTooLongError):
            asyncio.run(enrich_chunks(
                _mk_chunks(["片段"]), doc, {"contextual_retrieval": True}))
        # 改回 5000：恢复可用
        _set_threshold(5000)
        assert len(asyncio.run(enrich_chunks(
            _mk_chunks(["片段"]), doc, {"contextual_retrieval": True}))) == 1

    def test_error_message_wan_format(self, monkeypatch):
        """万字格式化：6084 字 → 约 0.6 万字；阈值 5000 → 0.5 万字"""
        _patch_ctx_client(monkeypatch, _MsgRecorderClient())
        _set_threshold(5000)
        doc = "长" * 6084
        with pytest.raises(DocTooLongError) as ei:
            asyncio.run(enrich_chunks(
                _mk_chunks(["片段"]), doc, {"contextual_retrieval": True}))
        msg = str(ei.value)
        assert "文档约 0.6 万字" in msg
        assert "（0.5 万字）" in msg

    def test_off_switch_no_limit_check(self, monkeypatch):
        """开关关：不做长度校验（enrich_chunks 直接返回空，不抛）"""
        fake = _patch_ctx_client(monkeypatch, _MsgRecorderClient())
        _set_threshold(10)
        result = asyncio.run(enrich_chunks(
            _mk_chunks(["片段"]), "很长" * 500,
            {"contextual_retrieval": False}))
        assert result == []
        assert fake.call_count == 0

    def test_empty_doc_within_threshold(self, monkeypatch):
        """空文档：不超限，正常走摘要生成（0 <= 阈值）"""
        fake = _patch_ctx_client(monkeypatch, _MsgRecorderClient())
        result = asyncio.run(enrich_chunks(
            _mk_chunks(["片段"]), "", {"contextual_retrieval": True}))
        assert len(result) == 1
        assert fake.call_count == 1


# ==================== ingestion 链路（超限任务失败 / 成功含完整文档） ====================

class TestIngestFullDoc:

    def _ingest(self, client, kb_id, doc_id, body=None, headers=None):
        return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                           json=body, headers=headers)

    def test_too_long_fails_task_with_hint(
            self, client, monkeypatch, admin_headers):
        """整文档超限：任务 failed，error 带明确提示（而非静默跳过）"""
        _patch_rec_embedding(monkeypatch)
        _patch_ctx_client(monkeypatch, _MsgRecorderClient())
        _set_threshold(500)
        content = "这是一段上下文检索完整文档阈值测试内容。" * 60  # ~1500 字符
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="超长文档.txt",
                         content=content)
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"contextual_retrieval": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"], status="failed")
        assert final["status"] == "failed"
        assert "超过上下文检索完整文档阈值" in final["error"]
        assert "万字" in final["error"]
        assert "不建议采用该方式入库" in final["error"]
        assert "请换用其他切块方式或关闭上下文检索增强" in final["error"]

    def test_success_within_threshold_full_doc_in_prompt(
            self, client, monkeypatch, admin_headers):
        """≤阈值成功入库：prompt 含完整文档全文（含文档结尾，无截断）"""
        _patch_rec_embedding(monkeypatch)
        fake = _patch_ctx_client(monkeypatch, _MsgRecorderClient())
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="测试文档.txt")
        resp = self._ingest(client, kb["id"], doc["id"],
                            body={"contextual_retrieval": True},
                            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert all(c.get("context") for c in final["chunks_meta"])
        assert fake.call_count == final["chunk_count"]
        for msgs in fake.messages_list:
            user_msg = _user_msg(msgs)
            assert "Python 简介" in user_msg          # 文档开头
            assert "数据分析、人工智能等领域" in user_msg  # 文档结尾（全量传入）
