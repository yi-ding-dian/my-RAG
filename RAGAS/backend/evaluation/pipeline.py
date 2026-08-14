"""RAGAS 评估管道 — 核心执行逻辑"""
from __future__ import annotations
import math
import logging
from typing import List, Optional, Callable
from datasets import Dataset

from ragas import evaluate as ragas_evaluate
from ragas.llms import llm_factory
from ragas.run_config import RunConfig

from backend.config import settings
from backend.evaluation.llm_config import create_qwen_client, create_qwen_embeddings
from backend.evaluation.metrics import resolve_metrics
from backend.models.eval_models import (
    EvalMetric, EvalConfig, LLMOverrideConfig, EvalResult, EvalResults, AggregateScores, EvalStatus,
)
from backend.models.data_models import EvalExample

logger = logging.getLogger(__name__)


class EvalCancelledError(Exception):
    """评估被用户取消"""
    pass


def build_llm_for_eval(
    config: EvalConfig,
    llm_override: Optional[LLMOverrideConfig] = None,
    log_callback: Optional[Callable[[str], None]] = None,
):
    """构造 Judge LLM。

    - 传入 llm_override（任务级覆盖配置）时，用覆盖的 base_url/api_key/model 构造客户端
    - 否则回落全局 settings（profiles.json 活跃配置，现状兜底）
    - log_callback 提供时包装客户端记录 LLM 调用日志
    返回 (ragas_llm, 实际使用的 model 名)。

    max_tokens 优先级：llm_override.max_tokens > 任务级 llm_max_tokens
    （LLMOverrideConfig 契约"缺省用任务级 llm_max_tokens"——覆盖值如 2048
    必须生效，否则 judge 输出被 256 截断判"不完整"导致指标失败）。

    DeepSeek 推理模型（deepseek-*）默认思考模式：reasoning 会消耗 max_tokens
    （2048 全部被 reasoning 占满时输出为空，instructor 报 "output is incomplete"）。
    评估 judge 场景统一关闭思考（extra_body: thinking.disabled），
    max_tokens 全部用于评分输出，且响应更快。
    """
    if llm_override is not None:
        model = llm_override.model
        client = create_qwen_client(
            base_url=llm_override.base_url,
            api_key=llm_override.api_key,
        )
        temperature = (
            llm_override.temperature
            if llm_override.temperature is not None
            else config.llm_temperature
        )
        timeout = llm_override.timeout or 300
        max_tokens = (
            llm_override.max_tokens
            if llm_override.max_tokens is not None
            else config.llm_max_tokens
        )
    else:
        model = settings.LLM_MODEL
        client = create_qwen_client()
        temperature = config.llm_temperature
        timeout = 300
        max_tokens = config.llm_max_tokens
    if log_callback:
        client = _wrap_llm_client(client, log_callback)
    llm_kwargs = dict(
        model=model,
        client=client,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    if "deepseek" in (model or "").lower():
        # DeepSeek 推理模型关闭思考（OpenAI 兼容 extra_body 字段）
        llm_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    llm = llm_factory(**llm_kwargs)
    return llm, model


# 需要 embedding 模型的指标
METRICS_NEED_EMBEDDINGS = {
    EvalMetric.ANSWER_RELEVANCY,
    EvalMetric.ANSWER_SIMILARITY,
    EvalMetric.ANSWER_CORRECTNESS,  # 内部依赖 answer_similarity，需要 embedding
}


def _wrap_llm_client(client, log_fn):
    """包装 OpenAI 客户端，记录每次 LLM 调用请求和回复"""
    if getattr(client, '_ragas_logged', False):
        return client
    original_create = client.chat.completions.create

    def logged_create(*args, **kwargs):
        messages = kwargs.get("messages", [])
        for m in messages:
            role = m.get("role", "?")
            content = str(m.get("content", ""))
            # 只记录最后一条 user 消息的关键内容，截断避免日志过长
            if role == "user" and len(content) > 50:
                preview = content[:300] + ("..." if len(content) > 300 else "")
                log_fn(f"[LLM] user: {preview}")
            elif role == "system":
                preview = content[:200] + ("..." if len(content) > 200 else "")
                log_fn(f"[LLM] system: {preview}")
        result = original_create(*args, **kwargs)
        if result.choices:
            resp = result.choices[0].message.content or ""
            log_fn(f"[LLM 回复] {resp[:500]}{'...' if len(resp) > 500 else ''}")
        return result

    client.chat.completions.create = logged_create
    client._ragas_logged = True
    return client


class EvalPipeline:
    """评估管道"""

    def __init__(
        self,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        eta_callback: Optional[Callable[[Optional[int]], None]] = None,
    ):
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.eta_callback = eta_callback

    def _report(self, progress: int, message: str):
        logger.info("[%d%%] %s", progress, message)
        if self.progress_callback:
            self.progress_callback(progress, message)

    def _report_eta(self, seconds: Optional[int]):
        if self.eta_callback:
            self.eta_callback(seconds)

    def _log(self, message: str):
        if self.log_callback:
            self.log_callback(message)

    def run(
        self,
        config: EvalConfig,
        samples: List[EvalExample],
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        eta_callback: Optional[Callable[[Optional[int]], None]] = None,
        llm_override: Optional[LLMOverrideConfig] = None,
    ) -> EvalResults:
        if log_callback:
            self.log_callback = log_callback
        if eta_callback:
            self.eta_callback = eta_callback

        import time
        _metric_times: list[float] = []

        def _update_eta():
            if len(_metric_times) < 1:
                return
            avg = sum(_metric_times) / len(_metric_times)
            remaining = len(active_metrics) - len(_metric_times)
            eta = int(avg * remaining)
            self._report_eta(eta)

        self._report(2, "准备数据集...")
        self._log("准备数据集...")

        # 检查哪些指标需要 embedding
        need_embed = [m for m in config.metrics if m in METRICS_NEED_EMBEDDINGS]
        embed_available = False
        if need_embed:
            self._log("检查 Embedding 模型可用性...")
            try:
                emb = create_qwen_embeddings()
                emb.embed_query("test")
                embed_available = True
                self._report(4, "Embedding 模型可用")
                self._log("Embedding 模型连接成功")
            except Exception as e:
                logger.warning("Embedding 不可用，将跳过需要 embedding 的指标: %s", e)
                self._log(f"Embedding 不可用: {e}，跳过相关指标")
                embed_available = False

        # 过滤不可用的指标
        active_metrics = list(config.metrics)
        skipped = []
        if not embed_available:
            for m in need_embed:
                active_metrics.remove(m)
                skipped.append(m)
                logger.warning("跳过指标 %s (需要 embedding 但不可用)", m.value)

        if not active_metrics:
            raise ValueError("所有选中的指标均不可用（需要 embedding 但 Qwen 不支持）")

        if skipped:
            msg = f"跳过需 embedding 的指标: {[s.value for s in skipped]}"
            self._report(5, msg)
            self._log(msg)

        # 构建 HF Dataset
        self._log("构建数据集...")
        data = {
            "question": [s.question for s in samples],
            "answer": [s.answer for s in samples],
            "contexts": [s.contexts for s in samples],
            # 无条件提供 ground_truth 列（RAGAS v1→v2 映射为 reference 列，
            # context_precision 等指标必需；无标准答案的样本留空串，
            # 该样本该指标记 nan 不参与聚合，但不影响指标整体出分）
            "ground_truth": [s.ground_truth or "" for s in samples],
        }

        dataset = Dataset.from_dict(data)
        self._report(6, f"数据集加载完成，共 {len(samples)} 条样本")
        self._log(f"数据集构建完成: {len(samples)} 条样本")

        # 验证指标可解析
        if not resolve_metrics(active_metrics):
            raise ValueError("没有有效的评估指标")
        metric_names_str = ", ".join(m.value for m in active_metrics)
        self._report(7, f"指标已加载: {metric_names_str}")
        self._log(f"指标: {metric_names_str}")

        # 配置 LLM（支持任务级覆盖配置，缺省用 profiles.json 活跃配置）
        if llm_override is not None:
            llm_base_url = llm_override.base_url
            llm_model = llm_override.model
        else:
            llm_base_url = settings.LLM_BASE_URL
            llm_model = settings.LLM_MODEL
        self._report(8, "正在连接评估模型...")
        self._log(f"连接 LLM: {llm_model} @ {llm_base_url}")
        try:
            llm, llm_model = build_llm_for_eval(config, llm_override, self.log_callback)
            self._report(10, "LLM 连接成功，开始评估...")
            self._log("LLM 连接成功")
        except Exception as e:
            logger.error("LLM 配置失败: %s", e)
            detail = f"LLM 初始化失败（请检查 base_url/api_key/model 是否有效）: {e}"
            self._log(detail)
            raise RuntimeError(detail) from e

        # 准备 embeddings（如果可用）
        embeddings = create_qwen_embeddings() if embed_available else None

        # 逐指标执行 RAGAS 评估（支持取消）
        total_calls = len(samples) * len(active_metrics)
        self._log(f"开始 RAGAS 评估: {len(samples)} 条 × {len(active_metrics)} 个指标 = {total_calls} 轮 LLM 调用")

        # 初始化每样本的分数和错误容器
        sample_scores: list[dict[str, Optional[float]]] = [{} for _ in range(len(samples))]
        sample_errors: list[dict[str, Optional[str]]] = [{} for _ in range(len(samples))]

        for idx, metric in enumerate(active_metrics):
            # 取消检查（每执行一个指标前检查一次）
            if cancel_check and cancel_check():
                raise EvalCancelledError("用户取消评估")

            self._log(f"评估指标 ({idx + 1}/{len(active_metrics)}): {metric.value}")

            _t0 = time.time()
            single_metric = resolve_metrics([metric])
            try:
                result = ragas_evaluate(
                    dataset=dataset,
                    metrics=single_metric,
                    llm=llm,
                    embeddings=embeddings,
                    raise_exceptions=True,
                    run_config=RunConfig(timeout=300, max_retries=2, max_workers=config.llm_max_workers),
                    batch_size=min(len(samples), config.batch_size),
                )
            except Exception as e:
                logger.error("指标 %s 评估失败: %s", metric.value, e)
                self._log(f"指标 {metric.value} 评估失败: {e}，跳过此指标")
                # 将此指标的所有样本分数标记为 None
                for i in range(len(samples)):
                    sample_scores[i][metric.value] = None
                    sample_errors[i][metric.value] = f"评估失败: {e}"
                _metric_times.append(time.time() - _t0)
                done = idx + 1
                progress = 10 + int(80 * done / len(active_metrics))
                _update_eta()
                self._report(progress, f"指标 {metric.value} 评估失败，已跳过 ({done}/{len(active_metrics)})")
                continue

            _metric_times.append(time.time() - _t0)

            df = result.to_pandas()
            col = metric.value
            for i in range(min(len(samples), len(df))):
                val = df.iloc[i].get(col) if col in df.columns else None
                if val is not None:
                    try:
                        v = float(val)
                        if math.isnan(v):
                            sample_scores[i][col] = None
                            sample_errors[i][col] = "得分无效 (nan)"
                        else:
                            sample_scores[i][col] = v
                    except (ValueError, TypeError):
                        sample_scores[i][col] = None
                        sample_errors[i][col] = f"无法转换: {val}"
                else:
                    sample_scores[i][col] = None
                    sample_errors[i][col] = "值为空"

            done = idx + 1
            progress = 10 + int(80 * done / len(active_metrics))
            _update_eta()
            self._report(progress, f"指标 {metric.value} 评估完成 ({done}/{len(active_metrics)})")
            self._log(f"指标 {metric.value} 评估完成")

        self._report(92, "全部指标评估完成，正在整理结果...")
        self._log("整理评估结果...")

        # 构建评估结果
        eval_results = []
        for i in range(len(samples)):
            # 跳过的指标标记为 None
            for s in skipped:
                sample_scores[i][s.value] = None
                sample_errors[i][s.value] = "跳过: 需要 embedding 但不可用"

            eval_results.append(EvalResult(
                question=samples[i].question,
                answer=samples[i].answer,
                contexts=samples[i].contexts,
                ground_truth=samples[i].ground_truth,
                scores=sample_scores[i],
                errors=sample_errors[i],
            ))

        # 计算聚合评分（只算有值的）
        agg_scores = {}
        for metric in active_metrics:
            vals = [s[metric.value] for s in sample_scores
                    if s.get(metric.value) is not None]
            if vals:
                agg_scores[metric.value] = round(sum(vals) / len(vals), 4)

        self._report(96, "结果整理完成")
        self._report_eta(None)
        self._report(100, "评估完成")

        return EvalResults(
            task_id="",
            task_name="",
            status=EvalStatus.COMPLETED,
            aggregate=AggregateScores(scores=agg_scores, count=len(eval_results)),
            results=eval_results,
        )
