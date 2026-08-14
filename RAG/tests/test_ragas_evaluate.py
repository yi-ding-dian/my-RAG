"""RAGAS 评估发起闭环测试：POST /api/stats/ragas/evaluations

覆盖：
- 采样策略：logs（真实问题去重保最近、answer 留空——RAGAS 样本 answer 非必填）、
  chat（user 问题 + 对应 assistant 回答组装、孤儿问题跳过、kb 过滤、答案截断）
- 发起流程：采样 → 检索填 contexts → 上传数据集 → 创建任务（llm 覆盖活跃配置）
  → 本地元数据落盘 → 响应；空样本 400
- 权限：user 403；dept_admin 跨部门库 404；dept_admin 本部门可发起
- 参数校验：sample_count 超限 / 非法指标 / 非法来源 400
- 任务列表合并本地元数据（kb_name / source / sample_count）

全部离线：ragas_client 用 FakeRagasClient 替换（不经 127.0.0.1:59998），
检索用 mock_embedding（空库/手工日志+会话文件构造采样输入）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from backend.config import DATA_DIR, get_active_config
from backend.services import ragas_sampling
from conftest import create_kb

# 测试样本默认指标（与路由默认一致：需 ground_truth 的 3 个——
# 手动测试集场景用户填了正确答案，默认启用可评标准答案的指标）
DEFAULT_METRICS = ["context_recall", "answer_correctness", "answer_similarity"]


# ==================== 工具：写日志 / 写会话 ====================

def _log_dir():
    return DATA_DIR / "retrieval_logs"


def _write_log(kb_id, query, hit_doc_ids, days_ago=0):
    """手工写一条检索日志（days_ago 天前，构造跨天数据）"""
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


def _write_session(kb_id, session_id, pairs, updated_at="2026-08-08 10:00:00"):
    """手工写一个会话文件（pairs: [(user问题, assistant回答或None), ...]）"""
    messages = []
    for q, a in pairs:
        messages.append({"role": "user", "content": q, "sources": []})
        if a is not None:
            messages.append({"role": "assistant", "content": a, "sources": []})
    path = DATA_DIR / "chat" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": session_id,
        "kb_id": kb_id,
        "user_id": "u1",
        "title": "会话",
        "messages": messages,
        "created_at": "2026-08-08 09:00:00",
        "updated_at": updated_at,
    }, ensure_ascii=False), encoding="utf-8")


class FakeRagasClient:
    """离线伪 ragas_client（记录上传样本/评估配置，返回固定 task id）"""

    def __init__(self):
        self.uploaded_samples = []
        self.datasets = []
        self.evals = []
        self.last_llm_cfg = None

    async def probe(self):
        return {"available": True, "base_url": "http://test", "tasks": [], "message": ""}

    async def upload_dataset(self, samples, name, description=""):
        self.uploaded_samples.append(samples)
        ds_id = f"ds-{len(self.datasets) + 1}"
        self.datasets.append({"id": ds_id, "name": name, "description": description})
        return ds_id

    async def create_evaluation(self, dataset_id, metrics, llm_cfg, name, top_k=3):
        self.last_llm_cfg = llm_cfg
        tid = f"task-{len(self.evals) + 1}"
        self.evals.append({"id": tid, "dataset_id": dataset_id,
                           "metrics": metrics, "name": name, "top_k": top_k})
        return {"id": tid, "name": name, "status": "queued", "dataset_id": dataset_id}

    async def get_report(self, task_id):
        return {"available": True, "report": {}, "message": ""}


@pytest.fixture()
def fake_ragas(monkeypatch):
    """替换 stats 路由的 ragas_client 为离线伪实现，返回其实例"""
    fake = FakeRagasClient()
    monkeypatch.setattr("backend.routers.stats.get_ragas_client", lambda: fake)
    return fake


# ==================== 采样策略 ====================

class TestSampling:

    def test_logs_dedup_recent_answer_empty(self):
        """logs 采样：query 去重保留最近一次，answer 留空（非必填），空白 query 跳过"""
        kb_id = "kb-sampling-logs"
        _write_log(kb_id, "历史问题", ["d1"], days_ago=1)
        _write_log(kb_id, "历史问题", ["d1"], days_ago=1)  # 重复
        _write_log(kb_id, "   ", [], days_ago=0)          # 空白 query 跳过
        _write_log(kb_id, "最新问题", ["d2"], days_ago=0)
        samples = ragas_sampling.sample_from_logs(kb_id, 10)
        questions = [s["question"] for s in samples]
        # 倒序遍历保最近：最新问题(今天) 先于 历史问题(昨天)，去重后各一条
        assert questions == ["最新问题", "历史问题"]
        assert all(s["answer"] == "" for s in samples)
        assert ragas_sampling.sample_from_logs("kb-none", 10) == []

    def test_logs_limit(self):
        """logs 采样：limit 截断"""
        kb_id = "kb-sampling-limit"
        for i in range(5):
            _write_log(kb_id, f"问题{i}", ["d"])
        samples = ragas_sampling.sample_from_logs(kb_id, 2)
        assert len(samples) == 2

    def test_chat_pairs_assembly(self):
        """chat 采样：user 问题 + 对应 assistant 回答；孤儿 user 跳过；kb 过滤；去重"""
        kb_id = "kb-sampling-chat"
        # 会话1：两对完整问答 + 孤儿 user（无回答跳过） + 重复问题
        _write_session(kb_id, "s1", [
            ("今天天气如何？", "今天晴天。"),
            ("重复问题", "回答一"),
            ("重复问题", "回答二"),
            ("孤儿问题", None),
        ], updated_at="2026-08-08 10:00:00")
        # 会话2：其他 kb，不应混入
        _write_session("kb-other", "s2", [("其他库问题", "其他库回答")])
        # 会话3：同 kb 更新更晚，问题相同也应去重（保首次出现即最近会话优先）
        _write_session(kb_id, "s3", [("今天天气如何？", "今天多云。")],
                       updated_at="2026-08-09 10:00:00")
        samples = ragas_sampling.sample_from_chat(kb_id, 10)
        by_q = {s["question"]: s["answer"] for s in samples}
        assert set(by_q) == {"今天天气如何？", "重复问题"}
        assert "孤儿问题" not in by_q
        assert by_q["今天天气如何？"] == "今天多云。"  # 最近会话（s3）优先
        # chat 样本同时写 ground_truth=answer（RAGAS context_precision 需 reference 列）
        assert all(s["ground_truth"] == s["answer"] for s in samples)

    def test_chat_answer_truncated(self):
        """chat 采样：答案截断 MAX_ANSWER_CHARS"""
        kb_id = "kb-sampling-trunc"
        long_answer = "长" * 5000
        _write_session(kb_id, "s4", [("问题", long_answer)])
        samples = ragas_sampling.sample_from_chat(kb_id, 10)
        assert len(samples[0]["answer"]) == ragas_sampling.MAX_ANSWER_CHARS


# ==================== 发起流程 ====================

class TestStartEvaluation:

    def test_start_from_logs_full_flow(self, client, mock_embedding,
                                       admin_headers, fake_ragas):
        """logs 来源全流程：采样去重 → contexts（空库=空列表）→ 上传 → 创建任务
        （llm 覆盖活跃配置）→ 元数据落盘 → 响应"""
        kb = create_kb(client)  # 空库
        _write_log(kb["id"], "Python 是什么？", ["d1"])
        _write_log(kb["id"], "Python 是什么？", ["d1"])  # 重复应去重
        _write_log(kb["id"], "如何部署？", ["d2"])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "sample_count": 20, "sample_source": "logs",
            "top_k": 3,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["task_id"] == "task-1"
        assert body["sample_count"] == 2
        assert body["kb_id"] == kb["id"]
        assert body["kb_name"] == kb["name"]
        assert body["dataset_id"] == "ds-1"

        # 上传样本：question 去重、answer 留空（logs 无回答）、contexts 空库为空列表
        samples = fake_ragas.uploaded_samples[0]
        assert len(samples) == 2
        by_q = {s["question"]: s for s in samples}
        assert set(by_q) == {"Python 是什么？", "如何部署？"}
        assert all(s["answer"] == "" for s in samples)
        assert all(s["contexts"] == [] for s in samples)

        # 创建评估：use_retrieval=false 契约 + llm 覆盖知识库活跃配置
        assert fake_ragas.evals[0]["top_k"] == 3
        assert fake_ragas.evals[0]["metrics"] == DEFAULT_METRICS
        cfg = get_active_config().llm
        assert fake_ragas.last_llm_cfg == {
            "base_url": cfg.base_url, "api_key": cfg.api_key,
            "model": cfg.model, "temperature": cfg.temperature,
            # max_tokens 随活跃配置透传，且评估下限 4096（推理型 judge 预留推理 token）
            "max_tokens": max(cfg.max_tokens, 4096),
        }

        # 本地元数据落盘（任务列表合并 kb 归属用）
        meta = ragas_sampling.load_task_meta()
        assert len(meta) == 1
        assert meta[0]["task_id"] == "task-1"
        assert meta[0]["kb_id"] == kb["id"]
        assert meta[0]["kb_name"] == kb["name"]
        assert meta[0]["source"] == "logs"
        assert meta[0]["sample_count"] == 2

    def test_start_from_chat(self, client, mock_embedding, admin_headers,
                             fake_ragas):
        """chat 来源：问题 + assistant 回答组装为样本"""
        kb = create_kb(client)
        _write_session(kb["id"], "s1", [("问题一", "回答一"), ("问题二", "回答二")])
        _write_session("kb-other", "s2", [("别的库", "别的回答")])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "sample_source": "chat", "sample_count": 10,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        samples = fake_ragas.uploaded_samples[0]
        by_q = {s["question"]: s["answer"] for s in samples}
        assert by_q == {"问题一": "回答一", "问题二": "回答二"}
        # chat 样本携带 ground_truth=answer（RAGAS context_precision 的 reference 列来源）
        assert all(s.get("ground_truth") == s["answer"] for s in samples)
        meta = ragas_sampling.load_task_meta()
        assert meta[0]["source"] == "chat"

    def test_no_samples_400(self, client, mock_embedding, admin_headers,
                            fake_ragas):
        """无检索日志/问答记录 → 400 中文提示（RAGAS 侧不产生任何调用）"""
        kb = create_kb(client)  # 无日志无会话
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "暂无可用样本" in resp.json()["detail"]
        assert fake_ragas.uploaded_samples == []

    def test_metrics_custom_passed_through(self, client, mock_embedding,
                                           admin_headers, fake_ragas):
        """自定义指标透传（含去重保序）"""
        kb = create_kb(client)
        _write_log(kb["id"], "问题", ["d"])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "metrics": ["faithfulness", "faithfulness",
                                           "context_recall", "answer_relevancy"],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake_ragas.evals[0]["metrics"] == [
            "faithfulness", "context_recall", "answer_relevancy"]

    def test_start_with_manual_samples(self, client, mock_embedding,
                                       admin_headers, fake_ragas):
        """手动测试集（samples）：优先于自动采样；answer 缺省=ground_truth；
        source=manual；无需聊天/日志数据"""
        kb = create_kb(client)  # 空库无日志无会话
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
            "samples": [
                {"question": "测试问题一", "ground_truth": "正确答案一"},
                {"question": "测试问题二", "answer": "答案二", "ground_truth": "参考答案二"},
            ],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["sample_count"] == 2
        samples = fake_ragas.uploaded_samples[0]
        assert len(samples) == 2
        assert samples[0]["question"] == "测试问题一"
        assert samples[0]["ground_truth"] == "正确答案一"
        assert samples[0]["answer"] == "正确答案一"  # answer 缺省=ground_truth
        assert samples[1]["answer"] == "答案二"
        # 本地元数据 source=manual（任务列表展示"手动填写"）
        meta = ragas_sampling.load_task_meta()
        assert meta[0]["source"] == "manual"

    def test_manual_samples_missing_ground_truth_400(self, client, mock_embedding,
                                                     admin_headers, fake_ragas):
        """手动测试集缺正确答案（ground_truth）→ 400 指明第几条"""
        kb = create_kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
            "samples": [
                {"question": "有答案", "ground_truth": "答"},
                {"question": "缺答案"},
            ],
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "第 2 条样本缺少正确答案" in resp.json()["detail"]
        assert fake_ragas.uploaded_samples == []

    def test_preview_chat_returns_samples_without_creating_task(
            self, client, mock_embedding, admin_headers, fake_ragas):
        """preview 模式：仅返回采样样本（从聊天历史导入用），不创建任务"""
        kb = create_kb(client)
        _write_session(kb["id"], "s1", [("问题一", "回答一"), ("问题二", "回答二")])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "sample_source": "chat", "sample_count": 10,
            "preview": True,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["samples"]) == 2
        assert body["samples"][0]["question"] == "问题一"
        assert fake_ragas.uploaded_samples == []  # 未上传数据集
        assert fake_ragas.evals == []             # 未创建评估任务


# ==================== 权限 ====================

class TestPermissions:

    def test_user_forbidden_403(self, client, mock_embedding, user_headers):
        """普通 user 发起 → 403（无论库归属）"""
        kb = create_kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
        }, headers=user_headers)
        assert resp.status_code == 403

    def test_dept_admin_cross_department_404(self, client, mock_embedding,
                                             admin_headers, dept_admin_headers,
                                             fake_ragas):
        """dept_admin 发起其他部门/全局库 → 404 伪装（防探测）"""
        kb = create_kb(client, name="全局库")  # admin 建，department_id=None
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
        }, headers=dept_admin_headers)
        assert resp.status_code == 404
        assert fake_ragas.uploaded_samples == []

    def test_dept_admin_own_department_ok(self, client, mock_embedding,
                                          dept_admin_headers, fake_ragas):
        """dept_admin 本部门库可发起"""
        kb = create_kb(client, headers=dept_admin_headers)  # 建库强制本部门
        _write_log(kb["id"], "部门问题", ["d"])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
        }, headers=dept_admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["sample_count"] == 1

    def test_kb_not_found_404(self, client, mock_embedding, admin_headers,
                              fake_ragas):
        """不存在的 kb → 404"""
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": "kb-not-exist",
        }, headers=admin_headers)
        assert resp.status_code == 404
        assert fake_ragas.uploaded_samples == []


# ==================== 参数校验 ====================

class TestParamValidation:

    def _kb(self, client):
        return create_kb(client)

    def test_sample_count_out_of_range_400(self, client, mock_embedding,
                                           admin_headers):
        kb = self._kb(client)
        for bad in (0, 101):
            resp = client.post("/api/stats/ragas/evaluations", json={
                "kb_id": kb["id"], "sample_count": bad,
            }, headers=admin_headers)
            assert resp.status_code == 400, resp.text
            assert "样本数量" in resp.json()["detail"]

    def test_top_k_out_of_range_400(self, client, mock_embedding, admin_headers):
        kb = self._kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "top_k": 0,
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_invalid_metric_400(self, client, mock_embedding, admin_headers):
        kb = self._kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "metrics": ["faithfulness", "fake_metric"],
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "fake_metric" in resp.json()["detail"]

    def test_empty_metrics_400(self, client, mock_embedding, admin_headers):
        kb = self._kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "metrics": [],
        }, headers=admin_headers)
        assert resp.status_code == 400

    def test_invalid_source_400(self, client, mock_embedding, admin_headers):
        kb = self._kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "sample_source": "unknown",
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "样本来源" in resp.json()["detail"]


# ==================== 任务列表元数据合并 ====================

class TestTaskListMetaMerge:

    def test_ragas_list_merges_local_meta(self, client, mock_embedding,
                                          admin_headers, monkeypatch):
        """GET /api/stats/ragas：本地发起的任务合并 kb_name/source/sample_count"""
        ragas_sampling.append_task_meta({
            "task_id": "task-1", "kb_id": "kb-x", "kb_name": "验收知识库",
            "dataset_id": "ds-1", "name": "验收-RAGAS评估",
            "source": "chat", "sample_count": 5,
            "created_at": ragas_sampling.now_iso(),
        })

        class FakeProbe:
            async def probe(self):
                return {
                    "available": True, "base_url": "http://test", "message": "",
                    "tasks": [{
                        "id": "task-1", "name": "验收-RAGAS评估",
                        "dataset_id": "ds-1", "dataset_name": "验收-RAGAS",
                        "status": "completed", "progress": 100, "metrics": [],
                        "created_at": "",
                    }, {
                        # 非本地任务（无元数据）保持原样
                        "id": "task-2", "name": "手动任务", "status": "running",
                        "progress": 30, "metrics": ["faithfulness"],
                        "created_at": "",
                    }],
                }
        monkeypatch.setattr("backend.routers.stats.get_ragas_client",
                            lambda: FakeProbe())
        resp = client.get("/api/stats/ragas", headers=admin_headers)
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        by_id = {t["id"]: t for t in tasks}
        assert by_id["task-1"]["kb_name"] == "验收知识库"
        assert by_id["task-1"]["source"] == "chat"
        assert by_id["task-1"]["sample_count"] == 5
        assert "kb_name" not in by_id["task-2"]
