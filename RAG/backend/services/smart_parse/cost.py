"""成本预估：画像 + 方案 → CostEstimate（原始单位）

计量口径（2026-09-15 与用户确认）：
- **不做 token 换算**——同一个汉字在不同模型下 0.6~2 token（中文词表覆盖率
  不同，差 3 倍），拍一个系数估出来会误导人
- 主计量 = LLM **调用次数**（成本主因子：成本 = 次数 × 单价）
- 辅助 = 输入/输出字符数、图片张数
- token/金额留给展示层，用"本模型实测系数"换算（后续自校准，拿真实 usage
  除以实际字符数）

口径常量为什么在这里声明而不是 import：这些数字来自各环节实现模块
（contextual_retriever / knowledge_graph_service / agentic_chunker /
heading_llm），而它们 import 较重（contextual_retriever 会拖入 chat_service）。
所以此处声明同值常量，并由 tests/test_parse_plan.py 断言"与实现模块一致"
——改了一处不改另一处，测试会红。agentic 的两个上限在 ingestion.params
（轻量模块），直接引用。
"""
from __future__ import annotations

import logging
import math

from backend.config import get_active_config
from backend.services.ingestion.params import (_MAX_AGENTIC_TEXT_CHARS,
                                               _MAX_AGENTIC_TEXT_CHARS_HARD)
from backend.services.smart_parse.profiling import format_chars
from backend.services.smart_parse.types import CostEstimate, CostItem

logger = logging.getLogger(__name__)

# ---- 与实现同源的口径常量（test_parse_plan.py 断言一致性）----
# contextual_retriever._CHUNK_INPUT_CHARS：每块送模型的字符数（截断）
CTX_CHUNK_CHARS = 1000
# contextual_retriever._CONTEXT_MAX_CHARS：摘要输出硬截断
CTX_OUTPUT_CHARS = 100
# knowledge_graph_service._CHUNK_INPUT_CHARS：每块送模型的字符数（截断）
KG_CHUNK_CHARS = 1000
# 图谱每块的抽取结果（实体/关系 JSON）的量级估算：无硬截断，按经验取值
KG_OUTPUT_CHARS_PER_CHUNK = 300
# agentic_chunker._MAX_TEXT_CHARS：全文进模型的硬上限
AGENTIC_MAX_TEXT_CHARS = 50000
# normative.heading_llm._BATCH_SIZE：每批送多少个标题
HEADING_BATCH = 150
# 标题分层每批的输入量估算（提示词 + 150 行标题 + 批间上下文尾巴）
HEADING_BATCH_CHARS = 8000

# 兜底切块参数（活跃配置缺失时用，与 config.py 的 CHUNK_SIZE 默认一致）
_FALLBACK_CHUNK_SIZE = 800
_FALLBACK_OVERLAP = 100


def estimate_chunks(*, kind: str, profile: dict, config: dict) -> int:
    """预估切块数（成本推算的中间量，本身也是有用的画像）

    - 表格：Sheet 数即块数（每张表独立成块）
    - QA：问答对数量即块数（画像已有精确值）
    - 其余：按「有效步长 = chunk_size - overlap」估算（hierarchical 的聚合块
      通常少于该值，属上界）
    """
    if kind == "spreadsheet":
        n = (profile.get("spreadsheet") or {}).get("sheet_count") or 1
        return max(1, int(n))
    if (profile.get("qa") or {}).get("is_qa"):
        n = (profile.get("qa") or {}).get("qa_pairs") or 1
        return max(1, int(n))
    doc_chars = (profile.get("length") or {}).get("doc_chars") or 0
    if not doc_chars:
        return 1
    size = config.get("chunk_size") or _FALLBACK_CHUNK_SIZE
    overlap = config.get("overlap")
    if overlap is None:
        overlap = _FALLBACK_OVERLAP
    step = max(1, int(size) - int(overlap))
    return max(1, math.ceil(doc_chars / step))


