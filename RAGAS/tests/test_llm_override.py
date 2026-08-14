"""评估任务级 LLM 配置覆盖 — 单元测试"""
from __future__ import annotations
import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.config import settings
from backend.models.eval_models import (
    EvalConfig, EvalTask, EvalStatus, EvalMetric, LLMOverrideConfig,
)
from backend.models.data_models import EvalExample
from backend.evaluation import pipeline as pipeline_module
from backend.evaluation.pipeline import build_llm_for_eval
from backend.services import eval_service as eval_service_module
from backend.services.eval_service import EvalService

OVERRIDE = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-super-secret-key",
    "model": "deepseek-v4-flash",
    "temperature": 0.2,
    "timeout": 120,
}

ACTIVE_PROFILE = {
    "llm_base_url": "http://127.0.0.1:8000/v1",
    "llm_model": "Qwen3.5-9B-GPTQ-4bit",
}


def make_dataset():
    return SimpleNamespace(
        name="测试数据集",
        samples=[
            EvalExample(question="问题1", answer="答案1", contexts=["上下文1"]),
            EvalExample(question="问题2", answer="答案2", contexts=["上下文2"]),
        ],
    )


def make_svc(tmp_path, monkeypatch):
    """构造使用临时 reports 目录的新 EvalService 实例（不污染真实报告目录）"""
    monkeypatch.setattr(settings, "REPORTS_DIR", tmp_path)
    return EvalService()


class TestBodyContract:
    """POST /api/evaluations body 契约：llm 覆盖字段"""

    def test_llm_override_parsing(self):
        config = EvalConfig(dataset_id="d1", llm=OVERRIDE)
        assert isinstance(config.llm, LLMOverrideConfig)
        assert config.llm.base_url == "https://api.deepseek.com/v1"
        assert config.llm.api_key == "sk-super-secret-key"
        assert config.llm.model == "deepseek-v4-flash"
        assert config.llm.temperature == 0.2
        assert config.llm.timeout == 120
        assert config.llm.max_tokens is None

    def test_llm_override_optional_fields(self):
        config = EvalConfig(
            dataset_id="d1",
            llm={"base_url": "http://localhost:8000/v1", "model": "m1"},
        )
        assert config.llm.api_key == ""
        assert config.llm.temperature is None
        assert config.llm.timeout is None

    def test_llm_override_absent_backward_compatible(self):
        """不传 llm 字段 → 行为与旧调用一致（llm=None）"""
        config = EvalConfig(dataset_id="d1")
        assert config.llm is None
        assert config.llm_temperature == 0.0
        assert config.llm_max_tokens == 256

    def test_full_body_json_roundtrip(self):
        """模拟前端完整 body JSON，含嵌套 llm 对象"""
        body = {
            "dataset_id": "d1",
            "metrics": ["faithfulness"],
            "use_retrieval": True,
            "retrieval_top_k": 3,
            "llm_temperature": 0.0,
            "llm_max_tokens": 256,
            "name": "带覆盖的评估",
            "llm": OVERRIDE,
        }
        config = EvalConfig.model_validate(body)
        assert config.llm.model == "deepseek-v4-flash"
        assert config.metrics == [EvalMetric.FAITHFULNESS]


