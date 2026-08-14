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
  contextual_retrieval，全部可选（默认见 _DEFAULT_PARSER_CONFIG），校验非法
  400；解析器参数透传 parser_client.parse（table_enable/formula_enable/
  return_images/lang_list/pages/backend），enable_heading_in_content 走切块后
  处理 add_heading_paths（块前缀标题路径）；contextual_retrieval（上下文检索
  增强，默认关）走切块后处理 enrich_chunks：为每个块用激活 LLM 生成上下文
  摘要（失败/超时跳过不阻塞入库），向量化用 "【上下文】摘要\n原文"，摘要存
  chunks_meta.context / 向量 metadata（截断 500），chunks_meta.text 保持原文
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
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from backend.chunking.splitter import (VALID_METHODS, Chunk, QaStats,
                                       add_heading_paths, analyze_qa_format,
                                       get_chunker, is_qa_format_valid,
                                       ParentChildChunkResult)
from backend.config import get_active_config
from backend.models.rag_models import DocumentItem
from backend.services.contextual_retriever import enrich_chunks
from backend.services.document_service import get_document_service
from backend.services.embedding_service import get_embedding_service
from backend.services.dim_check import VectorDimensionError
from backend.services.parser_client import get_parser_client
from backend.services.parser_probe import probe_parsers
from backend.services.parser_images import rewrite_image_refs
from backend.services.storage_service import get_storage_service
from backend.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)

# 后台入库任务并发上限（默认 3，环境变量 INGEST_CONCURRENCY 可调）：
# 批量解析同时打爆 MinerU/embedding 时限制并行任务数。
# 信号量惰性创建并绑定首次 acquire 时的运行事件循环（asyncio.Semaphore
# 3.10+ 懒绑定 loop）；conftest 重置 _ingest_semaphore 避免跨测试 loop 串用。
_INGEST_CONCURRENCY = max(1, int(os.environ.get("INGEST_CONCURRENCY", "3")))
_ingest_semaphore: Optional[asyncio.Semaphore] = None


def _get_ingest_semaphore() -> asyncio.Semaphore:
    """惰性获取全局入库信号量（首次调用绑定当前事件循环）"""
    global _ingest_semaphore
    if _ingest_semaphore is None:
        _ingest_semaphore = asyncio.Semaphore(_INGEST_CONCURRENCY)
    return _ingest_semaphore

# 切块参数合法范围（任务书契约：chunk_size 50~20000，title split_level 1~3，
# parent_child 父块参数 parent_chunk_size 200~4000 / parent_chunk_overlap 0~500 /
# parent_split_level 1~6）
_MIN_CHUNK_SIZE = 50
_MAX_CHUNK_SIZE = 20000
_MIN_SPLIT_LEVEL = 1
_MAX_SPLIT_LEVEL = 3
_MIN_PARENT_CHUNK_SIZE = 200
_MAX_PARENT_CHUNK_SIZE = 4000
_MIN_PARENT_CHUNK_OVERLAP = 0
_MAX_PARENT_CHUNK_OVERLAP = 500
_MIN_PARENT_SPLIT_LEVEL = 1
_MAX_PARENT_SPLIT_LEVEL = 6
# 检索模式：parent=命中返回父块全文作上下文 / child=仅返回子块
_VALID_RETRIEVAL_MODES = ("parent", "child")
# 入库 metadata 单值上限：父块全文随子块写进 Chroma metadata，章节父块可能
# 数千字，截断防超限报错（检索展示侧另有 2000 截断，见 chat_service，这里只管入库）
_PARENT_TEXT_META_LIMIT = 8000
# 上下文摘要 metadata 单值上限（Chroma metadata 单值限制；摘要本体在
# chunks_meta.context，这里存的是检索侧副本，截断不影响展示）
_CONTEXT_META_LIMIT = 500
# 解析引擎（parser_client.parse 的 engine 参数）：
# auto=自动（MinerU 优先，不可用降级；layout_recognize=DeepDOC 时走 DeepDoc、
# layout_recognize=PlainText 时走纯文本直提，见 resolve_parser_engine）/
# mineru=强制 MinerU（不可用标 failed）/ deepdoc=强制 DeepDoc（RAGFlow，
# 表格输出为可检索 HTML；仅 PDF）/ plain=纯文本提取
_VALID_PARSER_ENGINES = ("auto", "mineru", "deepdoc", "plain")
# MinerU 解析后端（mineru-api /file_parse backend 参数，实测对比见
# mcp-server/kb-ext-server/record.md）：
# hybrid-auto-engine=混合自动引擎（服务端默认，质量优：表格规范/OCR 准/流程图识别，
# 速度慢约 29-36%）/ pipeline=管线（快约 20s，表格可能错乱）
# auto（或 None）=跟随服务端默认：不持久化、不透传（与默认行为完全一致）
_VALID_MINERU_BACKENDS = ("auto", "hybrid-auto-engine", "pipeline")
# 父块参数默认值（与 KnowFlow parent_child 默认一致）
_DEFAULT_PARENT_CHUNK_SIZE = 1024
_DEFAULT_PARENT_CHUNK_OVERLAP = 100
_DEFAULT_PARENT_SPLIT_LEVEL = 2