def _ctx_item(*, profile: dict, chunks: int, enabled: bool) -> CostItem:
    """上下文检索增强：每块一次调用，**每次重发全文**（成本的放大项）"""
    length = profile.get("length") or {}
    doc_chars = int(length.get("doc_chars") or 0)
    threshold = int(length.get("threshold_chars") or 0)
    if threshold and doc_chars > threshold:
        # 超阈值 = 整文档任务失败（contextual_retriever 抛 DocTooLongError，
        # 异常冒泡写回 failed），不是"效果不佳"——所以是不可开启，不只是不推荐
        return CostItem(
            key="contextual_retrieval", label="上下文检索增强",
            calls=0, input_chars=0, output_chars=0, images=0, available=False,
            detail=(f"文档 {format_chars(doc_chars)} 超过完整文档阈值 "
                    f"{format_chars(threshold)}，开启会导致入库失败，不可开启"))
    per_call = min(doc_chars, threshold or doc_chars) + CTX_CHUNK_CHARS
    return CostItem(
        key="contextual_retrieval", label="上下文检索增强",
        calls=chunks, input_chars=chunks * per_call,
        output_chars=chunks * CTX_OUTPUT_CHARS, images=0,
        detail=(f"每块重发全文（≤{format_chars(threshold or doc_chars)}）"
                f"+ 块文本 {CTX_CHUNK_CHARS} 字 × {chunks} 块"))


def _kg_item(*, chunks: int, enabled: bool) -> CostItem:
    """知识图谱抽取：每块一次调用，只发块文本（比上下文检索便宜一个数量级）"""
    return CostItem(
        key="knowledge_graph", label="知识图谱抽取",
        calls=chunks, input_chars=chunks * KG_CHUNK_CHARS,
        output_chars=chunks * KG_OUTPUT_CHARS_PER_CHUNK, images=0,
        detail=f"每块抽取一次（块截断 {KG_CHUNK_CHARS} 字）× {chunks} 块")


def _agentic_item(*, profile: dict) -> CostItem:
    """Agentic 分块：整篇一次调用，输入全文、**输出≈输入**（逐字拷贝回填）"""
    doc_chars = int((profile.get("length") or {}).get("doc_chars") or 0)
    if doc_chars > _MAX_AGENTIC_TEXT_CHARS_HARD:
        return CostItem(
            key="agentic", label="Agentic 智能分块",
            calls=0, input_chars=0, output_chars=0, images=0, available=False,
            detail=(f"文档 {format_chars(doc_chars)} 超过 "
                    f"{format_chars(_MAX_AGENTIC_TEXT_CHARS_HARD)}，不支持 LLM 全量切分"))
    note = ""
    if doc_chars > _MAX_AGENTIC_TEXT_CHARS:
        note = (f"（{format_chars(_MAX_AGENTIC_TEXT_CHARS)}~"
                f"{format_chars(_MAX_AGENTIC_TEXT_CHARS_HARD)} 之间需逐文档确认）")
    return CostItem(
        key="agentic", label="Agentic 智能分块",
        calls=1, input_chars=doc_chars, output_chars=doc_chars, images=0,
        detail=(f"整篇一次调用，输入 {format_chars(doc_chars)}、"
                f"输出≈输入（逐字拷贝回填）{note}"))


def _image_item(*, profile: dict, cfg) -> CostItem:
    """图片摘要：每张图一次调用（视觉 token 按张计，与字符数无关）"""
    max_images = int(getattr(cfg.image_summary, "max_images", 0) or 0)
    refs = int((profile.get("length") or {}).get("image_refs") or 0)
    limit_note = f"，单文档上限 {max_images} 张" if max_images else "，不限张数"
    if refs:
        calls = min(refs, max_images) if max_images else refs
        detail = f"每张图一次调用（识别到 {refs} 张{limit_note}）"
    else:
        # 本地预览提取的文本通常不含图片引用（PDF/docx 的图要解析后才出现），
        # 此处只能给下限；实际张数取决于解析产物
        calls = 0
        detail = "张数取决于解析产物（本地预览无法预知），解析后按实际图片数调用"
    return CostItem(
        key="image_summary", label="图片摘要",
        calls=calls, input_chars=0, output_chars=0, images=calls,
        detail=detail)


