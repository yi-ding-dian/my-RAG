"""RAGAS 手动测试集发起闭环测试：POST /api/stats/ragas/evaluations（samples/preview）

覆盖：
- samples 优先：传 samples 时不调用采样逻辑（monkeypatch 断言）、忽略
  sample_count/sample_source 参数
- 校验：samples 空列表 / 超 100 条 / 缺 question / 缺 ground_truth / 空白 400
- samples 链路：组装 answer=ground_truth（显式 answer 优先）→ 检索填 contexts
  （空库空列表 / 入库文档命中非空）→ 上传/创建调用参数 → 元数据 source=manual
- 默认指标：手动测试集有正确答案，默认取需 ground_truth 的 3 个
- preview 模式：仅采样返回样本列表，不发起评估（chat/logs 来源）；不校验
  metrics；sample_count 超限 400；无样本 400
- 兼容：不传 samples 仍走自动采样（旧调用不受影响）

全部离线：ragas_client 用 FakeRagasClient 替换（不经 127.0.0.1:59998），
检索用 mock_embedding（空库/手工日志+会话文件/入库文档构造输入）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from backend.config import DATA_DIR, get_active_config
from backend.services import ragas_sampling
from conftest import create_kb, upload_and_ingest

# 手动测试集场景的默认指标（与路由默认一致：需 ground_truth 的 3 个）
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


# ==================== 手动测试集（samples 模式） ====================

class TestManualSamples:

    def test_samples_priority_skips_sampling(self, client, mock_embedding,
                                             admin_headers, fake_ragas,
                                             monkeypatch):
        """samples 优先：传 samples 时不调用采样逻辑；采样参数被忽略

        无日志无会话（空库）也能发起——证明未走自动采样路径；
        同时传 sample_count=0（自动采样模式会 400）也成功——证明参数被忽略。
        """
        def _boom(*args, **kwargs):
            raise AssertionError("samples 模式不应调用采样逻辑")
        monkeypatch.setattr("backend.routers.stats.ragas_sampling.sample_from_logs",
                            _boom)
        monkeypatch.setattr("backend.routers.stats.ragas_sampling.sample_from_chat",
                            _boom)
        kb = create_kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
            "samples": [
                {"question": "问题一", "ground_truth": "答案一"},
                {"question": "问题二", "ground_truth": "答案二"},
            ],
            "sample_count": 0, "sample_source": "chat",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["sample_count"] == 2
        assert len(fake_ragas.uploaded_samples[0]) == 2

    def test_assembly_answer_defaults_to_ground_truth(self, client,
                                                      mock_embedding,
                                                      admin_headers,
                                                      fake_ragas):
        """组装：未传 answer 时 answer=ground_truth（用户只填一次答案，
        它既是 answer 也是 ground_truth）；显式传 answer 时优先"""
        kb = create_kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
            "samples": [
                {"question": "问题一", "ground_truth": "正确答案"},
                {"question": "问题二", "ground_truth": "参考答案", "answer": "显式答案"},
            ],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        by_q = {s["question"]: s for s in fake_ragas.uploaded_samples[0]}
        assert by_q["问题一"]["answer"] == "正确答案"       # 缺省 → ground_truth
        assert by_q["问题一"]["ground_truth"] == "正确答案"
        assert by_q["问题二"]["answer"] == "显式答案"       # 显式优先
        assert by_q["问题二"]["ground_truth"] == "参考答案"

    def test_validation_400(self, client, mock_embedding, admin_headers,
                            fake_ragas):
        """校验：samples 空列表/超 100 条/缺 question/缺 ground_truth 均 400"""
        kb = create_kb(client)
        cases = [
            ({"samples": []}, "样本数量"),
            ({"samples": [{"question": "q", "ground_truth": "a"}] * 101},
             "样本数量"),
            ({"samples": [{"question": "  ", "ground_truth": "a"}]}, "缺少测试问题"),
            ({"samples": [{"question": "q"}]}, "缺少正确答案"),
            ({"samples": [{"question": "q1", "ground_truth": "a"},
                          {"question": "q2"}]}, "第 2 条"),
        ]
        for extra, keyword in cases:
            resp = client.post("/api/stats/ragas/evaluations", json={
                "kb_id": kb["id"], **extra,
            }, headers=admin_headers)
            assert resp.status_code == 400, extra
            assert keyword in resp.json()["detail"]
        # 校验失败均未发起：无上传无任务
        assert fake_ragas.uploaded_samples == []
        assert fake_ragas.evals == []

    def test_contexts_filled_from_retrieval(self, client, mock_embedding,
                                            admin_headers, fake_ragas):
        """链路：入库文档 → 问题检索命中 → contexts 非空（含文档文本）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])  # SAMPLE_TEXT（Python 简介）
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "top_k": 3,
            "samples": [{"question": "Python 是一种高级编程语言",
                         "ground_truth": "编程语言"}],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        s = fake_ragas.uploaded_samples[0][0]
        assert s["contexts"], "检索应命中入库文档"
        assert any("Python" in c for c in s["contexts"])

    def test_contexts_empty_on_empty_kb(self, client, mock_embedding,
                                        admin_headers, fake_ragas):
        """空库：contexts 为空列表（不跳过样本，忠实度等指标仍可评）"""
        kb = create_kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
            "samples": [{"question": "问题", "ground_truth": "答案"}],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        s = fake_ragas.uploaded_samples[0][0]
        assert s["contexts"] == []
        assert s["question"] == "问题"
        assert s["ground_truth"] == "答案"

    def test_create_args_and_meta_manual(self, client, mock_embedding,
                                         admin_headers, fake_ragas):
        """创建调用：metrics/top_k 透传 + llm 覆盖活跃配置 + 元数据 source=manual"""
        kb = create_kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "top_k": 5,
            "metrics": ["faithfulness", "context_recall"],
            "samples": [{"question": "q", "ground_truth": "a"}],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake_ragas.evals[0]["top_k"] == 5
        assert fake_ragas.evals[0]["metrics"] == ["faithfulness", "context_recall"]
        cfg = get_active_config().llm
        assert fake_ragas.last_llm_cfg == {
            "base_url": cfg.base_url, "api_key": cfg.api_key,
            "model": cfg.model, "temperature": cfg.temperature,
            # max_tokens 随活跃配置透传，且评估下限 4096（推理型 judge 预留推理 token）
            "max_tokens": max(cfg.max_tokens, 4096),
        }
        meta = ragas_sampling.load_task_meta()
        assert meta[0]["source"] == "manual"
        assert meta[0]["sample_count"] == 1
        assert meta[0]["kb_name"] == kb["name"]

    def test_default_metrics_are_ground_truth_metrics(self, client,
                                                      mock_embedding,
                                                      admin_headers,
                                                      fake_ragas):
        """默认指标：需 ground_truth 的 3 个（手动测试集有用户填的正确答案）"""
        kb = create_kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"],
            "samples": [{"question": "q", "ground_truth": "a"}],
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert fake_ragas.evals[0]["metrics"] == DEFAULT_METRICS


