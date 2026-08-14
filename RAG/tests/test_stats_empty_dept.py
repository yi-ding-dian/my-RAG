"""B1: stats 会话统计空集合泄露 测试

背景：dept_admin 部门无用户时 stats 路由调 list_sessions(set())——空集合
falsy 落入"不过滤全部"分支 → 部门管理员看到全系统会话统计，违反部门隔离。
修复：chat_service.list_sessions 过滤条件改为显式判空
（user_id is not None and user_id != "all"），空集合也走过滤分支 → 返回空列表；
super_admin（None/"all"）与有成员部门、普通用户语义不受影响。
"""
from __future__ import annotations

import json
import uuid

from conftest import create_department_and_admin, create_user


def _write_chat(user_id, kb_id="kb_x", messages=1):
    """直接写会话 JSON 文件（等价于 chat 落盘格式）"""
    from backend.config import CHAT_DIR
    sid = uuid.uuid4().hex[:12]
    data = {
        "id": sid, "kb_id": kb_id, "user_id": user_id,
        "title": f"会话{sid}",
        "messages": [{"role": "user", "content": "测试问题"}] * messages,
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-02 00:00:00",
    }
    (CHAT_DIR / f"{sid}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return sid


def _me_id(client, headers):
    return client.get("/api/auth/me", headers=headers).json()["id"]


class TestStatsEmptyDept:

    def test_dept_admin_no_members_sees_zero_sessions(self, client, admin_headers,
                                                      monkeypatch):
        """部门无用户（list_users 返回空 → 传空集合 set()）：session_count=0，
        不泄露全系统会话（修复前会返回全系统会话数）"""
        _write_chat("other-user-1")
        _write_chat("other-user-2")
        _, dept_headers = create_department_and_admin(
            client, admin_headers, "空部门", "empty_dept_admin",
            "dept123456", "空部门管理员")
        # 模拟部门无任何用户：stats 路由对 list_users 的引用复制打桩返回空
        import backend.routers.stats as stats_module

        async def _no_users(db, dept):
            return []

        monkeypatch.setattr(stats_module, "list_users", _no_users)
        stats = client.get("/api/stats", headers=dept_headers).json()
        assert stats["session_count"] == 0
        assert stats["message_count"] == 0

    def test_dept_admin_with_members_only_sees_their_sessions(self, client,
                                                              admin_headers):
        """有成员部门：dept_admin 仅统计本部门用户会话，看不到 admin 的"""
        dept_id, dept_headers = create_department_and_admin(
            client, admin_headers, "有成员部门", "member_dept_admin",
            "dept123456", "有成员管理员")
        u1 = create_user(client, admin_headers, dept_id, "u1_dept",
                         password="user123456", display_name="部门用户1")
        u1_id = _me_id(client, u1)
        _write_chat(u1_id, messages=2)
        _write_chat(u1_id, messages=1)
        # admin 自己的会话不应计入 dept_admin 统计
        admin_id = _me_id(client, admin_headers)
        _write_chat(admin_id, messages=1)
        stats = client.get("/api/stats", headers=dept_headers).json()
        assert stats["session_count"] == 2   # 只算 u1 的 2 个会话
        assert stats["message_count"] == 3

    def test_user_sees_only_own_sessions(self, client, admin_headers):
        """普通用户仅统计自己的会话"""
        dept_id, _ = create_department_and_admin(
            client, admin_headers, "用户部门", "user_dept_admin",
            "dept123456", "用户部门管理员")
        u1 = create_user(client, admin_headers, dept_id, "u1_own",
                         password="user123456", display_name="用户甲")
        u2 = create_user(client, admin_headers, dept_id, "u2_own",
                         password="user123456", display_name="用户乙")
        u1_id = _me_id(client, u1)
        u2_id = _me_id(client, u2)
        _write_chat(u1_id, messages=2)
        _write_chat(u2_id, messages=5)
        stats = client.get("/api/stats", headers=u1).json()
        assert stats["session_count"] == 1
        assert stats["message_count"] == 2

    def test_super_admin_sees_all(self, client, admin_headers):
        """super_admin 全量统计不受影响（None 不过滤）"""
        admin_id = _me_id(client, admin_headers)
        _write_chat(admin_id, messages=1)
        _write_chat("someone-else", messages=3)
        _write_chat(None, messages=1)  # 旧会话无 user_id → 归属 super_admin
        stats = client.get("/api/stats", headers=admin_headers).json()
        assert stats["session_count"] == 3
        assert stats["message_count"] == 5
