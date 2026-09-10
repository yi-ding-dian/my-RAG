"""入库参数校验：解析引擎 / 解析配置 / 切块参数的合法值与规范化

模块结构（原 backend/services/ingestion_service.py 按职责拆分而来，行为零变化）：
- 常量：切块参数范围（_MIN_*/_MAX_*）、枚举白名单（_VALID_*）、解析配置默认值
  （_DEFAULT_PARSER_CONFIG）与字段表（_PARSER_BOOL_FIELDS/_PARSER_PARSE_OPTS）
- resolve_parser_engine: 解析引擎联动（路由层与任务内同用）
- _validate_pages / resolve_parser_config: 参数解析与校验（路由层同步预校验
  与任务内校验同源，见 backend/services/ingestion/service.py）

调用方按具体子模块导入（如 `from backend.services.ingestion.params import
resolve_parser_config`）。
"""
from __future__ import annotations

from typing import Tuple

from backend.chunking import VALID_METHODS
from backend.config import get_active_config
from backend.models.rag_models import DocumentItem

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

# 解析引擎（parsers.client.parse 的 engine 参数）：
# auto=自动（MinerU 优先，不可用降级；layout_recognize=DeepDOC 时走 DeepDoc、
# layout_recognize=PlainText 时走纯文本直提，见 resolve_parser_engine）/
# mineru=强制 MinerU（不可用标 failed）/ deepdoc=强制 DeepDoc（RAGFlow，
# 表格输出为可检索 HTML；仅 PDF）/ docx_struct=本地结构化解析（OOXML 直读，
# 标题层级/自动编号保留；本地实现无服务依赖，doc 先经 LibreOffice 转 docx）/
# plain=纯文本提取
_VALID_PARSER_ENGINES = ("auto", "mineru", "deepdoc", "docx_struct", "plain")
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
# DeepSeek 思考模式（LLM thinking 控制，图谱抽取/上下文摘要共用）：
# disabled=关闭思考（默认：图谱抽取/摘要属简单延迟敏感任务，关闭加速并节省
# token）| enabled_low/enabled_high/enabled_max=开启思考并指定强度
# （extra_body 组装见 knowledge_graph_service.build_thinking_extra_body）
_VALID_THINKING_MODES = ("disabled", "enabled_low", "enabled_high", "enabled_max")
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
    "knowledge_graph": False,            # 知识图谱（切块后为每个块调用 LLM 抽取实体与关系，产生额外 token 费用）
    "thinking_mode": "disabled",         # 思考模式（DeepSeek thinking 控制，图谱抽取/上下文摘要共用；默认关闭加速省 token）
    "parse_llm_model": "",               # 解析 LLM 模型（上下文摘要/图谱抽取专用，值为激活档案模型列表的 name；空=用当前激活模型，对话不受影响）
}
# 布尔字段名（类型校验；注意 bool 是 int 子类，需单独判型）
_PARSER_BOOL_FIELDS = ("table_enable", "formula_enable", "return_images",
                       "enable_heading_in_content", "contextual_retrieval",
                       "knowledge_graph")
# 透传给 parsers.client.parse 的字段（enable_heading_in_content 是切块后处理，不传解析器；
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
    - parser_engine 解析引擎：auto/mineru/deepdoc/docx_struct/plain（默认 auto，
      非法 400），随 parser_config 持久化，重跑沿用；auto + layout_recognize=
      DeepDOC 时自动走 DeepDoc 引擎、auto + layout_recognize=PlainText 时自动走
      plain 纯文本直提（统一见 resolve_parser_engine，_ingest 与路由层同用）；
      docx_struct=本地结构化解析（OOXML 直读，仅 docx/doc；无服务依赖不降级）
    - 解析配置（新字段，全部可选，默认见 _DEFAULT_PARSER_CONFIG）：
      layout_recognize（MinerU/DeepDOC/PlainText，非法 400）、pages
      （[[from,to],...]，from/to>=1 且 from<=to，非法 400）、task_page_size
      （1~128，越界 400）、lang_list（ch/en，非法 400）、
      table_enable/formula_enable/return_images/enable_heading_in_content
      （布尔类型校验）；随 parser_config 持久化，重跑沿用
    """
    params = params or {}
    method = method or doc.parser_id
    if not method:
        # 缺省切块方式按解析引擎配套：结构解析（docx_struct）产物标题层级精确
        # （# 数量即真实层级），默认用层级聚合切块；其他解析产物层级不可靠
        # （OCR 类解析常输出全 ## 扁平结构），沿用通用递归切块
        engine = (params.get("parser_engine")
                  or (doc.parser_config or {}).get("parser_engine") or "auto")
        method = "hierarchical" if engine == "docx_struct" else "naive"
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
    # 默认改 pipeline（2026-09-09）：本环境 MinerU 服务未配 GPU device，
    # hybrid-auto-engine 报"Device string must not be empty"不可用，pipeline 实测可用；
    # 未显式传且文档无旧配置 → 按新默认 pipeline（写入 cfg 生效，重跑沿用）
    backend = params.get("backend", old.get("backend"))
    if backend is None:
        backend = "pipeline"
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
        elif key == "thinking_mode":
            if value not in _VALID_THINKING_MODES:
                raise ValueError(
                    f"thinking_mode 非法: {value}"
                    f"（支持: {'/'.join(_VALID_THINKING_MODES)}）")
        elif key in _PARSER_BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是布尔值（true/false）")
        cfg[key] = value
    # agentic 与上下文检索增强互斥（用户约束）：选 agentic 强制关闭上下文
    # 检索增强——前端互斥逻辑 + 后端双保险，防 API 直调/重跑沿用文档旧配置
    # 产生组合；知识图谱不互斥（切块后处理，与切块方式无关），可叠加选择
    if method == "agentic":
        cfg["contextual_retrieval"] = False
    # Agentic 分块超限确认标记（仅 method=agentic 生效）：只读请求显式传
    # （params），不读文档旧配置——避免旧配置持久化的确认标记绕过未来校验；
    # True 时作为临时键放 cfg（_ingest 校验处读取，入库前剔除不持久化）
    if method == "agentic" and params.get("agentic_confirm") is not None:
        confirm = params.get("agentic_confirm")
        if not isinstance(confirm, bool):
            raise ValueError("agentic_confirm 必须是布尔值（true/false）")
        if confirm:
            cfg["agentic_confirm"] = True
    return method, cfg

# Agentic 智能分块文本长度两档上限（用户约束：LLM 全量读取成本随长度
# 上升——1 万~5 万字需前端确认后继续（agentic_confirm=true），超过 5 万字
# 直接拒绝；校验在解析出文本后、切块前：1 万~5 万字未确认 → 文档进入
# pending_confirm 待确认状态（不算失败，error 带提示），确认后重提入库）
_MAX_AGENTIC_TEXT_CHARS = 10000
_MAX_AGENTIC_TEXT_CHARS_HARD = 50000
