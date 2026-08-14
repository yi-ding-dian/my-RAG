"""基础单元测试"""
from __future__ import annotations
import pytest
from datetime import datetime
from backend.models.eval_models import EvalMetric, EvalTask, EvalConfig, EvalStatus, EvalResults, AggregateScores, EvalResult


class TestEvalModels:
    """评估模型基础测试"""

    def test_eval_metric_values(self):
        assert EvalMetric.FAITHFULNESS.value == "faithfulness"
        assert EvalMetric.ANSWER_RELEVANCY.value == "answer_relevancy"

    def test_eval_status_transitions(self):
        assert EvalStatus.PENDING.value == "pending"
        assert EvalStatus.RUNNING.value == "running"
        assert EvalStatus.COMPLETED.value == "completed"
        assert EvalStatus.FAILED.value == "failed"

    def test_eval_config_defaults(self):
        config = EvalConfig(dataset_id="test-id")
        assert config.dataset_id == "test-id"
        assert config.use_retrieval is False
        assert config.llm_temperature == 0.0
        assert config.llm_max_tokens == 256

    def test_eval_task_creation(self):
        config = EvalConfig(dataset_id="test-id", metrics=[EvalMetric.FAITHFULNESS])
        task = EvalTask(
            id="test-1",
            name="测试任务",
            dataset_id="test-id",
            dataset_name="测试数据集",
            status=EvalStatus.RUNNING,
            config=config,
            progress=50,
            message="运行中",
        )
        assert task.id == "test-1"
        assert task.status == EvalStatus.RUNNING
        assert task.progress == 50
        assert task.eta_seconds is None

    def test_eval_results_aggregation(self):
        results = EvalResults(
            task_id="test-1",
            task_name="测试",
            status=EvalStatus.COMPLETED,
            aggregate=AggregateScores(
                scores={"faithfulness": 0.85, "answer_relevancy": 0.72},
                count=2,
            ),
            results=[
                EvalResult(
                    question="测试问题",
                    answer="测试答案",
                    contexts=["上下文1"],
                    scores={"faithfulness": 0.85, "answer_relevancy": 0.72},
                    errors={},
                )
            ],
        )
        assert results.aggregate.scores["faithfulness"] == 0.85
        assert len(results.results) == 1


class TestPipelineProgress:
    """进度计算逻辑测试"""

    def test_progress_mapping(self):
        """验证 10→90% 逐指标推进"""
        num_metrics = 3
        for idx in range(num_metrics):
            done = idx + 1
            progress = 10 + int(80 * done / num_metrics)
            assert 10 <= progress <= 90

    def test_single_metric_progress(self):
        done = 1
        total = 1
        progress = 10 + int(80 * done / total)
        assert progress == 90

    def test_four_metrics_progress(self):
        total = 4
        expected = [30, 50, 70, 90]
        for idx, exp in enumerate(expected):
            done = idx + 1
            progress = 10 + int(80 * done / total)
            assert progress == exp


class TestEtaCalculation:
    """ETA 预测逻辑测试"""

    def test_eta_after_first_metric(self):
        metric_times = [120.0]
        avg = sum(metric_times) / len(metric_times)
        remaining = 3 - len(metric_times)
        eta = int(avg * remaining)
        assert eta == 240

    def test_eta_multiple_metrics(self):
        metric_times = [100.0, 110.0]
        avg = sum(metric_times) / len(metric_times)
        remaining = 5 - len(metric_times)
        eta = int(avg * remaining)
        assert eta == 315