# ==================== preview 模式（从聊天历史导入用） ====================

class TestPreviewMode:

    def test_chat_preview_returns_samples_without_starting(self, client,
                                                           mock_embedding,
                                                           admin_headers,
                                                           fake_ragas):
        """chat 来源预览：返回样本列表（question/answer/ground_truth），不发起"""
        kb = create_kb(client)
        _write_session(kb["id"], "s1", [("问题一", "回答一"), ("问题二", "回答二")])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "sample_source": "chat", "sample_count": 10,
            "preview": True,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        by_q = {s["question"]: s for s in resp.json()["samples"]}
        assert set(by_q) == {"问题一", "问题二"}
        assert by_q["问题一"]["answer"] == "回答一"
        assert by_q["问题一"]["ground_truth"] == "回答一"
        # 不发起：无上传无任务
        assert fake_ragas.uploaded_samples == []
        assert fake_ragas.evals == []

    def test_logs_preview(self, client, mock_embedding, admin_headers,
                          fake_ragas):
        """logs 来源预览：question + answer 留空"""
        kb = create_kb(client)
        _write_log(kb["id"], "真实问题", ["d1"])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "sample_source": "logs", "preview": True,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["samples"] == [{"question": "真实问题", "answer": ""}]
        assert fake_ragas.uploaded_samples == []

    def test_preview_ignores_metrics(self, client, mock_embedding,
                                     admin_headers, fake_ragas):
        """预览模式不校验 metrics（非法指标也能预览，无需选指标）"""
        kb = create_kb(client)
        _write_log(kb["id"], "问题", ["d"])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "metrics": ["not_a_metric"], "preview": True,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["samples"]) == 1

    def test_preview_no_samples_400(self, client, mock_embedding,
                                    admin_headers, fake_ragas):
        """预览模式无可用样本 → 400（不发起）"""
        kb = create_kb(client)  # 无日志无会话
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "preview": True,
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "暂无可用样本" in resp.json()["detail"]
        assert fake_ragas.uploaded_samples == []

    def test_preview_sample_count_validation(self, client, mock_embedding,
                                             admin_headers):
        """预览模式 sample_count 超限 → 400"""
        kb = create_kb(client)
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "sample_count": 0, "preview": True,
        }, headers=admin_headers)
        assert resp.status_code == 400
        assert "样本数量" in resp.json()["detail"]


# ==================== 兼容：旧调用不受影响 ====================

class TestCompatibility:

    def test_without_samples_uses_auto_sampling(self, client, mock_embedding,
                                                admin_headers, fake_ragas):
        """不传 samples → 走自动采样（logs），元数据 source=logs（原逻辑）"""
        kb = create_kb(client)
        _write_log(kb["id"], "旧调用问题", ["d"])
        resp = client.post("/api/stats/ragas/evaluations", json={
            "kb_id": kb["id"], "sample_source": "logs", "sample_count": 10,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        by_q = {s["question"] for s in fake_ragas.uploaded_samples[0]}
        assert by_q == {"旧调用问题"}
        meta = ragas_sampling.load_task_meta()
        assert meta[0]["source"] == "logs"
