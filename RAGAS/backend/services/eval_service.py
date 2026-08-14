"""评估任务编排服务 — 支持多 LLM 端点并行调度"""
from __future__ import annotations
import json
import uuid
import logging
import threading
from datetime import datetime
from typing import Optional, List
from pathlib import Path

from backend.config import settings
from backend.models.eval_models import (
    EvalConfig, EvalTask, EvalResults, EvalStatus,
    EvalTaskListItem, EvalResult, AggregateScores,
)
from backend.models.data_models import EvalExample
from backend.services.data_service import get_data_service
from backend.evaluation.pipeline import EvalPipeline, EvalCancelledError
from backend.services.retrieval_service import get_retrieval_service

logger = logging.getLogger(__name__)


class EvalService:

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, EvalTask] = {}
        self._results: dict[str, EvalResults] = {}
        self._logs: dict[str, list] = {}
        self._cancel_flags: dict[str, bool] = {}

        # 调度器状态
        self._task_queue: list[str] = []                   # 排队中的 task_id（有序）
        self._running_threads: dict[str, threading.Thread] = {}  # task_id -> 线程
        self._active_endpoints: dict[str, str] = {}        # "base_url|model" -> task_id

        self._load_all()

    def _get_ep_key(self, task: EvalTask) -> str:
        """生成端点标识，用于判断是否使用同一个 LLM"""
        return f"{task.llm_base_url}|{task.llm_model}"

    def _start_task(self, task_id: str, ep_key: str):
        """启动任务（调用方须持有 _lock）"""
        task = self._tasks[task_id]
        task.status = EvalStatus.RUNNING
        task.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_task(task)
        self._active_endpoints[ep_key] = task_id
        t = threading.Thread(target=self._thread_run, args=(task_id,), daemon=True)
        t.start()
        self._running_threads[task_id] = t

    def _on_task_done(self, task_id: str):
        """任务线程结束后的清理 + 调度下一个排队任务"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                ep_key = self._get_ep_key(task)
                self._active_endpoints.pop(ep_key, None)
            self._running_threads.pop(task_id, None)

            # 检查同端点的排队任务，启动第一个
            for qid in list(self._task_queue):
                qt = self._tasks.get(qid)
                if qt and self._get_ep_key(qt) == ep_key and ep_key not in self._active_endpoints:
                    self._task_queue.remove(qid)
                    self._start_task(qid, ep_key)
                    break

    def _get_task_path(self, task_id: str) -> Path:
        return settings.REPORTS_DIR / f"task_{task_id}.json"

    def _get_result_path(self, task_id: str) -> Path:
        return settings.REPORTS_DIR / f"result_{task_id}.json"

    def _get_log_path(self, task_id: str) -> Path:
        return settings.REPORTS_DIR / f"log_{task_id}.json"

    def _load_all(self):
        """从磁盘恢复所有任务和结果"""
        if not settings.REPORTS_DIR.exists():
            return
        for f in settings.REPORTS_DIR.glob("task_*.json"):
            try:
                data = json.loads(f.read_text())
                task = EvalTask(**data)
                # 服务器重启后，之前的 running/pending/queued 任务已无实际线程，标记为失败
                if task.status in (EvalStatus.RUNNING, EvalStatus.PENDING, EvalStatus.QUEUED):
                    task.status = EvalStatus.FAILED
                    task.error = "服务器重启，任务已终止"
                    task.message = "服务器重启，任务已终止"
                    self._get_task_path(task.id).write_text(
                        json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2)
                    )
                self._tasks[task.id] = task
            except Exception as e:
                logger.warning("加载任务 %s 失败: %s", f.name, e)
        for f in settings.REPORTS_DIR.glob("result_*.json"):
            try:
                data = json.loads(f.read_text())
                results = EvalResults(**data)
                self._results[results.task_id] = results
            except Exception as e:
                logger.warning("加载结果 %s 失败: %s", f.name, e)
        for f in settings.REPORTS_DIR.glob("log_*.json"):
            try:
                data = json.loads(f.read_text())
                self._logs[data["task_id"]] = data["logs"]
            except Exception as e:
                logger.warning("加载日志 %s 失败: %s", f.name, e)

    def _save_task(self, task: EvalTask):
        data = task.model_dump(mode="json")
        # 安全：任务级 LLM 覆盖中的 api_key 仅内存使用，写盘时脱敏，不落日志
        llm_cfg = (data.get("config") or {}).get("llm")
        if llm_cfg and llm_cfg.get("api_key"):
            llm_cfg["api_key"] = "***"
        self._get_task_path(task.id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2)
        )

    def _save_results(self, task_id: str, results: EvalResults):
        self._get_result_path(task_id).write_text(
            json.dumps(results.model_dump(mode="json"), ensure_ascii=False, indent=2)
        )

    def _save_logs(self, task_id: str):
        logs = self._logs.get(task_id, [])
        self._get_log_path(task_id).write_text(
            json.dumps({"task_id": task_id, "logs": logs}, ensure_ascii=False, indent=2)
        )

    def _add_log(self, task_id: str, level: str, message: str):
        if task_id not in self._logs:
            self._logs[task_id] = []
        self._logs[task_id].append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        })
        self._save_logs(task_id)

    def get_logs(self, task_id: str, since: int = 0) -> list:
        return (self._logs.get(task_id) or [])[since:]

    async def create_task(self, config: EvalConfig) -> EvalTask:
        """创建并启动/排队评估任务"""
        # 检查数据集
        data_svc = get_data_service()
        dataset = data_svc.get(config.dataset_id)
        if not dataset:
            raise ValueError(f"数据集不存在: {config.dataset_id}")

        # 任务级 LLM 覆盖优先；否则捕获当前活跃配置（profiles.json）的 LLM 端点
        if config.llm is not None:
            llm_base_url = config.llm.base_url
            llm_model = config.llm.model
        else:
            try:
                from backend.services.settings_service import get_settings_service
                active = get_settings_service().get_active()
                llm_base_url = (active or {}).get("llm_base_url", settings.LLM_BASE_URL)
                llm_model = (active or {}).get("llm_model", settings.LLM_MODEL)
            except Exception:
                llm_base_url = settings.LLM_BASE_URL
                llm_model = settings.LLM_MODEL

        task_id = str(uuid.uuid4())[:8]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        task = EvalTask(
            id=task_id,
            name=config.name or f"评估_{dataset.name}_{now[:10]}",
            dataset_id=config.dataset_id,
            dataset_name=dataset.name,
            status=EvalStatus.QUEUED,
            config=config,
            progress=0,
            message="等待调度中",
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            created_at=now,
        )
        self._tasks[task_id] = task
        self._save_task(task)

        ep_key = f"{llm_base_url}|{llm_model}"
        with self._lock:
            if ep_key in self._active_endpoints:
                # 同端点正在运行 → 排队
                self._task_queue.append(task_id)
                self._add_log(task_id, "info",
                    f"LLM 端点忙 ({llm_model}@{llm_base_url})，任务已排队")
                self._save_task(task)
                logger.info("任务 %s 已排队 (端点 %s)", task_id, ep_key)
            else:
                # 端点空闲 → 立即执行
                self._start_task(task_id, ep_key)
                logger.info("任务 %s 启动 (端点 %s)", task_id, ep_key)

        return task

    def cancel_task(self, task_id: str) -> bool:
        """取消评估任务（运行中或排队中）"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status == EvalStatus.QUEUED:
                # 排队中的任务：从队列移除 + 标记失败
                if task_id in self._task_queue:
                    self._task_queue.remove(task_id)
                task.message = "用户取消"
                self._save_task(task)
                self._fail_task(task_id, "用户取消")
                self._add_log(task_id, "warn", "用户取消排队中的评估任务")
                return True
            if task.status != EvalStatus.RUNNING:
                return False
            self._cancel_flags[task_id] = True
            task.message = "正在停止..."
        self._add_log(task_id, "warn", "用户请求取消评估...")
        self._save_task(task)
        return True

    def _is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return self._cancel_flags.get(task_id, False)

    def _check_cancel(self, task_id: str) -> bool:
        """检查是否被取消, 如果是则标记失败并返回 True"""
        if self._is_cancelled(task_id):
            self._add_log(task_id, "warn", "评估已被用户取消")
            self._fail_task(task_id, "用户取消")
            return True
        return False

    def _thread_run(self, task_id: str):
        """在独立线程中运行评估（自带独立事件循环）"""
        import asyncio
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                new_loop.run_until_complete(self._run_eval(task_id))
            finally:
                new_loop.close()
        except Exception as e:
            logger.exception("后台评估任务异常: task=%s", task_id)
            self._fail_task(task_id, str(e))
        finally:
            self._on_task_done(task_id)

    async def _run_eval(self, task_id: str):
        """后台执行评估"""
        task = self._tasks.get(task_id)
        if not task:
            return

        data_svc = get_data_service()
        dataset = data_svc.get(task.dataset_id)
        if not dataset:
            self._fail_task(task_id, "数据集已不存在")
            return

        _last_save_progress = -1

        def update_progress(progress: int, message: str):
            nonlocal _last_save_progress
            with self._lock:
                task.progress = progress
                task.message = message
            # 节流：进度变化 >= 5% 或关键节点才写盘
            if progress - _last_save_progress >= 5 or progress in (0, 100):
                _last_save_progress = progress
                self._save_task(task)

        def log(level: str, msg: str):
            self._add_log(task_id, level, msg)

        def update_eta(seconds: int | None):
            with self._lock:
                task.eta_seconds = seconds
            self._save_task(task)

        pipeline = EvalPipeline(
            progress_callback=update_progress,
            log_callback=lambda msg: log("info", msg),
            eta_callback=update_eta,
        )

        try:
            samples = dataset.samples
            total = len(samples)
            metrics = [m.value for m in task.config.metrics]
            log("info", f"数据集加载完成: {dataset.name} ({total} 条)")
            log("info", f"评估指标: {', '.join(metrics)}")

            # 记录开始时间
            task.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_task(task)

            # 检查取消
            if self._check_cancel(task_id):
                return

            # 如果启用真实检索链路，替换 contexts
            if task.config.use_retrieval:
                update_progress(10, "正在通过知识库检索真实上下文...")
                ret_svc = get_retrieval_service()
                if not ret_svc.is_available():
                    log("warn", "检索服务不可用，使用原始 contexts")
                else:
                    log("info", f"开始检索知识库 ({total} 条)...")
                    for i, sample in enumerate(samples):
                        if self._check_cancel(task_id):
                            return
                        results = ret_svc.search(
                            query=sample.question,
                            top_k=task.config.retrieval_top_k,
                        )
                        sample.contexts = [r["text"] for r in results]
                        log("info", f"  已检索 {i + 1}/{total}: {sample.question[:40]}")
                        if (i + 1) % 10 == 0:
                            update_progress(
                                10 + int(20 * (i + 1) / total),
                                f"已检索 {i + 1}/{total} 条"
                            )
                    log("info", "检索完成")

            # 检查取消
            if self._check_cancel(task_id):
                return

            # 应用用户选择的提示词语言（由 PromptService 管理）
            try:
                from backend.services.prompt_service import get_prompt_service
                prompt_svc = get_prompt_service()
                prompt_svc.apply_prompts_for_eval(task.config.metrics)
                lang = prompt_svc.get_active_language()
                log("info", f"已应用提示词语言: {'中文' if lang == 'zh' else 'English'}")
            except Exception as e:
                log("warn", f"提示词应用失败，使用默认英文: {e}")

            # 执行评估
            config = task.config
            config.llm_temperature = config.llm_temperature or 0.0
            config.llm_max_tokens = config.llm_max_tokens or 256

            log("info", f"开始评估，共 {total} 条样本 × {len(metrics)} 个指标 = {total * len(metrics)} 轮 LLM 调用")
            log("info", f"模型: {task.llm_model} @ {task.llm_base_url}")
            log("info", f"参数: temperature={config.llm_temperature}, max_tokens={config.llm_max_tokens}")

            eval_results = pipeline.run(
                config, samples,
                log_callback=lambda msg: log("info", msg),
                cancel_check=lambda: self._is_cancelled(task_id),
                llm_override=config.llm,
            )

            eval_results.task_id = task_id
            eval_results.task_name = task.name
            eval_results.created_at = task.created_at
            eval_results.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 保存结果
            self._results[task_id] = eval_results
            self._save_results(task_id, eval_results)

            task.status = EvalStatus.COMPLETED
            task.progress = 100
            task.message = "评估完成"
            task.completed_at = eval_results.completed_at
            self._save_task(task)

            log("info", f"评估完成！各项评分: {eval_results.aggregate.scores}")
            logger.info("评估完成: task=%s metrics=%s", task_id, eval_results.aggregate.scores)

        except EvalCancelledError:
            log("warn", "评估已被用户取消")
            self._add_log(task_id, "warn", "评估已取消")
            self._fail_task(task_id, "用户取消")
        except Exception as e:
            logger.exception("评估失败: task=%s", task_id)
            log("error", f"评估异常: {e}")
            self._fail_task(task_id, str(e))

    def _fail_task(self, task_id: str, error: str):
        task = self._tasks.get(task_id)
        if task:
            task.status = EvalStatus.FAILED
            task.error = error
            task.message = f"失败: {error}"
            self._save_task(task)

    def list_tasks(self) -> list[EvalTaskListItem]:
        return [
            EvalTaskListItem(
                id=t.id,
                name=t.name,
                dataset_id=t.dataset_id,
                dataset_name=t.dataset_name,
                status=t.status,
                progress=t.progress,
                metrics=[m.value for m in t.config.metrics],
                created_at=t.created_at,
                completed_at=t.completed_at,
                error=t.error,
            )
            for t in sorted(self._tasks.values(), key=lambda x: x.created_at, reverse=True)
        ]

    def get_task(self, task_id: str) -> Optional[EvalTask]:
        return self._tasks.get(task_id)

    def get_results(self, task_id: str) -> Optional[EvalResults]:
        return self._results.get(task_id)

    def delete_task(self, task_id: str) -> bool:
        """删除评估任务及其文件（包括排队中的任务）"""
        if task_id not in self._tasks:
            return False
        # 不能删除正在运行的任务
        if self._tasks[task_id].status == EvalStatus.RUNNING:
            return False
        # 如果任务在排队队列中，移除
        with self._lock:
            if task_id in self._task_queue:
                self._task_queue.remove(task_id)
        self._tasks.pop(task_id, None)
        self._results.pop(task_id, None)
        self._logs.pop(task_id, None)
        self._cancel_flags.pop(task_id, None)
        for path in [self._get_task_path(task_id), self._get_result_path(task_id), self._get_log_path(task_id)]:
            if path.exists():
                path.unlink()
        return True


_eval_service = EvalService()


def get_eval_service() -> EvalService:
    return _eval_service
