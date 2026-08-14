"""评估任务模型"""
from __future__ import annotations
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from enum import Enum


class EvalStatus(str, Enum):
    QUEUED = "queued"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvalMetric(str, Enum):
    FAITHFULNESS = "faithfulness"
    ANSWER_RELEVANCY = "answer_relevancy"
    CONTEXT_PRECISION = "context_precision"
    CONTEXT_RECALL = "context_recall"
    ANSWER_CORRECTNESS = "answer_correctness"
    ANSWER_SIMILARITY = "answer_similarity"


METRIC_INFO = {
    EvalMetric.FAITHFULNESS: {"name": "忠实度", "desc": "答案是否忠于检索到的上下文，无虚构"},
    EvalMetric.ANSWER_RELEVANCY: {"name": "答案相关性", "desc": "答案与问题的相关程度"},
    EvalMetric.CONTEXT_PRECISION: {"name": "上下文精确度", "desc": "检索到的上下文是否包含有用信息"},
    EvalMetric.CONTEXT_RECALL: {"name": "上下文召回率", "desc": "检索到的上下文是否覆盖了所有必要信息"},
    EvalMetric.ANSWER_CORRECTNESS: {"name": "答案正确性", "desc": "答案与参考答案的匹配程度"},
    EvalMetric.ANSWER_SIMILARITY: {"name": "答案相似度", "desc": "答案与参考答案的语义相似度"},
}


class LLMOverrideConfig(BaseModel):
    """评估任务级 LLM 配置覆盖 — 传入后本任务使用该配置作为 Judge LLM，而非 profiles.json 活跃配置"""
    base_url: str = Field(..., description="LLM OpenAI 兼容地址，如 https://api.deepseek.com/v1")
    api_key: str = Field("", description="LLM API Key（仅内存使用，不写盘不打日志）")
    model: str = Field(..., description="LLM 模型名")
    temperature: Optional[float] = Field(None, description="温度，缺省用任务级 llm_temperature")
    timeout: Optional[float] = Field(None, description="请求超时秒数，缺省 300")
    max_tokens: Optional[int] = Field(None, description="最大生成 Token，缺省用任务级 llm_max_tokens")
    max_workers: Optional[int] = Field(None, description="LLM 并发请求数，缺省用任务级 llm_max_workers")


class EvalConfig(BaseModel):
    """评估配置"""
    dataset_id: str = Field(..., description="数据集 ID")
    metrics: List[EvalMetric] = Field(default_factory=lambda: [m for m in EvalMetric], description="评估指标列表")
    use_retrieval: bool = Field(False, description="是否接入真实检索链路")
    retrieval_top_k: int = Field(5, description="检索返回 top-k 条数")
    llm_temperature: float = Field(0.0, description="LLM 温度")
    llm_max_tokens: int = Field(256, description="LLM 最大 Token")
    llm_max_workers: int = Field(4, description="LLM 并发请求数（同时发送的评分请求数）")
    batch_size: int = Field(8, description="每批处理的样本数（批大小）")
    llm: Optional[LLMOverrideConfig] = Field(None, description="任务级 LLM 配置覆盖；不传则用 profiles.json 活跃配置")
    name: str = Field("", description="评估任务名称")


class EvalTask(BaseModel):
    """评估任务"""
    id: str
    name: str
    dataset_id: str
    dataset_name: str
    status: EvalStatus = EvalStatus.PENDING
    config: EvalConfig
    progress: int = Field(0, description="进度百分比 0-100")
    message: str = Field("", description="状态描述")
    error: Optional[str] = None
    eta_seconds: Optional[int] = Field(None, description="预计剩余秒数")
    llm_base_url: str = Field("", description="创建时的 LLM 地址")
    llm_model: str = Field("", description="创建时的 LLM 模型名")
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class AggregateScores(BaseModel):
    """聚合评分"""
    scores: Dict[str, float] = Field(default_factory=dict)
    count: int = 0


class EvalResult(BaseModel):
    """单条样本的评估结果"""
    question: str
    answer: str
    contexts: List[str]
    ground_truth: Optional[str] = None
    scores: Dict[str, Optional[float]] = Field(default_factory=dict)
    errors: Dict[str, Optional[str]] = Field(default_factory=dict)


class EvalResults(BaseModel):
    """完整评估结果"""
    task_id: str
    task_name: str
    status: EvalStatus
    aggregate: AggregateScores = Field(default_factory=AggregateScores)
    results: List[EvalResult] = Field(default_factory=list)
    created_at: str = ""
    completed_at: Optional[str] = None


class EvalTaskListItem(BaseModel):
    """评估任务列表项"""
    id: str
    name: str
    dataset_id: str
    dataset_name: str
    status: EvalStatus
    progress: int
    metrics: List[str]
    created_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None
