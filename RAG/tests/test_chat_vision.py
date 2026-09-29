"""聊天识图测试：读图 → 描述 → 并入检索与回答；视觉模型不可用降级

覆盖（`chat_service.describe_images` + `routers/chat.py` 的识图分支）：

- 成功链路：VLM 描述**同时**并入检索词（B）与注入 messages（A）——一次生成两处用
- 只发图不写字：自动补中性提问词（否则检索词/会话标题/图谱抽实体全是空串）
- 未配置视觉模型 → vision_error（不发消息、不落盘）
- 探活通过但读图失败 → 同样 vision_error（发送时兜底，与探活两层口径一致）
- 单次张数超限 → 400
- GET /api/chat/vision-status 四态：可用 / 探活失败 / 未配置 / 总开关关
- 落盘：user 消息带 images + image_desc，**且不含 base64**（防会话文件爆炸）
- 删会话连带删对象存储里的图（截图可能带敏感信息）

全部离线：`fake_vision` 替换整条视觉链路（配置解析/探活/读图），
`FakeRetrieval` 记录检索词并返回固定命中，不碰 192.168.0.11 与真实向量库。
"""
from __future__ import annotations

import base64
import json

from conftest import create_kb, extract_session_id

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


class FakeRetrieval:
    """记录检索词的伪检索服务；返回一条固定命中（走完整生成链路）"""

    def __init__(self):
        self.queries: list = []

    async def retrieve_multi(self, kb_ids, query, top_k=None, min_score=None,
                             enable_hybrid=None, enable_rerank=None):
        self.queries.append(query)
        from backend.models.rag_models import Source
        return [Source(id="d1_0", text="E-1042 是通信超时故障码。",
                       score=0.9, document_id="d1",
                       document_name="故障手册.txt", kb_id=kb_ids[0])]


def _patch_retrieval(monkeypatch) -> FakeRetrieval:
    fake = FakeRetrieval()
    monkeypatch.setattr(
        "backend.services.chat_service.get_retrieval_service", lambda: fake)
    return fake


