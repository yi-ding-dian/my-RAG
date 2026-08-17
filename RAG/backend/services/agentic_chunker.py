"""Agentic 智能分块：LLM 读全文自主判断完整逻辑段落并切割，每块附类型标签

- 适用：文档解析文本 ≤5 万字（1 万~5 万字需带 agentic_confirm 确认，
  超过 5 万字在 ingestion 层校验拒绝——成本太高）；LLM 单次调用读全文，输出 JSON
  {"chunks": [{"text": "块原文", "label": "论述类"}]}
- 标签白名单：论述类/事实类/操作类/数据类/其他（白名单外归"其他"宽容处理）
- 偏移对齐：LLM 输出块文本需在原文定位（align_chunks 纯函数，供测试直测）：
  逐块顺序推进（从上块结束处之后找，防错位匹配），三级策略——
  1) 精确匹配；2) 折叠空白匹配（LLM 可能改写换行/空白）；
  3) 前缀 seed + 滑动窗口 difflib 最长匹配（块文本被模型改写/截断时）。
  对齐失败块丢弃（warning 日志）；全部失败 → AgenticChunkError
  （上层 ingestion 回退 title 切块）
- 标题保留双保险：① prompt 要求块开头必须含所属章节标题行；
  ② 对齐成功后 restore_heading_prefix 兜底——LLM 丢标题时把块起点
  前移并入其前最近的连续标题行链（复用 splitter._iter_headings 识别），
  块 text 始终为原文切片（偏移契约保持）
- 失败语义：LLM 未配置 / 单次调用超时（默认 120s）/ 调用异常 /
  响应解析失败 / 对齐全失败 → AgenticChunkError（上层回退，绝不阻塞入库）
- 思考关闭策略复用 thinking_strategy：get_thinking_strategy(llm_cfg,
  thinking_mode)——在线 DeepSeek → extra_body 关闭思考；本地 LM Studio
  Qwen → messages 末尾注入空 <think> 块跳过思考（LM Studio 0.4.x 忽略
  extra_body 的 thinking 参数），与图谱抽取/上下文摘要同款策略
- LLM 客户端模式参照 contextual_retriever：独立 AsyncOpenAI（key 比对
  自动重建）；parser_config.parse_llm_model 指定时用该模型（从激活档案
  模型列表查完整配置覆盖，查不到回退激活模型）
- 偏移契约：返回块 text == 原文[char_start:char_end]（用原文切片重建，
  不直接用 LLM 输出文本），chunks_meta 偏移/检索定位以原文为基准
"""
from __future__ import annotations

import asyncio
import bisect
import difflib
import json
import logging
import re
from typing import List, Optional, Tuple

from openai import AsyncOpenAI

from backend.chunking.splitter import (
    Chunk, _iter_headings, find_protected_ranges)
from backend.config import get_active_config
from backend.services.chat_service import _llm_to_dict
from backend.services.settings_service import llm_cfg_for_parser
from backend.services.thinking_strategy import get_thinking_strategy

logger = logging.getLogger(__name__)

# 文档文本长度硬上限（防御校验：ingestion 层两档校验——1 万~5 万字带
# agentic_confirm 确认可通过，超过 5 万字拒绝；此处兜底防调用方漏校验）
_MAX_TEXT_CHARS = 50000
# 每块长度约束（提示词要求 LLM 每块 ≤1500 字）
_MAX_CHUNK_CHARS = 1500
# 单次调用 max_tokens：块文本需逐字拷贝输出（LLM 输出块文本总和接近
# 全文），给足输出余量（DeepSeek 输出上限 8192）；仍截断时靠偏移对齐
# 的模糊匹配容忍，截断块前缀仍可定位
_MAX_TOKENS = 8192
# 单次 LLM 调用超时（秒）：读全文 + 输出全部块文本较重，120s 给足余量
_TIMEOUT = 120.0
# 标签白名单（提示词 + 归一化共用）
_VALID_LABELS = ("论述类", "事实类", "操作类", "数据类", "其他")
# 模糊匹配最小覆盖比例：最长匹配长度 / 块长度低于该值视为定位失败
_MATCH_MIN_RATIO = 0.5
# 前缀 seed 长度（模糊匹配用，取块折叠空白后前 N 字符）
_SEED_CHARS = 30
# seed 候选上限（seed 太常见时限制候选数防退化）
_MAX_SEED_CANDIDATES = 5

