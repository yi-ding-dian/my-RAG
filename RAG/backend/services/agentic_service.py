"""Agentic 检索增强：LangGraph 检索决策层（retrieve -> grade -> rewrite 循环）

在"检索 -> 生成"之间插入决策循环（聊天设置 agentic.enabled 开关，默认关）：

         START
           │
        retrieve                       ┌── answer ──► END（直接生成）
           │                           │
        grade ── rewrite（未超重试上限）┘
           │  │
           │  └── abstain ──► END（拒答：沿用"未检索到相关内容"提示）
           │
           │  分档规则（相似度 = cos，0~1，取该轮最高 vector_score）：
           │  - score >= recheck_threshold  → answer
           │  - abstain_threshold <= score < recheck_threshold 且
           │    改写次数 < max_retries → rewrite
           │  - score < abstain_threshold 或重试用尽 → abstain
           │  - 全部命中无 vector_score（纯 BM25 关键词命中）→ 视为 answer
           │    （已通过阈值过滤，有强关键词匹配；trace 标注 score=None）

- 图只管决策：改写/重新检索/分档在 SSE 流式生成**之前**完成；
  生成仍走 chat_service 现有流式链路（决策结果在图外消费）
- 复用现有检索服务（retrieval_service.retrieve）：多租户 where 过滤/
  混合检索/rerank 全部天然继承；Rerank 分数（relevance_score）不回写
  vector_score——分档基准恒为 vector_score 原始语义
- 可预期失败降级（不影响问答兜底）：
  - rewrite 节点 LLM 调用失败 → 保持原查询再检索一次（attempts+1，
    trace 记 rewrite_failed）
  - 检索不可用（RetrievalUnavailableError）→ 原样向上抛，chat_service
    按既有 error 事件透传
"""
from __future__ import annotations

import logging
from typing import List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.config import get_active_config
from backend.models.rag_models import Source
from backend.services.llm_client import get_llm_client, llm_completion
from backend.services.retrieval_service import get_retrieval_service

logger = logging.getLogger(__name__)

# 查询改写 Prompt：把口语化/含无关词的问题改写成检索友好查询。
# 无解释、原样输出（明确要求，防止 LLM 加引号/序号/口语后缀）
_REWRITE_PROMPT = (
    "你是一名知识库检索助手。用户的问题可能是口语化表述，含与内容无关的"
    "寒暄、冗余词句。请把该问题改写为最适合在知识库文档中检索的简洁查询：\n"
    "- 保留核心实体与业务术语，去掉寒暄和冗余修饰；\n"
    "- 只输出改写后的查询文本本身，不要任何解释、引号或编号；\n"
    "- 如果原有表述已足够清晰，原样输出。\n\n"
    "用户问题：{query}"
)


class AgenticState(TypedDict, total=False):
    """LangGraph 图状态（跨节点共享）

    - query：当前轮检索使用的查询（rewrite 节点改写后更新）
    - original_query：初始问题（仅记录）
    - sources：最近一轮检索结果
    - best_score：最近一轮最高相似度（纯向量语义；无向量分 None）
    - attempts：已检索次数（1=首次检索；rewrite 后递增）
    - from_rewrite：本轮检索是否由改写触达（trace 展示用，False=首次）
    - rewrite_failed：rewrite 节点失败标志（trace 展示用）
    - decision：grade 节点输出，"answer" | "abstain"
    - llm_cfg：合并后的 LLM 配置（改写调用用；空=全局活跃配置）
    - top_k / min_score：检索参数（None=活跃配置默认；部门合并后的有效值
      由 chat_service 传入，保证部门覆盖与全局语义一致）
    """
    kb_id: str
    query: str
    original_query: str
    sources: List[Source]
    best_score: Optional[float]
    attempts: int
    from_rewrite: bool
    rewrite_failed: bool
    decision: str
    llm_cfg: dict
    top_k: Optional[int]
    min_score: Optional[float]
    trace: List[dict]


class AgenticResult:
    """面向调用方（chat_service）的决策结果

    - decision: "answer"（用最终 query + sources 进入生成）| "abstain"（拒答）
    - query / sources：最终采用的查询与检索结果（改写成功时 query != 原提问）
    - trace: 决策轨迹 [{"attempt", "query", "best_score", "sources_count",
      "from_rewrite", "rewrite_failed"}]——SSE agentic 事件与会话落盘用
    """

    def __init__(self, decision: str, query: str, sources: List[Source],
                 trace: List[dict]):
        self.decision = decision
        self.query = query
        self.sources = sources
        self.trace = trace


