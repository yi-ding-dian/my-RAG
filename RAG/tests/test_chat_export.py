"""会话导出 API 测试：GET /api/chat/history/{session_id}/export

覆盖：导出内容含问答与引用（Markdown 结构/引用编号/片段截断）、Content-Type
与 Content-Disposition 附件头、会话不存在 404、归属校验（非本人 404 伪装、
super_admin 放行）、空会话返回标题空模板。全部离线（mock embedding + LLM）。
"""
from __future__ import annotations

import json

from conftest import create_kb, extract_session_id, upload_and_ingest


def _write_session_file(session_id: str, user_id=None, messages=None,
                        title="测试会话", kb_id="kb_export"):
    """直接落盘会话 JSON（绕过 API，构造空会话/归属场景用）"""
    from backend.config import CHAT_DIR
    now = "2026-08-10 10:00:00"
    data = {
        "id": session_id,
        "kb_id": kb_id,
        "user_id": user_id,
        "title": title,
        "messages": messages or [],
        "created_at": now,
        "updated_at": now,
    }
    CHAT_DIR.joinpath(f"{session_id}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestExportContent:
    """导出内容与响应头"""

    def _create_chat_session(self, client, mock_embedding, mock_llm,
                             headers):
        """走 SSE 问答生成一条含检索命中的会话，返回 (session_id, 会话 JSON)"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        mock_llm(parts=["好的，根据[引用1]内容，", "Python 是一门编程语言。", "[1]"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么语言？",
        }, headers=headers)
        assert resp.status_code == 200
        session_id = extract_session_id(resp.text)
        detail = client.get(f"/api/chat/history/{session_id}", headers=headers)
        assert detail.status_code == 200
        return session_id, detail.json()

    def test_export_content_and_headers(self, client, mock_embedding,
                                        mock_llm, admin_headers):
        """导出 Markdown 含标题/问答/引用标与来源片段；附件头完整"""
        session_id, session = self._create_chat_session(
            client, mock_embedding, mock_llm, admin_headers)
        resp = client.get(f"/api/chat/history/{session_id}/export",
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text

        # 响应头
        assert resp.headers["content-type"].startswith("text/markdown")
        assert "charset=utf-8" in resp.headers["content-type"].lower()
        cd = resp.headers["content-disposition"]
        assert cd.startswith("attachment")
        assert "filename*=UTF-8''" in cd
        assert ".md" in cd
        assert resp.text.endswith("\n")

        # Markdown 内容结构
        text = resp.text
        assert f"# {session['title']}" in text, "标题行缺失"
        assert "kb_id:" in text and "时间:" in text
        assert "## 用户" in text
        assert "## 助手" in text
        assert "Python 是什么语言？" in text, "问题内容缺失"
        assert "Python 是一门编程语言。" in text, "回答正文缺失"
        # 引用标 [1] 与来源片段
        assert "[1]" in text, "回答中的 [n] 引用标缺失"
        assert "### 引用 1：" in text, "引用小节缺失"
        doc_name = session["messages"][1]["sources"][0]["document_name"]
        assert doc_name in text, "引用小节应含来源文档名"
        assert text.index("### 引用 1") > text.index("## 助手"), \
            "引用小节应位于对应助手消息之后"

    def test_export_snippet_truncated_to_500(self, client, admin_headers,
                                             mock_embedding, mock_llm):
        """引用片段截前 500 字（超长文本导出不膨胀）"""
        session_id, session = self._create_chat_session(
            client, mock_embedding, mock_llm, admin_headers)
        resp = client.get(f"/api/chat/history/{session_id}/export",
                          headers=admin_headers)
        assert resp.status_code == 200
        # 构造超长引用场景：直接写会话
        long_text = "长" * 800
        _write_session_file("long_session", user_id=None, messages=[
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答 [1]",
             "sources": [{
                 "id": "doc1_0", "text": long_text, "score": 0.9,
                 "document_id": "doc1", "document_name": "长文文档.txt",
                 "kb_id": "kb_export", "chunk_index": 0,
             }]},
        ])
        resp2 = client.get("/api/chat/history/long_session/export",
                           headers=admin_headers)
        assert resp2.status_code == 200
        assert "长" * 500 in resp2.text
        assert "长" * 501 not in resp2.text, "引用片段应截断至 500 字"

    def test_export_multiple_turns_refs_reset(self, client, admin_headers):
        """多轮会话：每条助手消息的引用编号从 1 重新编号（与回答内 [n] 一致）"""
        _write_session_file("multi_session", user_id=None, messages=[
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答 [1]",
             "sources": [{
                 "id": "a_0", "text": "片段A", "score": 0.9,
                 "document_id": "a", "document_name": "文档A.txt",
                 "kb_id": "kb_export", "chunk_index": 0,
             }]},
            {"role": "user", "content": "第二问"},
            {"role": "assistant", "content": "第二答 [1][2]",
             "sources": [
                 {"id": "b_0", "text": "片段B", "score": 0.9,
                  "document_id": "b", "document_name": "文档B.txt",
                  "kb_id": "kb_export", "chunk_index": 0},
                 {"id": "c_0", "text": "片段C", "score": 0.8,
                  "document_id": "c", "document_name": "文档C.txt",
                  "kb_id": "kb_export", "chunk_index": 1},
             ]},
        ])
        resp = client.get("/api/chat/history/multi_session/export",
                          headers=admin_headers)
        assert resp.status_code == 200
        text = resp.text
        assert text.count("### 引用 1：") == 2, "每条助手消息引用应从 1 编号"
        assert "### 引用 2：文档C.txt" in text
        assert "### 引用 3" not in text


class TestExportPermission:
    """归属校验：owner 或 super_admin，否则 404 伪装"""

    def test_export_not_found(self, client, admin_headers):
        """会话不存在 → 404"""
        resp = client.get("/api/chat/history/nonexist/export",
                          headers=admin_headers)
        assert resp.status_code == 404

    def test_export_non_owner_masquerade_404(self, client, admin_headers,
                                             dept_admin_headers,
                                             user_headers):
        """普通用户会话被部门管理员访问 → 404 伪装（不泄露存在性）"""
        me = client.get("/api/auth/me", headers=user_headers).json()
        _write_session_file("owner_session", user_id=me["id"], title="私有会话")
        resp = client.get("/api/chat/history/owner_session/export",
                          headers=dept_admin_headers)
        assert resp.status_code == 404
        assert "私有会话" not in resp.text

    def test_export_owner_and_super_admin_ok(self, client, admin_headers,
                                             user_headers):
        """本人与 super_admin 均可导出"""
        me = client.get("/api/auth/me", headers=user_headers).json()
        _write_session_file("owner2_session", user_id=me["id"], title="我的会话")
        resp = client.get("/api/chat/history/owner2_session/export",
                          headers=user_headers)
        assert resp.status_code == 200
        assert "我的会话" in resp.text
        resp2 = client.get("/api/chat/history/owner2_session/export",
                           headers=admin_headers)
        assert resp2.status_code == 200

    def test_export_empty_session_template(self, client, admin_headers):
        """空会话（无消息）→ 200，仅返回标题空模板"""
        _write_session_file("empty_session", user_id=None, title="空会话")
        resp = client.get("/api/chat/history/empty_session/export",
                          headers=admin_headers)
        assert resp.status_code == 200
        text = resp.text
        assert text.startswith("# 空会话（kb_id:")
        assert "## 用户" not in text
        assert "## 助手" not in text
        assert text.strip().endswith("时间: 2026-08-10 10:00:00）")

    def test_export_missing_auth_401(self, client):
        """未登录 → 401"""
        resp = client.get("/api/chat/history/any/export")
        assert resp.status_code == 401
