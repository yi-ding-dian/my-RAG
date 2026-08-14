"""多知识库对比检索测试：POST /api/chat/retrieve 的 kb_ids 支持

覆盖：多库合并按 score 降序取全局 top_k、Source 附带 kb_id/kb_name、
权限（任一库不可访问 → 404 伪装）、kb_ids 优先级（与 kb_id 都传时
kb_ids 优先）、参数校验、单库兼容（既有行为不破坏）。全部离线
（mock embedding）。
"""
from __future__ import annotations

from conftest import create_kb, upload_and_ingest


class TestMultiKB:
    """多库对比检索"""

    def _kb_with_docs(self, client, name):
        """建库并入库默认文档（多库内容相同，均可命中 Python 问题）"""
        kb = create_kb(client, name=name)
        upload_and_ingest(client, kb["id"])
        return kb

    def _retrieve(self, client, headers, body):
        resp = client.post("/api/chat/retrieve", json=body, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["sources"]

    def test_multi_kb_merged_desc(self, client, mock_embedding, admin_headers):
        """两库合并：Source 带 kb_id/kb_name，按 score 降序，数量不超 top_k"""
        kb_a = self._kb_with_docs(client, "知识库A")
        kb_b = self._kb_with_docs(client, "知识库B")
        sources = self._retrieve(client, admin_headers, {
            "kb_ids": [kb_a["id"], kb_b["id"]],
            "query": "Python 是什么语言？",
        })
        assert sources, "多库检索应至少命中一条"
        # 每个 Source 都带正确的库归属
        name_of = {kb_a["id"]: "知识库A", kb_b["id"]: "知识库B"}
        for s in sources:
            assert s["kb_id"] in (kb_a["id"], kb_b["id"])
            assert s["kb_name"] == name_of[s["kb_id"]], \
                "kb_name 应与 kb_id 对应"
        # 按 score 降序
        scores = [s["score"] for s in sources]
        assert scores == sorted(scores, reverse=True)

    def test_global_top_k_cap(self, client, mock_embedding, admin_headers):
        """每库独立候选后按全局 top_k 截断"""
        kb_a = self._kb_with_docs(client, "知识库A")
        kb_b = self._kb_with_docs(client, "知识库B")
        sources = self._retrieve(client, admin_headers, {
            "kb_ids": [kb_a["id"], kb_b["id"]],
            "query": "Python 是什么语言？", "top_k": 1,
        })
        assert len(sources) == 1, "合并后应截取全局 top_k=1"
        sources5 = self._retrieve(client, admin_headers, {
            "kb_ids": [kb_a["id"], kb_b["id"]],
            "query": "Python 是什么语言？", "top_k": 5,
        })
        assert len(sources5) <= 5 and len(sources5) >= 2

    def test_kb_ids_priority_over_kb_id(self, client, mock_embedding,
                                        admin_headers):
        """kb_ids 与 kb_id 都传时 kb_ids 优先"""
        kb_a = self._kb_with_docs(client, "知识库A")
        empty_kb = create_kb(client, name="空库")  # kb_id 指向空库
        sources = self._retrieve(client, admin_headers, {
            "kb_ids": [kb_a["id"]],
            "kb_id": empty_kb["id"],
            "query": "Python 是什么语言？",
        })
        assert sources, "kb_ids 优先：结果应来自知识库A"
        assert all(s["kb_id"] == kb_a["id"] for s in sources)

    def test_single_kb_compatible(self, client, mock_embedding, admin_headers):
        """只传 kb_id（既有契约）：行为不变，附带 kb_name"""
        kb = self._kb_with_docs(client, "知识库A")
        sources = self._retrieve(client, admin_headers, {
            "kb_id": kb["id"], "query": "Python 是什么语言？",
        })
        assert sources
        assert all(s["kb_id"] == kb["id"] and s["kb_name"] == "知识库A"
                   for s in sources)

    def test_kb_ids_only_no_kb_id(self, client, mock_embedding, admin_headers):
        """只传 kb_ids 不传 kb_id（kb_id 可选后）也可检索"""
        kb = self._kb_with_docs(client, "知识库A")
        sources = self._retrieve(client, admin_headers, {
            "kb_ids": [kb["id"]], "query": "Python 是什么语言？",
        })
        assert sources and sources[0]["kb_id"] == kb["id"]

    def test_any_kb_unaccessible_404(self, client, mock_embedding,
                                     admin_headers, user_headers):
        """任一库不可访问 → 404 伪装"""
        depts = client.get("/api/departments", headers=admin_headers).json()
        dept1 = next(d["id"] for d in depts if d["name"] == "测试部门")
        # 第二个部门
        resp = client.post("/api/departments",
                           json={"name": "另一部门", "description": "d"},
                           headers=admin_headers)
        dept2 = resp.json()["id"]
        kb_own = create_kb(client, department_id=dept1)
        upload_and_ingest(client, kb_own["id"])
        kb_other = create_kb(client, department_id=dept2)
        resp = client.post("/api/chat/retrieve", json={
            "kb_ids": [kb_own["id"], kb_other["id"]],
            "query": "Python 是什么语言？",
        }, headers=user_headers)
        assert resp.status_code == 404, "任一库不可访问应 404 伪装"

    def test_unknown_kb_404(self, client, admin_headers):
        """kb_ids 含不存在的库 → 404"""
        resp = client.post("/api/chat/retrieve", json={
            "kb_ids": ["nonexist"], "query": "x",
        }, headers=admin_headers)
        assert resp.status_code == 404


class TestMultiKBValidation:
    """参数校验"""

    def test_no_kb_400(self, client, admin_headers):
        """kb_id 与 kb_ids 都不传 → 400"""
        resp = client.post("/api/chat/retrieve", json={
            "query": "x",
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_empty_kb_ids_400(self, client, admin_headers):
        resp = client.post("/api/chat/retrieve", json={
            "kb_ids": [], "query": "x",
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_too_many_kb_ids_400(self, client, mock_embedding, admin_headers):
        """超过 5 个知识库 → 400"""
        kbs = [create_kb(client, name=f"库{i}") for i in range(6)]
        resp = client.post("/api/chat/retrieve", json={
            "kb_ids": [k["id"] for k in kbs], "query": "x",
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_empty_query_400(self, client, admin_headers):
        kb = create_kb(client)
        resp = client.post("/api/chat/retrieve", json={
            "kb_ids": [kb["id"]], "query": "   ",
        }, headers=admin_headers)
        assert resp.status_code == 400