class TestCreateTask:
    """create_task：覆盖配置优先，无覆盖回落 active profile"""

    def _setup(self, tmp_path, monkeypatch):
        make_svc(tmp_path, monkeypatch)
        monkeypatch.setattr(
            eval_service_module, "get_data_service",
            lambda: SimpleNamespace(get=lambda did: make_dataset()),
        )
        monkeypatch.setattr(
            "backend.services.settings_service.get_settings_service",
            lambda: SimpleNamespace(get_active=lambda: dict(ACTIVE_PROFILE)),
        )
        started = []
        monkeypatch.setattr(
            EvalService, "_start_task",
            lambda self, task_id, ep_key: started.append((task_id, ep_key)),
        )
        return started

    def test_create_task_uses_override(self, tmp_path, monkeypatch):
        started = self._setup(tmp_path, monkeypatch)
        svc = EvalService()
        config = EvalConfig(dataset_id="d1", metrics=[EvalMetric.FAITHFULNESS], llm=OVERRIDE)
        task = asyncio.run(svc.create_task(config))
        assert task.llm_base_url == "https://api.deepseek.com/v1"
        assert task.llm_model == "deepseek-v4-flash"
        assert len(started) == 1  # 端点空闲，立即启动
        # 磁盘上的 task 文件 api_key 必须脱敏
        raw = (tmp_path / f"task_{task.id}.json").read_text()
        assert "sk-super-secret-key" not in raw
        assert '"***"' in raw
        svc.delete_task(task.id)

    def test_create_task_fallback_to_active(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        svc = EvalService()
        config = EvalConfig(dataset_id="d1", metrics=[EvalMetric.FAITHFULNESS])
        task = asyncio.run(svc.create_task(config))
        assert task.llm_base_url == ACTIVE_PROFILE["llm_base_url"]
        assert task.llm_model == ACTIVE_PROFILE["llm_model"]
        svc.delete_task(task.id)


class TestBuildLlmForEval:
    """build_llm_for_eval：覆盖配置构造 Judge LLM"""

    def _mock_factories(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(
            pipeline_module, "create_qwen_client",
            lambda base_url=None, api_key=None: calls.setdefault("client", (base_url, api_key)),
        )
        monkeypatch.setattr(
            pipeline_module, "llm_factory",
            lambda **kwargs: calls.setdefault("factory", kwargs),
        )
        return calls

    def test_override_passed_to_client_and_factory(self, monkeypatch):
        calls = self._mock_factories(monkeypatch)
        config = EvalConfig(dataset_id="d1", llm_temperature=0.5, llm_max_tokens=512)
        llm, model = build_llm_for_eval(config, LLMOverrideConfig(**OVERRIDE))
        assert calls["client"] == ("https://api.deepseek.com/v1", "sk-super-secret-key")
        factory = calls["factory"]
        assert factory["model"] == "deepseek-v4-flash"
        assert factory["temperature"] == 0.2       # 覆盖温度生效
        assert factory["max_tokens"] == 512       # 任务级 max_tokens 仍生效
        assert factory["timeout"] == 120          # 覆盖超时生效
        assert model == "deepseek-v4-flash"
        assert llm == factory

    def test_override_temperature_falls_back_to_task_level(self, monkeypatch):
        calls = self._mock_factories(monkeypatch)
        config = EvalConfig(dataset_id="d1", llm_temperature=0.5)
        build_llm_for_eval(config, LLMOverrideConfig(base_url="http://x/v1", model="m1"))
        assert calls["factory"]["temperature"] == 0.5
        assert calls["factory"]["timeout"] == 300  # 缺省 300

    def test_override_max_tokens_priority(self, monkeypatch):
        """覆盖 max_tokens 优先于任务级 llm_max_tokens（防 judge 输出截断）"""
        calls = self._mock_factories(monkeypatch)
        config = EvalConfig(dataset_id="d1", llm_max_tokens=256)
        build_llm_for_eval(
            config,
            LLMOverrideConfig(base_url="http://x/v1", model="m1", max_tokens=2048),
        )
        assert calls["factory"]["max_tokens"] == 2048

    def test_override_max_tokens_falls_back_to_task_level(self, monkeypatch):
        """覆盖未传 max_tokens → 用任务级 llm_max_tokens（向后兼容）"""
        calls = self._mock_factories(monkeypatch)
        config = EvalConfig(dataset_id="d1", llm_max_tokens=512)
        build_llm_for_eval(config, LLMOverrideConfig(base_url="http://x/v1", model="m1"))
        assert calls["factory"]["max_tokens"] == 512

    def test_deepseek_disables_thinking(self, monkeypatch):
        """DeepSeek 推理模型：关闭思考（extra_body），max_tokens 全部用于输出"""
        calls = self._mock_factories(monkeypatch)
        config = EvalConfig(dataset_id="d1", llm_max_tokens=256)
        build_llm_for_eval(
            config,
            LLMOverrideConfig(base_url="http://x/v1", model="deepseek-v4-flash",
                              max_tokens=2048),
        )
        assert calls["factory"]["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_non_deepseek_no_thinking_override(self, monkeypatch):
        """非 DeepSeek 模型（Qwen 等）：不传 extra_body，避免端点拒绝未知字段"""
        calls = self._mock_factories(monkeypatch)
        config = EvalConfig(dataset_id="d1", llm_max_tokens=256)
        build_llm_for_eval(
            config,
            LLMOverrideConfig(base_url="http://x/v1", model="Qwen3.5-9B-GPTQ-4bit"),
        )
        assert "extra_body" not in calls["factory"]

    def test_no_override_uses_settings(self, monkeypatch):
        """不传覆盖 → 工厂无参调用（内部回落全局 settings，profiles active 兜底）"""
        calls = self._mock_factories(monkeypatch)
        config = EvalConfig(dataset_id="d1", llm_temperature=0.0)
        build_llm_for_eval(config, None)
        # 无参调用：base_url/api_key 为 None，由 llm_config 工厂回落 settings 默认值
        assert calls["client"] == (None, None)
        assert calls["factory"]["model"] == settings.LLM_MODEL
        assert calls["factory"]["timeout"] == 300


class TestFailureAndSecurity:
    """LLM 初始化失败 → 任务 failed；api_key 脱敏"""

    def test_eval_run_fails_with_clear_error(self, tmp_path, monkeypatch):
        make_svc(tmp_path, monkeypatch)
        monkeypatch.setattr(
            eval_service_module, "get_data_service",
            lambda: SimpleNamespace(get=lambda did: make_dataset()),
        )
        monkeypatch.setattr(
            "backend.services.prompt_service.get_prompt_service",
            lambda: SimpleNamespace(
                apply_prompts_for_eval=lambda metrics: None,
                get_active_language=lambda: "zh",
            ),
        )

        def boom(config, llm_override=None, log_callback=None):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(pipeline_module, "build_llm_for_eval", boom)

        svc = EvalService()
        config = EvalConfig(dataset_id="d1", metrics=[EvalMetric.FAITHFULNESS], llm=OVERRIDE)
        task = EvalTask(
            id="t-fail", name="失败任务", dataset_id="d1", dataset_name="测试数据集",
            status=EvalStatus.QUEUED, config=config,
            llm_base_url=config.llm.base_url, llm_model=config.llm.model,
        )
        svc._tasks[task.id] = task
        asyncio.run(svc._run_eval(task.id))
        assert task.status == EvalStatus.FAILED
        assert task.error and "LLM 初始化失败" in task.error
        assert task.error and "connection refused" in task.error
        # 任务日志中的错误信息
        logs = svc.get_logs(task.id)
        assert any("LLM 初始化失败" in l["message"] for l in logs)

    def test_save_task_masks_api_key_on_disk(self, tmp_path, monkeypatch):
        svc = make_svc(tmp_path, monkeypatch)
        config = EvalConfig(dataset_id="d1", llm=OVERRIDE)
        task = EvalTask(
            id="t-sec", name="安全任务", dataset_id="d1", dataset_name="测试数据集",
            status=EvalStatus.QUEUED, config=config,
            llm_base_url=config.llm.base_url, llm_model=config.llm.model,
        )
        svc._save_task(task)
        raw = (tmp_path / "task_t-sec.json").read_text()
        assert "sk-super-secret-key" not in raw
        data = json.loads(raw)
        assert data["config"]["llm"]["api_key"] == "***"
        # 内存中的任务仍保留真实 api_key（供线程内评估使用）
        assert task.config.llm.api_key == "sk-super-secret-key"