# ---- 解析配置（parser_config 新字段：版面/页码/任务页大小/表格/公式/图片/语言/父标题）----
# 集中管理（默认值 + 合法范围 + 校验），未来扩展解析器参数只需改这里
_VALID_LAYOUT_RECOGNIZE = ("MinerU", "DeepDOC", "PlainText")
_VALID_LANG_LIST = ("ch", "en")
_MIN_TASK_PAGE_SIZE = 1
_MAX_TASK_PAGE_SIZE = 128
_DEFAULT_PARSER_CONFIG = {
    "layout_recognize": "MinerU",        # 版面识别（MinerU=默认/DeepDOC=表格输出可检索 HTML/PlainText=纯文本直提，均已生效）
    "pages": [[1, 1000000]],             # 页码范围 [[from,to],...]（默认全量）
    "task_page_size": 12,                # 任务页大小（存配置，当前单任务解析，主要给 MinerU 分页参考）
    "table_enable": True,                # 表格识别开关（MinerU）
    "formula_enable": True,              # 公式识别开关（MinerU）
    "return_images": True,               # 图片提取开关（True 时 MinerU 返回图片→存 MinIO，False 不提取）
    "lang_list": "ch",                   # 语言 ch/en（MinerU lang_list）
    "enable_heading_in_content": False,  # 包含父标题（切块后为不含标题的块拼接前缀标题路径）
    "contextual_retrieval": False,       # 上下文检索增强（切块后为每个块调用 LLM 生成上下文摘要，产生额外 token 费用）
}
# 布尔字段名（类型校验；注意 bool 是 int 子类，需单独判型）
_PARSER_BOOL_FIELDS = ("table_enable", "formula_enable", "return_images",
                       "enable_heading_in_content", "contextual_retrieval")
# 透传给 parser_client.parse 的字段（enable_heading_in_content 是切块后处理，不传解析器；
# backend 仅在显式选择 hybrid-auto-engine/pipeline 时存在于 parser_config，auto/None 不写入）
_PARSER_PARSE_OPTS = ("table_enable", "formula_enable", "return_images",
                      "lang_list", "pages", "backend")


def resolve_parser_engine(parser_config: dict) -> str:
    """解析引擎解析（前端版面识别联动的后端镜像，路由层/任务内同用）：
    显式 parser_engine 优先；engine=auto 时按 layout_recognize 联动——
    DeepDOC→deepdoc（表格输出可检索 HTML）、PlainText→plain（纯文本直提，
    pypdf/python-docx，无表格/图片识别，无需探测降级）"""
    engine = parser_config.get("parser_engine", "auto")
    if engine == "auto":
        if parser_config.get("layout_recognize") == "DeepDOC":
            return "deepdoc"
        if parser_config.get("layout_recognize") == "PlainText":
            return "plain"
    return engine


def _validate_pages(pages) -> list:
    """页码范围校验：[[from,to],...]，from/to 为整数 >=1 且 from<=to；返回规范化列表

    非法（非列表/空/元素非 [from,to]/非整数/越界）抛 ValueError（上层 400 或 failed）
    """
    if not isinstance(pages, list) or not pages:
        raise ValueError("pages 必须是 [[from,to],...] 列表且至少包含一组")
    out = []
    for group in pages:
        if not isinstance(group, (list, tuple)) or len(group) != 2:
            raise ValueError(f"pages 每组须为 [from,to] 两个元素: {group}")
        frm, to = group[0], group[1]
        if isinstance(frm, bool) or isinstance(to, bool) \
                or not isinstance(frm, int) or not isinstance(to, int):
            raise ValueError(f"pages 的 from/to 必须是整数: {group}")
        if frm < 1 or to < 1 or frm > to:
            raise ValueError(f"pages 须满足 from>=1、to>=1、from<=to: {group}")
        out.append([frm, to])
    return out