# 注意：本 prompt 经 .format(text=...) 格式化，JSON 示例中的花括号必须
# 转义为 {{ }}（否则 format 会把 {"chunks" 当作字段名 → KeyError）
_AGENTIC_PROMPT = (
    "你是文档切分助手。请通读下面的完整文档，按【完整逻辑段落】自主划分成若干块。\n"
    "【规则】\n"
    "1. 每块必须是一个语义完整的逻辑段落（观点论证 / 事实陈述 / 操作步骤 / 数据说明），\n"
    "   不得把无关内容拼接在一起，也不要把一个完整逻辑段落拆散；\n"
    "2. 每个逻辑段落块的开头必须包含其所属的章节标题行（如 '## 第二章 xxx'、\n"
    "   '### 2.1 xxx'），标题行是段落的一部分、不能省略；输出块文本 = 原文逐字\n"
    "   拷贝（含标题行），标题行 + 该标题下第一个完整逻辑段落作为一块；\n"
    "   无标题的正文块保持原样；\n"
    "3. 每块不超过 {max_chunk} 字；块数尽量少（不要碎块化）；\n"
    "4. 块文本必须逐字拷贝原文（不改写、不概括、不增删字词，标点符号保持原样）；\n"
    "5. 每块打一个标签，只能从以下类型中选择：\n"
    "   论述类（观点、分析、论证、评价）；事实类（客观事实、背景、历史沿革）；\n"
    "   操作类（步骤、操作方法、配置指引）；数据类（数据、统计、指标说明）；\n"
    "   其他（不适合以上分类的内容）；\n"
    "6. 所有块按文档顺序排列，完整覆盖文档全部内容，不要遗漏也不要重复。\n"
    "【输出格式】只输出 JSON，不要任何多余文字、解释或代码块标记：\n"
    '{{"chunks":[{{"text":"块原文","label":"论述类"}}]}}\n\n'
    "文档全文：\n{text}"
)

# ```json 围栏剥离（部分模型习惯用围栏包裹 JSON）
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.S)


class AgenticChunkError(Exception):
    """Agentic 切块失败（LLM 未配置/超时/调用失败/解析失败/对齐全失败），
    由 ingestion 层捕获回退 title 切块"""


# ==================== 偏移对齐（纯函数，供测试直测） ====================

