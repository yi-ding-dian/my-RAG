"""聊天回答反馈闭环测试（v1：点赞/点踩/原因 + 超管汇总）"""
from __future__ import annotations


def test_feedback_flow_and_stats(client, admin_headers, user_headers):
    """提交 up/down（任意登录用户，含原因）→ 汇总接口总数/好差评正确"""
    # 用户提交点赞（带定位信息）
    r = client.post("/api/chat/feedback",
                    json={"rating": "up", "kb_id": "k1",
                          "session_id": "s1", "msg_idx": 0},
                    headers=user_headers)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    # 用户提交点踩（带原因）
    r = client.post("/api/chat/feedback",
                    json={"rating": "down", "reason": "答案不准确（应引用 2024 版规范）"},
                    headers=user_headers)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    # 非法 rating → 400
    r = client.post("/api/chat/feedback", json={"rating": "meh"},
                    headers=user_headers)
    assert r.status_code == 400
    # 未登录 → 401/404（不区分细节，核心是拒绝）
    r = client.post("/api/chat/feedback", json={"rating": "up"})
    assert r.status_code in (401, 404)

    # 超管汇总
    r = client.get("/api/stats/chat-feedback", headers=admin_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] >= 2 and d["up"] >= 1 and d["down"] >= 1
    assert any(x["rating"] == "down" and "2024" in (x["reason"] or "")
               for x in d["recent"])
    # 普通用户无权限（404 伪装防探测）
    r = client.get("/api/stats/chat-feedback", headers=user_headers)
    assert r.status_code == 404