def _upload(client, headers) -> str:
    """上传一张最小 PNG，返回 key"""
    resp = client.post("/api/chat/upload-image",
                       files={"file": ("t.png", PNG_BYTES, "image/png")},
                       headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["key"]


def _sse_events(text: str) -> list:
    """SSE 文本 → [(event, data), ...]"""
    out = []
    for block in text.split("\n\n"):
        if not block.startswith("event: "):
            continue
        head, _, body = block.partition("\ndata: ")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = body
        out.append((head[len("event: "):], payload))
    return out


def _event_map(text: str) -> dict:
    """SSE 文本 → {事件名: 数据}（同名事件取最后一条）"""
    return dict(_sse_events(text))


def _prompt_blob(text: str) -> str:
    """prompt 事件里实际发给 LLM 的 messages（JSON 串，便于子串断言）"""
    for ev, data in _sse_events(text):
        if ev == "prompt":
            return json.dumps(data.get("prompt") or [], ensure_ascii=False)
    return ""


def _ask(client, kb_id, headers, query, images, **extra):
    return client.post("/api/chat/stream", json={
        "kb_id": kb_id, "query": query, "images": images, **extra,
    }, headers=headers)


class TestVisionHappyPath:

    def test_desc_joins_retrieval_and_prompt(self, client, admin_headers,
                                             mock_embedding, mock_llm,
                                             fake_vision, monkeypatch):
        """★ 一次生成两处用：描述既进检索词（B）又进 messages（A）"""
        fake_vision(desc="图中显示设备型号 X200，屏幕提示报错 E-1042。")
        kb = create_kb(client)
        fake = _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "这个报错怎么解决", [key])
        assert resp.status_code == 200, resp.text

        assert fake.queries, "应发生检索"
        q = fake.queries[0]
        assert "E-1042" in q, "描述应并入检索词（否则'这张图'命中不了）"
        assert "这个报错怎么解决" in q, "原问题不能丢"

        blob = _prompt_blob(resp.text)
        assert "E-1042" in blob, "描述应注入 messages"
        assert "【用户上传的图片内容】" in blob, "注入块要有明确标识"

    def test_image_only_fills_default_question(self, client, admin_headers,
                                               mock_embedding, mock_llm,
                                               fake_vision, monkeypatch):
        """只发图不写字：补中性提问词（空串进图谱抽实体/问题拆分行为未定义）"""
        fake_vision()
        kb = create_kb(client)
        fake = _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "", [key])
        assert resp.status_code == 200, resp.text
        assert fake.queries
        assert "请描述这张图片" in fake.queries[0]

    def test_no_image_path_unchanged(self, client, admin_headers,
                                     mock_embedding, mock_llm,
                                     fake_vision, monkeypatch):
        """不发图：prompt 里不出现图片块（老路径逐字节不变，不影响既有行为）"""
        fake_vision()
        kb = create_kb(client)
        fake = _patch_retrieval(monkeypatch)
        resp = _ask(client, kb["id"], admin_headers, "普通问题", [])
        assert resp.status_code == 200
        assert fake.queries == ["普通问题"]
        assert "【用户上传的图片内容】" not in _prompt_blob(resp.text)

    def test_long_desc_truncated(self, client, admin_headers, mock_embedding,
                                 mock_llm, fake_vision, monkeypatch):
        """描述超长时硬截断到 chat.image_desc_max_chars（默认 800）

        提示词里那句"不超过 N 字"是软约束——真机实测：配置 800 字，
        模型实际输出 1498 字。这段描述要并进检索词，超长会淹没原问题。
        """
        fake_vision(desc="很" * 3000)
        kb = create_kb(client)
        fake = _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "问题", [key])
        assert resp.status_code == 200, resp.text
        q = fake.queries[0]
        assert len(q) < 1200, f"检索词不该被超长描述撑爆，实际 {len(q)} 字"
        assert q.endswith("…"), "截断要留省略标记"

    def test_requires_query_or_image(self, client, admin_headers,
                                     mock_embedding, fake_vision):
        """图和文字都没有 → 422（只发图合法，见上一个用例）"""
        fake_vision()
        kb = create_kb(client)
        resp = _ask(client, kb["id"], admin_headers, "", [])
        assert resp.status_code == 422


class TestVisionUnavailable:

    def test_unconfigured_yields_vision_error(self, client, admin_headers,
                                              mock_embedding, mock_llm,
                                              fake_vision):
        """未配置视觉模型 → vision_error，且**不发消息**（用户不必白等一轮）"""
        fake_vision(unconfigured=True)
        kb = create_kb(client)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "看图", [key])
        assert resp.status_code == 200, resp.text
        events = _event_map(resp.text)
        assert "vision_error" in events, "应下发专用的 vision_error 事件"
        assert "视觉模型当前无法使用，无法识图" in events["vision_error"]["message"]
        assert "done" not in events, "不该产出回答"
        assert "meta" not in events, "不该发生检索"

    def test_read_failure_yields_vision_error(self, client, admin_headers,
                                              mock_embedding, mock_llm,
                                              fake_vision):
        """探活通过但读图失败（发送时兜底）→ 同样 vision_error

        探活只证明服务活着，不证明模型真能读图，所以这一层必须有。
        """
        fake_vision(desc_error=True)
        kb = create_kb(client)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "看图", [key])
        events = _event_map(resp.text)
        assert "vision_error" in events
        assert "done" not in events

    def test_vision_error_not_persisted(self, client, admin_headers,
                                        mock_embedding, mock_llm,
                                        fake_vision):
        """vision_error 后不落盘：历史里不该出现这轮对话"""
        fake_vision(desc_error=True)
        kb = create_kb(client)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "看图", [key])
        # 没有 done 事件 → 前端拿不到新 session_id；此处直接查会话列表
        sessions = client.get("/api/chat/history",
                              headers=admin_headers).json()
        assert all(s["title"] != "看图" for s in sessions), \
            "读图失败的一轮不应落盘成会话"

    def test_too_many_images_400(self, client, admin_headers, mock_embedding,
                                 mock_llm, fake_vision):
        """超过 chat.image_max_count（默认 3）→ 400"""
        fake_vision()
        kb = create_kb(client)
        keys = [_upload(client, admin_headers) for _ in range(4)]
        resp = _ask(client, kb["id"], admin_headers, "看图", keys)
        assert resp.status_code == 400
        assert "最多" in resp.json()["detail"]