def _heading_item(*, profile: dict, config: dict, engine: str,
                  cfg) -> CostItem:
    """标题分层（仅层级聚合切块用）：每 150 个标题一批，只对**无编号**的交 LLM"""
    if config.get("method") != "hierarchical":
        return CostItem(
            key="heading_levels", label="标题分层", calls=0, input_chars=0,
            output_chars=0, images=0, available=False,
            detail="仅「层级聚合切块」需要")
    if engine == "docx_struct":
        return CostItem(
            key="heading_levels", label="标题分层", calls=0, input_chars=0,
            output_chars=0, images=0, available=False,
            detail="结构解析的标题层级由 OOXML 直读、精确，无需 LLM 分层")
    model = (getattr(cfg.chunking, "heading_llm_model", "") or "").strip()
    if not model:
        return CostItem(
            key="heading_levels", label="标题分层", calls=0, input_chars=0,
            output_chars=0, images=0, available=False,
            detail="未配置「标题分层模型」→ 只用编号规则分层，0 次调用")
    headings = int((profile.get("structure") or {}).get("heading_count") or 0)
    calls = max(1, math.ceil(headings / HEADING_BATCH)) if headings else 0
    return CostItem(
        key="heading_levels", label="标题分层",
        calls=calls, input_chars=calls * HEADING_BATCH_CHARS, output_chars=0,
        images=0,
        detail=(f"每 {HEADING_BATCH} 个标题一批，按全部 {headings} 个标题估算"
                f"（带编号的走规则，实际调用更少）"))


def estimate(*, kind: str, profile: dict, config: dict,
             switches: dict, engine: str, cfg=None) -> CostEstimate:
    """组装成本预估：total_* 只对**已开启**项求和，未开启项保留"开启后"的量

    switches 里的键与 CostItem.key 对应（agentic / heading_levels 属于"方式"
    而非"开关"，由 config.method 与引擎决定是否计入）。
    """
    if cfg is None:
        cfg = get_active_config()
    chunks = estimate_chunks(kind=kind, profile=profile, config=config)
    items = [
        _ctx_item(profile=profile, chunks=chunks, enabled=True),
        _kg_item(chunks=chunks, enabled=True),
        _image_item(profile=profile, cfg=cfg),
        _agentic_item(profile=profile),
        _heading_item(profile=profile, config=config, engine=engine, cfg=cfg),
    ]
    # 已开启判定：开关项看 switches；方式项（agentic）看 method；
    # heading_levels 由 method+引擎推导（见 _heading_item 的 available 语义）
    enabled: dict[str, bool] = {}
    for it in items:
        if it.key == "agentic":
            enabled[it.key] = (config.get("method") == "agentic"
                               and it.available)
        elif it.key == "heading_levels":
            enabled[it.key] = it.calls > 0
        else:
            enabled[it.key] = bool(switches.get(it.key)) and it.available
    on = [it for it in items if enabled[it.key]]
    return CostEstimate(
        chunks=chunks,
        enabled=enabled,
        items=items,
        total_calls=sum(it.calls for it in on),
        total_input_chars=sum(it.input_chars for it in on),
        total_output_chars=sum(it.output_chars for it in on),
        total_images=sum(it.images for it in on),
        notes=[
            "计量为 LLM 调用次数与字符数（原始单位），不含 token 换算——"
            "各模型词表不同（同一汉字 0.6~2 token），token/金额按本模型实测系数换算",
            "字符数按本地预览提取的文本估算，实际解析产物（MinerU/OCR）可能更长",
            "图片张数取决于解析产物，本地预览通常无法预知",
        ])
