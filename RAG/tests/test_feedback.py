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


def test_feedback_logs_filters(client, admin_headers, user_headers,
                               dept_admin_headers):
    """筛选：用户名模糊 / 部门 / 关键词搜原因 / 组合 AND / 部门名回填

    数据：dept_admin_test 与 user_test 同属"测试部门"，admin 为超管且无部门。
    """
    # admin（超管，无部门）好评（无原因）
    r = client.post("/api/chat/feedback", json={"rating": "up"},
                    headers=admin_headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    # 部门管理员点踩（原因含"数据"）
    r = client.post("/api/chat/feedback",
                    json={"rating": "down", "reason": "数据口径不对"},
                    headers=dept_admin_headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    # 普通用户点踩（原因含"引用"）
    r = client.post("/api/chat/feedback",
                    json={"rating": "down", "reason": "引用来源错误"},
                    headers=user_headers)
    assert r.status_code == 200 and r.json()["ok"] is True

    base = "/api/stats/chat-feedback/logs"
    assert client.get(base, headers=admin_headers).json()["total"] == 3

    # 用户名模糊搜索
    d = client.get(f"{base}?username=dept", headers=admin_headers).json()
    assert d["total"] == 1 and d["items"][0]["username"] == "dept_admin_test"
    d = client.get(f"{base}?username=user_test", headers=admin_headers).json()
    assert d["total"] == 1 and d["items"][0]["username"] == "user_test"
    d = client.get(f"{base}?username=查无此人", headers=admin_headers).json()
    assert d["total"] == 0

    # 关键词搜点踩原因（模糊匹配 reason）
    d = client.get(f"{base}?keyword=数据", headers=admin_headers).json()
    assert d["total"] == 1 and "数据口径" in d["items"][0]["reason"]
    d = client.get(f"{base}?keyword=引用", headers=admin_headers).json()
    assert d["total"] == 1 and d["items"][0]["reason"] == "引用来源错误"
    # 好评原因为空，不该被关键词命中
    d = client.get(f"{base}?keyword=乌仁吉", headers=admin_headers).json()
    assert d["total"] == 0

    # 部门过滤：测试部门 2 条（dept_admin + user），admin 无部门不命中
    dept_id = next(dd["id"] for dd in
                   client.get("/api/departments", headers=admin_headers).json()
                   if dd["name"] == "测试部门")
    d = client.get(f"{base}?department_id={dept_id}",
                   headers=admin_headers).json()
    assert d["total"] == 2
    assert {x["username"] for x in d["items"]} == {"dept_admin_test", "user_test"}
    # 部门名/部门 id 回填
    assert all(x["department_name"] == "测试部门" for x in d["items"])
    assert all(x["department_id"] == dept_id for x in d["items"])

    # 组合条件为 AND 语义
    d = client.get(f"{base}?department_id={dept_id}&rating=down&keyword=数据",
                   headers=admin_headers).json()
    assert d["total"] == 1 and d["items"][0]["username"] == "dept_admin_test"
    d = client.get(f"{base}?department_id={dept_id}&rating=up",
                   headers=admin_headers).json()
    assert d["total"] == 0

    # 无部门用户：department_id 为 null、department_name 为空串（前端显示 —）
    # 注：模糊搜 "admin" 会连 dept_admin_test 一起命中（含子串），故按用户名
    # 精确定位而非用模糊搜索的结果断言
    d = client.get(f"{base}?page_size=200", headers=admin_headers).json()
    admin_row = next(x for x in d["items"] if x["username"] == "admin")
    assert admin_row["department_id"] is None
    assert admin_row["department_name"] == ""

    # 空串参数按未传处理（前端清空筛选后可能带空串，不应把结果筛成 0）
    d = client.get(f"{base}?username=&keyword=&department_id=",
                   headers=admin_headers).json()
    assert d["total"] == 3


def test_feedback_logs_outer_join_keeps_orphan_rows(client, admin_headers,
                                                    user_headers):
    """已注销用户的反馈在**非用户名/部门**筛选下不消失（锁 outer join）

    用户名/部门条件需要 join users 表；若用 inner join，"用户已注销"的记录
    会在任何一次筛选（哪怕只是 rating/日期/关键词）时凭空消失，超管就再也
    看不到这些反馈了。
    """
    r = client.post("/api/users", json={
        "username": "orphan_fb", "password": "orphan12345",
        "display_name": "待注销用户", "role": "user",
    }, headers=admin_headers)
    assert r.status_code == 201, r.text
    orphan_id = r.json()["id"]
    r = client.post("/api/chat/feedback",
                    json={"rating": "down", "reason": "注销前提交的差评"},
                    headers=login_headers(client, "orphan_fb", "orphan12345"))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.delete(f"/api/users/{orphan_id}",
                         headers=admin_headers).status_code == 200

    # 仅按 rating/关键词筛选（不带用户名/部门条件）→ 孤儿记录必须还在
    d = client.get("/api/stats/chat-feedback/logs?rating=down&page_size=200",
                   headers=admin_headers).json()
    row = next((x for x in d["items"] if x["user_id"] == orphan_id), None)
    assert row is not None, "inner join 会让已注销用户的反馈在筛选后消失"
    assert row["username"] == "" and row["department_name"] == ""

    d = client.get("/api/stats/chat-feedback/logs?keyword=注销前",
                   headers=admin_headers).json()
    assert any(x["user_id"] == orphan_id for x in d["items"])


def test_feedback_viewer_scope_rejects_non_admin(client, user_headers,
                                                 dept_admin_headers):
    """反馈接口权限口径：仅超管；dept_admin 与普通用户均 404 伪装

    依赖注入点 feedback_viewer_scope 已预留部门管理员接入——放开时只改该
    函数（返回收窄的 FeedbackScope），本测试与路由签名均不受影响。
    """
    for headers in (user_headers, dept_admin_headers):
        for path in ("/api/stats/chat-feedback",
                     "/api/stats/chat-feedback/logs"):
            assert client.get(path, headers=headers).status_code == 404