def resolve_parser_config(doc: DocumentItem, method: str | None = None,
                          params: dict | None = None) -> Tuple[str, dict]:
    """解析切块方式与参数（校验失败抛 ValueError，由调用方决定 400 或写回 failed）

    优先级：请求显式传 > 文档已有 parser_config（重跑沿用）> 默认（活跃配置）
    - method 缺省时沿用 doc.parser_id，再缺省为 naive
    - regex 必须有 regex_pattern；chunk_size 限 50~20000；overlap 需小于 chunk_size
    - parent_child 父块参数：parent_chunk_size 200~4000 / parent_chunk_overlap
      0~500 / parent_split_level 1~6（越界 400）；retrieval_mode 仅 parent/child
      （默认 parent，所有方式都持久化到 parser_config，检索时写进向量 metadata）
    - parser_engine 解析引擎：auto/mineru/deepdoc/plain（默认 auto，非法 400），
      随 parser_config 持久化，重跑沿用；auto + layout_recognize=DeepDOC 时
      自动走 DeepDoc 引擎、auto + layout_recognize=PlainText 时自动走 plain
      纯文本直提（统一见 resolve_parser_engine，_ingest 与路由层同用）
    - 解析配置（新字段，全部可选，默认见 _DEFAULT_PARSER_CONFIG）：
      layout_recognize（MinerU/DeepDOC/PlainText，非法 400）、pages
      （[[from,to],...]，from/to>=1 且 from<=to，非法 400）、task_page_size
      （1~128，越界 400）、lang_list（ch/en，非法 400）、
      table_enable/formula_enable/return_images/enable_heading_in_content
      （布尔类型校验）；随 parser_config 持久化，重跑沿用
    """
    params = params or {}
    method = method or doc.parser_id or "naive"
    if method not in VALID_METHODS:
        raise ValueError(f"非法切块方式: {method}（支持: {'/'.join(VALID_METHODS)}）")
    old = doc.parser_config or {}
    active = get_active_config().chunking
    cfg: dict = {}
    # 解析引擎：请求显式传 > 文档已有配置（重跑沿用）> 默认 auto
    parser_engine = params.get("parser_engine", old.get("parser_engine", "auto"))
    if parser_engine not in _VALID_PARSER_ENGINES:
        raise ValueError(
            f"parser_engine 非法: {parser_engine}"
            f"（支持: {'/'.join(_VALID_PARSER_ENGINES)}）")
    cfg["parser_engine"] = parser_engine
    # MinerU 解析后端（仅 MinerU 引擎生效）：请求显式传 > 文档已有配置（重跑沿用）；
    # None/auto 语义=跟随服务端默认：不写入 cfg（不持久化、不透传），
    # 显式传 "auto" 可重置上次持久化的 backend（新配置覆盖旧值）
    backend = params.get("backend", old.get("backend"))
    if backend is not None:
        if backend not in _VALID_MINERU_BACKENDS:
            raise ValueError(
                f"backend 非法: {backend}"
                f"（支持: {'/'.join(_VALID_MINERU_BACKENDS)}，None=跟随服务端默认）")
        if backend != "auto":
            cfg["backend"] = backend
    # 块大小 / 重叠：请求 > 已有配置 > 活跃配置
    chunk_size = params.get("chunk_size", old.get("chunk_size", active.chunk_size))
    if not _MIN_CHUNK_SIZE <= chunk_size <= _MAX_CHUNK_SIZE:
        raise ValueError(
            f"chunk_size 超出范围: {chunk_size}（需 {_MIN_CHUNK_SIZE}~{_MAX_CHUNK_SIZE}）")
    cfg["chunk_size"] = chunk_size
    overlap = params.get("overlap", old.get("overlap", active.chunk_overlap))
    if not 0 <= overlap < chunk_size:
        raise ValueError(f"overlap 非法: {overlap}（需 0 <= overlap < chunk_size）")
    cfg["overlap"] = overlap
    if method == "naive":
        delimiter = params.get("delimiter", old.get("delimiter"))
        if delimiter:
            cfg["delimiter"] = delimiter
    elif method == "title":
        split_level = params.get("split_level", old.get("split_level", 3))
        if not _MIN_SPLIT_LEVEL <= split_level <= _MAX_SPLIT_LEVEL:
            raise ValueError(
                f"split_level 超出范围: {split_level}（需 {_MIN_SPLIT_LEVEL}~{_MAX_SPLIT_LEVEL}）")
        cfg["split_level"] = split_level
    elif method == "regex":
        pattern = params.get("regex_pattern", old.get("regex_pattern"))
        if not pattern or not str(pattern).strip():
            raise ValueError("正则切块需提供 regex_pattern")
        cfg["regex_pattern"] = pattern
    elif method == "parent_child":
        # 父块大小 / 重叠 / 标题层级：请求 > 已有配置 > 默认（KnowFlow 默认值）
        parent_chunk_size = params.get(
            "parent_chunk_size", old.get("parent_chunk_size",
                                         _DEFAULT_PARENT_CHUNK_SIZE))
        if not _MIN_PARENT_CHUNK_SIZE <= parent_chunk_size <= _MAX_PARENT_CHUNK_SIZE:
            raise ValueError(
                f"parent_chunk_size 超出范围: {parent_chunk_size}"
                f"（需 {_MIN_PARENT_CHUNK_SIZE}~{_MAX_PARENT_CHUNK_SIZE}）")
        cfg["parent_chunk_size"] = parent_chunk_size
        parent_chunk_overlap = params.get(
            "parent_chunk_overlap", old.get("parent_chunk_overlap",
                                            _DEFAULT_PARENT_CHUNK_OVERLAP))
        if not _MIN_PARENT_CHUNK_OVERLAP <= parent_chunk_overlap <= _MAX_PARENT_CHUNK_OVERLAP:
            raise ValueError(
                f"parent_chunk_overlap 超出范围: {parent_chunk_overlap}"
                f"（需 {_MIN_PARENT_CHUNK_OVERLAP}~{_MAX_PARENT_CHUNK_OVERLAP}）")
        cfg["parent_chunk_overlap"] = parent_chunk_overlap
        parent_split_level = params.get(
            "parent_split_level", old.get("parent_split_level",
                                          _DEFAULT_PARENT_SPLIT_LEVEL))
        if not _MIN_PARENT_SPLIT_LEVEL <= parent_split_level <= _MAX_PARENT_SPLIT_LEVEL:
            raise ValueError(
                f"parent_split_level 超出范围: {parent_split_level}"
                f"（需 {_MIN_PARENT_SPLIT_LEVEL}~{_MAX_PARENT_SPLIT_LEVEL}）")
        cfg["parent_split_level"] = parent_split_level
    # 检索模式（所有方式通用，默认 parent；仅 parent_child 入库时写进向量 metadata）
    retrieval_mode = params.get("retrieval_mode", old.get("retrieval_mode", "parent"))
    if retrieval_mode not in _VALID_RETRIEVAL_MODES:
        raise ValueError(
            f"retrieval_mode 非法: {retrieval_mode}"
            f"（支持: {'/'.join(_VALID_RETRIEVAL_MODES)}）")
    cfg["retrieval_mode"] = retrieval_mode

    # ---- 解析配置（解析器参数：布局/页码/任务页大小/表格/公式/图片/语言/父标题）----
    # 优先级同前：请求显式传 > 文档已有配置（重跑沿用）> 默认（_DEFAULT_PARSER_CONFIG）
    for key, default in _DEFAULT_PARSER_CONFIG.items():
        value = params.get(key, old.get(key, default))
        if key == "layout_recognize":
            if value not in _VALID_LAYOUT_RECOGNIZE:
                raise ValueError(
                    f"layout_recognize 非法: {value}"
                    f"（支持: {'/'.join(_VALID_LAYOUT_RECOGNIZE)}）")
        elif key == "lang_list":
            if value not in _VALID_LANG_LIST:
                raise ValueError(
                    f"lang_list 非法: {value}"
                    f"（支持: {'/'.join(_VALID_LANG_LIST)}）")
        elif key == "task_page_size":
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not _MIN_TASK_PAGE_SIZE <= value <= _MAX_TASK_PAGE_SIZE:
                raise ValueError(
                    f"task_page_size 超出范围: {value}"
                    f"（需 {_MIN_TASK_PAGE_SIZE}~{_MAX_TASK_PAGE_SIZE}）")
        elif key == "pages":
            value = _validate_pages(value)
        elif key in _PARSER_BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是布尔值（true/false）")
        cfg[key] = value
    return method, cfg