class TestVisionStatus:

    def test_available(self, client, admin_headers, fake_vision):
        fake_vision()
        body = client.get("/api/chat/vision-status",
                          headers=admin_headers).json()
        assert body["enabled"] is True and body["available"] is True
        assert body["reason"] == ""
        # 限制随探活一并下发（前端据此做同款拦截，不必再拉一次配置）
        assert body["max_count"] == 3 and body["max_mb"] == 5.0

    def test_probe_failure(self, client, admin_headers, fake_vision):
        fake_vision(available=False)
        body = client.get("/api/chat/vision-status",
                          headers=admin_headers).json()
        assert body["enabled"] is True
        assert body["available"] is False
        assert body["reason"], "不可用要给得出原因，前端好提示"

    def test_unconfigured(self, client, admin_headers, fake_vision):
        fake_vision(unconfigured=True)
        body = client.get("/api/chat/vision-status",
                          headers=admin_headers).json()
        assert body["available"] is False
        assert "管理员" in body["reason"], "要告诉用户找谁处理"

    def test_disabled(self, client, admin_headers, monkeypatch):
        """总开关关闭 → enabled=False（前端据此连图片入口都不显示）"""
        from types import SimpleNamespace

        from backend.routers import chat as chat_router

        monkeypatch.setattr(
            chat_router, "get_active_config",
            lambda: SimpleNamespace(chat=SimpleNamespace(
                image_enabled=False, image_max_count=3, image_max_mb=5.0)))
        body = client.get("/api/chat/vision-status",
                          headers=admin_headers).json()
        assert body["enabled"] is False
        assert body["available"] is False


class TestPersistAndCleanup:

    def test_message_stores_key_not_base64(self, client, admin_headers,
                                           mock_embedding, mock_llm,
                                           fake_vision, monkeypatch):
        """★ 落盘只存 key + 描述文本，**绝不存 base64**

        会话落盘在 data/chat/*.json：一张 2MB 的图 base64 后约 2.7MB，
        聊十轮该文件就 27MB，历史列表加载直接卡死。
        """
        fake_vision(desc="图片描述文本 ABCDEF")
        kb = create_kb(client)
        _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "看图", [key])
        sid = extract_session_id(resp.text)
        detail = client.get(f"/api/chat/history/{sid}",
                            headers=admin_headers).json()
        user_msg = next(m for m in detail["messages"] if m["role"] == "user")
        assert user_msg["images"] == [key]
        assert "ABCDEF" in user_msg["image_desc"]
        blob = json.dumps(detail, ensure_ascii=False)
        assert "base64" not in blob, "落盘内容里不该出现 base64"
        assert len(blob) < 10000, "会话详情不该被图片撑大"

    def test_delete_session_removes_images(self, client, admin_headers,
                                           mock_embedding, mock_llm,
                                           fake_vision, monkeypatch):
        """删会话 → 对象存储里的图一并删掉（截图可能带工号/客户名）"""
        fake_vision()
        kb = create_kb(client)
        _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "看图", [key])
        sid = extract_session_id(resp.text)

        _, user_id, name = key.split("/")
        url = f"/api/files/chat-images/{user_id}/{name}"
        assert client.get(url, headers=admin_headers).status_code == 200

        resp = client.delete(f"/api/chat/history/{sid}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert client.get(url, headers=admin_headers).status_code == 404, \
            "会话删除后图片应一并清理"
