"""聊天回答反馈闭环测试（v1：点赞/点踩/原因 + 超管汇总 + 分页查询）"""
from __future__ import annotations

import time

from conftest import create_kb, login_headers


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


def test_feedback_logs_pagination(client, admin_headers, user_headers):
    """分页查询接口:rating 过滤/created_at 倒序/用户与 kb 名回填/删除用户回退空串"""
    # admin 对真实知识库提交好评（kb_name 应回填真实名）
    kb = create_kb(client)
    r = client.post("/api/chat/feedback",
                    json={"rating": "up", "kb_id": kb["id"]},
                    headers=admin_headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    # 间隔 1s 再提交差评（ghost-kb 不存在 → kb_name 回退 kb_id），保证秒级时间可区分
    time.sleep(1.1)
    r = client.post("/api/chat/feedback",
                    json={"rating": "down", "kb_id": "ghost-kb",
                          "session_id": "s2", "msg_idx": 3,
                          "reason": "答案不准确，引用错误"},
                    headers=user_headers)
    assert r.status_code == 200 and r.json()["ok"] is True

    # 全量：默认分页字段 + created_at 倒序（最新=user 差评在前）
    d = client.get("/api/stats/chat-feedback/logs", headers=admin_headers).json()
    assert d["total"] == 2 and d["page"] == 1 and d["page_size"] == 20
    top = d["items"][0]
    assert top["rating"] == "down"
    assert top["username"] == "user_test" and top["display_name"] == "普通用户"
    assert top["kb_id"] == "ghost-kb" and top["kb_name"] == "ghost-kb"
    assert (top["session_id"] == "s2" and top["msg_idx"] == 3
            and top["reason"] == "答案不准确，引用错误")
    assert d["items"][1]["rating"] == "up"
    assert d["items"][1]["username"] == "admin"
    assert d["items"][1]["kb_name"] == kb["name"]

    # rating 过滤（up → 仅 admin 好评；非法值 → 400 显式提示）
    d = client.get("/api/stats/chat-feedback/logs?rating=up",
                   headers=admin_headers).json()
    assert d["total"] == 1 and d["items"][0]["username"] == "admin"
    r = client.get("/api/stats/chat-feedback/logs?rating=bad",
                   headers=admin_headers)
    assert r.status_code == 400

    # 分页：page_size=1 取最新一条；page 越界返回空 items 且 total 不变
    d = client.get("/api/stats/chat-feedback/logs?page=1&page_size=1",
                   headers=admin_headers).json()
    assert len(d["items"]) == 1 and d["items"][0]["username"] == "user_test"
    d = client.get("/api/stats/chat-feedback/logs?page=99",
                   headers=admin_headers).json()
    assert d["items"] == [] and d["total"] == 2
    # page_size 越界（<1 / >200）→ 422
    r = client.get("/api/stats/chat-feedback/logs?page_size=0",
                   headers=admin_headers)
    assert r.status_code == 422
    r = client.get("/api/stats/chat-feedback/logs?page_size=201",
                   headers=admin_headers)
    assert r.status_code == 422

    # 普通用户无权限（404 伪装，与汇总接口同口径）
    r = client.get("/api/stats/chat-feedback/logs", headers=user_headers)
    assert r.status_code == 404

    # 删除用户后其历史反馈仍在，username/display_name 回退空字符串
    r = client.post("/api/users", json={
        "username": "ghost_fb", "password": "ghost12345",
        "display_name": "幽灵用户", "role": "user",
    }, headers=admin_headers)
    assert r.status_code == 201, r.text
    ghost_id = r.json()["id"]
    r = client.post("/api/chat/feedback",
                    json={"rating": "down", "kb_id": "g-kb"},
                    headers=login_headers(client, "ghost_fb", "ghost12345"))
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.delete(f"/api/users/{ghost_id}", headers=admin_headers)
    assert r.status_code == 200, r.text
    items = client.get("/api/stats/chat-feedback/logs?rating=down&page_size=200",
                       headers=admin_headers).json()["items"]
    row = next(x for x in items if x["user_id"] == ghost_id)
    assert row["username"] == "" and row["display_name"] == ""
    assert row["kb_name"] == "g-kb"
