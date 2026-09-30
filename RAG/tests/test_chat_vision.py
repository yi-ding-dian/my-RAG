"""聊天识图测试：读图 → 描述 → 并入检索与回答；视觉模型不可用降级

覆盖（`chat_vision.describe_images` + `routers/chat.py` 的识图分支）：

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

from conftest import create_department_and_admin, create_kb, create_user, \
    extract_session_id

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


# ==================== 识图配置（模型选择 + 读图提示词） ====================

def _activate_profile(client, admin_headers, **sections) -> str:
    """建一个新档案并激活（sections 透传，如 chat={"image_prompt": "..."}）

    改配置走**真实接口**而非 monkeypatch 配置对象：这样连"schema 注册 →
    保存 → 激活 → get_active_config 生效"整条链路一起验证。只 patch 的话，
    schema 里漏注册字段（配置存不进去）这类错误测不出来。
    """
    resp = client.post("/api/settings/profiles",
                       json={"name": "识图配置测试", **sections},
                       headers=admin_headers)
    assert resp.status_code == 200, resp.text
    pid = resp.json()["id"]
    resp = client.post(f"/api/settings/profiles/{pid}/activate",
                       headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return pid


def _vlm_prompt(st) -> str:
    """伪视觉客户端实际收到的提示词文本（第一张图那次调用）"""
    sent = st.clients[0].calls[0]["messages"]
    return sent[0]["content"][0]["text"]


def _vision_entry(name: str, model: str = "") -> dict:
    """构造一个 vision 段模型条目（resolve_chat_vision 按 name 匹配）"""
    return {"name": name, "base_url": f"http://{name}/v1",
            "api_key": "k", "model": model or name, "timeout": 30}


class TestVisionPromptConfigurable:
    """读图提示词可配（chat.image_prompt；空 = 内置默认）"""

    def test_custom_prompt_reaches_vlm(self, client, admin_headers,
                                       mock_embedding, mock_llm, fake_vision,
                                       monkeypatch):
        """★ 配了自定义提示词 → 发给 VLM 的就是它（此前硬编码改不了）"""
        st = fake_vision()
        _activate_profile(client, admin_headers,
                          chat={"image_prompt": "只认报错码，不描述画面。"})
        kb = create_kb(client)
        _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "看图", [key])
        assert resp.status_code == 200, resp.text
        text = _vlm_prompt(st)
        assert "只认报错码" in text
        assert "逐字抄录图中所有文字" not in text, \
            "自定义生效后不该再叠加内置默认（否则用户改了也没用）"

    def test_max_chars_placeholder_replaced(self, client, admin_headers,
                                            mock_embedding, mock_llm,
                                            fake_vision, monkeypatch):
        """{max_chars} 替换为 image_desc_max_chars 的实际值"""
        st = fake_vision()
        _activate_profile(client, admin_headers, chat={
            "image_prompt": "描述控制在 {max_chars} 字内。",
            "image_desc_max_chars": 1500,
        })
        kb = create_kb(client)
        _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "看图", [key])
        assert resp.status_code == 200, resp.text
        text = _vlm_prompt(st)
        assert "1500" in text, "占位符应替换为配置的描述上限"
        assert "{max_chars}" not in text, "占位符本身不该原样发给模型"

    def test_other_braces_intact(self, client, admin_headers, mock_embedding,
                                 mock_llm, fake_vision, monkeypatch):
        """提示词里的其他花括号原样保留（只 replace 一个占位符）

        防回归：若改用 str.format，用户写的 JSON 示例 `{"型号": "..."}`
        会因未知占位符直接抛 KeyError，整轮识图挂掉。
        """
        st = fake_vision()
        _activate_profile(client, admin_headers, chat={
            "image_prompt": '按 {"型号": "...", "报错": "..."} 的 JSON 输出。'})
        kb = create_kb(client)
        _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "看图", [key])
        assert resp.status_code == 200, resp.text
        assert '{"型号"' in _vlm_prompt(st), "用户提示词里的花括号必须原样保留"

    def test_default_prompt_when_unset(self, client, admin_headers,
                                       mock_embedding, mock_llm, fake_vision,
                                       monkeypatch):
        """没配提示词 → 用内置默认提示词，占位符照常替换"""
        st = fake_vision()
        kb = create_kb(client)
        _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "看图", [key])
        assert resp.status_code == 200, resp.text
        text = _vlm_prompt(st)
        assert "逐字抄录图中所有文字" in text, "空配置应回退内置默认提示词"
        assert "200" in text, "内置默认里的 {max_chars} 同样要替换"
        assert "{max_chars}" not in text, "占位符本身不该原样发给模型"


class TestChatVisionModelResolve:
    """识图模型三级优先：chat.image_model → image_summary.model → vision 第一个

    直接测解析函数而非走 HTTP：`fake_vision` 会把 resolve_chat_vision 整个
    换掉（否则每个用例都要真连 VLM），优先级逻辑就测不到了。
    db/dept_id 传 None——本组只验"全局档案"这一层，部门层由 merge 覆盖。
    """

    def _resolve(self):
        import asyncio
        from backend.services.image_summary import resolve_chat_vision
        return asyncio.run(resolve_chat_vision(None, None))

    def test_specified_model_wins(self, client, admin_headers):
        """① 指定了 image_model → 用它（与文档入库的图片摘要解耦）"""
        _activate_profile(client, admin_headers,
                          vision={"models": [_vision_entry("vl-a"),
                                             _vision_entry("vl-b")],
                                  "active": 0},
                          chat={"image_model": "vl-b"})
        assert self._resolve().name == "vl-b"

    def test_falls_back_to_image_summary_model(self, client, admin_headers):
        """② 没指定 → 沿用「图片摘要」选的模型（老档案行为不变）"""
        _activate_profile(client, admin_headers,
                          vision={"models": [_vision_entry("vl-a"),
                                             _vision_entry("vl-b")],
                                  "active": 0},
                          image_summary={"model": "vl-b"})
        assert self._resolve().name == "vl-b"

    def test_falls_back_to_first(self, client, admin_headers):
        """③ 前两级都没指定 → vision 段第一个"""
        _activate_profile(client, admin_headers,
                          vision={"models": [_vision_entry("vl-a"),
                                             _vision_entry("vl-b")],
                                  "active": 0})
        assert self._resolve().name == "vl-a"

    def test_deleted_model_falls_back(self, client, admin_headers):
        """指定的模型名被超管删掉 → 回退默认，不让识图直接不可用"""
        _activate_profile(client, admin_headers,
                          vision={"models": [_vision_entry("vl-a")],
                                  "active": 0},
                          chat={"image_model": "已经被删掉的模型"})
        assert self._resolve().name == "vl-a"

    def test_no_models_returns_none(self, client, admin_headers):
        """vision 段为空 → None（调用方据此下发 vision_error）"""
        _activate_profile(client, admin_headers, vision={"models": [],
                                                         "active": 0})
        assert self._resolve() is None


class TestDepartmentVisionPrompt:
    """部门覆盖读图提示词（端到端）：白名单 → 保存 → 部门成员发图实际生效

    覆盖"schema 注册 → 白名单 → chat_payload 带出 → merge 合并 → 读图用上"
    整条链路。只测 merge 纯函数的话，chat_payload 漏字段那个坑测不出来
    （它已经踩过两次：system_prompt_ref、citation_snippet_chars）。
    """

    def test_dept_prompt_applied_end_to_end(self, client, admin_headers,
                                            mock_embedding, mock_llm,
                                            fake_vision, monkeypatch):
        """★ 部门配了提示词 → 本部门成员发图走部门的（不是全局的）"""
        st = fake_vision()
        # 全局也配一个：断言必须能区分"部门生效"与"恰好全局也是这个"
        _activate_profile(client, admin_headers,
                          chat={"image_prompt": "全局读图提示词"})
        dept_id, dept_admin_hdrs = create_department_and_admin(
            client, admin_headers, "识图部", "vision_dept_admin",
            "pass123456", "识图主管")
        user_hdrs = create_user(client, admin_headers, dept_id,
                                "vision_member")
        resp = client.post("/api/settings/chat",
                           json={"chat": {"image_prompt": "部门读图提示词"}},
                           headers=dept_admin_hdrs)
        assert resp.status_code == 200, resp.text

        kb = create_kb(client, "识图部知识库", department_id=dept_id)
        _patch_retrieval(monkeypatch)
        key = _upload(client, user_hdrs)
        resp = _ask(client, kb["id"], user_hdrs, "看图", [key])
        assert resp.status_code == 200, resp.text
        text = _vlm_prompt(st)
        assert "部门读图提示词" in text, "部门覆盖应生效"
        assert "全局读图提示词" not in text

    def test_dept_empty_prompt_follows_global(self, client, admin_headers,
                                              mock_embedding, mock_llm,
                                              fake_vision, monkeypatch):
        """部门没配（留空）→ 跟随全局；部门管理员清空也回到跟随全局"""
        st = fake_vision()
        _activate_profile(client, admin_headers,
                          chat={"image_prompt": "全局读图提示词"})
        dept_id, dept_admin_hdrs = create_department_and_admin(
            client, admin_headers, "留空部", "vision_empty_admin",
            "pass123456", "留空主管")
        user_hdrs = create_user(client, admin_headers, dept_id,
                                "vision_empty_user")
        # 先配一个再清空：验证"清空能回到跟随"（不是设过就锁死）
        client.post("/api/settings/chat",
                    json={"chat": {"image_prompt": "临时提示词"}},
                    headers=dept_admin_hdrs)
        resp = client.post("/api/settings/chat",
                           json={"chat": {"image_prompt": ""}},
                           headers=dept_admin_hdrs)
        assert resp.status_code == 200, resp.text

        kb = create_kb(client, "留空部知识库", department_id=dept_id)
        _patch_retrieval(monkeypatch)
        key = _upload(client, user_hdrs)
        resp = _ask(client, kb["id"], user_hdrs, "看图", [key])
        assert resp.status_code == 200, resp.text
        text = _vlm_prompt(st)
        assert "全局读图提示词" in text, "部门留空应回退全局"
        assert "临时提示词" not in text


# ==================== 带问题读图（{question} 占位符） ====================

class TestVisionQuestionAware:
    """用户问题传进视觉模型：盲读会把整页界面当查询词，用户问的却常是
    箭头指的那一个字段——问题传进去，模型才知道往哪儿看。

    实测（同一张界面截图 + 「这个怎么配置」）：不传问题时模型会去描述
    「卡通头像」「11:55」这类无关元素；传了问题并明确要求"箭头指向的元素
    报出准确名称"，才认得出红色箭头所指的输入框。
    """

    def test_question_reaches_vlm(self, client, admin_headers, mock_embedding,
                                  mock_llm, fake_vision, monkeypatch):
        """★ 用户问题应出现在发给 VLM 的提示词里（内置默认提示词）"""
        st = fake_vision()
        kb = create_kb(client)
        _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "这个怎么配置", [key])
        assert resp.status_code == 200, resp.text
        text = _vlm_prompt(st)
        assert "这个怎么配置" in text, "用户问题要传给视觉模型（带问题读图）"
        assert "{question}" not in text, "占位符本身不该原样发给模型"

    def test_custom_prompt_question_placeholder(self, client, admin_headers,
                                                mock_embedding, mock_llm,
                                                fake_vision, monkeypatch):
        """自定义提示词里写了 {question} → 同样替换"""
        st = fake_vision()
        _activate_profile(client, admin_headers, chat={
            "image_prompt": "用户问的是：{question}。只描述与它相关的部分。"})
        kb = create_kb(client)
        _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "报错码是多少", [key])
        assert resp.status_code == 200, resp.text
        text = _vlm_prompt(st)
        assert "报错码是多少" in text
        assert "{question}" not in text

    def test_custom_prompt_without_placeholder_stays_blind(
            self, client, admin_headers, mock_embedding, mock_llm, fake_vision,
            monkeypatch):
        """自定义提示词没写 {question} → 盲读（老档案行为逐字节不变）

        兼容性保证：既有档案的提示词一个字不改，读图行为也不变——强行把
        问题拼进去会破坏用户自己写的输出格式约束（如"只输出 JSON"）。
        """
        st = fake_vision()
        _activate_profile(client, admin_headers, chat={
            "image_prompt": "只认报错码，不描述画面。"})
        kb = create_kb(client)
        _patch_retrieval(monkeypatch)
        key = _upload(client, admin_headers)
        resp = _ask(client, kb["id"], admin_headers, "这个怎么配置", [key])
        assert resp.status_code == 200, resp.text
        text = _vlm_prompt(st)
        assert "只认报错码" in text
        assert "这个怎么配置" not in text, \
            "没写 {question} 就不该带问题（老档案行为不变）"


class TestBuildMessages:
    """`_build_messages` 单元测试——它是 chat_vision 里「给视觉模型看什么」
    的唯一出口，改读图行为都从这儿下手，所以要单独锁住它的契约。"""

    def _text(self, **kw) -> str:
        from backend.services.chat_vision import _build_messages
        msg = _build_messages(data_url="data:image/png;base64,AAA", **kw)
        return msg[0]["content"][0]["text"]

    def test_empty_question_uses_placeholder(self):
        """只发图不打字 → 用中性占位，而不是在占位符处留一对空引号"""
        text = self._text(prompt="", max_chars=200, question="")
        assert "用户未提问" in text
        assert "「」" not in text, "空引号会让模型以为用户问了个空问题"

    def test_both_placeholders_replaced(self):
        text = self._text(prompt="问题：{question}，限 {max_chars} 字",
                          max_chars=333, question="怎么配")
        assert "怎么配" in text and "333" in text

    def test_other_braces_intact(self):
        """其他花括号原样保留（用 replace 而非 format，防 KeyError 挂掉整轮）"""
        text = self._text(prompt='按 {"型号": "..."} 输出', max_chars=200,
                          question="q")
        assert '{"型号"' in text

    def test_image_part_untouched(self):
        """图片部分不受占位符替换影响"""
        from backend.services.chat_vision import _build_messages
        url = "data:image/png;base64,ZZZ"
        msg = _build_messages(data_url=url, prompt="", max_chars=200,
                              question="q")
        assert msg[0]["content"][1]["image_url"]["url"] == url
