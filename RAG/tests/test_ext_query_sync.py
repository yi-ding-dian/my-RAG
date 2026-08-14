"""外部同步查询（MCP 接入 /api/ext/{id}/query）测试

覆盖：
- 鉴权：错 token/停用/配置不存在统一 401（与 /chat 一致防探测）
- 同步返回结构：{answer, sources: [{document_name, text, image_urls}]}，
  非流式 LLM（stream=False 一次返回）、config 生成参数覆盖、body top_k 覆盖
- sources 图片 URL 提取：含 /api/files/images/ 链接的 chunk → image_urls
- 无命中固定文案（不创建 LLM 客户端）；query 为空 422；top_k 越界 422
- LLM 失败 → 502；检索失败 → 400
- answer ≤6000 截断；sources text ≤2000/条
- 限流与 /chat 共用同一 config 桶（429）
- 审计日志与 /chat 同文件落盘
全部离线（mock embedding + LLM）。
"""
from __future__ import annotations

import json

from conftest import create_kb, upload_and_ingest
from backend.config import DATA_DIR


def create_ext(client, headers, name="外部查询", kb_ids=None, config=None):
    """创建外部查询（默认 admin 登录态），断言 201 并返回完整配置"""
    resp = client.post("/api/ext-queries", json={
        "name": name, "kb_ids": kb_ids or [], "config": config,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def sync_query(client, config_id, token, query, top_k=None):
    """调用 /api/ext/{id}/query 同步接口"""
    body = {"query": query}
    if top_k is not None:
        body["top_k"] = top_k
    return client.post(f"/api/ext/{config_id}/query", json=body,
                       headers={"Authorization": f"Bearer {token}"})


class TestSyncAuth:
    """同步接口鉴权：与 /chat 完全一致（统一 401 防探测）"""

    def test_auth_failures(self, client, admin_headers, mock_embedding):
        """无 token / 错 token / 配置不存在 / 停用 → 统一 401"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        # 无 token
        r = client.post(f"/api/ext/{item['id']}/query", json={"query": "x"})
        assert r.status_code == 401
        # 错 token
        r = sync_query(client, item["id"], "wrong-token-xxx", "x")
        assert r.status_code == 401
        # 配置不存在（与 token 错误同文案同状态码，防探测）
        r = client.post("/api/ext/nonexist/query", json={"query": "x"},
                        headers={"Authorization": "Bearer whatever"})
        assert r.status_code == 401
        assert "链接无效" in r.json()["detail"]
        # 停用
        client.post(f"/api/ext-queries/{item['id']}/toggle", headers=admin_headers)
        r = sync_query(client, item["id"], item["token"], "x")
        assert r.status_code == 401


class TestSyncResponse:
    """同步返回结构与生成参数"""

    def test_sync_structure_and_non_stream(self, client, admin_headers,
                                           mock_embedding, mock_llm):
        """返回 {answer, sources}；LLM 非流式调用（stream=False）且
        config 的 temperature/top_p/max_tokens 覆盖生效"""
        kb = create_kb(client, name="测试知识库")
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"temperature": 0.11, "top_p": 0.55,
                                  "max_tokens": 777})
        state = mock_llm()
        r = sync_query(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 200, r.text
        data = r.json()
        # 契约字段
        assert isinstance(data["answer"], str) and data["answer"]
        assert isinstance(data["sources"], list) and data["sources"]
        src = data["sources"][0]
        assert set(src) == {"document_name", "text", "image_urls"}
        assert src["document_name"] == "测试文档.txt"
        assert src["text"].strip()
        assert src["image_urls"] == []
        # 非流式调用 + 参数覆盖
        kwargs = state.instances[0].last_kwargs
        assert kwargs["stream"] is False
        assert kwargs["temperature"] == 0.11
        assert kwargs["top_p"] == 0.55
        assert kwargs["max_tokens"] == 777
        # messages 只有 system + user（同步查询无会话概念，不带历史）
        assert [m["role"] for m in kwargs["messages"]] == ["system", "user"]

    def test_top_k_override(self, client, admin_headers, mock_embedding,
                            mock_llm):
        """body.top_k 覆盖 config 检索条数；越界 422；空 query 422"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"top_k": 5})
        mock_llm()
        r = sync_query(client, item["id"], item["token"], "Python 是什么？",
                       top_k=1)
        assert r.status_code == 200, r.text
        assert len(r.json()["sources"]) <= 1, "top_k=1 应最多返回 1 条来源"
        # 越界 → 422
        r = sync_query(client, item["id"], item["token"], "Python", top_k=21)
        assert r.status_code == 422
        r = sync_query(client, item["id"], item["token"], "Python", top_k=0)
        assert r.status_code == 422
        # query 为空 → 422
        r = sync_query(client, item["id"], item["token"], "   ")
        assert r.status_code == 422
        assert "query 不能为空" in r.json()["detail"]

    def test_image_urls_extracted(self, client, admin_headers, mock_embedding,
                                  mock_llm):
        """含 /api/files/images/ 链接的 chunk → image_urls 提取（去重保序）"""
        content = ("# Python 简介\n\nPython 是一种高级编程语言，"
                   "由 Guido 于 1991 年发布。\n\n"
                   "![图1](/api/files/images/doc001/图1.png) 架构说明。\n"
                   "再引用一次 [图1](/api/files/images/doc001/图1.png)，"
                   "以及 [图2](/api/files/images/doc001/图2.png)。")
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], content=content)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        mock_llm()
        r = sync_query(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 200, r.text
        urls = [s["image_urls"] for s in r.json()["sources"]]
        all_urls = [u for sub in urls for u in sub]
        assert "/api/files/images/doc001/图1.png" in all_urls
        assert "/api/files/images/doc001/图2.png" in all_urls
        hit = next(s for s in r.json()["sources"]
                   if "/api/files/images" in s["text"])
        assert hit["image_urls"] == ["/api/files/images/doc001/图1.png",
                                     "/api/files/images/doc001/图2.png"], \
            "图片链接去重保序，且 image_urls 只含该条引用文本内的链接"

    def test_answer_truncated(self, client, admin_headers, mock_embedding,
                              mock_llm):
        """answer 超过 6000 字 → 截断到 ≤6000；sources text ≤2000/条"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        mock_llm(parts=["超" * 7000])
        r = sync_query(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data["answer"]) == 6000, "超长回答应截断至 6000 字"
        for s in data["sources"]:
            assert len(s["text"]) <= 2000, "单条引用文本应 ≤2000 字"


class TestSyncErrors:
    """同步接口错误路径"""

    def test_no_hit_no_llm(self, client, admin_headers, mock_embedding,
                           mock_llm):
        """空库无命中：固定文案 + sources=[]，不调用 LLM"""
        state = mock_llm(mode="error")  # 若被调用则会抛异常/记录实例
        kb = create_kb(client)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        r = sync_query(client, item["id"], item["token"], "完全不相关的问题")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["sources"] == []
        assert "未检索到相关内容" in data["answer"]
        assert not state.instances, "无命中时不应创建 LLM 客户端"

    def test_llm_failure_502(self, client, admin_headers, mock_embedding,
                             mock_llm):
        """LLM 调用失败 → 502 中文错误"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        mock_llm(mode="error")
        r = sync_query(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 502
        assert "LLM 调用失败" in r.json()["detail"]

    def test_retrieve_failure_400(self, client, admin_headers, mock_embedding,
                                  monkeypatch):
        """检索失败 → 400 中文错误"""
        from backend.routers import ext_query as ext_router_mod

        class _FailRetrieval:
            async def retrieve(self, *args, **kwargs):
                raise RuntimeError("向量库连接断开")

        monkeypatch.setattr(ext_router_mod, "get_retrieval_service",
                            lambda: _FailRetrieval())
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        r = sync_query(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 400
        assert "检索失败" in r.json()["detail"]


class TestSyncRateLimitAndLog:
    """限流与日志：与 /chat 共用同一 config 桶与同一日志文件"""

    def test_rate_limit_shared_with_chat(self, client, admin_headers,
                                         mock_embedding, monkeypatch):
        """/query 与 /chat 共用同一限流桶：/chat 消耗计数后 /query 满 → 429"""
        import backend.services.ext_query_service as eqs_mod
        monkeypatch.setattr(eqs_mod, "RATE_LIMIT_PER_MIN", 2)
        kb = create_kb(client)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        # /chat 消耗 1 次计数
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "x"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 200
        # /query 消耗第 2 次计数
        r = sync_query(client, item["id"], item["token"], "x")
        assert r.status_code == 200
        # 第 3 次（走 /chat 验证桶确实共用）→ 429
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "x"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 429
        # 不影响其他 config
        item2 = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                           name="另一个")
        r = sync_query(client, item2["id"], item2["token"], "x")
        assert r.status_code == 200

    def test_query_log_written_same_file(self, client, admin_headers,
                                         mock_embedding, mock_llm):
        """/query 审计日志与 /chat 同一文件落盘（命中记录 hit_count）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        mock_llm()
        # 先 /chat 再 /query，验证同文件追加
        client.post(f"/api/ext/{item['id']}/chat", json={"query": "Python 是什么？"},
                    headers={"Authorization": f"Bearer {item['token']}"})
        r = sync_query(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 200, r.text
        log_path = DATA_DIR / "ext_query_logs.jsonl"
        assert log_path.exists()
        lines = [json.loads(l) for l in
                 log_path.read_text(encoding="utf-8").strip().splitlines()]
        assert len(lines) == 2, "chat 与 query 应同文件各记一行"
        assert lines[1]["config_id"] == item["id"]
        assert lines[1]["query"] == "Python 是什么？"
        assert lines[1]["hit_count"] >= 1