def _build_collapsed(text: str) -> Tuple[str, List[int]]:
    """去空白后的文本与偏移映射：(flat, flat_pos)

    flat = 原文去掉所有空白字符（isspace）后的连续串；
    flat_pos[i] = flat 第 i 个字符在原文中的偏移（单调递增）。
    供折叠空白匹配用：LLM 输出块可能改写换行/缩进等空白。
    """
    flat: List[str] = []
    flat_pos: List[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            flat.append(ch)
            flat_pos.append(i)
    return "".join(flat), flat_pos


def _locate_collapsed(flat: str, flat_pos: List[int], flat_needle: str,
                      start: int) -> Optional[Tuple[int, int]]:
    """折叠空白匹配：在原文（flat 形态）中找 flat_needle，返回原文偏移区间

    start 为原文偏移（上次对齐结束处）；flat_pos 单调递增 → 二分定位
    start 在 flat 中的下标，从该处向后找，保证顺序推进不回头。
    """
    fi = bisect.bisect_left(flat_pos, start)
    f_idx = flat.find(flat_needle, fi)
    if f_idx == -1:
        return None
    return flat_pos[f_idx], flat_pos[f_idx + len(flat_needle) - 1] + 1


def _locate_fuzzy(text: str, flat: str, flat_pos: List[int], needle: str,
                  start: int) -> Optional[Tuple[int, int]]:
    """前缀 seed 定位 + 滑动窗口 difflib 最长匹配（块文本被模型改写/截断时）

    - seed = 块折叠空白后的前 _SEED_CHARS 字符（最多 5 个候选防退化）；
    - 以 seed 命中点为候选中心，在原文 ± 窗口内用 SequenceMatcher 求
      needle 与窗口的最长匹配（autojunk=False 防长度启发式误判）；
    - 匹配长度 / 块长度 >= _MATCH_MIN_RATIO 才接受，且起点必须 >= start
      （顺序推进，防与前块重叠/回头）；
    - 返回原文区间（块文本以原文切片重建，偏移契约成立）
    """
    flat_needle = "".join(ch for ch in needle if not ch.isspace())
    if len(flat_needle) < 8:
        return None  # 块太短，模糊匹配无意义
    # seed 逐级缩短（30→20→10→5 中 < 块长的级别）：短块直接取全量 seed，
    # 长块前缀可能含被改写字符时用更短前缀仍可定位（改写通常在中后部）
    seed_lens = sorted({s for s in (_SEED_CHARS, 20, 10, 5)
                        if s < len(flat_needle)}, reverse=True)
    if not seed_lens:
        seed_lens = [len(flat_needle)]
    fi = bisect.bisect_left(flat_pos, start)
    candidates: List[int] = []
    for slen in seed_lens:
        seed = flat_needle[:slen]
        f_idx = flat.find(seed, fi)
        while f_idx != -1 and len(candidates) < _MAX_SEED_CANDIDATES:
            candidates.append(f_idx)
            f_idx = flat.find(seed, f_idx + 1)
        if len(candidates) >= _MAX_SEED_CANDIDATES:
            break
    best: Optional[Tuple[int, int, int]] = None  # (匹配长度, s, e)
    for f_idx in candidates:
        center = flat_pos[f_idx]
        win_start = max(0, center - len(needle))
        win_end = min(len(text), center + 2 * len(needle))
        window = text[win_start:win_end]
        matcher = difflib.SequenceMatcher(None, needle, window, autojunk=False)
        m = matcher.find_longest_match(0, len(needle), 0, len(window))
        if m.size <= 0 or m.size / len(needle) < _MATCH_MIN_RATIO:
            continue
        s, e = win_start + m.b, win_start + m.b + m.size
        if s < start:
            continue  # 与前块重叠/回头 → 不取
        if best is None or m.size > best[0]:
            best = (m.size, s, e)
    if best is None:
        return None
    return best[1], best[2]


# 对齐失败日志截断
_CHUNK_LOG_CHARS = 50


def align_chunks(original_text: str, llm_chunks: List[str]) -> List[Tuple[int, int, int]]:
    """LLM 输出块 → 原文偏移对齐（纯函数，供测试直测）

    - 逐块顺序推进（每块从上一块结束处之后定位，防错位匹配）；
    - 三级策略：精确匹配 → 折叠空白匹配（LLM 改写换行/空白）
      → 前缀 seed + 滑动窗口 difflib 最长匹配（改写/截断）；
    - 定位失败的块丢弃（返回不含该块），返回 [(start, end, 块下标)] 升序；
    - 全部失败返回 []（调用方 agentic_chunk 抛 AgenticChunkError 触发回退）
    """
    if not llm_chunks:
        return []
    flat, flat_pos = _build_collapsed(original_text)
    results: List[Tuple[int, int, int]] = []
    cursor = 0
    for i, raw in enumerate(llm_chunks):
        needle = (raw or "").strip()
        if not needle:
            continue  # 空块丢弃
        span = None
        # 1) 精确匹配（原文）
        idx = original_text.find(needle, cursor)
        if idx != -1:
            span = (idx, idx + len(needle))
        else:
            # 2) 折叠空白匹配
            flat_needle = "".join(ch for ch in needle if not ch.isspace())
            if flat_needle:
                span = _locate_collapsed(flat, flat_pos, flat_needle, cursor)
            # 3) 前缀 seed + 滑动窗口模糊匹配
            if span is None:
                span = _locate_fuzzy(original_text, flat, flat_pos,
                                     needle, cursor)
        if span is None:
            logger.warning("Agentic 切块偏移对齐失败，丢弃块#%d: %r…",
                           i, needle[:_CHUNK_LOG_CHARS])
            continue
        s, e = span
        cursor = e
        results.append((s, e, i))
    return results


# 标题链并入上限行数（块起点前连续标题行最多并入行数，防过度扩展）
_MAX_HEADING_PATH_LINES = 3


def _line_end(text: str, start: int) -> int:
    """start 所在行结束偏移（不含换行符）；start 越界返回 len(text)"""
    nl = text.find("\n", start)
    return nl if nl != -1 else len(text)


def restore_heading_prefix(original_text: str,
                           aligned: List[Tuple[int, int, int]]
                           ) -> List[Tuple[int, int, int]]:
    """标题归属兜底：对齐成功后把块起点前移，并入所属章节标题行

    - 背景：LLM 可能把标题行当作"非逻辑段落内容"丢弃，导致每个块不含
      所属章节标题（如 '## 第二章 xxx'、'### 2.1 xxx'）——本函数在
      align_chunks 对齐成功后调用，双保险之一（另一重是 prompt 约束）；
    - 标题行识别复用 splitter._iter_headings（ATX # / setext / 包裹式 /
      前导符号式统一识别，protected 过滤表格与代码块内的伪标题）；
    - 对每个块（s 为块起点）：取 s 之前最近的标题行；满足全部条件才扩展——
      ① 块起点本身是标题行（块已以标题行开头）→ 不动；
      ② 块文本已含该标题行内容（折叠空白比较，LLM 已保留标题）→ 不动；
      ③ 标题行已被前一块覆盖（同一标题已并入前一块，后续同章节块不再
         重复并入）→ 不动；
    - 需要扩展时：把块起点前"连续标题行链"整体并入——从最近标题行向上，
      标题行两两之间只允许空行（直接相邻或空行间隔），最多
      _MAX_HEADING_PATH_LINES 行（如 '## 第二章' + '### 2.1' 两级并入，
      层级链思路参照 splitter.add_heading_paths）；
    - 扩展仅把 char_start 前移到链首标题行起点，end 不变；text 由调用方
      按原文切片重建（偏移契约 text == 原文[char_start:char_end] 保持）；
      与前一扩展块重叠的标题行不并入（防块区间交叉）
    - 返回 [(start, end, 块下标)]，仅 start 可能前移，顺序与输入一致
    """
    if not aligned or not original_text:
        return aligned
    headings = _iter_headings(
        original_text, find_protected_ranges(original_text))
    if not headings:
        return aligned
    h_starts = [h[0] for h in headings]
    result: List[Tuple[int, int, int]] = []
    prev_end = 0  # 上一块处理后的 end（用于防重复并入 / 防区间交叉）
    for s, e, src_idx in aligned:
        # ① 块起点本身就是标题行 → 块已含所属标题，不动
        pos = bisect.bisect_left(h_starts, s)
        if pos < len(h_starts) and h_starts[pos] == s:
            result.append((s, e, src_idx))
            prev_end = e
            continue
        # 块起点之前最近的标题行
        j = bisect.bisect_left(h_starts, s) - 1
        if j < 0:
            result.append((s, e, src_idx))
            prev_end = e
            continue
        h_start = h_starts[j]
        # ③ 标题行已被前一块覆盖（同一标题已并入前一块）→ 不动
        if h_start < prev_end:
            result.append((s, e, src_idx))
            prev_end = e
            continue
        # ② 块文本已含该标题行内容（LLM 已保留标题）→ 不动
        h_line = original_text[h_start:_line_end(original_text, h_start)]
        flat_block = "".join(ch for ch in original_text[s:e]
                             if not ch.isspace())
        if not h_line or "".join(h_line.split()) in flat_block:
            result.append((s, e, src_idx))
            prev_end = e
            continue
        # 向上收集连续标题行链（两两之间只允许空行，且不得与前一块重叠，
        # 最多 _MAX_HEADING_PATH_LINES 行）
        chain: List[int] = [h_start]
        k = j - 1
        while k >= 0 and len(chain) < _MAX_HEADING_PATH_LINES:
            if h_starts[k] < prev_end:
                break  # 更早标题已被前一块覆盖 → 不再并入
            gap = original_text[
                _line_end(original_text, h_starts[k]):h_starts[k + 1]]
            if gap.strip():
                break  # 标题行之间有正文 → 链断开
            chain.append(h_starts[k])
            k -= 1
        result.append((chain[-1], e, src_idx))
        prev_end = e
    return result


# ==================== LLM 客户端（key 比对自动重建） ====================

_client: Optional[AsyncOpenAI] = None
_client_key: Optional[str] = None


def _get_client(llm_cfg: Optional[dict] = None) -> AsyncOpenAI:
    """按 LLM 配置 key 比对自动重建客户端（与 contextual_retriever 同款模式）"""
    global _client, _client_key
    if llm_cfg is None:
        llm_cfg = _llm_to_dict(get_active_config().llm)
    key = json.dumps(llm_cfg, sort_keys=True, ensure_ascii=False)
    if _client is None or _client_key != key:
        _client = AsyncOpenAI(
            base_url=llm_cfg.get("base_url", ""),
            api_key=llm_cfg.get("api_key", ""),
            timeout=float(llm_cfg.get("timeout") or 60),
        )
        _client_key = key
    return _client


# ==================== 响应解析（纯函数） ====================

def _parse_response(content: str) -> Optional[dict]:
    """LLM 响应解析：直接 json.loads → 剥 ```json 围栏 → 剥多余前缀文字

    失败返回 None（调用方抛 AgenticChunkError；围栏/前后缀是模型常见
    输出形态，宽容处理）
    """
    s = (content or "").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    m = _JSON_FENCE_RE.search(s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    # 模型在 JSON 前后加说明文字：截取第一个 { 到最后一个 }
    lo, hi = s.find("{"), s.rfind("}")
    if 0 <= lo < hi:
        try:
            return json.loads(s[lo:hi + 1])
        except Exception:
            pass
    return None


def normalize_label(label) -> str:
    """标签归一化：白名单内原样返回，白名单外归"其他"（宽容处理不丢块）"""
    s = str(label or "").strip()
    return s if s in _VALID_LABELS else "其他"


# ==================== 主入口 ====================

async def agentic_chunk(text: str, cfg: Optional[dict] = None,
                        timeout: float = _TIMEOUT) -> Tuple[List[Chunk], List[str]]:
    """Agentic 智能分块：LLM 读全文切逻辑段落，返回 (chunks, labels)

    - text: 文档解析文本（调用方已保证 ≤5 万字，此处再防御校验）
    - cfg: parser_config（thinking_mode/parse_llm_model 生效；
      parse_llm_model 指定时用该模型，空/查不到回退激活模型）
    - timeout: 单次调用超时（秒，测试可缩小）
    - 失败语义：LLM 未配置/超时/调用异常/响应解析失败/空块列表/对齐全
      失败 → 抛 AgenticChunkError（ingestion 层捕获回退 title 切块）
    - 返回块按原文切片重建（text == 原文[char_start:char_end]，偏移契约），
      labels 与返回块一一对应（对齐失败块连同其标签一起丢弃）
    """
    cfg = cfg or {}
    text = (text or "").strip()
    if len(text) > _MAX_TEXT_CHARS:
        raise AgenticChunkError(
            "文档超过 5 万字，不支持 Agentic 分块，请换用其他切块方式")
    if not text:
        raise AgenticChunkError("文档文本为空，无法 Agentic 分块")

    # 解析 LLM 模型：cfg.parse_llm_model 指定（Agentic 分块专用模型，
    # 从激活档案模型列表查完整配置）→ 覆盖；未指定/查不到 → 激活模型
    llm_cfg = _llm_to_dict(get_active_config().llm)
    override = llm_cfg_for_parser(cfg.get("parse_llm_model"))
    if override:
        llm_cfg = {**llm_cfg, **override}
    if not (llm_cfg.get("base_url") and llm_cfg.get("model")):
        raise AgenticChunkError("LLM 未配置（base_url/model 为空），"
                                "无法 Agentic 分块")

    # 思考关闭策略（与图谱抽取/上下文摘要同款）：在线 DeepSeek → extra_body
    # 关闭思考；本地 LM Studio Qwen → messages 末尾注入空 <think> 块
    strategy = get_thinking_strategy(llm_cfg, cfg.get("thinking_mode"))
    client = _get_client(llm_cfg)
    payload = {
        "messages": [
            {"role": "system", "content": "你是文档切分助手。"},
            {"role": "user", "content": _AGENTIC_PROMPT.format(
                text=text, max_chunk=_MAX_CHUNK_CHARS)},
        ],
    }
    strategy.apply(payload)
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=llm_cfg.get("model") or "",
                messages=payload["messages"],
                max_tokens=_MAX_TOKENS,
                temperature=0.0,
                extra_body=payload.get("extra_body"),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise AgenticChunkError(
            f"Agentic 分块 LLM 调用超时（>{timeout:g}s），回退其他切块方式")
    except Exception as e:
        raise AgenticChunkError(
            f"Agentic 分块 LLM 调用失败: {str(e)[:150]}")

    try:
        content = (resp.choices[0].message.content or "").strip()
    except Exception:
        content = ""
    data = _parse_response(content)
    if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
        raise AgenticChunkError("Agentic 分块 LLM 返回格式非法（无 chunks 数组）")

    llm_chunks: List[str] = []
    labels: List[str] = []
    for item in data["chunks"]:
        if not isinstance(item, dict):
            continue
        block = str(item.get("text") or "").strip()
        if not block:
            continue  # 空块丢弃
        llm_chunks.append(block)
        labels.append(normalize_label(item.get("label")))
    if not llm_chunks:
        raise AgenticChunkError("Agentic 分块 LLM 返回空块列表")

    # 偏移对齐：失败块丢弃（连同标签）；全部失败 → 抛错触发回退
    aligned = align_chunks(text, llm_chunks)
    if not aligned:
        raise AgenticChunkError("Agentic 分块偏移对齐全失败，回退其他切块方式")
    # 标题归属兜底：LLM 丢弃标题行时把块起点前移并入所属标题
    # （偏移契约验证在构建 Chunk 前，修复后仍严格一致）
    aligned = restore_heading_prefix(text, aligned)
    chunks: List[Chunk] = []
    out_labels: List[str] = []
    for s, e, src_idx in aligned:
        chunks.append(Chunk(text=text[s:e], char_start=s, char_end=e))
        out_labels.append(labels[src_idx])
    logger.info("Agentic 分块完成: %d 块（LLM 输出 %d 块，丢弃 %d 块）",
                len(chunks), len(llm_chunks), len(llm_chunks) - len(chunks))
    return chunks, out_labels
