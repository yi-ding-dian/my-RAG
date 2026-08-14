"""A2 P1 chat top_k 透传测试

问题：前端 Chat.tsx 发送 top_k 但 ChatRequest 无此字段被静默忽略 →
聊天页 top_k 选择器无效。
修复：ChatRequest 加 top_k；chat_service.stream_chat 接收并透传给
retrieve(top_k=...)，None=用配置默认；1~50 范围校验（之外 400）。
"""
from __future__ import annotations


class FakeRetrieval:
    """记录 top_k 参数的伪检索服务（返回空命中 → 不调 LLM，直接 done）"""

    def __init__(self):
        self.calls = []

    async def retrieve(self, kb_id, query, top_k=None, min_score=None,
                       enable_hybrid=None, enable_rerank=None):
        self.calls.append(top_k)
        return []


def _patch_retrieval(monkeypatch) -> FakeRetrieval:
    fake = FakeRetrieval()
    monkeypatch.setattr(
        "backend.services.chat_service.get_retrieval_service",
        lambda: fake)
    return fake


def _stream(client, kb_id, headers, **extra):
    return client.post("/api/chat/stream", json={
        "kb_id": kb_id, "query": "测试问题", **extra}, headers=headers)


class TestChatTopK:

    def test_top_k_passed_to_retrieve(self, client, admin_headers, monkeypatch):
        """传 top_k=1 → 检索只用 1 条（断言透传参数）"""
        from conftest import create_kb
        kb = create_kb(client)
        fake = _patch_retrieval(monkeypatch)
        resp = _stream(client, kb["id"], admin_headers, top_k=1)
        assert resp.status_code == 200
        assert fake.calls == [1], "检索应收到 top_k=1"

    def test_top_k_default_when_omitted(self, client, admin_headers, monkeypatch):
        """不传 top_k → 检索收到 None（内部取配置默认）"""
        from conftest import create_kb
        kb = create_kb(client)
        fake = _patch_retrieval(monkeypatch)
        resp = _stream(client, kb["id"], admin_headers)
        assert resp.status_code == 200
        assert fake.calls == [None], "不传时透传 None 走配置默认"

    def test_top_k_boundary_ok(self, client, admin_headers, monkeypatch):
        """边界 1 与 50 → 200"""
        from conftest import create_kb
        kb = create_kb(client)
        fake = _patch_retrieval(monkeypatch)
        for tk in (1, 50):
            resp = _stream(client, kb["id"], admin_headers, top_k=tk)
            assert resp.status_code == 200, resp.text
        assert fake.calls == [1, 50]

    def test_top_k_out_of_range_400(self, client, admin_headers):
        """0 / 51 / 负数 → 400（范围校验，不进入检索）"""
        from conftest import create_kb
        kb = create_kb(client)
        for tk in (0, -1, 51, 100):
            resp = _stream(client, kb["id"], admin_headers, top_k=tk)
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"] == "top_k 需为 1~50"
