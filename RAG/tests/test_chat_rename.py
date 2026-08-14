"""会话重命名 API 测试

覆盖：本人重命名成功（接口返回 + 详情 + 列表 + 落盘文件 title 全同步）、
非本人 404 伪装（与 history 其他接口一致防探测）、会话不存在 404、
标题超长/空 400、super_admin 可改他人会话、旧会话（无 user_id）视为
super_admin 归属。全部离线：直接向 data/chat/ 写入会话 JSON 构造数据，
不依赖 LLM/embedding mock。
"""
from __future__ import annotations

import json

from conftest import _find_dept_id, create_user

from backend.config import CHAT_DIR


def _write_session(session_id: str, user_id=None, title="旧标题", kb_id="kb-test"):
    """直接落盘一个会话 JSON（与 ChatSession 落盘结构一致）"""
    data = {
        "id": session_id,
        "kb_id": kb_id,
        "user_id": user_id,
        "title": title,
        "messages": [],
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-01 00:00:00",
    }
    (CHAT_DIR / f"{session_id}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _user_id(client, headers) -> str:
    """当前登录用户 id"""
    return client.get("/api/auth/me", headers=headers).json()["id"]


def _rename(client, session_id, title, headers):
    return client.post(f"/api/chat/history/{session_id}/rename",
                       json={"title": title}, headers=headers)


class TestRenameSession:

    def test_rename_ok(self, client, user_headers):
        """本人重命名成功：接口返回 + 详情 + 列表 + 落盘文件 title 同步更新"""
        uid = _user_id(client, user_headers)
        sid = "sess-rename-ok"
        _write_session(sid, user_id=uid)

        resp = _rename(client, sid, "新标题", user_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "新标题"

        # 详情
        detail = client.get(f"/api/chat/history/{sid}",
                            headers=user_headers).json()
        assert detail["title"] == "新标题"
        # 落盘文件（任务书要求：文件内容 title 更新）
        on_disk = json.loads((CHAT_DIR / f"{sid}.json").read_text(encoding="utf-8"))
        assert on_disk["title"] == "新标题"
        # 列表
        item = next(h for h in client.get("/api/chat/history",
                                          headers=user_headers).json()
                    if h["id"] == sid)
        assert item["title"] == "新标题"

    def test_rename_other_user_404(self, client, admin_headers, user_headers):
        """非本人重命名 → 404 伪装（与 history 其他接口一致防探测），文件不被篡改"""
        uid = _user_id(client, user_headers)
        _write_session("sess-other", user_id=uid)
        dept_id = _find_dept_id(client, admin_headers, "测试部门")
        user_b_headers = create_user(client, admin_headers, dept_id, "user_b",
                                     password="user123456", display_name="用户B")
        resp = _rename(client, "sess-other", "篡改标题", user_b_headers)
        assert resp.status_code == 404
        assert "篡改标题" not in (CHAT_DIR / "sess-other.json").read_text(
            encoding="utf-8")

    def test_rename_missing_404(self, client, user_headers):
        """会话文件不存在 → 404"""
        assert _rename(client, "no-such-session", "标题",
                       user_headers).status_code == 404

    def test_rename_title_too_long_400(self, client, user_headers):
        """标题超 50 字 → 400"""
        uid = _user_id(client, user_headers)
        _write_session("sess-long", user_id=uid)
        resp = _rename(client, "sess-long", "长" * 51, user_headers)
        assert resp.status_code == 400
        assert "1~50" in resp.json()["detail"]

    def test_rename_blank_title_400(self, client, user_headers):
        """标题为空/全空格 → 400"""
        uid = _user_id(client, user_headers)
        _write_session("sess-blank", user_id=uid)
        assert _rename(client, "sess-blank", "   ",
                       user_headers).status_code == 400

    def test_super_admin_rename_other(self, client, admin_headers, user_headers):
        """super_admin 可重命名他人会话"""
        uid = _user_id(client, user_headers)
        _write_session("sess-admin", user_id=uid)
        resp = _rename(client, "sess-admin", "管理员改名", admin_headers)
        assert resp.status_code == 200, resp.text
        detail = client.get("/api/chat/history/sess-admin",
                            headers=admin_headers).json()
        assert detail["title"] == "管理员改名"

    def test_rename_legacy_session_no_user(self, client, admin_headers,
                                           user_headers):
        """旧会话无 user_id（视为 super_admin 归属）：普通用户 404，admin 可改"""
        _write_session("sess-legacy", user_id=None)
        assert _rename(client, "sess-legacy", "用户改名",
                       user_headers).status_code == 404
        assert _rename(client, "sess-legacy", "管理员改名",
                       admin_headers).status_code == 200
