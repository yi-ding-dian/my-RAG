"""A6 P2 POST /api/settings/chat 竞态测试

问题：update_profile 并发返回 None → _chat_payload(None) 抛 500
（TypeError: 'NoneType' object has no attribute 'get'）。
修复：失败分支（None）返回 400 明确错误"配置更新失败"。
"""
from __future__ import annotations


class TestChatSettingsUpdateNone:

    def test_update_none_returns_400(self, client, admin_headers, monkeypatch):
        """update_profile 返回 None（并发删除活跃档案等）→ 400 而非 500"""
        from backend.routers import settings as settings_router
        svc = settings_router.get_settings_service()
        monkeypatch.setattr(svc, "update_profile", lambda *a, **k: None)
        resp = client.post("/api/settings/chat",
                           json={"chat": {"history_rounds": 5}},
                           headers=admin_headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "配置更新失败"

    def test_normal_update_still_200(self, client, admin_headers):
        """未竞态时正常更新 → 200（回归）"""
        resp = client.post("/api/settings/chat",
                           json={"chat": {"history_rounds": 8}},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["chat"]["history_rounds"] == 8
