"""评估指标定义 — 组合可用的 RAGAS 指标"""
from __future__ import annotations
from typing import List
from backend.models.eval_models import EvalMetric

# 使用类而不是模块级单例，每次调用 resolve_metrics 都创建新实例，
# 避免并发评估时多个任务共享同一个 metric 对象导致 llm/embeddings 互相覆盖
from ragas.metrics._faithfulness import Faithfulness
from ragas.metrics._answer_relevance import AnswerRelevancy, ResponseRelevanceInput
from ragas.metrics._context_precision import ContextPrecision
from ragas.metrics._context_recall import ContextRecall
from ragas.metrics._answer_correctness import AnswerCorrectness
from ragas.metrics._answer_similarity import AnswerSimilarity


class _MultiGenAnswerRelevancy(AnswerRelevancy):
    """answer_relevancy 多代版本。

    RAGAS 0.4.3 的 llm_factory 返回 Instructor 模式 LLM，pydantic_prompt 的
    Instructor 分支不支持 n 参数透传（generate_multiple 每次只取 1 个响应），
    导致 strictness=3 只生成 1 个问题（警告 "LLM returned 1 generations
    instead of requested 3"，生成问题过少会拉低分数且与指标语义不符）。
    这里改为循环调用 strictness 次（每次 n=1），凑足 strictness 个生成问题，
    语义与 RAGAS 原生一致（每个问题独立 LLM 调用，无 n 透传依赖）。
    """

    async def _ascore(self, row: dict, callbacks):
        assert self.llm is not None, "LLM is not set"
        prompt_input = ResponseRelevanceInput(response=row["response"])
        responses = []
        for _ in range(self.strictness):
            batch = await self.question_generation.generate_multiple(
                data=prompt_input, llm=self.llm, callbacks=callbacks, n=1,
            )
            responses.extend(batch)
        return self._calculate_score(responses, row)


# 单例实例 — 用于 prompt_service 管理提示词（翻译、编辑、语言切换）
# 这些对象不在评估中使用，仅作为提示词状态的持有者
METRIC_MAP = {
    EvalMetric.FAITHFULNESS: Faithfulness(),
    EvalMetric.ANSWER_RELEVANCY: _MultiGenAnswerRelevancy(),
    EvalMetric.CONTEXT_PRECISION: ContextPrecision(),
    EvalMetric.CONTEXT_RECALL: ContextRecall(),
    EvalMetric.ANSWER_CORRECTNESS: AnswerCorrectness(),
    EvalMetric.ANSWER_SIMILARITY: AnswerSimilarity(),
}

# 类映射 — 用于每次评估时创建全新的实例
_METRIC_CLASSES = {
    EvalMetric.FAITHFULNESS: Faithfulness,
    EvalMetric.ANSWER_RELEVANCY: _MultiGenAnswerRelevancy,
    EvalMetric.CONTEXT_PRECISION: ContextPrecision,
    EvalMetric.CONTEXT_RECALL: ContextRecall,
    EvalMetric.ANSWER_CORRECTNESS: AnswerCorrectness,
    EvalMetric.ANSWER_SIMILARITY: AnswerSimilarity,
}


def resolve_metrics(metric_names: List[EvalMetric]) -> list:
    """根据名称列表创建新的 RAGAS metric 实例"""
    result = []
    for m in metric_names:
        cls = _METRIC_CLASSES[m]
        instance = cls()
        # 从单例复制提示词状态（如中文翻译，由 prompt_service 加载）
        singleton = METRIC_MAP.get(m)
        if singleton and hasattr(singleton, 'get_prompts'):
            try:
                prompts = singleton.get_prompts()
                instance.set_prompts(**prompts)
            except Exception:
                pass
        result.append(instance)
    return result


def get_all_metrics() -> dict:
    """获取所有可用指标信息"""
    from backend.models.eval_models import METRIC_INFO
    return {
        m.value: {
            "key": m.value,
            "label": METRIC_INFO[m]["name"],
            "desc": METRIC_INFO[m]["desc"],
        }
        for m in _METRIC_CLASSES
    }
