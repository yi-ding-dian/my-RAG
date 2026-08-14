"""检索质量统计 API 测试：GET /api/stats/quality

覆盖：检索触发日志落盘（含 query 截断、无命中空数组）、汇总正确性
（总数/平均命中/文档排行/零命中文档/日粒度 hit_rate）、权限（dept_admin
仅本部门库，404 伪装）、无数据返回空数组、日志 30 天过期清理。
全部离线（mock embedding）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from conftest import create_kb, upload_and_ingest


def _log_dir():
    from backend.config import DATA_DIR
    return DATA_DIR / "retrieval_logs"


def _write_log_entry(kb_id, hit_doc_ids, query="问题", days_ago=0):
    """手工写一条日志（days_ago 天前），用于构造多天汇总数据"""
    d = datetime.now() - timedelta(days=days_ago)
    path = _log_dir() / f"{d.strftime('%Y-%m-%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": d.isoformat(timespec="seconds"),
        "kb_id": kb_id,
        "query": query,
        "hit_doc_ids": list(hit_doc_ids),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class TestLogWrite:
    """检索日志落盘"""

    def test_retrieve_writes_log(self, client, mock_embedding, admin_headers):
        """检索测试接口触发日志：含 kb_id/query/hit_doc_ids"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "Python 是什么语言？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and resp.json()["sources"]
        files = list(_log_dir().glob("*.jsonl"))
        assert files, "检索后应生成当日日志文件"
        entries = [json.loads(line) for f in files
                   for line in f.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        kb_entries = [e for e in entries if e["kb_id"] == kb["id"]]
        assert kb_entries, "日志应包含该 kb 条目"
        e = kb_entries[0]
        assert e["ts"] and e["hit_doc_ids"] == [doc["id"]]
        assert e["query"] == "Python 是什么语言？"

    def test_no_hit_also_logged_with_empty_ids(self, client, mock_embedding,
                                               admin_headers):
        """无命中的检索也记录（hit_doc_ids=[]，支撑日粒度命中率统计）"""
        kb = create_kb(client)  # 空库：必然无命中
        resp = client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": "任何问题",
        }, headers=admin_headers)
        assert resp.status_code == 200 and resp.json()["sources"] == []
        entries = [json.loads(line) for f in _log_dir().glob("*.jsonl")
                   for line in f.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        kb_entries = [e for e in entries if e["kb_id"] == kb["id"]]
        assert kb_entries and kb_entries[0]["hit_doc_ids"] == []

    def test_query_truncated_to_100(self, client, mock_embedding, admin_headers):
        """query 落盘截断前 100 字"""
        kb = create_kb(client)
        long_q = "很" * 200
        client.post("/api/chat/retrieve", json={
            "kb_id": kb["id"], "query": long_q,
        }, headers=admin_headers)
        entries = [json.loads(line) for f in _log_dir().glob("*.jsonl")
                   for line in f.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        assert any(e["kb_id"] == kb["id"] and len(e["query"]) == 100
                   for e in entries)


class TestQualitySummary:
    """汇总正确性（构造日志 + 真实 ingested 文档）"""

    def _setup_kb(self, client):
        """建库并入库 3 个文档，返回 (kb_id, [docA, docB, docC])"""
        kb = create_kb(client)
        doc_a = upload_and_ingest(client, kb["id"], filename="文档A.txt")
        doc_b = upload_and_ingest(client, kb["id"], filename="文档B.txt")
        doc_c = upload_and_ingest(client, kb["id"], filename="文档C.txt")
        return kb["id"], [doc_a, doc_b, doc_c]

    def test_summary_fields(self, client, mock_embedding, admin_headers):
        """总数/平均命中/文档排行/零命中文档/日粒度全部正确"""
        kb_id, (doc_a, doc_b, doc_c) = self._setup_kb(client)
        _write_log_entry(kb_id, [doc_a["id"], doc_b["id"]], "今天问题1", 0)
        _write_log_entry(kb_id, [doc_a["id"]], "今天问题2", 0)
        _write_log_entry(kb_id, [doc_b["id"]], "昨天问题", 1)
        _write_log_entry(kb_id, [], "前天问题", 2)  # 无命中
        resp = client.get(f"/api/stats/quality?kb_id={kb_id}",
                          headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["kb_id"] == kb_id
        assert data["window_days"] == 30
        assert data["total_retrievals"] == 4
        assert data["avg_hits_per_retrieval"] == 1.0  # (2+1+1+0)/4
        # 文档命中排行（降序）
        hits = {h["doc_id"]: h["hits"] for h in data["hit_docs"]}
        assert hits == {doc_a["id"]: 2, doc_b["id"]: 2}
        for h in data["hit_docs"]:
            assert h["doc_name"] in ("文档A.txt", "文档B.txt")
        assert len(data["hit_docs"]) == 2
        # 零命中文档：仅 C 从未命中
        assert [z["doc_id"] for z in data["zero_hit_docs"]] == [doc_c["id"]]
        assert data["zero_hit_docs"][0]["doc_name"] == "文档C.txt"
        assert data["zero_hit_docs"][0]["chunks"] >= 1
        # 日粒度：近 30 天，命中率按日计算
        daily = {d["date"]: d for d in data["daily"]}
        assert len(data["daily"]) == 30
        today = datetime.now().date().isoformat()
        yest = (datetime.now() - timedelta(days=1)).date().isoformat()
        d2 = (datetime.now() - timedelta(days=2)).date().isoformat()
        assert daily[today]["retrievals"] == 2
        assert daily[today]["hit_rate"] == 1.0
        assert daily[yest]["retrievals"] == 1 and daily[yest]["hit_rate"] == 1.0
        assert daily[d2]["retrievals"] == 1 and daily[d2]["hit_rate"] == 0.0

    def test_hit_docs_top10_limit(self, client, mock_embedding, admin_headers):
        """命中排行仅返回 top10"""
        kb = create_kb(client)
        docs = []
        for i in range(12):
            docs.append(upload_and_ingest(client, kb["id"],
                                          filename=f"文档{i}.txt"))
        _write_log_entry(kb["id"], [d["id"] for d in docs], "一次全命中", 0)
        resp = client.get(f"/api/stats/quality?kb_id={kb['id']}",
                          headers=admin_headers)
        data = resp.json()
        assert len(data["hit_docs"]) == 10
        assert data["hit_docs"][0]["hits"] == 1

    def test_zero_hit_excludes_non_ingested(self, client, mock_embedding,
                                            admin_headers):
        """零命中文档只统计 ingested 状态（未入库的 uploaded 不列出）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], filename="已入库.txt")
        # 只上传不 ingest
        from conftest import upload_doc
        upload_doc(client, kb["id"], filename="未入库.txt")
        _write_log_entry(kb["id"], [], "问题", 0)
        resp = client.get(f"/api/stats/quality?kb_id={kb['id']}",
                          headers=admin_headers)
        data = resp.json()
        names = [z["doc_name"] for z in data["zero_hit_docs"]]
        assert names == ["已入库.txt"], "未 ingested 的文档不应计入零命中列表"


class TestQualityNoData:
    """无检索数据：返回空数组不报错"""

    def test_no_data(self, client, mock_embedding, admin_headers):
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        resp = client.get(f"/api/stats/quality?kb_id={kb['id']}",
                          headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_retrievals"] == 0
        assert data["avg_hits_per_retrieval"] == 0
        assert data["hit_docs"] == []
        assert len(data["zero_hit_docs"]) == 1  # 文档从未命中 → 零命中列表
        assert len(data["daily"]) == 30
        assert all(d["retrievals"] == 0 and d["hit_rate"] == 0
                   for d in data["daily"])

    def test_unknown_kb_404(self, client, admin_headers):
        resp = client.get("/api/stats/quality?kb_id=nonexist",
                          headers=admin_headers)
        assert resp.status_code == 404

    def test_missing_kb_param_422(self, client, admin_headers):
        resp = client.get("/api/stats/quality", headers=admin_headers)
        assert resp.status_code == 422


class TestQualityPermission:
    """权限：登录即可；dept_admin 仅本部门库（404 伪装）"""

    def _find_dept_id(self, client, admin_headers, dept_name="测试部门"):
        depts = client.get("/api/departments", headers=admin_headers).json()
        return next(d["id"] for d in depts if d["name"] == dept_name)

    def test_dept_admin_cannot_access_global_kb(self, client, admin_headers,
                                                dept_admin_headers):
        """dept_admin 访问全局库（无部门）→ 404"""
        kb = create_kb(client)  # 默认全局库
        resp = client.get(f"/api/stats/quality?kb_id={kb['id']}",
                          headers=dept_admin_headers)
        assert resp.status_code == 404

    def test_dept_admin_own_department_ok(self, client, admin_headers,
                                          dept_admin_headers):
        """dept_admin 访问本部门库 → 200；user 同部门也 200"""
        dept_id = self._find_dept_id(client, admin_headers)
        kb = create_kb(client, department_id=dept_id)
        resp = client.get(f"/api/stats/quality?kb_id={kb['id']}",
                          headers=dept_admin_headers)
        assert resp.status_code == 200

    def test_super_admin_access_any(self, client, admin_headers,
                                    dept_admin_headers):
        """super_admin 可访问任意库"""
        dept_id = self._find_dept_id(client, admin_headers)
        kb = create_kb(client, department_id=dept_id)
        resp = client.get(f"/api/stats/quality?kb_id={kb['id']}",
                          headers=admin_headers)
        assert resp.status_code == 200


class TestLogRetention:
    """日志 30 天轮转清理（写入时清理过期文件）"""

    def test_expired_files_cleaned_on_write(self, client, admin_headers):
        from backend.services.retrieval_log import get_retrieval_log_service
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        old_date = (datetime.now() - timedelta(days=32)).strftime("%Y-%m-%d")
        old_path = log_dir / f"{old_date}.jsonl"
        old_path.write_text('{"ts":"2026-01-01T00:00:00","kb_id":"x"}\n',
                            encoding="utf-8")
        recent_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        recent_path = log_dir / f"{recent_date}.jsonl"
        recent_path.write_text('{"ts":"2026-01-01T00:00:00","kb_id":"x"}\n',
                               encoding="utf-8")
        # 触发写入（内部先清理过期文件）
        get_retrieval_log_service().log("kb_x", "问题", [])
        assert not old_path.exists(), "超过 30 天的日志文件应被清理"
        assert recent_path.exists(), "窗口期内的日志文件应保留"

    def test_unparseable_filename_kept(self, client, admin_headers):
        """命名不合规的文件不做日期判断，直接保留"""
        from backend.services.retrieval_log import get_retrieval_log_service
        weird = _log_dir() / "bad_file.jsonl"
        weird.write_text("x\n", encoding="utf-8")
        get_retrieval_log_service().log("kb_y", "问题", [])
        assert weird.exists()