# 切块预览上限
_PREVIEW_LIMIT = 20
_PREVIEW_CHAR = 500


class IngestionService:

    def __init__(self):
        self._running: Set[str] = set()  # 正在入库的 doc_id，防并发重复触发

    def is_running(self, doc_id: str) -> bool:
        return doc_id in self._running

    async def run_ingestion(self, doc_id: str, method: str | None = None,
                            **params):
        """后台任务主流程（路由层 asyncio.create_task 调用）

        method/params: 切块方式与参数（不传则沿用文档已有配置或默认，
        见 resolve_parser_config）；校验失败（非法 method / regex 无 pattern /
        参数越界）在任务内写回 status=failed + error（路由层同步 400 双保险）
        并发上限：模块级信号量（INGEST_CONCURRENCY，默认 3），
        批量解析不会同时打爆 MinerU/embedding 服务。
        """
        # 信号量在整个任务期间持有（含排队等待），并发解析数不会超过上限
        async with _get_ingest_semaphore():
            await self._run_ingestion_locked(doc_id, method=method, **params)

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
            self._running.discard(doc_id)

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
        try:
            # 1.5) 原始文件从对象存储下载到 data/uploads/（供解析器读 Path）
            storage = get_storage_service()
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
            if engine != "plain" and file_type in ("pdf", "docx"):
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
            text, images, parse_method = await parser.parse(
                upload_path, doc.file_type,
                engine=engine,
                **parse_opts)
            if not text or not text.strip():
                raise RuntimeError(
                    "解析结果为空（扫描版 PDF 或无文本内容），请检查解析服务后重试")

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

            # 3) 解析文本落盘 data/parsed/{doc_id}.md
            # （新流程无 parsed 中间态：解析+入库一步完成，直接到 ingested）
            parsed_path = doc_svc.get_parsed_path(doc)
            parsed_path.write_text(text, encoding="utf-8")
            logger.info("解析完成: %s (%s) %d 字符%s", doc.original_name,
                        parse_method, len(text),
                        f"，图片 {len(images)} 张" if images else "")

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

            # 4) 切块（按用户选择的切块方式与参数；用替换后文本保证图片引用可加载）
            splitter = get_chunker(parser_id, parser_config)
            chunk_objects: List[Chunk] = []
            parent_chunks: List[Chunk] = []
            child_parent_map: Dict[int, int] = {}
            if parser_id == "parent_child":
                # 父子分块：子块入库，父块作上下文写进 metadata
                result: ParentChildChunkResult = splitter.chunk_parent_child(text)
                chunk_objects = result.children
                parent_chunks = result.parents
                child_parent_map = result.child_parent_map
            else:
                chunk_objects = splitter.chunk(text)

            # 4.5) 父标题前缀（enable_heading_in_content）：为不含标题的块拼接
            # 其前最近的标题链（如 "第一章 > 1.1"），块文本自带标题行则跳过；
            # 仅改块文本，char_start/char_end 保持原文偏移（归属/定位不受影响）
            if parser_config.get("enable_heading_in_content"):
                chunk_objects = add_heading_paths(chunk_objects, text)

            # 4.7) 上下文检索增强（contextual_retrieval）：切块后为每个块调用
            # LLM 生成简短上下文摘要（激活模型，并发限流 3，失败/超时跳过——
            # 绝不阻塞入库）。摘要只用于向量化与检索展示：chunks_meta.text
            # 保持原文（偏移契约不破坏），摘要存 chunks_meta.context 字段
            contexts: Dict[int, str] = {}
            if parser_config.get("contextual_retrieval"):
                for item in await enrich_chunks(
                        chunk_objects, text, parser_config,
                        doc_name=doc.original_name):
                    ctx = (item.get("context") or "").strip()
                    if ctx:
                        contexts[int(item["index"])] = ctx
                logger.info("上下文摘要生成: %s (%d/%d 块)", doc.original_name,
                            len(contexts), len(chunk_objects))

            chunks: List[str] = [c.text for c in chunk_objects]
            if not chunks:
                raise RuntimeError("切块结果为空")

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
            embeddings = await emb_svc.embed(embed_texts)
            vec = get_vector_store()
            # 5.5) 维度冲突防护（P0）：collection 已有旧维度向量（更换 embedding
            # 模型后），新维度写入 Chroma 会报错；这里提前校验，失败中止入库并
            # 写回友好 error（而不是 add 报错后给晦涩异常）。校验在删旧向量之前，
            # 维度不匹配时不破坏已有向量。
            current_dim = vec.get_embedding_dimension(doc.kb_id)
            if embeddings and current_dim is not None \
                    and current_dim != len(embeddings[0]):
                raise VectorDimensionError(
                    f"Embedding 模型维度不匹配（collection {current_dim} 维 vs "
                    f"模型 {len(embeddings[0])} 维），请更换模型或重建向量")
            vec.delete_by_document(doc.kb_id, doc_id)
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
                    parent_idx = child_parent_map.get(i, -1)
                    meta["parent_chunk_index"] = parent_idx
                    # 父块全文随子块入库作检索上下文；章节父块可能数千字，
                    # 按 8000 截断防 Chroma metadata 单值超限（展示侧另有 2000 截断）
                    meta["parent_text"] = (
                        parent_chunks[parent_idx].text if 0 <= parent_idx < len(parent_chunks)
                        else c.text)[:_PARENT_TEXT_META_LIMIT]
                    meta["retrieval_mode"] = parser_config.get("retrieval_mode", "parent")
                metadatas.append(meta)
            vec.add(doc.kb_id, doc_id, doc.original_name, embed_texts,
                    embeddings, metadatas=metadatas)
            # 混合检索 BM25 索引失效（下次检索自动重建；函数内导入防循环依赖）
            from backend.services.retrieval_service import get_retrieval_service
            get_retrieval_service().invalidate_bm25(doc.kb_id)

            # 6) 完成：解析配置持久化到文档元数据（重跑沿用）；
            # chunks_meta 存完整列表（text+偏移，详情接口读），chunk_preview 兼容保留
            doc_svc.transition(
                doc_id, "ingested",
                parse_method=parse_method,
                chunk_count=len(chunks),
                chunk_preview=[c[:_PREVIEW_CHAR] for c in chunks[:_PREVIEW_LIMIT]],
                chunks_meta=[
                    {"text": c.text, "char_start": c.char_start,
                     "char_end": c.char_end,
                     **({"context": contexts[i]} if i in contexts else {})}
                    for i, c in enumerate(chunk_objects)],
                parser_id=parser_id,
                parser_config=parser_config,
            )
            logger.info("入库完成: %s (%s) chunks=%d method=%s", doc.original_name,
                        doc_id, len(chunks), parser_id)
        except Exception as e:
            logger.exception("入库失败: %s (%s)", doc_id, e)
            doc_svc.mark_failed(doc_id, str(e))

    async def _upload_images(self, doc, text: str, images: List[dict]) -> str:
        """上传有字节的解析图片，替换 markdown 引用为鉴权代理 URL；返回替换后文本

        - images 形态 [{name, data: bytes}]（parser_client 归一化产物）
        - 无字节的图片（仅文件名/解码失败）不上传、不替换（保留原引用，不阻塞入库）
        - 串行上传（大图批量 201 张/16MB 量级，并发无收益），每 50 张打进度日志
        - 上传失败仅 warning（图片缺失不阻断文本入库）
        """
        storage = get_storage_service()
        mapping: dict = {}
        uploaded = 0
        total = len(images)
        for i, img in enumerate(images, start=1):
            name = img.get("name") or ""
            data = img.get("data")
            if not data:
                continue
            # 文件名取 basename（MinerU 可能带 images/ 前缀），避免 key 层级逃逸
            base_name = Path(name).name if name else "image"
            if not base_name:
                continue
            key = f"images/{doc.id}/{base_name}"
            try:
                await storage.upload_bytes(key, data)
                mapping[base_name] = f"/api/files/images/{doc.id}/{base_name}"
                uploaded += 1
                if uploaded % 50 == 0:
                    logger.info("解析图片上传进度: %s (%d/%d 张)",
                                doc.id, uploaded, total)
            except Exception as e:
                logger.warning("解析图片上传失败 %s: %s", key, str(e)[:150])
        if uploaded:
            logger.info("解析图片上传完成: %s 共 %d 张", doc.id, uploaded)
        if not mapping:
            return text
        rewritten = rewrite_image_refs(text, mapping)
        if rewritten != text:
            logger.info("图片引用已替换: %s (%d 处)", doc.id, len(mapping))
        return rewritten


_ingestion_service: Optional[IngestionService] = None


def get_ingestion_service() -> IngestionService:
    global _ingestion_service
    if _ingestion_service is None:
        _ingestion_service = IngestionService()
    return _ingestion_service