class AgenticService:

    def __init__(self):
        self._graph_spec: Optional[tuple] = None
        self._graph = None

    # ================= 编译（阈值固定，运行期按配置重建，不反复编译） =================

    @staticmethod
    def _build_graph(max_retries: int, recheck: float, abstain: float):
        """构建 LangGraph 状态图（阈值编译期绑定；配置变化时 _ensure_graph 重建）"""

        async def retrieve_node(state: AgenticState) -> AgenticState:
            """节点：检索（复用 retrieval_service，多租户/混合/rerank 天然继承）"""
            sources = await get_retrieval_service().retrieve(
                state["kb_id"], state["query"],
                top_k=state.get("top_k"), min_score=state.get("min_score"))
            # best_score：取本轮最高向量相似度（Rerank 后 Source.score 语义
            # 已变，不采用；vector_score 保持原始 cos 语义才与阈值同量纲）
            vec_scores = [s.vector_score for s in sources
                          if s.vector_score is not None]
            best = max(vec_scores) if vec_scores else None
            attempt = int(state.get("attempts", 0)) + 1
            return {
                **state,
                "sources": sources,
                "best_score": best,
                "attempts": attempt,
                "trace": state.get("trace", []) + [{
                    "attempt": attempt,
                    "query": state["query"],
                    "best_score": best,
                    "sources_count": len(sources),
                    "from_rewrite": bool(state.get("from_rewrite", False)),
                    "rewrite_failed": bool(state.get("rewrite_failed", False)),
                }],
            }

        def grade_node(state: AgenticState) -> AgenticState:
            """节点：分档判定（纯函数，无副作用；条件边按 decision 分派）

            无向量分（纯 BM25 命中）视为通过——已过阈值过滤，有强关键词匹配。
            """
            score = state.get("best_score")
            if score is None:
                decision = "answer"
            elif score >= recheck:
                decision = "answer"
            elif score < abstain:
                decision = "abstain"
            else:
                # 中档：改写次数是否已用完（改写了 max_retries 次后仍中档
                # → 拒答；尚未改写 → rewrite）
                rewrite_used = int(state.get("attempts", 0)) - 1
                decision = "rewrite" if rewrite_used < max_retries else "abstain"
            return {**state, "decision": decision}

        async def rewrite_node(state: AgenticState) -> AgenticState:
            """节点：LLM 查询改写（非流式一次调用；失败降级 = 原查询重检）

            改写用当前生效 LLM（合并部门配置后的 dict，调用方经 llm_cfg 传入；
            空 dict = 全局活跃配置）。失败不打堆栈（warning），标记
            rewrite_failed 后原查询再检一次（无害：无非预期副作用）。
            """
            query = state["query"]
            llm_cfg = state.get("llm_cfg") or {}
            rewritten = query
            try:
                client = get_llm_client(llm_cfg)
                llm = get_active_config().llm
                model = llm_cfg.get("model") or llm.model
                resp = await llm_completion(
                    client,
                    model=model,
                    messages=[{"role": "user",
                               "content": _REWRITE_PROMPT.format(query=query)}],
                    max_tokens=100,
                    temperature=0.1,
                )
                rewritten = (resp.choices[0].message.content or "").strip()
            except Exception as e:  # noqa: BLE001 - 可预期失败（超时/断连等）
                logger.warning("查询改写失败（降级为原查询重检）: query=%s err=%s",
                               query[:30], str(e)[:150])
                rewritten = query
            return {
                **state,
                "query": rewritten or query,
                "from_rewrite": True,
                "rewrite_failed": (rewritten or query) == query,
            }

        builder = StateGraph(AgenticState)
        builder.add_node("retrieve", retrieve_node)
        builder.add_node("grade", grade_node)
        builder.add_node("rewrite", rewrite_node)
        builder.add_edge(START, "retrieve")
        builder.add_edge("retrieve", "grade")
        builder.add_conditional_edges(
            "grade",
            lambda state: state["decision"],
            {"answer": END, "rewrite": "rewrite", "abstain": END},
        )
        builder.add_edge("rewrite", "retrieve")
        return builder.compile()

    # ================= 运行时 =================

    def _ensure_graph(self):
        """配置变化（档案激活/部门合并）时重建编译图（阈值编译期绑定）"""
        cfg = get_active_config().agentic
        spec = (int(cfg.max_retries), float(cfg.recheck_threshold),
                float(cfg.abstain_threshold))
        if self._graph_spec != spec:
            self._graph = self._build_graph(*spec)
            self._graph_spec = spec

    async def run(self, kb_id: str, query: str, *,
                  llm_cfg: Optional[dict] = None,
                  top_k: Optional[int] = None,
                  min_score: Optional[float] = None) -> AgenticResult:
        """执行检索决策循环，返回最终决策（answer/abstain）与轨迹

        - kb_id/query：知识库与原始问题
        - llm_cfg：合并后的 LLM 配置 dict（query rewrite 用；None=全局活跃配置）
        - top_k/min_score：检索参数（None=活跃配置默认）
        - 检索不可用（RetrievalUnavailableError）原样上抛（chat_service 透传）
        """
        result = None
        async for kind, payload in self.run_iter(
                kb_id, query, llm_cfg=llm_cfg, top_k=top_k,
                min_score=min_score):
            if kind == "result":
                result = payload
        assert result is not None
        return result

    async def run_iter(self, kb_id: str, query: str, *,
                       llm_cfg: Optional[dict] = None,
                       top_k: Optional[int] = None,
                       min_score: Optional[float] = None):
        """带进度的检索决策：异步生成器，边执行边产出阶段事件

        事件序列（chat_service 转发为 SSE agentic_status，前端按阶段换提示，
        不让用户"干等不知道在干嘛"）：
        - ("phase", {"stage": "retrieving", "attempt": N})  检索进行中
        - ("phase", {"stage": "rewriting"})                 查询改写进行中
          （grade 判定中档且未超重试上限时，改写 LLM 调用前的瞬间下发）
        - ("phase", {"stage": "rechecking", "query": ...})  改写后重新检索
        - ("result", AgenticResult)                         最终决策（唯一终值）

        实现：LangGraph astream(stream_mode="updates") 按节点完成逐个产出，
        阶段事件粒度 = 节点粒度（每个节点 0.3~3s，用户可见进度变化）。
        """
        self._ensure_graph()
        initial: AgenticState = {
            "kb_id": kb_id,
            "query": query,
            "original_query": query,
            "sources": [],
            "best_score": None,
            "attempts": 0,
            "from_rewrite": False,
            "rewrite_failed": False,
            "llm_cfg": llm_cfg or {},
            "top_k": top_k,
            "min_score": min_score,
            "trace": [],
        }
        yield ("phase", {"stage": "retrieving", "attempt": 1})
        final_state: dict | None = None
        async for update in self._graph.astream(initial, stream_mode="updates"):
            if not update:
                continue
            # updates 模式：{节点名: 该节点返回值（state）}，逐个节点到达
            node_name, node_state = next(iter(update.items()))
            if node_name == "grade" and node_state.get("decision") == "rewrite":
                # 改写即将开始（grade 判定瞬间）：前端提示"查询改写中"
                yield ("phase", {"stage": "rewriting"})
            elif node_name == "rewrite":
                yield ("phase", {
                    "stage": "rechecking",
                    "query": node_state.get("query", query),
                })
            final_state = node_state
        result = self._from_state(final_state, query)
        yield ("result", result)

    @staticmethod
    def _from_state(state: Optional[dict], fallback_query: str) -> AgenticResult:
        """末端 state → AgenticResult（astream 最后一节点为全量 state；
        防御状态缺失兜底 abstain）"""
        if state is None:
            return AgenticResult("abstain", fallback_query, [], [])
        return AgenticResult(
            decision=state.get("decision", "abstain"),
            query=state.get("query", fallback_query),
            sources=state.get("sources", []),
            trace=state.get("trace", []),
        )


_agentic_service: Optional[AgenticService] = None


def get_agentic_service() -> AgenticService:
    global _agentic_service
    if _agentic_service is None:
        _agentic_service = AgenticService()
    return _agentic_service
