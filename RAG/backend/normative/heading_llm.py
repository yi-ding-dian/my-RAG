"""给解析产物重新分层（MinerU 全 `##` 输出的层级还原）

背景：MinerU 解析 PDF 时把所有标题统一输出为 `##`（markdown 层级信息丢失）
——真实层级藏在标题**编号**里（如「一、」是章、「1.1」是节、「1.1.1」是小节），
不带编号的标题里还混着伪标题（正文中的强调行被误判成标题，如「注意事项:」
「安装流程:」）。本模块提供两个对外入口：
- layer_headings：**全量**交给 LLM 逐标题判定（level 1~6 = 大纲层级，
  level 0 = 不是标题，应作为正文处理）；
- hybrid_levels：**混合方案**——带编号的标题按编号规则确定性定级（零成本），
  无编号的交给 LLM 判（清单里附「前文最近的带编号标题及其层级」作定位参考）。
  实测（627 标题的技术手册）全量要 19833 token / 252 秒，混合只用 1/4 左右的
  成本且层级一致率很高——带编号那批规则本来就能算，没必要问模型。

设计要点：
- 标题提取复用 chunking.common._iter_headings（ATX + 纯文本标题样式 +
  protected 表格/代码块过滤），本模块只读复用、不改切块逻辑；
- 分批判定：每批 _BATCH_SIZE 个标题，批间把上批末尾几条标题及其判定结果
  拼进下一批提示词末尾（+「承接上批，请沿用同一套层级标准」），保证跨批
  层级标准一致（LLM 无线索时容易逐批重新定标）；
- 校验（任一不过 → 重试 1 次；仍不过 → 整批作废、不抛异常）：条数一致 /
  序号恰为 {1..N} / level 为 0~6 整数 / 返回对象不得含 i、level 以外的字段
  （防 LLM 复述或改写标题文本）。作废批回退「规则口径」层级
  （_iter_headings 编号推断），并在返回值里标记为未判定；
- 偏移契约：items 的 offset 直接取自 _iter_headings 的标题行起始偏移，
  LLM 只回「序号 + 层级」，标题文本全程不经 LLM 之手；
- 失败语义参照 agentic_chunker：LLM 未配置 → HeadingLLMError（调用方决定
  是否回退）；单批超时/调用异常/解析失败 → 该批未判定，不阻断其余批次。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from backend.chunking import find_protected_ranges
from backend.chunking.common import _iter_headings
from backend.chunking.heading_presets import (detect_heading_systems,
                                              order_systems)
from backend.config import LLMConfig, get_active_config
from backend.services.llm_client import (LLMRequestError, LLMTimeoutError,
                                         get_llm_client, llm_completion,
                                         llm_to_dict)
from backend.services.settings.service import llm_cfg_for_parser
from backend.services.thinking_strategy import get_thinking_strategy

logger = logging.getLogger(__name__)

# 每批标题数（分批防单次上下文/输出过长；743 个标题 → 5 批）
_BATCH_SIZE = 150
# 批间上下文条数（把上批末尾 N 条标题及其判定结果拼进下一批提示词）
_CONTEXT_TAIL = 3
# 单次调用 max_tokens（输出是「序号 + 层级」短 JSON，每条约 20 字符；
# 150 条约 1500 tokens，8192 留足余量，且不超过模型配置的 max_tokens）
_MAX_TOKENS = 8192
# 单批调用超时（秒）：本地模型输出 150 条 JSON 较慢，给足余量
_TIMEOUT = 240.0
# 校验不过的重试次数（1 次后仍不过 → 整批作废回退）
_MAX_RETRY = 1

# 提示词拼装：经 .format(items=...) 格式化，JSON 示例中的花括号必须转义为
# {{ }}（否则 format 会把 {"i":1 当作字段名 → KeyError）；两套方案共用「判定
# 依据 + 输出要求」，只有开头说明与清单格式不同（全量 / 混合）
_PROMPT_HEAD = (
    "你是文档结构分析助手。下面是一份技术手册从 PDF 提取出的标题清单。\n"
    "\n"
    "提取时层级信息丢失了——所有标题都被标记成同一级，但它们实际上分属\n"
    "不同层级。请为每个标题重新判定它在文档中的真实大纲层级。"
)

# 判定依据（全量方案与混合方案共用）：第 1 条是硬要求——编号是显式层级信号，
# 关键词只是启发式（实测缺陷：真章节标题「12.1.5 绘图注意事项」被"注意事项"
# 这个关键词误伤判成正文行）；混合方案下带编号的标题走规则、不经过 LLM，
# 但这条仍要保留——防止 LLM 用同样的错误标准推断无编号标题的上下文
_PROMPT_RULES = (
    "\n\n判断依据：\n"
    "1. 编号优先：带编号的标题一定是章节标题，编号优先于关键词判断——\n"
    "   「1.1.1 注意事项」是章节标题，不因出现“注意事项”就被判成正文行；\n"
    "2. 编号点分段数：「一、」=1 级，「1.1」=2 级，「1.1.1」=3 级，「3.3.1.5」=4 级；\n"
    "3. 编号的递进与归属：同级编号连续递进（1.1→1.2→1.3），下级编号归属于其\n"
    "   前缀所示节点（1.1.1 属于 1.1）；\n"
    "4. 多套编号混用：中文数字（一、二、三）通常层级较高，阿拉伯点分编号\n"
    "   （1.1）通常在其之下；\n"
    "5. 不是章节标题的行：**不带编号**的正文强调行/操作步骤/列表项（以冒号\n"
    "   结尾、描述操作细节或注意事项，如「注意事项:」「安装流程:」），层级填 0，\n"
    "   表示它不是标题、应作为正文处理。"
)

# 输出要求（两套方案共用；混合方案的清单里没有"[上级参考]"行——那些行没有
# 序号，模型只需为「序号 标题文本」行输出条目，故要求里点明"输入序号"）
_PROMPT_TAIL = (
    "\n\n输出要求（严格遵守）：\n"
    "- 只输出 JSON 数组：[{{\"i\":1,\"level\":1}},{{\"i\":2,\"level\":2}},...]\n"
    "- i = 输入序号，必须完整覆盖全部行，不增、不减、保持原顺序\n"
    "- level = 1~6 的整数（大纲层级），或 0（不是标题）\n"
    "- 不要输出任何解释、注释或额外文字\n"
    "- 不要复述或修改标题文本"
)

_LAYER_PROMPT = (
    _PROMPT_HEAD
    + _PROMPT_RULES
    + "\n\n输入格式：每行「序号 标题文本」。"
    + _PROMPT_TAIL
    + "\n\n标题清单：\n{items}"
)

# 混合方案提示词：清单里全是不带编号的标题候选（伪标题集中在这里），故额外
# 说明"它们可能不是标题"，并给出定位参考行（见 build_hybrid_items）
_HYBRID_PROMPT = (
    _PROMPT_HEAD
    + "\n"
    "\n"
    "本清单里的行**都不带编号**（带编号的标题已由规则另行定级，不在清单里），\n"
    "而且混着大量伪标题——正文里的强调行、操作步骤、列表项被误当成标题提取\n"
    "出来了（如「注意事项:」「安装流程:」「(1) 规约特性」）。"
    + _PROMPT_RULES
    + "\n\n定位参考：清单里以「[上级参考]」开头的行不是待判标题，它给出紧随其后"
      "\n标题的**层级定位线索**——上文最近标题及其层级（带编号的标题层级已由规则"
      "\n定好，可直接当标尺；标注“不带编号”的则要你看清单里那一行的判定，与之保持"
      "\n一致）。同一条参考行覆盖它下面直到下一条参考行为止的所有标题。"
    + "\n\n输入格式：每行「序号 标题文本」，标题前给出所在上级的参考行。"
    + _PROMPT_TAIL
    + "\n\n标题清单（「[上级参考]」行没有序号、不要为它们输出条目）：\n{items}"
)

# ```json 围栏剥离（部分模型习惯用围栏包裹 JSON）
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.S)


class HeadingLLMError(Exception):
    """分层失败（LLM 未配置），由调用方决定是否回退其他切块方式"""


@dataclass
class BatchStat:
    """单批判定统计（诊断/成本核算用）"""
    index: int                     # 批次序号（0 起）
    first: int                     # 本批首标题在标题列表中的下标
    count: int                     # 本批标题数
    ok: bool                       # 判定成功（校验通过）→ False = 未判定
    retries: int                   # 重试次数
    error: str                     # 未判定原因（ok=True 时为空串）
    prompt_tokens: int             # 输入 token（服务端返回，缺省 0）
    completion_tokens: int         # 输出 token
    elapsed: float                 # 本批总耗时（秒，含重试）
    raw: str = ""                  # 最后一次响应开头（诊断用，截断）


@dataclass
class HeadingLayerResult:
    """分层结果：items 与 headings 一一对应（同序同长）"""
    items: List[Dict[str, int]]        # [{"offset": 标题行起始偏移, "level": 0~6}]
    headings: List[Tuple[int, int, str]]  # [(偏移, 原层级, 标题文本)]
    batches: List[BatchStat] = field(default_factory=list)
    systems: List[str] = field(default_factory=list)  # 规则口径用的编号体系

    @property
    def levels(self) -> List[int]:
        """各标题层级（与 headings 同序）"""
        return [it["level"] for it in self.items]

    @property
    def failed_batches(self) -> List[int]:
        """未判定（整批作废回退规则口径）的批次序号"""
        return [b.index for b in self.batches if not b.ok]

    @property
    def token_usage(self) -> Tuple[int, int]:
        """(输入 token, 输出 token) 合计"""
        return (sum(b.prompt_tokens for b in self.batches),
                sum(b.completion_tokens for b in self.batches))


@dataclass
class HybridLevelResult:
    """混合层级表结果：items 与 headings 一一对应（同序同长）

    levels = 最终层级表（带编号的取规则定级、无编号的取 LLM 判定），可直接
    喂给 HierarchicalChunker(heading_levels=...)。
    """
    items: List[Dict[str, int]]            # [{"offset": 标题行起始偏移, "level": 0~6}]
    headings: List[Tuple[int, int, str]]   # [(偏移, 原层级, 标题文本)]
    families: List[Optional[str]] = field(default_factory=list)  # 规则编号家族
    batches: List[BatchStat] = field(default_factory=list)
    systems: List[str] = field(default_factory=list)  # 回退口径用的编号体系
    elapsed: float = 0.0                   # 本函数总耗时（秒，规则 + LLM 全部）

    @property
    def levels(self) -> List[int]:
        """各标题层级（与 headings 同序）——即外部层级表"""
        return [it["level"] for it in self.items]

    @property
    def rule_indexes(self) -> List[int]:
        """规则定级的标题下标（带编号，未经过 LLM）"""
        return [i for i, f in enumerate(self.families) if f is not None]

    @property
    def llm_indexes(self) -> List[int]:
        """交 LLM 判定的标题下标（无编号/弱编号）"""
        return [i for i, f in enumerate(self.families) if f is None]

    @property
    def failed_batches(self) -> List[int]:
        """未判定（整批作废回退规则口径）的批次序号"""
        return [b.index for b in self.batches if not b.ok]

    @property
    def token_usage(self) -> Tuple[int, int]:
        """(输入 token, 输出 token) 合计"""
        return (sum(b.prompt_tokens for b in self.batches),
                sum(b.completion_tokens for b in self.batches))


# ==================== 标题提取 + 规则回退口径（纯函数） ====================

def _line_end(text: str, start: int) -> int:
    """start 所在行结束偏移（不含换行符）；start 越界返回 len(text)"""
    nl = text.find("\n", start)
    return nl if nl != -1 else len(text)


# markdown ATX 标题行（'# 标题'）识别（_iter_headings 之外的纯文本标题样式
# 不属于本模块目标：本模块面向「全 ##」的 PDF 解析产物）
_ATX_RE = re.compile(r"^#{1,6}\s+")


def extract_headings(text: str, atx_only: bool = False,
                     ) -> Tuple[List[Tuple[int, int, str]], List[Tuple[int, int]]]:
    """提取标题：[(偏移, 原层级, 标题文本)] + 保护区间（只读复用 _iter_headings）

    - 默认：_iter_headings 全量结果（ATX # 标题 + 纯文本标题样式，protected
      过滤表格/代码块内的伪标题行），与切块侧口径一致——LLM 判定结果要喂回
      切块链路，口径必须同源；
    - atx_only=True：只取 markdown ATX 标题行（`^#{1,6}\\s+`），用于确认
      纯文本样式（setext/包裹式/前导符号式）带来的差异。
    """
    protected = find_protected_ranges(text)
    headings = _iter_headings(text, protected)
    if atx_only:
        headings = [(off, lv, title) for off, lv, title in headings
                    if _ATX_RE.match(text[off:_line_end(text, off)])]
    return headings, protected


def rule_levels(text: str, headings: List[Tuple[int, int, str]],
                protected: List[Tuple[int, int]],
                systems: Optional[List[str]] = None) -> List[int]:
    """规则口径层级（未判定批的回退值）：编号推断级别（_iter_headings）

    无可用的编号体系（全文无编号标题）或标题未命中编号时，回退该标题的
    原始层级（ATX # 数量）。
    """
    if not systems:
        systems = detect_systems(headings)
    rule = {off: lv for off, lv, _t in _iter_headings(text, protected, systems)}
    return [rule.get(off, lv) for off, lv, _t in headings]


def detect_systems(headings: List[Tuple[int, int, str]]) -> List[str]:
    """自动检测文档用到的编号体系（检测结果 → 语义优先级排序）"""
    det = detect_heading_systems([t for _o, _l, t in headings])
    return order_systems([d["system"] for d in det])


# ==================== 混合方案：规则定级表（纯函数） ====================

# 强编号：编号本身就是显式层级信号，层级可与文档其它编号自洽地算出来——
# 只认这四类，且**独立实现**（不复用 heading_presets 的归一化路径：归一化把
# "文档实际用到的位置"压进 1..N，4 段以上编号全挤在同一档，且多体系拼接会让
# 层号整体偏移；实测 450 个带编号标题里 249 个被偏移 1 级）
_DOT_NUM_RE = re.compile(r"^(\d+(?:\.\d+)+)(?![.\d])")     # 1.1 / 1.1.1 / …
_CN_NUM_RE = re.compile(r"^[一二三四五六七八九十百千]+[、.]")  # 一、/ 十一.
_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百千\d]+[章篇]")
_SECTION_RE = re.compile(r"^第[一二三四五六七八九十百千\d]+节")

# 编号家族（层级从高到低；见 _family_of / rule_table）
_FAMILY_CHAPTER = "chapter"
_FAMILY_SECTION = "section"
_FAMILY_CN = "cn"
_FAMILY_DOT = "dot"
_FAMILY_ORDER: Tuple[str, ...] = (_FAMILY_CHAPTER, _FAMILY_SECTION,
                                  _FAMILY_CN, _FAMILY_DOT)

# 编号段数上限（层级上限，与切块侧 1~6 的层级语义一致）：超出按 6 计
_MAX_LEVEL = 6

# 标题文本里的上下标标签（解析产物把标题中的数字/字母包成 <sub>/<sup>，
# 如 "## <sub>1.1</sub> 节标题"）：编号匹配前剥离，否则正则匹配失败
_SUBSUP_RE = re.compile(r"</?(?:sub|sup)>", re.IGNORECASE)


def _strip_subsup(text: str) -> str:
    """剥离 <sub>/<sup> 标签（仅用于编号匹配，不改动原文）"""
    return _SUBSUP_RE.sub("", text)


# 弱编号：括号编号（（1）/（一）/1)）与裸数字（1. / 1、）——它们的层级取决于
# "所属上级"，且静态无法判断它究竟是章节标题还是正文里的步骤/列表项
# （实测本文档 74 个里 LLM 判 0 = 62 个），故不参与规则定级、交 LLM 判
_WEAK_NUM_RE = re.compile(r"^[（(]?[\d一二三四五六七八九十百千]+[）)．.、]")


def _family_of(title: str) -> Tuple[Optional[str], int]:
    """标题文本 → (编号家族, 点分段数)；无强编号 → (None, 0)"""
    t = _strip_subsup(title.strip())
    if not t:
        return None, 0
    if _CHAPTER_RE.match(t):
        return _FAMILY_CHAPTER, 0
    if _SECTION_RE.match(t):
        return _FAMILY_SECTION, 0
    if _CN_NUM_RE.match(t):
        return _FAMILY_CN, 0
    m = _DOT_NUM_RE.match(t)
    if m:
        return _FAMILY_DOT, m.group(1).count(".") + 1
    return None, 0


def is_weak_numbered(title: str) -> bool:
    """是否弱编号标题（括号/裸数字，层级交 LLM 判；诊断统计用）"""
    fam, _d = _family_of(title)
    return fam is None and bool(_WEAK_NUM_RE.match(_strip_subsup(title.strip())))


def rule_table(headings: List[Tuple[int, int, str]],
               ) -> Tuple[List[Optional[int]], List[Optional[str]]]:
    """规则定级表：(层级表, 编号家族表)，与 headings 同序同长；None = 交 LLM

    定级口径（逐条都可确定性计算，且与同文档其它编号自洽）：
    - 点分阿拉伯编号「1.1.1」→ 段数即层级，但要与**文档实际用到的最小段数**
      对齐：家族档位 = 它前面已用到的强编号家族所占档位之和 + 1，段数每多一段
      深一级。本文档「一、」占 1 档（1 级），点分编号最小段数 2（1.1），故
      1.1=2 级 …… 1.1.1.1.1.1=6 级；
    - 中文数字「一、」/「第X章」/「第X节」→ 按家族语义顺序（章 → 节 → 中文
      数字 → 点分）在文档实际用到的家族里顺次占档：只用到「一、」时它是 1 级，
      同时用到「第X章」时章=1 级、一、=2 级（与 heading_presets 的语义优先级
      一致，但这里是独立实现，不做"实际位置 → 1..N"的压缩）；
    - 括号编号/裸数字/无编号 → None（交 LLM 判，见 _WEAK_NUM_RE）。
    """
    parsed = [_family_of(t) for _o, _l, t in headings]
    fams = [f for f, _d in parsed]
    dots = [d for _f, d in parsed if _f == _FAMILY_DOT]
    base: Dict[str, int] = {}
    level = 1
    for fam in _FAMILY_ORDER:
        if fam not in fams:
            continue
        base[fam] = level
        # 点分家族按段数跨度占档（最小段数 → base，每多一段深一级）
        level += (max(dots) - min(dots) + 1) if fam == _FAMILY_DOT else 1
    levels: List[Optional[int]] = []
    for fam, depth in parsed:
        if fam is None:
            levels.append(None)
        elif fam == _FAMILY_DOT:
            levels.append(min(_MAX_LEVEL, base[fam] + depth - min(dots)))
        else:
            levels.append(min(_MAX_LEVEL, base[fam]))
    return levels, fams


# ==================== 提示词构建（纯函数） ====================

def build_items(rows: List[Tuple[int, int, str]]) -> str:
    """拼输入清单：每行「序号 空格 标题文本」（序号 = 批内 1..N）"""
    lines = []
    for i, (_off, _lv, title) in enumerate(rows, 1):
        lines.append(f"{i} {' '.join(title.split())}")
    return "\n".join(lines)


def build_prompt(rows: List[Tuple[int, int, str]],
                 context: str = "") -> str:
    """拼本批提示词（主提示词 + 批间上下文块）

    context：上批末尾参考（「全局序号 层级 标题文本」逐行），非空时拼在
    主提示词之后 + 承接语；空串 = 首批（无上下文）。
    """
    prompt = _LAYER_PROMPT.format(items=build_items(rows))
    if context:
        prompt += ("\n\n【上批末尾参考（已判定，请勿重复输出；序号为全局编号）】\n"
                   + context
                   + "\n\n承接上批，请沿用同一套层级标准。")
    return prompt


def build_context(tail: List[Tuple[int, int, str]], levels: List[int],
                  base: int) -> str:
    """批间上下文：上批末尾 N 条「全局序号 层级 标题文本」逐行

    tail = 上批末尾标题（(偏移, 原层级, 标题文本)），levels = 对应判定层级，
    base = 这些标题的全局序号（1 起）。
    """
    return "\n".join(
        f"{base + i} {lv} {' '.join(t.split())}"
        for i, ((_o, _l, t), lv) in enumerate(zip(tail, levels)))


# 无编号标题的定位参考样式：只看前文最近的带编号标题（默认，最省 token）/
# 前后文都看 / 以"紧邻上一行标题"为主锚点（见 context_label）
_CTX_PREV = "prev"
_CTX_BOTH = "both"
_CTX_NEAR = "near"
_CTX_STYLES = (_CTX_PREV, _CTX_BOTH, _CTX_NEAR)


def _nearest_label(headings: List[Tuple[int, int, str]],
                   rule_levels: List[Optional[int]], idx: int,
                   step: int) -> str:
    """idx 之前（step=-1）/ 之后（step=1）最近的带编号标题「文本（N 级）」

    只认规则定过级的标题（带编号）——无编号的还没判、不能当标尺。
    """
    i = idx + step
    while 0 <= i < len(headings):
        if rule_levels[i] is not None:
            return f"{' '.join(headings[i][2].split())}（{rule_levels[i]} 级）"
        i += step
    return ""


def _prev_label(headings: List[Tuple[int, int, str]],
                rule_levels: List[Optional[int]], idx: int) -> str:
    """紧邻上一行标题及其层级：已判定/规则定级 → 「X（N 级）」，否则指向清单"""
    if idx <= 0:
        return "文首（其前没有标题）"
    title = " ".join(headings[idx - 1][2].split())
    lv = rule_levels[idx - 1]
    if lv is not None:
        return f"{title}（{lv} 级）"
    return f"{title}（不带编号，层级见清单上一行）"


def context_label(headings: List[Tuple[int, int, str]],
                  known_levels: List[Optional[int]], idx: int,
                  style: str = _CTX_PREV) -> str:
    """待判标题的定位参考（LLM 判无编号标题时的标尺）

    known_levels：已能给出层级的标题表（带编号的规则层级 + 已判定的无编号
    标题层级；未判定的为 None）。
    - style=_CTX_PREV：前文最近的带编号标题及其层级（省 token，但中间隔着
      无编号标题时会指偏——实测：附录里的"1、xxx"被前面的"13.2 xxx（2 级）"
      带偏、判成了正文行）；
    - style=_CTX_BOTH：前文 + 后文各一个带编号标尺（无编号标题夹在两批带编号
      标题之间时，前文给下界、后文给上界）；
    - style=_CTX_NEAR：以**紧邻上一行标题**为主锚点（层级已知就给，未知则指向
      清单上一行，由模型自行保持一致），再补前文最近的带编号标题——长清单里
      的层级延续主要靠"上一行"，编号标尺只是兜底。
    """
    prev = _nearest_label(headings, known_levels, idx, -1)
    if style == _CTX_NEAR:
        label = f"紧邻上一行 {_prev_label(headings, known_levels, idx)}"
        if prev:
            label += f"；前文最近的带编号标题 {prev}"
        return label
    label = (f"前文 {prev}" if prev else "前文没有带编号的标题（清单开头）")
    if style == _CTX_BOTH:
        nxt = _nearest_label(headings, known_levels, idx, 1)
        if nxt:
            label += f"；后文 {nxt}"
    return label


def build_hybrid_items(rows: List[Tuple[int, int, str]],
                       contexts: List[str]) -> str:
    """拼混合方案的输入清单：定位参考行（变化时才输出）+「序号 标题文本」

    同一批里连续若干标题的"前文最近带编号标题"往往相同——参考行只在变化时
    输出一次（省 token），组内的标题共用它；参考行没有序号，模型不为它输出
    条目（提示词里已说明）。
    """
    lines: List[str] = []
    last: Optional[str] = None
    for i, ((_off, _lv, title), ctx) in enumerate(zip(rows, contexts), 1):
        if ctx != last:
            lines.append(f"[上级参考] {ctx}")
            last = ctx
        lines.append(f"{i} {' '.join(title.split())}")
    return "\n".join(lines)


# ==================== 响应解析 + 校验（纯函数） ====================

def _extract_json_array(content: str):
    """从响应文本提取 JSON 数组：直接 loads → 剥 ```json 围栏 → 截取 [..]

    失败返回 None（模型可能加说明文字/代码块标记）。
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
    # 模型在 JSON 前后加说明文字：截取第一个 [ 到最后一个 ]
    lo, hi = s.find("["), s.rfind("]")
    if 0 <= lo < hi:
        try:
            return json.loads(s[lo:hi + 1])
        except Exception:
            pass
    return None


def _as_int(v) -> Optional[int]:
    """宽松取整：int 原样；整数值 float（2.0）转换；其余（含字符串/布尔）None"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def validate_levels(content: str, n: int) -> Tuple[Optional[List[int]], str]:
    """响应校验（纯函数，供测试直测）：(层级列表, 错误信息)

    返回 (levels, "") 表示校验通过；否则 (None, 原因)。
    校验项（任一不过即判废，由调用方重试）：
    1. 是 JSON 数组且条数 == n；
    2. 每条是对象，且**只含 i、level 两个字段**（出现标题文本等额外字段
       直接判废——防 LLM 复述/改写标题文本）；
    3. i 为 1..n 的整数、无缺失无重复（集合恰为 {1..n}）；
    4. level 为 0~6 的整数（0 = 不是标题）。
    """
    data = _extract_json_array(content)
    if not isinstance(data, list):
        return None, "响应不是 JSON 数组"
    if len(data) != n:
        return None, f"返回条数不符（{len(data)} != {n}）"
    levels: List[int] = [0] * n
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            return None, "数组元素不是对象"
        extra = set(item.keys()) - {"i", "level"}
        if extra:
            return None, f"含多余字段 {sorted(extra)}（防复述标题文本）"
        i = _as_int(item.get("i"))
        lv = _as_int(item.get("level"))
        if i is None or not (1 <= i <= n):
            return None, f"序号非法：{item.get('i')!r}"
        if i in seen:
            return None, f"序号重复：{i}"
        seen.add(i)
        if lv is None or not (0 <= lv <= 6):
            return None, f"层级非法：{item.get('level')!r}"
        levels[i - 1] = lv
    if seen != set(range(1, n + 1)):
        return None, "序号集合不完整（应恰为 1..N）"
    return levels, ""


# ==================== 主入口 ====================

def _usage_of(resp) -> Tuple[int, int]:
    """响应 usage → (输入 token, 输出 token)（服务端未返回时 0）"""
    u = getattr(resp, "usage", None)
    if u is None:
        return 0, 0
    return (int(getattr(u, "prompt_tokens", 0) or 0),
            int(getattr(u, "completion_tokens", 0) or 0))


def _content_of(resp) -> str:
    """响应 content（结构异常时返回空串）"""
    try:
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


async def _run_batch(client, model: str, base_user: str, n: int, max_tokens: int,
                     strategy, timeout: float) -> Tuple[Optional[List[int]], BatchStat]:
    """单批调用 + 校验 + 重试 1 次（仍不过 → (None, stat) 整批作废）

    base_user = 已拼好的本批提示词（全量/混合两套方案的清单格式不同，拼装由
    调用方完成）；n = 本批待判条数（校验用）。
    stat.ok=False 表示未判定，调用方回退规则口径层级；本函数不抛异常
    （超时/调用失败/解析失败都归入未判定）。
    strategy：思考关闭策略——每次尝试都重新 apply（重试会追加纠错消息，
    prefill 注入必须在 messages 末尾才对模型生效）。
    """
    stat = BatchStat(index=0, first=0, count=n, ok=False, retries=0,
                     error="", prompt_tokens=0, completion_tokens=0,
                     elapsed=0.0)
    t0 = time.time()
    last_err = ""
    for attempt in range(_MAX_RETRY + 1):
        stat.retries = attempt
        messages = [
            {"role": "system", "content": "你是文档结构分析助手。"},
            {"role": "user", "content": base_user},
        ]
        if attempt:
            # 重试：追加纠错消息（temperature=0 下原样重发会得到同样输出）
            messages.append(
                {"role": "user", "content":
                 f"上一次输出不符合要求（{last_err}）。请严格按输出要求重新"
                 f"输出：只含 i 与 level 两个字段的 JSON 数组，i 覆盖 1~{n} "
                 f"全部序号、顺序不变、不增不减，不要输出任何其他文字。"})
        payload: Dict[str, object] = {"messages": messages}
        strategy.apply(payload)
        try:
            resp = await llm_completion(
                client, model=model, messages=payload["messages"],
                max_tokens=max_tokens, temperature=0.0,
                extra_body=payload.get("extra_body"), timeout=timeout,
            )
        except LLMTimeoutError:
            last_err = f"调用超时（>{timeout:g}s）"
            logger.warning("标题分层第 %d 批调用超时，重试中", stat.index)
            continue
        except LLMRequestError as e:
            last_err = f"调用失败：{str(e)[:150]}"
            logger.warning("标题分层第 %d 批调用失败：%s", stat.index, last_err)
            continue
        except Exception as e:  # 兜底：未知异常同样归入未判定
            last_err = f"调用异常：{str(e)[:150]}"
            logger.warning("标题分层第 %d 批调用异常：%s", stat.index, last_err)
            continue
        content = _content_of(resp)
        stat.raw = content[:300]
        p_tok, c_tok = _usage_of(resp)
        stat.prompt_tokens += p_tok
        stat.completion_tokens += c_tok
        levels, err = validate_levels(content, n)
        if levels is not None:
            stat.ok = True
            stat.error = ""
            stat.elapsed = time.time() - t0
            return levels, stat
        last_err = err
        logger.warning("标题分层第 %d 批校验不过（%s），重试中", stat.index, err)
    stat.error = last_err
    stat.elapsed = time.time() - t0
    logger.warning("标题分层第 %d 批整批作废（%s），回退规则口径", stat.index, last_err)
    return None, stat


def _build_client(cfg: dict, timeout: float):
    """解析 LLM 配置并建客户端：(client, model, max_tokens, strategy)

    取配置方式与 agentic_chunker 同款：parse_llm_model 指定 → 覆盖激活模型；
    未指定/查不到 → 激活模型。未配置 → HeadingLLMError（调用方决定是否回退）。
    思考关闭策略在 _run_batch 内对真实 messages 应用（prefill 必须落在
    messages 末尾）。
    """
    llm_cfg = llm_to_dict(get_active_config().llm)
    override = llm_cfg_for_parser(cfg.get("parse_llm_model"))
    if override:
        llm_cfg = {**llm_cfg, **override}
    cfg_obj = LLMConfig.from_dict(llm_cfg)
    if not (cfg_obj.base_url and cfg_obj.model):
        raise HeadingLLMError("LLM 未配置（base_url/model 为空），无法做标题分层")
    strategy = get_thinking_strategy(llm_cfg, cfg.get("thinking_mode"))
    client = get_llm_client(llm_cfg, timeout=timeout)
    max_tokens = min(_MAX_TOKENS, int(cfg_obj.max_tokens or _MAX_TOKENS))
    return client, cfg_obj.model, max_tokens, strategy


async def layer_headings(text: str,
                         cfg: Optional[dict] = None,
                         *,
                         batch_size: int = _BATCH_SIZE,
                         timeout: float = _TIMEOUT,
                         limit: Optional[int] = None) -> HeadingLayerResult:
    """用 LLM 给解析产物重新分层：返回每个标题的偏移与真实层级

    - text: markdown 全文（PDF 解析产物）
    - cfg: parser_config（thinking_mode 生效；parse_llm_model 指定时用该
      模型，空/查不到回退激活模型——与 agentic_chunker 同款取配置方式）
    - batch_size: 每批标题数（默认 150）
    - timeout: 单批调用超时（秒）
    - limit: 只处理前 N 个标题（试跑/调试用；None = 全部）
    - 失败语义：LLM 未配置 → HeadingLLMError；单批未判定 → 回退规则口径
      层级并记入 batches（ok=False），不抛异常
    - 返回 items 与 headings 一一对应：offset = 标题行在全文的起始偏移，
      level = 0~6（0 = 不是标题，应作为正文），标题文本不受影响
    """
    cfg = cfg or {}
    if not text:
        return HeadingLayerResult(items=[], headings=[], batches=[])

    headings, protected = extract_headings(text)
    if limit is not None:
        headings = headings[:limit]
    systems = detect_systems(headings)
    fallback = rule_levels(text, headings, protected, systems)
    if not headings:
        return HeadingLayerResult(items=[], headings=[], batches=[], systems=systems)

    client, model, max_tokens, strategy = _build_client(cfg, timeout)

    items: List[Dict[str, int]] = [
        {"offset": off, "level": fallback[i]} for i, (off, _lv, _t) in enumerate(headings)]
    batches: List[BatchStat] = []
    context = ""
    for b_idx, start in enumerate(range(0, len(headings), batch_size)):
        rows = headings[start:start + batch_size]
        levels, stat = await _run_batch(
            client, model, build_prompt(rows, context), len(rows),
            max_tokens, strategy, timeout)
        stat.index = b_idx
        stat.first = start
        batches.append(stat)
        if levels is not None:
            for k, lv in enumerate(levels):
                items[start + k]["level"] = lv
        # 批间上下文：上批末尾 N 条标题及其判定结果（未判定批用回退层级，
        # 保证上下文始终有值、层级标准不因单批失败断裂）
        tail = rows[-_CONTEXT_TAIL:]
        tail_levels = (levels[-_CONTEXT_TAIL:] if levels is not None
                       else fallback[start + len(rows) - len(tail):
                                     start + len(rows)])
        context = build_context(tail, tail_levels, start + len(rows) - len(tail) + 1)
    logger.info("标题分层完成：%d 个标题 / %d 批（未判定 %d 批）",
                len(headings), len(batches), len(batches) - sum(
                    1 for b in batches if b.ok))
    return HeadingLayerResult(items=items, headings=headings, batches=batches,
                              systems=systems)


async def hybrid_levels(text: str,
                        cfg: Optional[dict] = None,
                        *,
                        batch_size: int = _BATCH_SIZE,
                        timeout: float = _TIMEOUT,
                        limit: Optional[int] = None,
                        context_style: str = _CTX_PREV) -> HybridLevelResult:
    """混合层级表：带编号标题走规则定级，无编号标题交 LLM 判

    - 带编号（点分阿拉伯 / 中文数字 / 第X章·节）：rule_table 确定性定级，
      **零 token、零耗时**——编号是显式层级信号，规则算得出来就不该问模型；
    - 无编号（含括号/裸数字等弱编号）：交 LLM 判「是不是标题 + 几级」，每批
      清单里以「[上级参考]」行给出定位参考（该标题前文最近的带编号标题及其
      层级，见 context_label 的 style）；
    - 两段结果按标题原顺序合并成一张与 headings 等长的层级表（.levels），
      可直接作为 HierarchicalChunker(heading_levels=...) 传入；
    - 全文标题都带编号时不调用 LLM（也就不要求 LLM 配置）；
    - 失败语义与 layer_headings 一致：有待判标题但 LLM 未配置 → HeadingLLMError；
      单批超时/调用异常/解析失败 → 该批未判定，回退规则口径（_iter_headings
      编号推断 / # 数量）并记入 batches（ok=False），不阻断其余批次。

    参数：cfg/batch_size/timeout/limit 同 layer_headings；context_style 取
    "prev"（前文最近的带编号标题）/ "both"（前后文各给一个带编号标尺）/
    "near"（以紧邻上一行标题为主锚点 + 前文最近带编号标题兜底）。
    """
    if context_style not in _CTX_STYLES:
        raise ValueError(f"context_style 非法: {context_style!r}（需 {'/'.join(_CTX_STYLES)}）")
    cfg = cfg or {}
    t0 = time.time()
    if not text:
        return HybridLevelResult(items=[], headings=[], families=[])

    headings, protected = extract_headings(text)
    if limit is not None:
        headings = headings[:limit]
    systems = detect_systems(headings)
    table, families = rule_table(headings)
    fallback = rule_levels(text, headings, protected, systems)
    # 先落两段结果：规则段已定，LLM 段先放回退值、判定成功后再覆盖
    items: List[Dict[str, int]] = [
        {"offset": off,
         "level": table[i] if table[i] is not None else fallback[i]}
        for i, (off, _lv, _t) in enumerate(headings)]
    result = HybridLevelResult(items=items, headings=headings, families=families,
                               systems=systems)
    targets = [i for i, lv in enumerate(table) if lv is None]
    if not headings or not targets:
        result.elapsed = time.time() - t0
        logger.info("混合分层完成：%d 个标题全部按编号规则定级（未调用 LLM）",
                    len(headings))
        return result

    client, model, max_tokens, strategy = _build_client(cfg, timeout)
    # 定位参考用"当前已知层级表"：带编号的规则层级 + 已判定的无编号标题层级
    # （未判定的为 None）——批内/跨批的层级延续都能用上
    known: List[Optional[int]] = list(table)
    batches: List[BatchStat] = []
    context = ""
    for b_idx, start in enumerate(range(0, len(targets), batch_size)):
        idxs = targets[start:start + batch_size]
        rows = [headings[i] for i in idxs]
        contexts = [context_label(headings, known, i, context_style) for i in idxs]
        base_user = _HYBRID_PROMPT.format(items=build_hybrid_items(rows, contexts))
        if context:
            base_user += ("\n\n【上批末尾参考（已判定，请勿重复输出；序号为全局编号）】\n"
                          + context
                          + "\n\n承接上批，请沿用同一套层级标准。")
        levels, stat = await _run_batch(client, model, base_user, len(rows),
                                        max_tokens, strategy, timeout)
        stat.index = b_idx
        stat.first = idxs[0]
        batches.append(stat)
        if levels is not None:
            for k, lv in enumerate(levels):
                items[idxs[k]]["level"] = lv
                known[idxs[k]] = lv
        # 批间上下文：上批末尾 N 条待判标题及其层级（未判定批用回退层级，
        # 保证上下文始终有值、层级标准不因单批失败断裂）
        tail = idxs[-_CONTEXT_TAIL:]
        context = build_context([headings[i] for i in tail],
                                [items[i]["level"] for i in tail], tail[0] + 1)
    result.batches = batches
    result.elapsed = time.time() - t0
    logger.info("混合分层完成：%d 个标题（规则 %d + LLM %d）/ %d 批（未判定 %d 批）",
                len(headings), len(headings) - len(targets), len(targets),
                len(batches), len(batches) - sum(1 for b in batches if b.ok))
    return result
