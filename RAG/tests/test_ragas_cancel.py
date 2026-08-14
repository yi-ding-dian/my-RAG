"""RAGAS 评估任务取消测试：POST /api/stats/ragas/evaluations/{task_id}/cancel

覆盖：
- 权限：发起人本人可取消；他人（其他部门 dept_admin）404 伪装；super_admin 可
  取消任意任务；dept_admin 可取消本部门用户发起的任务；普通 user 403；
  旧任务（元数据无 user_id）仅 super_admin 可取消；任务不在本地元数据 404；
  未登录 401
- cancel_task 调用参数（task_id 透传）；成功后返回 {"message"} 中文
- RAGAS 错误：服务不可达 → 400 中文；已完成任务取消被拒 → 400 透传 RAGAS
  错误详情；RAGAS 侧任务不存在 → 404
- 数据来源：发起评估时元数据记录 user_id；GET /api/stats/ragas 合并 user_id
  （前端取消按钮显隐依据）

全部离线：ragas_client 用 FakeRagasClient 替换（不经 127.0.0.1:59998）。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from backend.config import DATA_DIR
from backend.services import ragas_sampling
from backend.services.ragas_client import RagasApiError
from conftest import create_department_and_admin, create_kb


def _write_log(kb_id, query, hit_doc_ids):
    """手工写一条检索日志（构造发起评估的采样输入）"""
    d = datetime.now()
    path = DATA_DIR / "retrieval_logs" / f"{d.strftime('%Y-%m-%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": d.isoformat(timespec="seconds"), "kb_id": kb_id,
             "query": query, "hit_doc_ids": list(hit_doc_ids)}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _uid(client, headers) -> str:
    """GET /api/auth/me 拿当前登录用户 id（权限测试构造发起人用）"""
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _append_meta(user_id, task_id="task-1"):
    """直接写一条本地任务元数据（user_id=None → 不写该字段，模拟旧任务）"""
    meta = {
        "task_id": task_id, "kb_id": "kb-x", "kb_name": "验收知识库",
        "dataset_id": "ds-1", "name": "验收-RAGAS评估",
        "source": "chat", "sample_count": 5,
        "created_at": ragas_sampling.now_iso(),
    }
    if user_id is not None:
        meta["user_id"] = user_id
    ragas_sampling.append_task_meta(meta)


class FakeRagasClient:
    """离线伪 ragas_client：发起流程 + 取消（记录调用，可配置取消失败）"""

    def __init__(self):
        self.cancel_error = None
        self.cancelled = []
        self.uploaded_samples = []
        self.evals = []

    async def probe(self):
        return {"available": True, "base_url": "http://test", "tasks": [],
                "message": ""}

    async def upload_dataset(self, samples, name, description=""):
        self.uploaded_samples.append(samples)
        return f"ds-{len(self.uploaded_samples)}"

    async def create_evaluation(self, dataset_id, metrics, llm_cfg, name, top_k=3):
        self.evals.append({"id": "task-eval", "dataset_id": dataset_id})
        return {"id": "task-eval", "name": name, "status": "queued",
                "dataset_id": dataset_id}

    async def cancel_task(self, task_id):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled.append(task_id)

    async def get_report(self, task_id):
        return {"available": True, "report": {}, "message": ""}


@pytest.fixture()
def fake_ragas(monkeypatch):
    """替换 stats 路由的 ragas_client 为离线伪实现，返回其实例"""
    fake = FakeRagasClient()
    monkeypatch.setattr("backend.routers.stats.get_ragas_client", lambda: fake)
    return fake


def _cancel(client, headers, task_id="task-1"):
    return client.post(f"/api/stats/ragas/evaluations/{task_id}/cancel",
                       headers=headers)


# ==================== 权限 ====================

class TestCancelPermissions:

    def test_requires_login(self, client):
        """未登录 401"""
        assert _cancel(client, {}).status_code == 401

    def test_owner_cancels_ok(self, client, fake_ragas, dept_admin_headers):
        """发起人本人可取消 → 200 {"message"}，cancel_task 收到 task_id"""
        _append_meta(user_id=_uid(client, dept_admin_headers))
        resp = _cancel(client, dept_admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"message": "评估任务已取消"}
        assert fake_ragas.cancelled == ["task-1"]

    def test_super_admin_cancels_any(self, client, fake_ragas, admin_headers,
                                     dept_admin_headers):
        """super_admin 可取消其他部门管理员发起的任务"""
        _append_meta(user_id=_uid(client, dept_admin_headers))
        resp = _cancel(client, admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake_ragas.cancelled == ["task-1"]

    def test_other_department_admin_404(self, client, fake_ragas, admin_headers,
                                        dept_admin_headers):
        """其他部门 dept_admin 取消 → 404 伪装（且不调用 cancel_task）"""
        _append_meta(user_id=_uid(client, dept_admin_headers))
        _, dept_b_headers = create_department_and_admin(
            client, admin_headers, "其他部门", "dept_admin_b",
            "dept123456", "部门管理员B")
        resp = _cancel(client, dept_b_headers)
        assert resp.status_code == 404
        assert "任务" in resp.json()["detail"]
        assert fake_ragas.cancelled == []

    def test_same_department_admin_ok(self, client, fake_ragas,
                                      dept_admin_headers, user_headers):
        """dept_admin 可取消本部门用户（同部门普通 user）发起的任务"""
        _append_meta(user_id=_uid(client, user_headers))
        resp = _cancel(client, dept_admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake_ragas.cancelled == ["task-1"]

    def test_user_forbidden_403(self, client, fake_ragas, admin_headers,
                                user_headers):
        """普通 user 取消 → 403（与发起评估权限口径一致，不查元数据）"""
        _append_meta(user_id=_uid(client, admin_headers))
        resp = _cancel(client, user_headers)
        assert resp.status_code == 403
        assert "仅管理员" in resp.json()["detail"]
        assert fake_ragas.cancelled == []

    def test_legacy_task_no_user_id_super_admin_only(
            self, client, fake_ragas, admin_headers, dept_admin_headers):
        """旧任务（元数据无 user_id）：dept_admin 404，super_admin 可取消"""
        _append_meta(user_id=None)
        resp = _cancel(client, dept_admin_headers)
        assert resp.status_code == 404
        assert fake_ragas.cancelled == []
        resp = _cancel(client, admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake_ragas.cancelled == ["task-1"]

    def test_task_not_in_meta_404(self, client, fake_ragas, admin_headers):
        """任务不在本地元数据（非本系统发起）→ 404，不调用 cancel_task"""
        resp = _cancel(client, admin_headers, task_id="task-unknown")
        assert resp.status_code == 404
        assert fake_ragas.cancelled == []


# ==================== RAGAS 侧错误 ====================

class TestCancelRagasErrors:

    def _meta_for_admin(self, client, admin_headers):
        _append_meta(user_id=_uid(client, admin_headers))

    def test_ragas_unavailable_400(self, client, fake_ragas, admin_headers):
        """RAGAS 服务不可达 → 400 中文提示（连接失败无状态码）"""
        self._meta_for_admin(client, admin_headers)
        fake_ragas.cancel_error = RagasApiError(
            "无法连接 RAGAS 评估系统（http://localhost:8090）："
            "ConnectError，请确认服务已启动（端口 8090）")
        resp = _cancel(client, admin_headers)
        assert resp.status_code == 400
        assert "无法连接" in resp.json()["detail"]

    def test_completed_task_cancel_error_passed(
            self, client, fake_ragas, admin_headers):
        """RAGAS 侧任务已完成再取消 → RAGAS 错误透传（400 中文提示）"""
        self._meta_for_admin(client, admin_headers)
        fake_ragas.cancel_error = RagasApiError(
            "RAGAS 请求失败（/api/evaluations/task-1/cancel，HTTP 400）："
            "任务已完成无法取消", status_code=400)
        resp = _cancel(client, admin_headers)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "取消评估任务失败" in detail
        assert "任务已完成无法取消" in detail  # RAGAS 错误详情透传

    def test_ragas_task_not_found_404(self, client, fake_ragas, admin_headers):
        """RAGAS 侧任务不存在（404）→ 明确 404 中文提示"""
        self._meta_for_admin(client, admin_headers)
        fake_ragas.cancel_error = RagasApiError(
            "RAGAS 请求失败（/api/evaluations/task-1/cancel，HTTP 404）："
            "任务不存在", status_code=404)
        resp = _cancel(client, admin_headers)
        assert resp.status_code == 404
        assert "任务不存在" in resp.json()["detail"]


# ==================== 数据来源（user_id） ====================

class TestMetaUserIdSource:

    def test_start_records_user_id(self, client, mock_embedding, fake_ragas,
                                   admin_headers):
        """发起评估 → 本地元数据记录发起人 user_id（取消权限校验依据）"""
        kb = create_kb(client)
        _write_log(kb["id"], "Python 是什么？", ["d1"])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        meta = ragas_sampling.load_task_meta()
        assert len(meta) == 1
        assert meta[0]["task_id"] == resp.json()["task_id"]
        assert meta[0]["user_id"] == _uid(client, admin_headers)

    def test_ragas_list_merges_user_id(self, client, fake_ragas, admin_headers,
                                       monkeypatch):
        """GET /api/stats/ragas：本地任务合并 user_id（前端按钮显隐依据）"""
        _append_meta(user_id="u-owner")

        class FakeProbe:
            async def probe(self):
                return {
                    "available": True, "base_url": "http://test", "message": "",
                    "tasks": [{
                        "id": "task-1", "name": "验收-RAGAS评估",
                        "status": "running", "progress": 30, "metrics": [],
                        "created_at": "",
                    }, {
                        # 非本地任务（无元数据）不加 user_id 字段
                        "id": "task-2", "name": "手动任务", "status": "completed",
                        "progress": 100, "metrics": [], "created_at": "",
                    }],
                }
        monkeypatch.setattr("backend.routers.stats.get_ragas_client",
                            lambda: FakeProbe())
        resp = client.get("/api/stats/ragas", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        tasks = {t["id"]: t for t in resp.json()["tasks"]}
        assert tasks["task-1"]["user_id"] == "u-owner"
        assert "user_id" not in tasks["task-2"]

    def test_legacy_task_list_without_user_id(self, client, admin_headers,
                                              monkeypatch):
        """旧任务（无 user_id）：任务列表不返回 user_id 字段（前端不显示取消）"""
        _append_meta(user_id=None)

        class FakeProbe:
            async def probe(self):
                return {
                    "available": True, "base_url": "http://test", "message": "",
                    "tasks": [{
                        "id": "task-1", "name": "旧任务", "status": "running",
                        "progress": 50, "metrics": [], "created_at": "",
                    }],
                }
        monkeypatch.setattr("backend.routers.stats.get_ragas_client",
                            lambda: FakeProbe())
        resp = client.get("/api/stats/ragas", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert "user_id" not in resp.json()["tasks"][0]
