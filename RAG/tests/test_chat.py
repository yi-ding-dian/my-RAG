"""聊天 API 集成测试：检索 / SSE 流式 / 会话历史

覆盖：retrieve 命中与空库、SSE 事件顺序 meta→delta→done、meta 携带
sources 数组、无命中不调 LLM、LLM 故障走 error 事件、query/message
双字段契约、会话历史列表/详情/删除。全部离线（mock embedding + LLM）。
多租户改造后：所有 API 需登录（默认 admin 登录态，helper 自动注入；
直接请求显式传 admin_headers）。
"""
from __future__ import annotations

import json

from conftest import create_kb, extract_session_id, upload_and_ingest


class TestRetrieve:
    """检索调试接口"""

    def test_retrieve_hits(self, client, mock_embedding, admin_headers):
        """入库后检索命中，sources 结构完整（契约 {sources: [...]}）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "Python 是什么语言？",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "sources" in data
        assert data["sources"], "入库后检索应至少命中一条"
        s = data["sources"][0]
        for field in ("id", "text", "score", "document_id",
                      "document_name", "chunk_index"):
            assert field in s, f"Source 缺字段: {field}"
        assert 0 <= s["score"] <= 1

    def test_retrieve_empty_kb(self, client, mock_embedding, admin_headers):
        """空库返回 {sources: []}（HTTP 200，无命中不报错）"""
        kb = create_kb(client)
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "任何问题",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == {"sources": []}

    def test_retrieve_empty_query_400(self, client, admin_headers):
        kb = create_kb(client)
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "   ",
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_retrieve_unknown_kb_404(self, client, admin_headers):
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": "nonexist", "query": "x",
        }, headers=admin_headers)
        assert resp.status_code == 404


class TestStream:
    """SSE 流式问答"""

    def test_event_order_meta_delta_done(self, client, mock_embedding,
                                         mock_llm, admin_headers):
        """SSE 事件顺序 meta → delta → done，meta 携带 sources 数组"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        text = resp.text
        for ev in ("meta", "delta", "done"):
            assert f"event: {ev}" in text, f"缺少事件 {ev}"
        idx_meta = text.index("event: meta")
        idx_delta = text.index("event: delta")
        idx_done = text.index("event: done")
        assert idx_meta < idx_delta < idx_done, "事件顺序必须是 meta→delta→done"
        # meta 的 data 是 {"sources": [...]}
        meta_block = text.split("event: meta", 1)[1].split("\n\n", 1)[0]
        meta_data = json.loads(meta_block.split("data: ", 1)[1].strip())
        assert isinstance(meta_data["sources"], list) and meta_data["sources"]
        # delta 的 data 是 {"text": ...}
        delta_block = text.split("event: delta", 1)[1].split("\n\n", 1)[0]
        delta_data = json.loads(delta_block.split("data: ", 1)[1].strip())
        assert isinstance(delta_data.get("text"), str)

    def test_stream_no_hit_without_llm(self, client, mock_embedding,
                                       mock_llm, admin_headers):
        """空库无命中：不调用 LLM，直接提示 + done"""
        state = mock_llm(mode="error")  # 若被调用则流式会抛异常/记录实例
        kb = create_kb(client)
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "完全不相关的问题",
        }, headers=admin_headers)
        assert resp.status_code == 200
        text = resp.text
        assert "event: delta" in text
        assert "event: done" in text
        assert "未检索到相关内容" in text
        assert not state.instances, "无命中时不应创建 LLM 客户端"

    def test_stream_llm_error_event(self, client, mock_embedding, mock_llm,
                                    admin_headers):
        """LLM 调用失败 → SSE error 事件（HTTP 仍 200，异常不冒出）"""
        mock_llm(mode="error")
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert "event: error" in resp.text
        assert "LLM" in resp.text or "失败" in resp.text

    def test_stream_missing_query_422(self, client, admin_headers):
        """query 与 message 均缺失 → 422"""
        kb = create_kb(client)
        resp = client.post("/api/chat/stream", json={"kb_id": kb["id"]},
                           headers=admin_headers)
        assert resp.status_code == 422

    def test_stream_message_backward_compat(self, client, mock_embedding,
                                            mock_llm, admin_headers):
        """向后兼容：仅传 message 字段也可对话"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "message": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert "event: done" in resp.text

    def test_stream_unknown_kb_404(self, client, admin_headers):
        resp = client.post("/api/chat/stream", json={
            "kb_id": "nonexist", "query": "hi",
        }, headers=admin_headers)
        assert resp.status_code == 404


class TestHistory:
    """会话历史 CRUD"""

    def test_history_flow(self, client, mock_embedding, mock_llm,
                          admin_headers):
        """对话后：列表出现会话 → 详情含消息与 sources 快照 → 删除"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        session_id = extract_session_id(resp.text)
        assert session_id

        # 列表
        history = client.get("/api/chat/history", headers=admin_headers).json()
        item = next(h for h in history if h["id"] == session_id)
        assert item["message_count"] == 2
        assert item["title"]  # 标题取问题前 20 字
        assert item["kb_id"] == kb["id"]

        # 详情：user + assistant 消息，assistant 带 sources 快照
        detail = client.get(f"/api/chat/history/{session_id}",
                            headers=admin_headers).json()
        assert detail["messages"][0]["role"] == "user"
        assert detail["messages"][0]["content"] == "Python 是什么？"
        assistant = detail["messages"][1]
        assert assistant["role"] == "assistant"
        assert assistant["content"]
        assert assistant["sources"], "assistant 消息应带 sources 快照"

        # 删除
        assert client.delete(f"/api/chat/history/{session_id}",
                             headers=admin_headers).status_code == 200
        assert client.get(f"/api/chat/history/{session_id}",
                          headers=admin_headers).status_code == 404
        assert client.delete(f"/api/chat/history/{session_id}",
                             headers=admin_headers).status_code == 404
        assert client.get("/api/chat/history",
                          headers=admin_headers).json() == []

    def test_history_no_hit_saved(self, client, mock_embedding, mock_llm,
                                  admin_headers):
        """无命中对话也会落盘会话（提示作为 assistant 消息）"""
        kb = create_kb(client)
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "空库问题",
        }, headers=admin_headers)
        session_id = extract_session_id(resp.text)
        detail = client.get(f"/api/chat/history/{session_id}",
                            headers=admin_headers).json()
        assert "未检索到相关内容" in detail["messages"][1]["content"]
        assert detail["messages"][1]["sources"] == []
