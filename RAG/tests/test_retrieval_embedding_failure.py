"""P1-2 Embedding 服务故障时不再静默空结果

问题：retrieval_service 捕获 embedding 异常后 return []，问答提示"未检索到
相关内容"误导用户（实际是 Embedding 服务不可用）。
修复：embedding 调用失败/无输出 → 抛 RetrievalUnavailableError（"检索服务
不可用：..."）；chat SSE 以 error 事件透传；检索测试接口 500；正常"无命中"
路径不受影响（仍返回空 + 友好提示）。
"""
from __future__ import annotations

from conftest import create_kb


class FailingEmbedding:
    """模拟 Embedding 服务不可用（网络错误）"""

    async def embed(self, texts):
        raise ConnectionError("connection refused (mock)")


class EmptyEmbedding:
    """模拟 Embedding 服务异常返回空（无输出）"""

    async def embed(self, texts):
        return []


class TestEmbeddingFailureSurfacesError:

    def _fail_embedding(self, monkeypatch, cls=FailingEmbedding):
        """替换 retrieval_service 模块内的 get_embedding_service 引用"""
        monkeypatch.setattr("backend.services.retrieval_service."
                            "get_embedding_service", lambda: cls())

    def test_chat_sse_emits_error_event(self, client, monkeypatch,
                                        admin_headers):
        """问答 SSE：embedding 抛错 → event: error（"检索服务不可用：..."）"""
        kb = create_kb(client)
        self._fail_embedding(monkeypatch)
        resp = client.post("/api/chat/stream",
                           json={"kb_id": kb["id"], "query": "测试问题"},
                           headers=admin_headers)
        assert resp.status_code == 200
        assert "event: error" in resp.text
        assert "检索服务不可用" in resp.text
        assert "Embedding 服务调用失败" in resp.text
        assert "event: done" not in resp.text  # 失败路径不落盘成功会话

    def test_chat_sse_empty_vector_output(self, client, monkeypatch,
                                          admin_headers):
        """embedding 返回空 → 同样报"检索服务不可用"而非空结果"""
        kb = create_kb(client)
        self._fail_embedding(monkeypatch, cls=EmptyEmbedding)
        resp = client.post("/api/chat/stream",
                           json={"kb_id": kb["id"], "query": "测试问题"},
                           headers=admin_headers)
        assert "event: error" in resp.text
        assert "检索服务不可用" in resp.text

    def test_retrieve_endpoint_500(self, client, monkeypatch, admin_headers):
        """检索测试接口：embedding 抛错 → 500 + detail 明确（前端显示错误）"""
        kb = create_kb(client)
        self._fail_embedding(monkeypatch)
        resp = client.post("/api/chat/retrieve",
                           json={"kb_id": kb["id"], "query": "测试问题"},
                           headers=admin_headers)
        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert "检索服务不可用" in detail
        assert "Embedding 服务调用失败" in detail

    def test_no_hits_still_normal_path(self, client, mock_embedding,
                                       admin_headers):
        """无命中正常路径不受影响：空知识库 → 友好提示而非 error"""
        kb = create_kb(client)
        resp = client.post("/api/chat/stream",
                           json={"kb_id": kb["id"], "query": "任何问题"},
                           headers=admin_headers)
        assert resp.status_code == 200
        assert "event: error" not in resp.text
        assert "未检索到相关内容" in resp.text

    def test_retrieve_no_hits_200(self, client, mock_embedding, admin_headers):
        """检索测试接口无命中 → 200 空 sources（区别于 500 失败）"""
        kb = create_kb(client)
        resp = client.post("/api/chat/retrieve",
                           json={"kb_id": kb["id"], "query": "任何问题"},
                           headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["sources"] == []
