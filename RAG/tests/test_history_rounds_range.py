"""A5 P2 history_rounds 范围校验测试

问题：API 直传负数/0/超大值无校验 → chat_service messages[-(n*2):] 切片
语义错乱。
修复：settings_service.update_profile 对 chat.history_rounds 校验 1~20
（越界 → ValueError → 路由层 400）；前端表单已限 1-20（InputNumber）。
"""
from __future__ import annotations


class TestHistoryRoundsRange:

    def _update(self, client, headers, rounds):
        return client.post("/api/settings/chat",
                           json={"chat": {"history_rounds": rounds}},
                           headers=headers)

    def test_negative_400(self, client, admin_headers):
        """负值 → 400"""
        resp = self._update(client, admin_headers, -1)
        assert resp.status_code == 400
        assert "历史轮数" in resp.json()["detail"]

    def test_zero_400(self, client, admin_headers):
        """0 → 400"""
        resp = self._update(client, admin_headers, 0)
        assert resp.status_code == 400

    def test_over_20_400(self, client, admin_headers):
        """21 → 400"""
        resp = self._update(client, admin_headers, 21)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "历史轮数需为 1~20"

    def test_non_numeric_400(self, client, admin_headers):
        """非数字 → 400"""
        resp = self._update(client, admin_headers, "abc")
        assert resp.status_code == 400

    def test_boundary_1_and_20_ok(self, client, admin_headers):
        """边界 1 与 20 → 200 且生效"""
        for rounds in (1, 20):
            resp = self._update(client, admin_headers, rounds)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["chat"]["history_rounds"] == rounds

    def test_profile_put_also_400(self, client, admin_headers):
        """PUT /api/settings/profiles/{id} 直改 chat 段同样校验 400"""
        profiles = client.get("/api/settings/profiles",
                              headers=admin_headers).json()
        pid = profiles[0]["id"]
        resp = client.put(f"/api/settings/profiles/{pid}",
                          json={"chat": {"history_rounds": 0}},
                          headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "历史轮数需为 1~20"
