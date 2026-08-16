"""RAGAS 评估审计埋点 + 超管文档操作审计覆盖 + 审计按天删除测试

覆盖：
- 发起 RAGAS 评估成功 → 审计落库 ragas.evaluate（target_type/kb_name/sample_count/
  source/metrics/top_k/task_id，preview 模式不落）
- /api/audit/actions 中文标签含 ragas.evaluate（AUDIT_ACTION_LABELS 同源）
- 超管重命名/软删文档走现有端点 → doc.rename/doc.delete 埋点已覆盖
  （admin_documents.py 仅有列表接口无写操作，超管复用部门内端点，天然被审计）
- DELETE /api/audit/logs?date=YYYY-MM-DD：按天删除审计记录（created_at 前缀
  LIKE 匹配，sqlite 下验证）；非法日期 400；非超管 403

全部离线：ragas_client 用 FakeRagasClient 替换；检索用 conftest mock_embedding。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from conftest import create_kb, upload_doc


class FakeRagasClient:
    """离线伪 ragas_client（记录上传/评估调用，返回固定 task id）"""

    def __init__(self):
        self.uploaded = []
        self.evals = []

    async def probe(self):
        return {"available": True, "base_url": "http://test", "tasks": [], "message": ""}

    async def upload_dataset(self, samples, name, description=""):
        self.uploaded.append(samples)
        return f"ds-{len(self.uploaded)}"

    async def create_evaluation(self, dataset_id, metrics, llm_cfg, name, top_k=3):
        self.evals.append({"dataset_id": dataset_id, "metrics": metrics, "top_k": top_k})
        return {"id": "task-1", "name": name, "status": "queued", "dataset_id": dataset_id}

    async def get_report(self, task_id):
        return {"available": True, "report": {}, "message": ""}


@pytest.fixture()
def fake_ragas(monkeypatch):
    """替换 stats 路由的 ragas_client 为离线伪实现"""
    fake = FakeRagasClient()
    monkeypatch.setattr("backend.routers.stats.get_ragas_client", lambda: fake)
    return fake


def _audit_items(client, headers, **params):
    resp = client.get("/api/audit/logs", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _write_retrieval_log(kb_id, query, hit_doc_ids):
    """手工写一条今日检索日志（preview 采样数据源）"""
    from backend.config import DATA_DIR
    now = datetime.now()
    path = DATA_DIR / "retrieval_logs" / f"{now.strftime('%Y-%m-%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": now.isoformat(timespec="seconds"), "kb_id": kb_id,
             "query": query, "hit_doc_ids": list(hit_doc_ids)}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class TestRagasAudit:

    def test_evaluate_recorded(self, client, mock_embedding, admin_headers,
                               fake_ragas):
        """手动测试集发起评估成功 → 审计落库 ragas.evaluate（detail 完整）"""
        kb = create_kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
            "samples": [{"question": "测试问题", "ground_truth": "参考答案"}],
            "metrics": ["faithfulness"],
            "top_k": 2,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["task_id"] == "task-1"

        items = _audit_items(client, admin_headers, action="ragas.evaluate")
        assert len(items) == 1
        item = items[0]
        assert item["username"] == "admin"
        assert item["target_type"] == "kb"
        assert item["target_id"] == kb["id"]
        assert item["target_name"] == kb["name"]
        assert item["status"] == "success"

        detail = json.loads(item["detail"])
        assert detail["task_id"] == "task-1"
        assert detail["kb_name"] == kb["name"]
        assert detail["sample_count"] == 1
        assert detail["source"] == "manual"
        assert detail["metrics"] == ["faithfulness"]
        assert detail["top_k"] == 2

    def test_preview_not_recorded(self, client, mock_embedding, admin_headers,
                                  fake_ragas):
        """preview 模式（仅采样不发起评估）不落审计记录"""
        kb = create_kb(client)
        _write_retrieval_log(kb["id"], "测试问题", ["d1"])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "preview": True, "sample_count": 5,
            "sample_source": "logs",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert "samples" in resp.json()
        assert _audit_items(client, admin_headers, action="ragas.evaluate") == []

    def test_actions_labels_include_ragas(self, client, admin_headers):
        """AUDIT_ACTION_LABELS 新增项：/api/audit/actions 含中文标签"""
        resp = client.get("/api/audit/actions", headers=admin_headers)
        mapping = {a["action"]: a["label"] for a in resp.json()["actions"]}
        assert mapping["ragas.evaluate"] == "RAGAS 评估发起"


class TestSuperAdminDocAudit:
    """超管文档操作（admin_documents 页的入口）走现有端点，埋点已覆盖"""

    def test_rename_and_delete_recorded(self, client, admin_headers):
        """超管重命名/软删 → doc.rename/doc.delete 审计记录"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])

        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/rename",
            json={"name": "新名字.txt"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text

        resp = client.delete(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}", headers=admin_headers)
        assert resp.status_code == 200, resp.text

        items = _audit_items(client, admin_headers)
        actions = [i["action"] for i in items]
        assert "doc.rename" in actions
        assert "doc.delete" in actions

        rename = next(i for i in items if i["action"] == "doc.rename")
        assert rename["username"] == "admin"
        assert rename["target_name"] == "新名字.txt"
        assert json.loads(rename["detail"]) == {"old_name": "测试文档.txt"}

        dele = next(i for i in items if i["action"] == "doc.delete")
        assert dele["target_name"] == "新名字.txt"


class TestAuditDeleteByDate:
    """DELETE /api/audit/logs?date=YYYY-MM-DD（仅超管，按天删除审计记录）"""

    def test_delete_by_date(self, client, admin_headers):
        """删除今天全部审计记录；删除后查询为空"""
        kb = create_kb(client)
        upload_doc(client, kb["id"])  # 产生 doc.upload 审计
        assert _audit_items(client, admin_headers)

        today = datetime.now().strftime("%Y-%m-%d")
        resp = client.delete("/api/audit/logs", params={"date": today},
                             headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] >= 1
        # 今天的记录已清空
        assert _audit_items(client, admin_headers) == []

    def test_delete_empty_date_ok(self, client, admin_headers):
        """无该天记录 → 删除 0 条，不报错"""
        resp = client.delete("/api/audit/logs",
                             params={"date": "2099-01-01"}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0

    def test_invalid_date_400(self, client, admin_headers):
        resp = client.delete("/api/audit/logs",
                             params={"date": "2026/01/01"}, headers=admin_headers)
        assert resp.status_code == 400

    def test_non_super_admin_forbidden(self, client, dept_admin_headers,
                                       user_headers):
        for headers in (dept_admin_headers, user_headers):
            resp = client.delete("/api/audit/logs",
                                 params={"date": "2026-01-01"}, headers=headers)
            assert resp.status_code == 403
