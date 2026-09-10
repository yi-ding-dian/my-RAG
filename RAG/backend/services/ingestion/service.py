"""入库服务：解析 -> 切块 -> 向量化 -> 入库 的异步状态机

状态流: uploaded -> parsing -> ingested / failed（新流程解析+入库一步完成；
parsed 为历史中间态，仅兼容保留）
- 后台任务 asyncio.create_task 执行，任何异常捕获并写回 status=failed + error
- 切块方式与参数可配置（method/chunk_size/overlap/delimiter/split_level/regex_pattern/
  parent_chunk_size/parent_chunk_overlap/parent_split_level/retrieval_mode），
  校验失败（非法 method / regex 无 pattern / 参数越界）也写回 failed；
  入库成功时解析配置持久化到文档元数据（parser_id/parser_config），重跑沿用
- 解析配置（parser_config 扩展字段）：layout_recognize/pages/task_page_size/
  table_enable/formula_enable/return_images/lang_list/enable_heading_in_content/
  contextual_retrieval/knowledge_graph/thinking_mode/parse_llm_model，全部可选
  （默认见 _DEFAULT_PARSER_CONFIG），校验非法 400；解析器参数透传
  parsers.client.parse（table_enable/formula_enable/return_images/lang_list/
  pages/backend），enable_heading_in_content 走切块后处理 add_heading_paths
  （块前缀标题路径）；contextual_retrieval（上下文检索增强，默认关）走切块后
  处理 enrich_chunks：为每个块生成上下文摘要（失败/超时跳过不阻塞入库），
  向量化用 "【上下文】摘要\n原文"，摘要存 chunks_meta.context / 向量 metadata
  （截断 500），chunks_meta.text 保持原文；thinking_mode（思考模式，默认
  disabled）控制图谱抽取/上下文摘要调用的思考关闭策略（在线 DeepSeek →
  extra_body，本地 LM Studio Qwen → messages prefill 注入，实现见
  backend/services/thinking_strategy.py）；parse_llm_model
  （解析 LLM 模型，默认空=激活模型）：上下文摘要/图谱抽取专用模型（值为激活
  档案模型列表的 name，后端按 name 查完整配置覆盖 llm 段，查不到回退激活
  模型，仅影响摘要/抽取，对话不受影响）
- MinerU 解析后端 backend（mineru-api /file_parse 参数）：可选
  hybrid-auto-engine（质量优，默认）/pipeline（快但表格错乱）；None 或 "auto"
  不持久化不透传（跟随服务端默认）；仅 MinerU 引擎解析时透传
- 解析引擎：parser_engine auto/mineru/deepdoc/plain（默认 auto）；显式 deepdoc
  或 layout_recognize=DeepDOC 且 engine=auto 时走 DeepDoc 引擎（RAGFlow，
  表格输出为可检索 HTML，仅 PDF；此时不传 MinerU 解析参数）；
  layout_recognize=PlainText 且 engine=auto 时走 plain 纯文本直提（pypdf/
  python-docx，无表格/图片识别），统一封装 resolve_parser_engine（路由层同用）
- qa 切块方式规范性检测：解析完成后、切块前统计问答对占比（问答对/总段落，
  与切块器同口径），低于 50% 且未带 qa_force_continue → 任务失败，错误信息
  带检测详情（占比/对数/段数，前端据此弹"确认继续入库"）；强制标记则跳过
  检测正常入库
- 解析前可用性检测 + 自动降级（pdf/docx）：路由层探测（结果随 _probe 传入任务，
  未传则任务内探测 ≤5s），所选解析器不可用按降级链自动切换并记录说明：
  deepdoc 不可用 → mineru → plain；mineru 不可用 → plain；降级说明写进
  parser_config["degrade"]（随文档元数据返回），parser_config 同时记录实际
  使用的 layout_recognize/parser_engine（重跑沿用实际配置，避免再次降级）
- parent_child 父子分块：子块入向量库（metadata 带 char_start/char_end/
  parent_chunk_index/parent_text/retrieval_mode），父块全文随子块存储供检索上下文；
  其他方式 metadata 仅 document_id/document_name/chunk_index/char_start/char_end
- agentic（Agentic 智能分块，第六种方式）：LLM 读全文自主判断
  完整逻辑段落切割，每块附类型标签（论述类/事实类/操作类/数据类/其他，
  实现见 backend/services/agentic_chunker.py）。文本长度两档校验（解析出
  文本后、切块前）：>5 万字直接拒绝（不支持 Agentic 分块）；1 万~5 万字
  且未带 agentic_confirm → ValueError（错误信息带"约 X.X 万字"提示，任务
  失败，前端弹确认框，确认后带 agentic_confirm=true 重新提交）；带确认
  → 跳过超限校验直接分块。确认标记仅本次提交生效（入库前从 parser_config
  剔除，不持久化，重跑仍需再次确认；防旧配置带确认标记绕过未来校验）。
  LLM 失败/超时/对齐全失败 → 回退 title 切块（warning 注明原因，不阻塞
  入库）。标签存 chunks_meta.label（可选字段，仅 agentic 成功块带）。
  思考关闭策略复用 thinking_strategy（在线 extra_body / 本地 Qwen prefill）。
  与上下文检索增强互斥：前端互斥逻辑 + 后端 resolve_parser_config 强制
  关闭（防 API 直调/重跑旧配置组合）；知识图谱不互斥，可叠加选择
- 防重复触发：任务执行中用内存集合标记，状态机迁移双保险
- 入库前先清旧向量（对任何切块方式都执行，幂等）
- 原始文件从对象存储（MinIO/local）下载到 data/uploads/ 供解析；
  存储不可用/对象不存在时 fallback：本地文件已存在则直接使用
- 解析图片：有字节的图片上传存储 images/{doc_id}/{name}，markdown 引用
  经 rewrite_image_refs 替换为 /api/files/images/{doc_id}/{name}（鉴权代理）
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from backend.chunking import (Chunk, QaStats, add_heading_paths,
                              analyze_qa_format, detect_heading_systems,
                              get_chunker, is_qa_format_valid, order_systems,
                              ParentChildChunkResult)
from backend.chunking.common import _iter_headings
from backend.config import get_active_config
from backend.services.agentic_chunker import (AgenticChunkError,
                                              agentic_chunk)
from backend.services.contextual_retriever import enrich_chunks
from backend.services.document_service import get_document_service
from backend.services.dim_check import VectorDimensionError
from backend.services.embedding_service import (EmbeddingError,
                                                get_embedding_service)
from backend.services.ingestion.images import _ImageMixin
from backend.services.ingestion.params import (_MAX_AGENTIC_TEXT_CHARS,
                                               _MAX_AGENTIC_TEXT_CHARS_HARD,
                                               _PARSER_PARSE_OPTS,
                                               resolve_parser_config,
                                               resolve_parser_engine)
from backend.services.ingestion.trace import _TraceMixin
from backend.services.knowledge_graph_service import build_graph_for_doc
from backend.services.llm_client import LLMRequestError, LLMTimeoutError
from backend.services.parsers.client import (ParserUnavailableError,
                                            get_parser_client)
from backend.services.parsers.probe import probe_parsers
from backend.services.settings.service import llm_cfg_for_parser
from backend.services.storage_service import get_storage_service
from backend.services.table_normalizer import html_tables_to_pipe
from backend.services.text_cleanup import strip_subsup_tags
from backend.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)

# 后台入库任务并发上限（系统配置 ingestion.concurrency，默认 3，超管在
# Settings 页可调，改动即时生效；.env INGEST_CONCURRENCY 仅作出厂默认，
# 由 build_default_config 读取）：批量解析同时打爆 MinerU/embedding 时
# 限制并行任务数。
# 信号量惰性创建并绑定首次 acquire 时的运行事件循环（asyncio.Semaphore
# 3.10+ 懒绑定 loop）；conftest 重置 _ingest_semaphore/_ingest_semaphore_value
# 避免跨测试 loop 串用。
_ingest_semaphore: Optional[asyncio.Semaphore] = None
# 当前信号量绑定的配置值（配置变更时按新值重建信号量，见 _get_ingest_semaphore）
_ingest_semaphore_value: int = 0


def _ingest_concurrency() -> int:
    """实时读系统配置的入库并发数（每次调用读取，改配置即生效；防御性兜底 ≥1）"""
    return max(1, int(get_active_config().ingestion.concurrency))


def _get_ingest_semaphore() -> asyncio.Semaphore:
    """惰性获取全局入库信号量（首次调用绑定当前事件循环）

    动态生效设计：asyncio.Semaphore 创建后不可变，因此每次 acquire 前比对
    当前系统配置值，变化 → 按新值重建信号量（以配置值为 key，旧信号量随
    GC 释放）。并发语义保持：新任务按新上限排队等待；持有中的任务按各自
    获取时的信号量计数继续，不会出现计数错乱。
    """
    global _ingest_semaphore, _ingest_semaphore_value
    target = _ingest_concurrency()
    if _ingest_semaphore is None or _ingest_semaphore_value != target:
        _ingest_semaphore = asyncio.Semaphore(target)
        _ingest_semaphore_value = target
    return _ingest_semaphore

# 入库 metadata 单值上限：父块全文随子块写进 Chroma metadata，章节父块可能
# 数千字，截断防超限报错（检索展示侧另有 2000 截断，见 chat_service，这里只管入库）
_PARENT_TEXT_META_LIMIT = 8000
# 上下文摘要 metadata 单值上限（Chroma metadata 单值限制；摘要本体在
# chunks_meta.context，这里存的是检索侧副本，截断不影响展示）
_CONTEXT_META_LIMIT = 500

# 切块预览上限
_PREVIEW_LIMIT = 20
_PREVIEW_CHAR = 500


class _IngestCancelled(Exception):
    """入库任务被用户取消（路由 cancel 接口置标记，任务检查点抛出）"""


class _AgenticConfirmRequired(Exception):
    """Agentic 分块 1 万~5 万字超限且未带 agentic_confirm

    与普通失败区分：捕获后文档进入 pending_confirm 待确认状态（不写
    failed、不计入失败），error 带字数提示与 agentic_confirm=true 指引，
    前端"确认继续"带确认标记重提入库。
    """


@dataclass
class _IngestChunkStage:
    """切块阶段产物（_ingest 各阶段间的共享状态）

    用参数传递/返回值而非实例属性：入库任务并发执行（多文档并行入库），
    任务局部状态存实例属性会互相串扰。
    """
    chunk_objects: List[Chunk]
    parent_chunks: List[Chunk]
    child_parent_map: Dict[int, int]
    agentic_labels: Dict[int, str]
    raw_chunk_texts: List[str]


class IngestionService(_TraceMixin, _ImageMixin):

    def __init__(self):
        self._running: Set[str] = set()  # 正在入库的 doc_id，防并发重复触发
        # 排队中的 doc_id（已登记、还在等并发信号量）：并发满时任务在信号量上
        # 等待，该窗口未被 _running 覆盖——不单独登记的话路由层 is_running()
        # 判为"未运行"，同一文档会被重复触发（排队串行各跑一遍完整入库）。
        # 取消逻辑仍只看 _running（排队取消走"不在运行即拨回 failed"的既有路径），
        # 故不复用同一集合
        self._queued: Set[str] = set()
        self._cancel: Set[str] = set()   # 用户取消标记（cancel 接口置位，任务检查点消费）
        # 任务当前阶段（doc_id → 阶段名）：解析中状态悬停进度展示用
        # （阶段名在 _ingest 各 stage 前后更新，任务结束清理）
        self._stage: Dict[str, str] = {}
        self._stage_since: Dict[str, str] = {}  # 阶段开始时间（HH:mm:ss，展示用）
        # 入库流程耗时轨迹（doc_id → [{stage, ms}] + 当前阶段起点时间戳）：
        # 阶段切换累计耗时，任务结束写入文档 ingest_trace（可追溯）
        self._stage_ms: Dict[str, int] = {}     # 当前阶段起点时间（perf_counter）
        self._trace: Dict[str, List[dict]] = {}  # 已完成阶段耗时 [{stage, ms}]
        self._task_started_at: Dict[str, str] = {}  # 任务开始时间（展示用）

    def is_running(self, doc_id: str) -> bool:
        return doc_id in self._running

    def is_queued(self, doc_id: str) -> bool:
        """是否已登记但还在排队等并发信号量

        与 is_running 刻意分开：路由层的"取消解析"依赖 is_running 的
        「排队期不算运行」语义来回拨 failed，合并会改掉该行为
        """
        return doc_id in self._queued

    def get_progress(self, doc_id: str) -> Optional[dict]:
        """解析中任务段状态（悬停进度展示）：非运行中返回 None

        返回 {stage: 中文阶段名, since: 阶段开始时间 HH:mm:ss}
        """
        if doc_id not in self._running:
            return None
        stage = self._stage.get(doc_id)
        if not stage:
            return {"stage": "准备中", "since": ""}
        return {"stage": stage, "since": self._stage_since.get(doc_id, "")}

    def running_doc_ids(self) -> List[str]:
        """当前正在入库的 doc_id 列表（进度接口聚合用）"""
        return list(self._running)

    def is_cancelled(self, doc_id: str) -> bool:
        return doc_id in self._cancel

    def cancel(self, doc_id: str) -> None:
        """置取消标记（幂等）。任务内检查点命中后中止本次入库并清除标记"""
        self._cancel.add(doc_id)

    def _is_doc_removed(self, doc_id: str) -> bool:
        """文档是否已被移入回收站（或已彻底删除）

        入库任务后台异步执行，期间文档可能被删；写向量/落库前必须据此中止——
        否则会用同一批向量 id 覆盖写回并把 doc_active 置回 True，表现为
        "已删文档重新出现在问答引用里"（彻底删后则成为清不掉的幽灵向量）
        """
        doc = get_document_service().get(doc_id)
        return doc is None or bool(getattr(doc, "deleted", False))

    def _raise_if_cancelled(self, doc_id: str) -> None:
        """任务内取消检查点：命中标记 → 清除标记并抛 _IngestCancelled
        （调用方捕获后 mark_failed"用户取消解析"）；未命中无操作"""
        if doc_id in self._cancel:
            self._cancel.discard(doc_id)
            raise _IngestCancelled(doc_id)

    async def _await_with_cancel(self, doc_id: str, to_await,
                                 poll: float = 1.0):
        """可取消等待：轮询取消标记，命中 → 中断 to_await（底层请求随之
        取消）并抛 _IngestCancelled；完成 → 正常返回结果。

        背景（修复"取消解析半天没反应"）：原取消是检查点式——MinerU /
        embedding 等对外 HTTP 长等待期间没有检查点可消费，任务实际阻塞
        在调用里，取消要等本次解析跑完才生效（可能数分钟）。本包装把
        等待改成"轮询 + 可丢弃"：命中标记即取消底层任务（httpx 连接
        释放；丢弃部分结果——取消本就不要结果）。
        """
        task = asyncio.ensure_future(to_await)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=poll)
                if task in done:
                    return task.result()
                self._raise_if_cancelled(doc_id)
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception:
            task.cancel()
            raise

    async def run_ingestion(self, doc_id: str, method: str | None = None,
                            **params):
        """后台任务主流程（路由层 asyncio.create_task 调用）

        method/params: 切块方式与参数（不传则沿用文档已有配置或默认，
        见 resolve_parser_config）；校验失败（非法 method / regex 无 pattern /
        参数越界）在任务内写回 status=failed + error（路由层同步 400 双保险）
        并发上限：模块级信号量（系统配置 ingestion.concurrency，默认 3，
        超管可调，改配置即时生效），批量解析不会同时打爆 MinerU/embedding。
        """
        # 排队期即登记：并发满时任务会在信号量上等待，该窗口若不登记，路由层
        # 会当作新任务再次触发同一文档（排队串行各跑一遍完整入库）
        if doc_id in self._queued or doc_id in self._running:
            logger.info("入库任务: 已在执行或排队中，忽略重复触发 %s", doc_id)
            return
        self._queued.add(doc_id)
        try:
            # 信号量在整个任务期间持有（含排队等待），并发解析数不会超过上限
            async with _get_ingest_semaphore():
                await self._run_ingestion_locked(doc_id, method=method, **params)
        finally:
            self._queued.discard(doc_id)

    async def _run_ingestion_locked(self, doc_id: str,
                                    method: str | None = None, **params):
        """run_ingestion 的并发受限主体（原逻辑整体移入，信号量包裹）"""
        doc_svc = get_document_service()
        doc = doc_svc.get(doc_id)
        if not doc:
            logger.warning("入库任务: 文档不存在 %s", doc_id)
            return
        if doc_id in self._running:
            logger.info("入库任务: 已在执行，忽略重复触发 %s", doc_id)
            return
        if not doc_svc.is_ingestable(doc_id):
            logger.info("入库任务: 当前状态不可触发 %s (%s)", doc_id, doc.status)
            return
        self._running.add(doc_id)
        try:
            await self._ingest(doc_id, method=method, **params)
        finally:
            # 失败路径 trace：成功路径已在 _ingest 内 _finalize_trace 消费并落
            # 文档（trace pop）；若仍存在（异常路径文档已 failed）→ 标 failed
            # 再落（失败发生在哪个阶段可追溯）
            if doc_id in self._trace:
                # 顺序要紧：_finalize_trace 会把 _stage_ms 里的「当前失败阶段」
                # append 进 trace 并 pop 出来，所以必须先 finalize 拿到完整
                # trace，再对最后一条置 failed；反过来的话标记会落在上一个已
                # 成功完成的阶段上（用户按轨迹排查会看错方向）
                trace, total_ms, _, started_at, finished_at = self._finalize_trace(doc_id)
                if trace:
                    trace[-1]["status"] = "failed"
                    get_document_service().update_doc(
                        doc_id, ingest_trace=trace, ingest_total_ms=total_ms,
                        ingest_started_at=started_at, ingest_finished_at=finished_at)
            self._running.discard(doc_id)
            self._cancel.discard(doc_id)  # 取消标记随任务结束清除（含正常完成）
            self._clear_stage(doc_id)     # 任务结束清理阶段状态（悬停进度不再返回）

    async def _ingest(self, doc_id: str, method: str | None = None, **params):
        doc_svc = get_document_service()
        doc = doc_svc.get(doc_id)
        if not doc:
            return

        # 路由层传入的解析器探测结果（_probe，内部参数不走 IngestRequest）；
        # 未传（直接调用/历史路径）时任务内自行探测（见解析步骤）
        probe = params.pop("_probe", None)
        # QA 规范性检测强制继续标记（IngestRequest.qa_force_continue，
        # 前端"确认继续入库"时提交；True 跳过检测，见 3.7 步）
        qa_force_continue = bool(params.pop("qa_force_continue", False))

        # 0) 切块方式与参数解析（校验失败 → failed，不进入解析流程）
        try:
            parser_id, parser_config = resolve_parser_config(doc, method, params)
        except ValueError as e:
            logger.warning("入库参数校验失败: %s (%s)", doc_id, e)
            # uploaded -> failed 为状态机非法迁移，先拨 parsing 再 failed（parsing -> failed 合法）
            doc_svc.transition(doc_id, "parsing")
            doc_svc.mark_failed(doc_id, str(e))
            return

        # 1) uploaded -> parsing
        doc_svc.transition(doc_id, "parsing")
        # 0.5) 轨迹起点（准备中：含参数解析/探测/切换，任务初始化耗时）
        self._set_stage(doc_id, "准备中")
        try:
            # 2) 解析阶段（下载 → 探测降级 → 解析 → 取消检查点1 →
            #    图片上传 → 落盘）
            self._set_stage(doc_id, "解析文档")
            text, parse_method = await self._stage_parse(
                doc, doc_id, parser_config, probe)
            # 3) 切块阶段（QA 规范性检测 → 切块 → 父标题前缀）
            self._set_stage(doc_id, "切块")
            stage = await self._stage_chunk(
                doc, doc_id, parser_id, parser_config,
                qa_force_continue, text)
            # 4) 增强阶段（上下文检索摘要 → 知识图谱，两者失败不阻塞入库）
            self._set_stage(doc_id, "增强（摘要/图谱）")
            contexts, kg_status, kg_error = await self._stage_enhance(
                doc, doc_id, parser_config, stage, text)
            # 5) 向量化阶段（空块校验 → 取消检查点2a → embedding →
            #    取消检查点2b → 维度校验 → 清旧向量 → 写入 + BM25 失效）
            # 文档在解析/切块期间被移入回收站（或已彻底删除）→ 中止入库：
            # 既不写向量也不落库（见 _is_doc_removed 说明）
            if self._is_doc_removed(doc_id):
                logger.info("入库任务: 文档已移入回收站或已删除，中止入库 %s",
                            doc_id)
                return
            self._set_stage(doc_id, "向量化入库")
            await self._stage_vectorize(
                doc, doc_id, parser_id, parser_config, contexts, stage)
            # 6) 完成阶段（解析配置/chunks_meta/图谱状态落库 + 统计日志）
            self._set_stage(doc_id, "完成落库")
            await self._stage_finalize(
                doc, doc_id, parser_id, parser_config, parse_method,
                contexts, stage, kg_status, kg_error)
            # 7) 成功：入库轨迹落文档（5 阶段耗时 + 总耗时 + 启止时间）
            trace, total_ms, _, started_at, finished_at = self._finalize_trace(doc_id)
            if trace:
                doc_svc.update_doc(doc_id, ingest_trace=trace,
                                   ingest_total_ms=total_ms,
                                   ingest_started_at=started_at,
                                   ingest_finished_at=finished_at)
        except _AgenticConfirmRequired as e:
            # Agentic 分块 1 万~5 万字超限未确认：不算失败 → 文档进入
            # pending_confirm 待确认状态（状态列橙色"待确认" + error 提示，
            # 不计入失败统计；"确认继续"带 agentic_confirm=true 重提入库）
            logger.info(
                "Agentic 分块需确认: %s (%s) 提示=%s（agentic_confirm=true 可继续）",
                doc.original_name, doc_id, str(e))
            doc_svc.transition(doc_id, "pending_confirm", error=str(e))
        except _IngestCancelled:
            # 用户取消解析（检查点命中）：写回 failed + 取消原因，不打堆栈
            logger.info("入库任务已取消: %s (%s)", doc.original_name, doc_id)
            doc_svc.mark_failed(doc_id, "用户取消解析")
        except ParserUnavailableError as e:
            # 可预期失败（解析服务不可用/调用失败）：warning 不记堆栈；
            # 错误消息 = 原始解析异常文本（mark_failed 文案与历史一致）
            logger.warning("入库失败（解析服务不可用）: %s (%s)", doc_id, e)
            doc_svc.mark_failed(doc_id, str(e))
        except EmbeddingError as e:
            # 可预期失败（Embedding 服务不可用/超时，重试后仍失败）：
            # warning 不记堆栈，任务失败语义不变
            logger.warning("入库失败（Embedding 服务不可用）: %s (%s)",
                           doc_id, e)
            doc_svc.mark_failed(doc_id, str(e))
        except VectorDimensionError as e:
            # 可预期失败（更换 embedding 模型后维度不匹配）：warning，
            # 任务失败语义不变（提示更换模型或重建向量）
            logger.warning("入库失败（向量维度不匹配）: %s (%s)", doc_id, e)
            doc_svc.mark_failed(doc_id, str(e))
        except (LLMTimeoutError, LLMRequestError) as e:
            # 可预期失败（LLM 超时/限流/网络错误冒泡到任务层）：warning
            # 不记堆栈，任务失败语义不变
            logger.warning("入库失败（LLM 调用失败）: %s (%s)", doc_id, e)
            doc_svc.mark_failed(doc_id, str(e))
        except Exception as e:
            # 兜底（未知异常）：不记堆栈，信息保留
            logger.error("入库失败: %s (%s)", doc_id, e)
            doc_svc.mark_failed(doc_id, str(e))

    async def _stage_parse(self, doc, doc_id: str, parser_config: dict,
                           probe) -> Tuple[str, str]:
        """解析阶段：下载原始文件 → 探测降级 → 解析 → 取消检查点1 →
        图片上传 → 落盘；返回 (text, parse_method)。

        parser_config 原地修改（降级说明/实际引擎随文档元数据持久化）；
        解析器调用失败包装 ParserUnavailableError（消息 = 原始异常文本，
        mark_failed 文案与历史一致）。
        """
        doc_svc = get_document_service()
        storage = get_storage_service()
        # 1.5) 原始文件从对象存储下载到 data/uploads/（供解析器读 Path）
        upload_path = doc_svc.get_upload_path(doc)
        try:
            await storage.download_to(f"uploads/{doc.name}", upload_path)
        except Exception as e:
            # 存储不可用/对象不存在：本地已有文件（历史数据/本地副本）则直接用
            if not upload_path.exists() or upload_path.stat().st_size == 0:
                raise RuntimeError(f"原始文件不可用（存储读取失败: {e}）") from e
            logger.warning("存储下载失败，使用本地文件 %s: %s",
                           upload_path.name, str(e)[:150])

        # 2) 解析（引擎选择：显式 deepdoc 或 layout_recognize=DeepDOC 且
        # engine=auto 都走 DeepDoc——与前端 ParseConfigModal 联动一致；
        # 其余：auto 探测降级 / mineru 强制 / plain 直提；
        # 解析配置透传：表格/公式/图片/语言/页码，见 _PARSER_PARSE_OPTS）
        # 解析前可用性检测 + 自动降级（pdf/docx，外部解析器场景）：
        # deepdoc 不可用 → mineru → plain；mineru 不可用 → plain；
        # 降级说明写进 parser_config["degrade"]（随文档元数据返回，前端
        # 可在 ingest 响应/文档详情读取），parser_config 同时记录实际使用
        # 的 layout_recognize/parser_engine（重跑沿用实际配置，避免再次降级）
        parser = get_parser_client()
        engine = resolve_parser_engine(parser_config)
        parse_opts = {k: parser_config[k] for k in _PARSER_PARSE_OPTS
                      if k in parser_config}
        if engine == "deepdoc":
            parse_opts = {}  # DeepDoc 无 MinerU 解析参数（表格/图片开关不适用）
        parse_degrade: Optional[str] = None
        file_type = (doc.file_type or "").lower().lstrip(".")
        # docx_struct（本地结构化解析）无外部服务依赖：与 plain 一样跳过
        # 可用性探测与降级链（探测对象是 MinerU/DeepDoc 服务）
        if engine not in ("plain", "docx_struct") \
                and file_type in ("pdf", "docx"):
            if probe is None:
                probe = await probe_parsers(
                    mineru_timeout=3.0, deepdoc_timeout=5.0)
            if engine == "deepdoc" and not probe["deepdoc"]["available"]:
                reason = probe["deepdoc"]["reason"] or "无响应"
                if probe["mineru"]["available"]:
                    engine = "mineru"
                    parser_config["parser_engine"] = "mineru"
                    parser_config["layout_recognize"] = "MinerU"
                    parse_opts = {k: parser_config[k]
                                  for k in _PARSER_PARSE_OPTS
                                  if k in parser_config}
                    parse_degrade = (
                        f"DeepDoc 服务不可用（{reason}），已自动切换 MinerU 解析")
                else:
                    engine = "plain"
                    parser_config["parser_engine"] = "plain"
                    parser_config["layout_recognize"] = "PlainText"
                    parse_opts = {}
                    parse_degrade = (
                        f"DeepDoc 服务不可用（{reason}），MinerU 也不可用"
                        f"（{probe['mineru']['reason'] or '无响应'}），"
                        f"已降级纯文本提取")
            elif engine in ("auto", "mineru") \
                    and not probe["mineru"]["available"]:
                reason = probe["mineru"]["reason"] or "无响应"
                engine = "plain"
                parser_config["parser_engine"] = "plain"
                parser_config["layout_recognize"] = "PlainText"
                parse_opts = {}
                parse_degrade = (
                    f"MinerU 服务不可用（{reason}），已切换纯文本提取")
        if parse_degrade:
            parser_config["degrade"] = parse_degrade
        try:
            text, images, parse_method = await self._await_with_cancel(
                doc_id,
                parser.parse(upload_path, doc.file_type,
                             engine=engine,
                             **parse_opts))
        except ParserUnavailableError:
            raise
        except _IngestCancelled:
            raise
        except Exception as e:
            # 可预期失败（解析器不可用/网络错误）：类型化异常（消息 =
            # 原始异常文本），外层捕获记 warning + 写回 failed
            raise ParserUnavailableError(str(e)) from e
        if not text or not text.strip():
            raise RuntimeError(
                "解析结果为空（扫描版 PDF 或无文本内容），请检查解析服务后重试")

        # 取消检查点 1：解析已完成、尚未上传图片/落盘/切块——用户取消
        # 后不再做后续耗时处理（解析服务调用本身无法中途打断，已白跑）
        self._raise_if_cancelled(doc_id)

        # 2.5) 解析图片上传存储 + markdown 引用替换
        # 上传前先清该文档旧解析图片（re-ingest 防残留孤儿对象；
        # 即使本次解析无图也清理，失败仅 warning 不阻断入库）
        try:
            await storage.delete_prefix(f"images/{doc.id}/")
        except Exception as e:
            logger.warning("清理旧解析图片失败 %s: %s",
                           doc.id, str(e)[:150])
        if images:
            text = await self._upload_images(doc, text, images)

        # 2.8) HTML 表格 → markdown 管道表格规范化（图片引用替换后执行，
        # 表格内 <img> 的 src 已是代理 URL 原样保留）。目的：入库内容
        # token 更省、LLM 可读性更高（不会把 <table><tr><td> 标签流
        # 原样输出给用户）；非表格文本不动，无 HTML 表格时零开销。
        # 转换后落盘，预览/切块/检索共用干净文本。
        text = html_tables_to_pipe(text)

        # 2.9) 上下标标签清洗（MinerU 误识别的 <sub>/<sup> 去标签留文字）：
        # PDF 无上下标语义，MinerU 按字号+基线推断，标题字体/全角引号等
        # 坐标不齐时大量误判（编号判成下标、标题判成上标、引号判成下标）。
        # 落盘前清洗保证全文/切块/偏移一致
        # （切块偏移契约不允许切块阶段改文本）。
        text = strip_subsup_tags(text)

        # 3) 解析文本落盘 data/parsed/{doc_id}.md
        # （新流程无 parsed 中间态：解析+入库一步完成，直接到 ingested）
        parsed_path = doc_svc.get_parsed_path(doc)
        parsed_path.write_text(text, encoding="utf-8")
        logger.info("解析完成: %s (%s) %d 字符%s", doc.original_name,
                    parse_method, len(text),
                    f"，图片 {len(images)} 张" if images else "")
        return text, parse_method

    @staticmethod
    def _build_hierarchical_chunker(parser_config: dict,
                                    heading_levels: List[int]):
        """层级聚合切块器（带外部层级表 heading_levels）

        参数与 get_chunker("hierarchical", ...) 分支一致（get_chunker 没有
        外部层级表入口）；函数内延迟导入，避免模块加载时就拉入 normative
        包的 docx 解析依赖（与 get_chunker 同款处理）。
        """
        from backend.normative.chunker import HierarchicalChunker
        return HierarchicalChunker(
            chunk_size=parser_config.get("chunk_size"),
            overlap=parser_config.get("overlap"),
            chapter_level=parser_config.get("chapter_level") or 1,
            heading_levels=heading_levels,
        )

    async def _resolve_heading_levels(self, doc, doc_id: str, parser_id: str,
                                      parser_config: dict,
                                      text: str) -> Optional[List[int]]:
        """标题分层（可选，仅 hierarchical 切块）：外部层级表；None = 不分层

        触发条件（三条全满足才走 LLM）：
        1. 切块方式为 hierarchical；
        2. 解析产物层级不可靠——解析引擎非 docx_struct（docx 结构解析产出的
           # 层级本来就精确，再跑 LLM 纯属浪费）；
        3. 系统配置「标题分层模型」（chunking.heading_llm_model）非空，且该
           标识能在 LLM 模型列表里查到完整配置（含 api_key）——查不到按未
           配置处理（活跃配置的 api_key 可能为空，直接调会抛 Missing
           credentials，故必须用明确配置的模型）。
        成功 → HybridLevelResult.levels（与标题一一对应的层级表，可直接传给
        HierarchicalChunker(heading_levels=...)），标题总数 / 规则定级数 /
        LLM 判定数 / token 用量 / 耗时 / 失败批次记 info 日志；
        LLM 未配置 / 调用异常 / 超时 → 日志说明原因 + 回退 None（按原 #
        层级切块），不阻塞入库。
        """
        if parser_id != "hierarchical":
            return None
        if resolve_parser_engine(parser_config) == "docx_struct":
            return None
        model_ident = (get_active_config().chunking.heading_llm_model
                       or "").strip()
        if not model_ident:
            # 可追溯输出：这份文档为什么没走 LLM 分层（正常的未配置状态，非错误）
            logger.info("标题分层：文档 %s 需要 LLM 分层但未配置「标题分层模型」"
                        "（heading_llm_model），已回退规则分层", doc_id)
            return None
        if not llm_cfg_for_parser(model_ident):
            logger.info("标题分层：文档 %s 配置的「标题分层模型」%s 不在 LLM "
                        "模型列表中（heading_llm_model），已回退规则分层",
                        doc_id, model_ident)
            return None
        # 标题分层单独成段（trace 可追溯耗时）：结掉「切块」已用部分，本段
        # 结束后恢复「切块」继续计时（切块阶段在轨迹里显示为前后两段）
        self._set_stage(doc_id, "标题分层")
        try:
            # 延迟导入：heading_llm 依赖 normative 包（见 _build_hierarchical_chunker）
            from backend.normative.heading_llm import hybrid_levels
            # 模型标识经 cfg.parse_llm_model 传入：hybrid_levels 内部按
            # llm_cfg_for_parser 取完整配置（与解析配置选模型同款机制）
            result = await self._await_with_cancel(
                doc_id, hybrid_levels(text, {"parse_llm_model": model_ident}))
        except _IngestCancelled:
            raise  # 用户取消：不按失败回退，交由任务层处理
        except Exception as e:
            logger.warning("标题分层失败，回退规则分层: %s (%s) 模型=%s 原因=%s",
                           doc.original_name, doc_id, model_ident, e)
            return None
        finally:
            self._set_stage(doc_id, "切块")  # 恢复切块阶段（后续切块耗时计入）
        prompt_tokens, completion_tokens = result.token_usage
        logger.info(
            "标题分层完成: %s (%s) 模型=%s 标题 %d 个（规则 %d / LLM %d）"
            "批次 %d（未判定 %s）token 输入 %d / 输出 %d 耗时 %.1fs",
            doc.original_name, doc_id, model_ident, len(result.headings),
            len(result.rule_indexes), len(result.llm_indexes),
            len(result.batches), result.failed_batches or "无",
            prompt_tokens, completion_tokens, result.elapsed)
        return result.levels or None

    async def _stage_chunk(self, doc, doc_id: str, parser_id: str,
                           parser_config: dict, qa_force_continue: bool,
                           text: str) -> _IngestChunkStage:
        """切块阶段：QA 规范性检测 → 切块（agentic 两档校验 / parent_child
        / 其他）→ 父标题前缀。返回 _IngestChunkStage。

        空块校验不在本阶段：保持原"图谱构建完成后才校验"的顺序语义
        （校验在 _stage_vectorize 开头）。
        """
        # 3.6) 标题分层（可选，仅 hierarchical 切块）：解析产物层级不可靠
        # （非 docx 结构解析）时用配置的「标题分层模型」给标题重新分层——
        # 带编号的走规则、无编号的交 LLM，结果作为外部层级表传给切块器
        # （见 _resolve_heading_levels；未配置/失败 → None = 按原 # 层级切）
        heading_levels = await self._resolve_heading_levels(
            doc, doc_id, parser_id, parser_config, text)

        # 3.7) QA 规范性检测（仅 qa 方式，切块前）：问答对占比（问答对/
        # 总段落，与 QaChunker 切块同口径）低于 50% 且未强制 → 任务失败，
        # 错误信息带检测详情（占比/对数/段数，前端据此弹"确认继续入库"，
        # 确认后带 qa_force_continue=true 重新提交）；强制标记跳过检测
        if parser_id == "qa" and not qa_force_continue:
            stats = analyze_qa_format(text)
            if not is_qa_format_valid(stats):
                ratio = (stats.qa_pairs / stats.total_paragraphs
                         if stats.total_paragraphs else 0.0)
                raise RuntimeError(
                    f"QA 问答格式检测未通过：问答对占比 {ratio:.1%}"
                    f"（{stats.qa_pairs} 对 / {stats.total_paragraphs} 段），"
                    f"未达到 50% 规范要求。确认文档符合预期可强制继续入库"
                    f"（qa_force_continue=true）")

        # 3.8) 标题编号体系自动检测（title/parent_child/regex 等基于标题的
        # 方式）：解析产物标题常为 MinerU 全 ## 扁平输出（真实层级在编号
        # "一、"/"1.1" 里），检测所用体系并写回 parser_config —— 切块
        # 层级推断（_iter_headings）与标题链（add_heading_paths）随之生效。
        # 显式传入 heading_systems（重跑沿用/界面覆盖）时跳过检测
        if parser_id in ("title", "parent_child") \
                and not parser_config.get("heading_systems"):
            candidates = [t for _off, _lvl, t in _iter_headings(text)]
            detected = order_systems(
                [r["system"] for r in detect_heading_systems(candidates)])
            if detected:
                parser_config["heading_systems"] = detected
                logger.info("标题体系检测: %s (%s) %s",
                            doc.original_name, doc_id, detected)

        # 4) 切块（按用户选择的切块方式与参数；用替换后文本保证图片引用可加载）
        chunk_objects: List[Chunk] = []
        parent_chunks: List[Chunk] = []
        child_parent_map: Dict[int, int] = {}
        # Agentic 分块标签（仅 method=agentic 成功时填充；无标签的块不带
        # 该字段，chunks_meta.label 可选）
        agentic_labels: Dict[int, str] = {}
        if parser_id == "agentic":
            # Agentic 智能分块：LLM 读全文自主切逻辑段落并打标签（论述类/
            # 事实类/操作类/数据类/其他，实现与失败语义见 agentic_chunker）。
            # 两档校验：>5 万字直接拒绝（ValueError → 任务失败 failed，
            # 不支持 Agentic 分块）；1 万~5 万字且未带 agentic_confirm →
            # _AgenticConfirmRequired → 文档进入 pending_confirm 待确认
            # 状态（不算失败，error 带字数提示，前端"确认继续"带
            # agentic_confirm=true 重新提交），带确认 → 跳过校验直接分块；
            # LLM 失败/超时/对齐全失败 → 回退 title 切块（warning 注明
            # 原因，不阻塞入库）；思考关闭策略复用 thinking_strategy
            if len(text) > _MAX_AGENTIC_TEXT_CHARS_HARD:
                raise ValueError(
                    "文档超过 5 万字，不支持 Agentic 分块，"
                    "请换用其他切块方式")
            if len(text) > _MAX_AGENTIC_TEXT_CHARS \
                    and not parser_config.get("agentic_confirm"):
                raise _AgenticConfirmRequired(
                    f"文档约 {len(text)/10000:.1f} 万字，"
                    f"Agentic 分块成本较高，确认继续请重试"
                    f"（agentic_confirm=true）")
            try:
                agentic_chunks, labels = await self._await_with_cancel(
                    doc_id, agentic_chunk(text, parser_config))
                agentic_labels = {i: labels[i]
                                  for i in range(len(labels))}
                chunk_objects = agentic_chunks
            except AgenticChunkError as e:
                logger.warning(
                    "Agentic 切块失败，回退标题切块: %s (%s) 原因=%s",
                    doc.original_name, doc_id, str(e))
                chunk_objects = get_chunker("title",
                                            parser_config).chunk(text)
        elif parser_id == "parent_child":
            # 父子分块：子块入库，父块作上下文写进 metadata
            splitter = get_chunker(parser_id, parser_config)
            result: ParentChildChunkResult = splitter.chunk_parent_child(text)
            chunk_objects = result.children
            parent_chunks = result.parents
            child_parent_map = result.child_parent_map
        elif parser_id == "hierarchical" and heading_levels is not None:
            # 层级聚合切块 + 标题分层层级表（3.6 步产出的外部层级表）：直接
            # 构造切块器（get_chunker 无外部层级表入口；未分层时走下方 else
            # 原路径，行为与之前一致）
            chunk_objects = self._build_hierarchical_chunker(
                parser_config, heading_levels).chunk(text)
        else:
            chunk_objects = get_chunker(parser_id, parser_config).chunk(text)

        # 4.5) 父标题前缀（enable_heading_in_content）：为不含标题的块拼接
        # 其前最近的标题链（如 "第一章 > 1.1"），块文本自带标题行则补全祖先；
        # 仅改块文本，char_start/char_end 保持原文偏移（归属/定位不受影响）
        # 原始块文本在加前缀前保留：父标题只用于展示，知识图谱实体偏移
        # 以原文为准（与 chunks_meta 偏移契约一致）
        # 父块（parent_text，retrieval_mode=parent 时检索返回的上下文）同样
        # 拼接标题链：父块全文常以「节标题」开头但缺「章标题」（如
        # "1.1 节标题" 开头而缺 "一、章标题"），LLM 无法
        # 定位章节归属 → 与子块一致处理
        # 标题编号体系（heading_systems）：解析产物标题多为 MinerU 全 ## 扁平
        # 输出，编号（"一、"/"1.1"）才是真实层级 → 自动检测后用于推断级别
        systems = parser_config.get("heading_systems") or []
        raw_chunk_texts: List[str] = [c.text for c in chunk_objects]
        # hierarchical 切块自带标题链（按章节树生成），此处不叠加，避免块首
        # 出现重复的祖先链前缀
        if (parser_config.get("enable_heading_in_content")
                and parser_id != "hierarchical"):
            chunk_objects = add_heading_paths(chunk_objects, text, systems)
            if parent_chunks:
                parent_chunks = add_heading_paths(parent_chunks, text, systems)

        return _IngestChunkStage(
            chunk_objects=chunk_objects, parent_chunks=parent_chunks,
            child_parent_map=child_parent_map,
            agentic_labels=agentic_labels, raw_chunk_texts=raw_chunk_texts)

    async def _stage_enhance(self, doc, doc_id: str, parser_config: dict,
                             stage: _IngestChunkStage,
                             text: str) -> Tuple[Dict[int, str],
                                                 Optional[str],
                                                 Optional[str]]:
        """增强阶段：上下文检索摘要 + 知识图谱（两者失败均不阻塞入库）。

        返回 (contexts, kg_status, kg_error)：kg_status=None 表示未开开关
        （graph_status 保持原值）；kg_error 仅 kg_status=failed 时非 None。
        """
        # 4.7) 上下文检索增强（contextual_retrieval）：切块后为每个块调用
        # LLM 生成简短上下文摘要（激活模型，并发限流 3，失败/超时跳过——
        # 单块失败绝不阻塞入库）。完整文档视角：解析文本 <= 系统配置
        # 阈值（contextual_retrieval.max_full_doc_chars，默认 2 万字，
        # 每次调用实时读配置不缓存）→ 完整文档作文档背景；超过阈值 →
        # enrich_chunks 抛 DocTooLongError（整文档超限 = 任务失败，区别于
        # 单块失败跳过），异常冒泡到任务外层写回 failed 带明确提示。
        # 摘要只用于向量化与检索展示：chunks_meta.text 保持原文（偏移
        # 契约不破坏），摘要存 chunks_meta.context 字段
        contexts: Dict[int, str] = {}
        if parser_config.get("contextual_retrieval"):
            for item in await self._await_with_cancel(doc_id, enrich_chunks(
                    stage.chunk_objects, text, parser_config,
                    doc_name=doc.original_name)):
                ctx = (item.get("context") or "").strip()
                if ctx:
                    contexts[int(item["index"])] = ctx
            logger.info("上下文摘要生成: %s (%d/%d 块)", doc.original_name,
                        len(contexts), len(stage.chunk_objects))

        # 4.8) 知识图谱（knowledge_graph）：切块后为每个块调用激活 LLM
        # 抽取实体与关系，合并进 data/storage/graphs/{kb_id}.json（幂等：
        # 重入库先清该文档旧引用再合并；失败/超时跳过对应块——绝不阻塞
        # 入库）。用原始块文本（父标题前缀只用于展示，实体偏移以原文为
        # 准，与 chunks_meta 偏移契约一致）。构建成功 → graph_status=ready，
        # 失败 → failed + graph_error（随 transition ingested 写回文档元数据）
        kg_status: Optional[str] = None  # None=未开开关（graph_status 保持原值）
        kg_error: Optional[str] = None
        if parser_config.get("knowledge_graph"):
            try:
                kg_stats = await self._await_with_cancel(
                    doc_id,
                    build_graph_for_doc(
                        doc.kb_id, doc_id, doc.original_name,
                        stage.chunk_objects, raw_texts=stage.raw_chunk_texts,
                        cfg=parser_config))
                if kg_stats.get("extracted"):
                    logger.info(
                        "知识图谱构建: %s (%s) 抽取 %d/%d 块 → 实体 %d / 关系 %d",
                        doc.original_name, doc_id, kg_stats["extracted"],
                        kg_stats["chunks"], kg_stats["entities"],
                        kg_stats["relations"])
                kg_status = "ready"
            except _IngestCancelled:
                raise  # 用户取消：保留取消语义（不落成"图谱失败"）
            except Exception as e:
                # 图谱构建失败不阻塞入库（与上下文检索增强同策略）
                logger.warning("知识图谱构建失败（不阻塞入库）: %s err=%s",
                               doc_id, str(e)[:150])
                kg_status = "failed"
                kg_error = str(e)[:500]
        return contexts, kg_status, kg_error

    async def _stage_vectorize(self, doc, doc_id: str, parser_id: str,
                               parser_config: dict,
                               contexts: Dict[int, str],
                               stage: _IngestChunkStage) -> None:
        """向量化 + 入库：空块校验 → 取消检查点2a → embedding →
        取消检查点2b → 维度校验 → 清旧向量 → 写入 + BM25 失效"""
        chunk_objects = stage.chunk_objects
        chunks: List[str] = [c.text for c in chunk_objects]
        if not chunks:
            raise RuntimeError("切块结果为空")

        # 取消检查点 2a：切块/图谱/摘要已完成、向量化开始前——命中则中止，
        # 跳过耗时的 embedding，立即结束（状态由 _IngestCancelled 写 failed）
        self._raise_if_cancelled(doc_id)

        # 5) 向量化 + 入库（入库前先清旧向量——对任何切块方式都执行，幂等）
        emb_svc = get_embedding_service()
        # 向量化文本：有摘要的块用 "【上下文】摘要\n原文"（检索质量提升的
        # 核心）；入库 Chroma documents 也用该增强文本——检索命中返回的
        # text 天然含摘要（引用/预览显示），BM25 索引与向量重建（collection
        # 保真路径）自动一致；chunks_meta.text 仍是原文（偏移契约不变）
        embed_texts = [
            f"【上下文】{contexts[i]}\n{c.text}" if i in contexts else c.text
            for i, c in enumerate(chunk_objects)
        ]
        embeddings = await self._await_with_cancel(doc_id,
                                                   emb_svc.embed(embed_texts))
        # 取消检查点 2b：嵌入完成后、向量写入前——命中则不写向量库
        # （嵌入白算可接受，向量库保持干净，不污染检索）
        self._raise_if_cancelled(doc_id)
        vec = get_vector_store()
        # 5.5) 维度冲突防护（P0）：collection 已有旧维度向量（更换 embedding
        # 模型后），新维度写入 Chroma 会报错；这里提前校验，失败中止入库并
        # 写回友好 error（而不是 add 报错后给晦涩异常）。校验在删旧向量之前，
        # 维度不匹配时不破坏已有向量。
        current_dim = await vec.get_embedding_dimension(doc.kb_id)
        if embeddings and current_dim is not None \
                and current_dim != len(embeddings[0]):
            raise VectorDimensionError(
                f"Embedding 模型维度不匹配（collection {current_dim} 维 vs "
                f"模型 {len(embeddings[0])} 维），请更换模型或重建向量")
        await vec.delete_by_document(doc.kb_id, doc_id)
        metadatas = []
        for i, c in enumerate(chunk_objects):
            meta = {
                "document_id": doc_id,
                "document_name": doc.original_name,
                "chunk_index": i,
                "char_start": c.char_start,
                "char_end": c.char_end,
            }
            # 上下文摘要随块入库（截断防 Chroma metadata 单值超限），
            # 检索时透传到 Source.context / 引用拼接
            if i in contexts:
                meta["context"] = contexts[i][:_CONTEXT_META_LIMIT]
            if parser_id == "parent_child":
                # 父块索引（-1 表示无父块，此时父块文本=子块自身）；
                # 父块全文 + 检索模式随子块入库，检索时直接读取
                parent_idx = stage.child_parent_map.get(i, -1)
                meta["parent_chunk_index"] = parent_idx
                # 父块全文随子块入库作检索上下文；章节父块可能数千字，
                # 按 8000 截断防 Chroma metadata 单值超限（展示侧另有 2000 截断）
                meta["parent_text"] = (
                    stage.parent_chunks[parent_idx].text
                    if 0 <= parent_idx < len(stage.parent_chunks)
                    else c.text)[:_PARENT_TEXT_META_LIMIT]
                meta["retrieval_mode"] = parser_config.get(
                    "retrieval_mode", "parent")
            metadatas.append(meta)
        await vec.add(doc.kb_id, doc_id, doc.original_name, embed_texts,
                      embeddings, metadatas=metadatas)
        # 混合检索 BM25 索引失效（下次检索自动重建；函数内导入防循环依赖）
        from backend.services.retrieval_service import get_retrieval_service
        get_retrieval_service().invalidate_bm25(doc.kb_id)

    async def _stage_finalize(self, doc, doc_id: str, parser_id: str,
                              parser_config: dict, parse_method: str,
                              contexts: Dict[int, str],
                              stage: _IngestChunkStage,
                              kg_status: Optional[str],
                              kg_error: Optional[str]) -> None:
        """完成阶段：解析配置持久化到文档元数据（重跑沿用）；
        chunks_meta 存完整列表（text+偏移，详情接口读），chunk_preview
        兼容保留；图谱状态随入库写回（kg_status=None 即开关关，保持文档
        原值不清空）。"""
        doc_svc = get_document_service()
        chunk_objects = stage.chunk_objects
        chunks: List[str] = [c.text for c in chunk_objects]
        # Agentic 超限确认标记为"本次提交确认"语义：入库前剔除，不持久化到
        # 文档 parser_config（重跑仍需再次确认；防旧配置带确认标记绕过校验）
        parser_config.pop("agentic_confirm", None)
        transition_kwargs = {
            "parse_method": parse_method,
            "chunk_count": len(chunks),
            "chunk_preview": [c[:_PREVIEW_CHAR] for c in chunks[:_PREVIEW_LIMIT]],
            "chunks_meta": [
                {"text": c.text, "char_start": c.char_start,
                 "char_end": c.char_end,
                 **({"context": contexts[i]} if i in contexts else {}),
                 **({"label": stage.agentic_labels[i]}
                    if i in stage.agentic_labels else {})}
                for i, c in enumerate(chunk_objects)],
            "parser_id": parser_id,
            "parser_config": parser_config,
        }
        if kg_status is not None:
            transition_kwargs["graph_status"] = kg_status
            if kg_status == "failed":
                transition_kwargs["graph_error"] = kg_error
        doc_svc.transition(doc_id, "ingested", **transition_kwargs)
        logger.info("入库完成: %s (%s) chunks=%d method=%s", doc.original_name,
                    doc_id, len(chunks), parser_id)


_ingestion_service: Optional[IngestionService] = None


def get_ingestion_service() -> IngestionService:
    global _ingestion_service
    if _ingestion_service is None:
        _ingestion_service = IngestionService()
    return _ingestion_service
