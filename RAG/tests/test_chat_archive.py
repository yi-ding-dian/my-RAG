"""会话删除与归档裁剪测试

删除会话对用户始终是真删除（详情 404、列表不见）。归档只服务超管从
「用户反馈」页回溯现场，且按反馈裁剪：

- 无任何反馈 → 物理删除，不归档（绝大多数会话如此，是省空间的主要来源）
- 有反馈 → 只保留被反馈的回答 + 其前 2 轮上下文，其余消息丢弃
- 反馈查询失败 → 归档完整会话（宁可多留，不能丢证据）

仅 super_admin 带 include_deleted=true 能读归档——普通用户/owner 读不回
自己已删的会话，否则"删除"形同虚设。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

from conftest import login_headers


def _multi_round(rounds: int) -> list:
    """构造 rounds 轮问答的消息数组（8 轮 = 16 条，index 偶数=用户、奇数=回答）"""
    out = []
    for i in range(rounds):
        out.append({"role": "user", "content": f"问题{i}"})
        out.append({"role": "assistant", "content": f"回答{i}"})
    return out


def _write_session(session_id, *, user_id=None, title="归档测试会话",
                   messages=None, kb_id="kb_archive"):
    """直接落盘会话 JSON（绕过问答链路，聚焦归档行为本身）"""
    from backend.config import CHAT_DIR
    CHAT_DIR.joinpath(f"{session_id}.json").write_text(json.dumps({
        "id": session_id,
        "kb_id": kb_id,
        "user_id": user_id,
        "title": title,
        "messages": messages or [
            {"role": "user", "content": "会议纪要模板在哪？"},
            {"role": "assistant", "content": "见《会议纪要规范》第 3 节。"},
        ],
        "created_at": "2026-08-10 10:00:00",
        "updated_at": "2026-08-10 10:00:00",
    }, ensure_ascii=False), encoding="utf-8")


def _feedback(client, headers, session_id, msg_idx, rating="down"):
    """提交一条反馈（会话删除时的归档范围由它决定）"""
    r = client.post("/api/chat/feedback", json={
        "rating": rating, "session_id": session_id, "msg_idx": msg_idx,
        "reason": "测试反馈",
    }, headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text


def _make_user(client, admin_headers, username="arch_user"):
    """建一个普通用户（无部门），返回 (user_id, headers)"""
    r = client.post("/api/users", json={
        "username": username, "password": "arch123456",
        "display_name": "归档测试用户", "role": "user",
    }, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()["id"], login_headers(client, username, "arch123456")


def _archived_ids():
    """当前归档目录里的会话 ID 集合"""
    from backend.config import CHAT_DELETED_DIR
    return {p.stem for p in CHAT_DELETED_DIR.glob("*.json")}


class TestDeleteWithoutFeedback:
    """无反馈的会话：直接物理删除，不占归档"""

    def test_no_feedback_deletes_physically(self, client, admin_headers):
        """没人反馈过的会话，删了就真删——归档目录不应出现它"""
        from backend.config import CHAT_DIR
        _write_session("sess_nofb")
        assert client.delete("/api/chat/history/sess_nofb",
                             headers=admin_headers).status_code == 200
        assert not CHAT_DIR.joinpath("sess_nofb.json").exists()
        assert "sess_nofb" not in _archived_ids(), "无反馈会话不该进归档"
        # 归档目录也没了 → 超管也回溯不了（本来就没有回溯价值）
        assert client.get("/api/chat/history/sess_nofb?include_deleted=true",
                          headers=admin_headers).status_code == 404

    def test_delete_twice_404(self, client, admin_headers):
        """重复删除 404（语义与改动前一致）"""
        _write_session("sess_twice")
        assert client.delete("/api/chat/history/sess_twice",
                             headers=admin_headers).status_code == 200
        assert client.delete("/api/chat/history/sess_twice",
                             headers=admin_headers).status_code == 404

    def test_archived_hidden_from_list(self, client, admin_headers):
        """归档会话不出现在会话列表（用户侧不可见）"""
        _write_session("sess_hidden")
        _feedback(client, admin_headers, "sess_hidden", 1)
        assert client.delete("/api/chat/history/sess_hidden",
                             headers=admin_headers).status_code == 200
        ids = [h["id"] for h in client.get("/api/chat/history",
                                           headers=admin_headers).json()]
        assert "sess_hidden" not in ids


class TestArchiveTrim:
    """有反馈的会话：裁剪归档（只留被反馈轮 + 前 2 轮上下文）"""

    def test_trim_keeps_feedback_round_and_context(self, client, admin_headers):
        """反馈在最后一轮 → 保留该轮 + 前 2 轮（共 3 轮），之前的丢弃"""
        from backend.config import CHAT_DELETED_DIR
        _write_session("sess_trim", messages=_multi_round(5))
        _feedback(client, admin_headers, "sess_trim", 9)  # 最后一条回答 A4
        assert client.delete("/api/chat/history/sess_trim",
                             headers=admin_headers).status_code == 200

        assert CHAT_DELETED_DIR.joinpath("sess_trim.json").exists()
        d = client.get("/api/chat/history/sess_trim?include_deleted=true",
                       headers=admin_headers).json()
        assert d["trimmed"] is True
        # Q2 A2 Q3 A3 Q4 A4（index 4..9）
        assert [m["content"] for m in d["messages"]] == [
            "问题2", "回答2", "问题3", "回答3", "问题4", "回答4"]

    def test_trim_from_start_when_context_insufficient(self, client, admin_headers):
        """反馈靠前、前面不足 2 轮 → 从头保留到该轮，不清空"""
        _write_session("sess_trim_head", messages=_multi_round(4))
        _feedback(client, admin_headers, "sess_trim_head", 3)  # A1，前面只有 2 个提问
        assert client.delete("/api/chat/history/sess_trim_head",
                             headers=admin_headers).status_code == 200
        d = client.get("/api/chat/history/sess_trim_head?include_deleted=true",
                       headers=admin_headers).json()
        assert [m["content"] for m in d["messages"]] == [
            "问题0", "回答0", "问题1", "回答1"]

    def test_multi_feedback_union(self, client, admin_headers):
        """同一会话多个反馈 → 各取上下文后并集"""
        _write_session("sess_union", messages=_multi_round(8))  # 16 条
        _feedback(client, admin_headers, "sess_union", 13)  # A6 → 保留 8..13
        _feedback(client, admin_headers, "sess_union", 15)  # A7 → 保留 10..15
        assert client.delete("/api/chat/history/sess_union",
                             headers=admin_headers).status_code == 200
        d = client.get("/api/chat/history/sess_union?include_deleted=true",
                       headers=admin_headers).json()
        assert d["trimmed"] is True
        assert [m["content"] for m in d["messages"]] == [
            "问题4", "回答4", "问题5", "回答5",
            "问题6", "回答6", "问题7", "回答7"]

    def test_trim_preserves_detail_fields(self, client, admin_headers):
        """裁剪只决定留哪几条，消息整条保留 → 详情字段（prompt/耗时/引用）不丢"""
        msgs = _multi_round(4)
        msgs[3]["prompt"] = [{"role": "system", "content": "系统提示"},
                             {"role": "user", "content": "问题1"}]
        msgs[3]["retrieval_ms"] = 123
        msgs[3]["sources"] = [{"id": "c1", "text": "片段", "score": 0.9,
                               "document_id": "d1", "document_name": "规程.pdf",
                               "chunk_index": 0}]
        _write_session("sess_detail", messages=msgs)
        _feedback(client, admin_headers, "sess_detail", 3)
        assert client.delete("/api/chat/history/sess_detail",
                             headers=admin_headers).status_code == 200
        d = client.get("/api/chat/history/sess_detail?include_deleted=true",
                       headers=admin_headers).json()
        hit = next(m for m in d["messages"] if m["content"] == "回答1")
        assert hit["retrieval_ms"] == 123
        assert hit["sources"][0]["document_name"] == "规程.pdf"
        assert hit["prompt"][0]["content"] == "系统提示"

    def test_feedback_query_failure_archives_full(self, client, admin_headers,
                                                  monkeypatch):
        """反馈查询失败 → 归档**完整**会话（宁可多留，不能丢证据）"""
        import backend.routers.chat as chat_router

        async def _fail(_session_id):
            return None

        monkeypatch.setattr(chat_router, "get_feedback_msg_idxs", _fail)
        msgs = _multi_round(4)
        _write_session("sess_dbfail", messages=msgs)
        assert client.delete("/api/chat/history/sess_dbfail",
                             headers=admin_headers).status_code == 200
        d = client.get("/api/chat/history/sess_dbfail?include_deleted=true",
                       headers=admin_headers).json()
        assert len(d["messages"]) == len(msgs), "查询失败时必须保留完整会话"
        assert d["trimmed"] is False

    def test_archived_mtime_refreshed_at_archive_time(self, client, admin_headers):
        """归档时 mtime 刷为归档时刻（归档时间语义用，非会话最后活动时间）"""
        from backend.config import CHAT_DELETED_DIR
        _write_session("sess_mtime")  # updated_at 为 2026-08-10
        _feedback(client, admin_headers, "sess_mtime", 1)
        assert client.delete("/api/chat/history/sess_mtime",
                             headers=admin_headers).status_code == 200
        p = CHAT_DELETED_DIR.joinpath("sess_mtime.json")
        assert abs(p.stat().st_mtime - time.time()) < 60


class TestArchiveReadPermission:
    """归档读取权限：仅超管带 include_deleted 可读"""

    def test_default_read_still_404(self, client, admin_headers):
        """不带 include_deleted 的超管读已删会话仍是 404（默认行为零变化）"""
        _write_session("sess_default")
        _feedback(client, admin_headers, "sess_default", 1)
        assert client.delete("/api/chat/history/sess_default",
                             headers=admin_headers).status_code == 200
        assert client.get("/api/chat/history/sess_default",
                          headers=admin_headers).status_code == 404

    def test_owner_cannot_read_archived(self, client, admin_headers):
        """关键安全断言：owner 带 include_deleted 也读不回自己删的会话

        否则"删除"形同虚设——用户以为删干净了，实际一个查询参数就能读回。
        非超管静默按 false 处理（而非 403），与 404 伪装口径一致。
        """
        uid, headers = _make_user(client, admin_headers)
        _write_session("sess_owner", user_id=uid)
        _feedback(client, headers, "sess_owner", 1)
        assert client.delete("/api/chat/history/sess_owner",
                             headers=headers).status_code == 200
        assert client.get("/api/chat/history/sess_owner",
                          headers=headers).status_code == 404
        assert client.get(
            "/api/chat/history/sess_owner?include_deleted=true",
            headers=headers).status_code == 404

    def test_other_user_cannot_read_archived(self, client, admin_headers):
        """他人（非 owner）带 include_deleted 也读不到（归属校验仍生效）"""
        uid, headers = _make_user(client, admin_headers)
        _, other = _make_user(client, admin_headers, username="arch_other")
        _write_session("sess_other", user_id=uid)
        _feedback(client, headers, "sess_other", 1)
        assert client.delete("/api/chat/history/sess_other",
                             headers=headers).status_code == 200
        assert client.get(
            "/api/chat/history/sess_other?include_deleted=true",
            headers=other).status_code == 404

    def test_super_admin_reads_archived_with_flag(self, client, admin_headers):
        """超管带 include_deleted=true → 读到现场（含归档标记）"""
        uid, headers = _make_user(client, admin_headers)
        _write_session("sess_admin_read", user_id=uid)
        _feedback(client, headers, "sess_admin_read", 1)
        assert client.delete("/api/chat/history/sess_admin_read",
                             headers=headers).status_code == 200

        r = client.get("/api/chat/history/sess_admin_read?include_deleted=true",
                       headers=admin_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["id"] == "sess_admin_read"
        assert d["archived"] is True, "归档内容须带 archived 标记供前端提示"
        assert d["messages"][0]["content"] == "会议纪要模板在哪？"
        assert d["messages"][1]["content"] == "见《会议纪要规范》第 3 节。"

    def test_live_session_archived_flag_false(self, client, admin_headers):
        """未删除的会话：archived=false（前端不显示"已被删除"提示）"""
        _write_session("sess_live")
        d = client.get("/api/chat/history/sess_live",
                       headers=admin_headers).json()
        assert d["archived"] is False


class TestFeedbackMsgIdxs:
    """get_feedback_msg_idxs：归档裁剪的输入（三态语义）"""

    def test_returns_idxs_for_session(self, client, admin_headers):
        _feedback(client, admin_headers, "sess_i1", 3)
        _feedback(client, admin_headers, "sess_i1", 7, rating="up")
        _feedback(client, admin_headers, "sess_i2", 5)  # 别的会话不该混入

        from backend.services.feedback_service import get_feedback_msg_idxs
        assert asyncio.run(get_feedback_msg_idxs("sess_i1")) == {3, 7}
        assert asyncio.run(get_feedback_msg_idxs("sess_i2")) == {5}
        # 无反馈 → 空集（调用方据此物理删除，不是 None）
        assert asyncio.run(get_feedback_msg_idxs("sess_none")) == set()
        # 空 session_id 直接空集，不查库
        assert asyncio.run(get_feedback_msg_idxs("")) == set()


def test_feedback_today_boundary_not_lost(client, admin_headers):
    """反馈时间筛选的当天边界（date_to 需补 23:59:59，否则当天记录全漏）

    created_at 为 "%Y-%m-%d %H:%M:%S" 字符串，字符串比较下
    "今天 14:00:00" <= "今天" 为 False。
    """
    r = client.post("/api/chat/feedback", json={"rating": "up"},
                    headers=admin_headers)
    assert r.status_code == 200 and r.json()["ok"] is True

    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    d = client.get(f"/api/stats/chat-feedback/logs?date_to={today}",
                   headers=admin_headers).json()
    assert d["total"] == 1, "date_to=今天 必须包含今天的反馈"
    d = client.get(f"/api/stats/chat-feedback/logs?date_from={today}",
                   headers=admin_headers).json()
    assert d["total"] == 1
    d = client.get(f"/api/stats/chat-feedback/logs?date_to={yesterday}",
                   headers=admin_headers).json()
    assert d["total"] == 0
    d = client.get(f"/api/stats/chat-feedback/logs?date_from={tomorrow}",
                   headers=admin_headers).json()
    assert d["total"] == 0
    # 起止同传（区间）
    d = client.get(
        f"/api/stats/chat-feedback/logs?date_from={today}&date_to={today}",
        headers=admin_headers).json()
    assert d["total"] == 1
