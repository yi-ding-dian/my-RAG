"""自底向上的章节树聚合切块（hierarchical）

与 title/parent_child 的"自顶向下按标题切"相反：先按标题层级把全文建成章节树，
再**自底向上**判断"整棵子树能否装进一个块"——装得下就整棵子树成一个块并允许
继续"上浮"（回到父节点与兄弟子树合并），装不下才在子节点之间贪心装箱。写作
格式规范的文档（层级深、小节短、正文配图）在这种切法下天然得到语义完整的块：
短小节被并进父级、长小节内部再切，块边界永远落在标题/子节点之间，不出现
"标题与正文被拆散"或"半句话开头"的块。

设计要点（与既有切块器的差异都是有意为之）：
1. 合并不跨章级标题（chapter_level，默认 1 级标题）：章级标题是最外层边界，
   章与章之间永不合并（章内可一路聚合到底，整章 200 字也并成一个块）；文首
   第一个章级标题之前的内容（封面/修订记录）作独立"前言组"，不跨入后面的章。
   原因：章级标题是文档最稳定的语义边界，跨章合并会让块同时携带两个主题，
   检索命中后无法定位到具体章节；
2. 块长度按**有效内容**计：图片引用（markdown ![]() 与 HTML <img>）不计入
   chunk_size 预算——图片 URL 是资源引用而非语义内容（长哈希路径会吃光配额，
   正文被迫提前断块），另设真实长度硬上限（chunk_size × _MAX_RAW_LEN_FACTOR）
   兜底图片密集块的真实体积；
3. 超长兜底二次切：节点内容自身就超预算、且没有更细层级可聚合时（无子节点的
   长正文），按 段落 → 句子 → 硬切 逐级降级；保护区间（表格/代码块/图片引用）
   在每一级都是原子，不被切开；
4. 块首带祖先标题链（"祖先 > 父 > 自己"）：块自带所属章节上下文，检索命中后
   不必回查原文即可判断归属。链由本模块按章节树直接生成（不用
   common.add_heading_paths：它判断"块首自带标题"用的正则
   `^#{1,6}\\s+\\S` 只取到标题的首字符，再拿首字符去标题链里匹配自然匹配不上
   → 以标题开头的块几乎补不出祖先链；本算法的块恰恰几乎都以自己的标题行
   开头，且树已在手，按树取链更准也更省）；
5. 偏移契约：Chunk(text, char_start, char_end) 中 text 恒等于
   full[char_start:char_end]（拼接标题链前缀时只改 text 不改偏移，与项目既有
   做法一致，见 common.add_heading_paths）；
6. 外部层级表（heading_levels，可选）：MinerU 解析 PDF 时把所有标题统一输出
   为 `##`（层级信息丢失），此时可由外部（如 backend.normative.heading_llm
   的混合层级表）传入每个标题的真实层级，覆盖从 `#` 数量得到的层级；level=0
   的标题不参与建树（不是章节边界），但其文本仍按原文留在正文里——偏移契约
   与内容覆盖不受影响。

overlap 的落点：聚合块的边界本身是章节语义边界（子树/章节），人为回移起点会让
块首不再是它自己的标题、标题链前缀与块首标题错位，故 overlap 只在"被强制切断
的连续文本"里生效，即超长兜底二次切分内部（与 recursive 的续接语义一致）。
"""
from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass, field
from typing import List, Tuple

from backend.chunking.base import Chunk
from backend.chunking.common import _iter_headings, find_protected_ranges
from backend.config import get_active_config

logger = logging.getLogger(__name__)

# 标题层级取值域（与 _iter_headings / markdown 层级语义一致）
_MIN_LEVEL = 0
_MAX_LEVEL = 6

# 图片引用（markdown ![alt](src) / HTML <img>）：块长度统计时不计入。
# 与 recursive.py 同语义但独立实现——本算法不依赖其他切块器的私有实现，
# 便于单独演进（如后续按图片数量而非 URL 长度计价）
_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)|<img\b[^>]*>", re.IGNORECASE)

# 真实长度硬上限系数：有效长度（不含图片 URL）在预算内、但真实长度超过
# chunk_size × 本系数的块不再合并（极端图片密集块的 embedding/上下文开销兜底）
_MAX_RAW_LEN_FACTOR = 4

# 句子边界（中英文句号/问号/感叹号，非句界标点留在句内）：兜底二次切的句级边界
_SENTENCE_RE = re.compile(r"[^。！？.!?]+[。！？.!?]?")

# 兜底二次切的降级模式：段落（空行分界）→ 句子
_MODE_PARA = "para"
_MODE_SENT = "sent"


@dataclass
class _Node:
    """章节树节点：一个标题（或前言伪节点）+ 直属正文 + 子节点

    区间语义：node.start = 标题行起始（前言为 0），node.end = 下一个同级或
    更高级标题的起始（文末收尾），node.body_end = 直属正文结束位置（第一个
    子节点起始处）。三者构成"整棵子树 / 直属正文"两种切片，节点区间严格
    嵌套（子节点区间 ⊆ 父节点区间）。
    """

    level: int
    title: str
    start: int
    end: int
    children: List["_Node"] = field(default_factory=list)

    @property
    def body_end(self) -> int:
        """直属正文结束偏移（第一个子节点起点；无子节点即节点终点）"""
        return self.children[0].start if self.children else self.end


class HierarchicalChunker:
    """自底向上的章节树聚合切块

    chunk_size/overlap 缺省时取活跃配置（backend.config.chunking，与其他
    切块器一致）；chapter_level = "不可跨越的章级标题层级"（默认 1，即 # 一级
    标题；文档没有该级标题时整篇作为一个聚合组，不设强制边界）；
    add_heading_path = 是否给块首拼接祖先标题链（默认开，见类 docstring 第 4 点）；
    heading_levels = 外部给定的层级表（可选，见类 docstring 第 6 点）：与标题
    识别结果（_iter_headings）按顺序一一对应，level 取 0~6，0 表示"不是标题"
    （不参与建树，文本仍留在正文里）。

    产出保证（测试断言依赖）：
    - 每块 text == full[char_start:char_end]（拼了标题链前缀的块只多一个前缀行）；
    - 块按原文顺序、区间互不重叠（overlap 续接仅在兜底二次切分内部，同组同段内）；
    - 任何块区间内部不含章级标题（章级边界永不跨过）；
    - 表格/代码块/图片引用不会被切开。
    """

    def __init__(self, chunk_size: int | None = None,
                 overlap: int | None = None,
                 chapter_level: int = 1,
                 add_heading_path: bool = True,
                 heading_levels: List[int] | None = None):
        cfg = get_active_config().chunking
        self.chunk_size = chunk_size if chunk_size is not None else cfg.chunk_size
        self.overlap = overlap if overlap is not None else cfg.chunk_overlap
        if not 1 <= chapter_level <= 6:
            raise ValueError(f"chapter_level 超出范围: {chapter_level}（需 1~6）")
        if self.chunk_size <= 0:
            raise ValueError(f"chunk_size 需为正数: {self.chunk_size}")
        self.chapter_level = chapter_level
        self.add_heading_path = add_heading_path
        # 外部层级表（None = 用 # 数量的既有行为）；取值域即时校验，长度校验
        # 依赖实际标题数、在 chunk() 里做（见 _external_levels）
        self.heading_levels = self._check_levels(heading_levels)
        # 表格/代码块/图片引用保护区间（每次 chunk 按当前文本重算；
        # 与 RecursiveChunker 同样挂在实例上，便于各辅助方法直接取用）
        self.protected_ranges: List[Tuple[int, int]] = []
        # 标题行起始偏移（集合 = 判断"纯标题单元"用；有序表 = 取标题行区间用）
        self._heading_starts: set[int] = set()
        self._heading_starts_sorted: List[int] = []

    @staticmethod
    def _check_levels(levels: List[int] | None) -> List[int] | None:
        """外部层级表取值域校验（None 原样返回；非法即抛 ValueError）

        只校验"每个值是不是 0~6 的整数"——长度是否与标题数一致要等拿到标题
        识别结果才知道，放在 chunk() 里校验（不匹配则忽略并告警）。
        """
        if levels is None:
            return None
        out: List[int] = []
        for v in levels:
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError(f"heading_levels 需为整数（0~6）: {v!r}")
            if not _MIN_LEVEL <= v <= _MAX_LEVEL:
                raise ValueError(f"heading_levels 超出范围: {v}（需 0~6）")
            out.append(v)
        return out

    def _external_levels(self, headings: List[Tuple[int, int, str]],
                         ) -> List[int] | None:
        """外部层级表可用性校验：长度与识别出的标题数不符 → 忽略并告警

        层级表与标题按顺序一一对应，长度对不上说明调用方拿到的标题口径与
        本次识别结果不一致（如文本已被改写），继续使用会**整体错位**——宁可不
        分层（回退 # 数量的既有行为）也不把层级安到别的标题上。
        """
        if self.heading_levels is None:
            return None
        if len(self.heading_levels) != len(headings):
            logger.warning(
                "heading_levels 长度与识别出的标题数不符（%d != %d），"
                "本次切块忽略外部层级表（回退 # 数量）",
                len(self.heading_levels), len(headings))
            return None
        return self.heading_levels

    # ---- 主流程 ----

    def chunk(self, text: str) -> List[Chunk]:
        """切块入口：建树 → 分组 → 自底向上收集单元 → 组内贪心装箱 → 拼标题链"""
        if not text or not text.strip():
            return []
        self.protected_ranges = find_protected_ranges(text)
        headings = _iter_headings(text, self.protected_ranges)
        self._heading_starts = {off for off, _lvl, _t in headings}
        self._heading_starts_sorted = sorted(self._heading_starts)
        root = self._build_tree(text, self._tree_headings(headings))
        chunks: List[Chunk] = []
        for group in self._groups(text, root):
            # 组内独立装箱：组边界（章级标题/前言）永不跨过；
            # 单元级粘合：纯标题单元并进后继单元（标题不单独成块，见粘合方法）
            units = self._glue_heading_units(text, self._collect_units(text, group))
            for s, e in self._pack(text, units):
                chunk = self._make_chunk(text, s, e)
                if chunk.text.strip():
                    chunks.append(chunk)
        if self.add_heading_path:
            chunks = self._with_heading_chains(chunks, root)
        return chunks

    # ---- 建树与分组 ----

    def _tree_headings(self, headings: List[Tuple[int, int, str]],
                       ) -> List[Tuple[int, int, str]]:
        """建树用标题表：外部层级表生效时覆盖级别、剔除 level=0 的伪标题

        - 未传层级表 / 长度不符 → 原样返回（既有行为，docx 产物路径不变）；
        - level=0（伪标题）不进树、不再是章节边界——但它只是**不参与建树**：
          正文区间由树节点区间按原文偏移划分，这些行仍落在所在节点的正文里
          （偏移契约与内容覆盖不受影响，见类 docstring 第 6 点）；
        - level=0 的标题行仍留在 _heading_starts 里（标题行原子性/纯标题粘合
          沿用既有语义，行为与未分层时一致）。
        """
        levels = self._external_levels(headings)
        if levels is None:
            return headings
        return [(off, lv, title)
                for (off, _lv, title), lv in zip(headings, levels) if lv > 0]

    @staticmethod
    def _build_tree(text: str, headings: List[Tuple[int, int, str]]) -> _Node:
        """按标题层级建树（栈式）：父节点 level 严格小于子节点

        新标题入栈前弹出所有 level >= 自身的节点——被弹出即"区间到此结束"
        （end = 新标题起点）。标题层级保证树深 ≤ 6（_iter_headings 的级别
        取值域），故递归深度有界。
        """
        root = _Node(level=0, title="", start=0, end=len(text))
        stack: List[_Node] = [root]
        for off, level, title in headings:
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop().end = off
            node = _Node(level=level, title=title, start=off, end=len(text))
            stack[-1].children.append(node)
            stack.append(node)
        for node in stack:  # 收尾：仍在栈上的节点（末标题链）都到文末
            node.end = len(text)
        return root

    @staticmethod
    def _heading_chains(root: _Node) -> List[Tuple[int, List[str]]]:
        """标题链表：[(标题行起始偏移, 该标题的完整链)]，按偏移升序

        链 = 从最外层祖先到该标题的标题文本（伪节点 title 为空，不入户）；
        块的标题链前缀由它查表得到（见 _with_heading_chains）。
        """
        chains: List[Tuple[int, List[str]]] = []
        stack: List[Tuple[_Node, List[str]]] = [(root, [])]
        while stack:
            node, chain = stack.pop()
            for child in node.children:
                child_chain = chain + [child.title] if child.title else chain
                chains.append((child.start, child_chain))
                stack.append((child, child_chain))
        chains.sort()
        return chains

    def _with_heading_chains(self, chunks: List[Chunk],
                             root: _Node) -> List[Chunk]:
        """块首拼接祖先标题链（只改 text，char_start/char_end 保持原文偏移）

        - 块首就是自己的标题行（块起点即该标题行起点）→ 只补**祖先链**
          （"祖先 > 父"，自己的标题已在块首第一行，拼全链会重复）；
        - 块首不是标题行（兜底二次切分的续接片等）→ 补整条链（含最近祖先
          标题），块凭空多出所属章节信息，检索命中后能自解释归属。
        """
        chains = self._heading_chains(root)
        if not chains:
            return chunks
        offsets = [off for off, _ in chains]
        out: List[Chunk] = []
        for c in chunks:
            i = bisect.bisect_right(offsets, c.char_start) - 1
            if i < 0:  # 首个标题之前（前言）：无祖先链
                out.append(c)
                continue
            chain = chains[i][1]
            # 块起点即标题行起点 → 块首自成标题，只补祖先
            titles = chain[:-1] if chains[i][0] == c.char_start else chain
            if not titles:
                out.append(c)
                continue
            out.append(Chunk(text=f"{' > '.join(titles)}\n{c.text}",
                             char_start=c.char_start, char_end=c.char_end))
        return out

    def _groups(self, text: str, root: _Node) -> List[_Node]:
        """聚合组划分：组间永不合并，组内一路聚合

        - 章级标题（level <= chapter_level，默认 1 级）各成一个组；
        - 第一个章级标题之前的内容（封面/修订记录，可能含更低级标题）整体
          为一个"前言组"（伪节点，level 0）；
        - 全文没有章级标题 → 整篇一个前言组（不设强制边界，仍可一路聚合）。
        """
        tops = root.children
        first = next((i for i, n in enumerate(tops)
                      if n.level <= self.chapter_level), len(tops))
        if first >= len(tops):  # 无章级标题：整篇一个组
            return [_Node(level=0, title="", start=0, end=len(text),
                          children=tops)]
        groups: List[_Node] = []
        pre_end = tops[first].start
        if first > 0 or text[:pre_end].strip():
            groups.append(_Node(level=0, title="", start=0, end=pre_end,
                                children=tops[:first]))
        groups.extend(tops[first:])
        return groups

    # ---- 自底向上收集可上浮单元 ----

    def _collect_units(self, text: str, node: _Node) -> List[Tuple[int, int]]:
        """单元列表：整棵子树装得下 → 一个单元（可继续上浮参与父级合并），
        装不下 → [直属正文] + 各子节点单元的展平（边界落在子节点之间）

        单元是"贪心装箱的最小整体"：装箱不会把单元拆开，因此块边界天然落在
        子树/子节点之间（不出现"标题与正文被拆散"）。超长子树（无更细层级
        可聚合，如无子节点的长正文）同样返回一个单元，由装箱阶段二次切分兜底。
        """
        if node.end <= node.start:
            return []
        if not self._over_budget(text, node.start, node.end):
            return [(node.start, node.end)]
        units: List[Tuple[int, int]] = []
        if text[node.start:node.body_end].strip():
            units.append((node.start, node.body_end))
        for child in node.children:
            units.extend(self._collect_units(text, child))
        # 兜底：子树超预算却收不出任何单元（内容全为空白）→ 整段交装箱阶段
        return units or [(node.start, node.end)]

    def _is_heading_only_unit(self, text: str, s: int, e: int) -> bool:
        """区间内非空行是否全是标题行（纯标题内容，自身没有正文）

        纯标题内容单独成块毫无检索价值 → 调用方把它并入其后单元
        （见 _glue_heading_units）。连续多个标题行（"# 章\\n\\n## 节"）也是
        纯标题内容——标题**连续向前粘**，直到遇到第一行正文。
        """
        pos = s
        for line in text[s:e].split("\n"):
            if line.strip() and pos not in self._heading_starts:
                return False
            pos += len(line) + 1
        return True

    # ---- 贪心装箱 ----

    def _pack(self, text: str, units: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """单元按序贪心装箱：每块尽量接近 chunk_size，块边界落在单元之间

        超长单元（子树自身超预算）先二次切成片，前若干片各自成块（片已接近
        chunk_size，无合并空间），末片作为当前块起点继续向后贪心——避免
        "超长内容尾部剩一小片"变成碎片块（末片可与后续短单元并成满块）。
        """
        blocks: List[Tuple[int, int]] = []
        i, n = 0, len(units)
        while i < n:
            s, e = units[i]
            i += 1
            if self._over_budget(text, s, e):
                pieces = self._split_oversize(text, s, e)
                if not pieces:
                    continue
                blocks.extend(pieces[:-1])
                cs, ce = pieces[-1]
            else:
                cs, ce = s, e
            while i < n and self._fits(text, cs, units[i][1]):
                ce = units[i][1]
                i += 1
            blocks.append((cs, ce))
        return blocks

    # ---- 长度度量 ----

    @staticmethod
    def _eff(text: str, s: int, e: int) -> int:
        """区间有效长度：去掉图片引用后的字符数（图片 URL 不计入预算）"""
        if s >= e:
            return 0
        seg = text[s:e]
        if "![" not in seg and "<img" not in seg.lower():
            return e - s
        return (e - s) - sum(len(m.group(0))
                             for m in _IMAGE_REF_RE.finditer(seg))

    def _over_budget(self, text: str, s: int, e: int) -> bool:
        """区间是否超预算：有效长度超 chunk_size，或真实长度超硬上限"""
        return (self._eff(text, s, e) > self.chunk_size
                or (e - s) > self.chunk_size * _MAX_RAW_LEN_FACTOR)

    def _fits(self, text: str, s: int, e: int) -> bool:
        """[s, e) 是否在预算内（装箱时用：追加后仍不超预算才合并）"""
        return not self._over_budget(text, s, e)

    # ---- 兜底二次切分（超长内容：段落 → 句子 → 硬切）----

    def _split_oversize(self, text: str, s: int,
                        e: int) -> List[Tuple[int, int]]:
        """超长内容二次切分：段落级贪心 → 仍超长的段落降级句级 → 句子仍超长硬切

        只在"节点内容自身超预算"时进入。保护区间（表格/代码块/图片引用）在
        每一级都是原子单元——超长表格宁可整块输出也不切开（项目既有语义）。
        """
        out: List[Tuple[int, int]] = []
        for ps, pe in self._greedy(text, self._atomic_units(text, s, e, _MODE_PARA)):
            if self._fits(text, ps, pe) or self._is_protected_span(ps, pe):
                out.append((ps, pe))
                continue
            for qs, qe in self._greedy(text,
                                       self._atomic_units(text, ps, pe, _MODE_SENT)):
                if self._fits(text, qs, qe) or self._is_protected_span(qs, qe):
                    out.append((qs, qe))
                else:
                    out.extend(self._hard_cut(text, qs, qe))
        return self._apply_overlap(text, out)

    def _greedy(self, text: str,
                units: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """单元贪心装箱：连续单元合并到不超过预算（有效长度 + 真实长度双限）"""
        blocks: List[Tuple[int, int]] = []
        cs, ce = -1, -1
        for us, ue in units:
            if cs < 0:
                cs, ce = us, ue
            elif self._fits(text, cs, ue):
                ce = ue
            else:
                blocks.append((cs, ce))
                cs, ce = us, ue
        if cs >= 0:
            blocks.append((cs, ce))
        return blocks

    def _atomic_units(self, text: str, s: int, e: int,
                      mode: str) -> List[Tuple[int, int]]:
        """原子单元（升序互不重叠）：原子区间整体 + 区间外文本按 mode 切

        mode="para" 按空行切段、mode="sent" 按句界切句；原子区间夹在中间
        （文本与原子区间交替拼接），保证任何一级切分都不会切进原子区间
        （表格/代码块/图片引用 + 句级下的标题行，见 _atomic_spans）。
        纯标题单元并入其后单元（标题与紧随的正文同片，见 _is_heading_only_unit）。
        """
        units: List[Tuple[int, int]] = []
        pos = s
        for ps, pe in self._atomic_spans(text, s, e, mode):
            units.extend(self._text_units(text, pos, ps, mode))
            units.append((ps, pe))
            pos = pe
        units.extend(self._text_units(text, pos, e, mode))
        units = [u for u in units if text[u[0]:u[1]].strip()]
        return self._glue_heading_units(text, units)

    def _atomic_spans(self, text: str, s: int, e: int,
                      mode: str) -> List[Tuple[int, int]]:
        """不可切分的原子区间（升序合并）：保护区间 + 句级下的标题行

        标题行自身必须原子：标题文本里的编号点号（如 "##### 3.3.3.5.6 设备
        专用术语"）恰好是句级切分的句界，标题会被切成 "3.3." / "3.5." /
        "6 设备专用术语" 这类残片（实测缺陷）。段落级不切标题行——段落级
        本就按空行断片，标题行与其后的正文同段时不该被拆开。
        """
        spans = list(self._protected_spans(s, e))
        if mode == _MODE_SENT:
            i = bisect.bisect_left(self._heading_starts_sorted, s)
            stops = self._heading_starts_sorted
            while i < len(stops) and stops[i] < e:
                off = stops[i]
                i += 1
                nl = text.find("\n", off)
                spans.append((off, e if nl == -1 else min(nl, e)))
        spans.sort()
        merged: List[Tuple[int, int]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def _glue_heading_units(self, text: str,
                            units: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """纯标题单元粘合：并进其后单元（标题不单独成块）

        纯标题单元（"# 章"、"## 3.3 节" 这类只有标题行的内容）成块时没有正文
        相伴，检索价值极低，且极易变成几字符的碎片块——两种触发场景：
        1. 装箱阶段：标题单元与后继单元都装得进同一块时本会自然合并，但后继
           单元自身超预算时标题会孤零零成块（如章标题下第一个小节就是长正文）；
        2. 兜底二次切分：标题段落与后一段装不进同一片时同理。
        粘合把标题的行首并到后继单元/段落的起点上，标题于是总与其后的首句
        同块同片（末尾纯标题单元无后继可并，保持原样）。
        """
        out: List[Tuple[int, int]] = []
        pending: int | None = None
        for s, e in units:
            if pending is not None:
                s = pending
            if self._is_heading_only_unit(text, s, e):
                pending = s
                continue
            out.append((s, e))
            pending = None
        if pending is not None:
            out.append((pending, units[-1][1]))
        return out

    @staticmethod
    def _text_units(text: str, s: int, e: int,
                    mode: str) -> List[Tuple[int, int]]:
        """区间内文本的切分单元（无保护区间参与）：段落或句子，纯空白单元丢弃"""
        if s >= e:
            return []
        seg = text[s:e]
        if mode == _MODE_SENT:
            return [(s + m.start(), s + m.end())
                    for m in _SENTENCE_RE.finditer(seg)]
        units: List[Tuple[int, int]] = []
        off = 0
        for part in seg.split("\n\n"):
            units.append((s + off, s + off + len(part)))
            off += len(part) + 2
        return units

    def _protected_spans(self, s: int, e: int) -> List[Tuple[int, int]]:
        """与区间 [s, e) 相交的保护区间（裁剪到区间内、合并重叠、升序）"""
        spans = [(max(ps, s), min(pe, e)) for ps, pe in self.protected_ranges
                 if ps < e and pe > s]
        spans.sort()
        merged: List[Tuple[int, int]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def _is_protected_span(self, s: int, e: int) -> bool:
        """区间是否就是某个保护区间（完整落在其内部）：超长保护区间整体成块"""
        return any(ps <= s and e <= pe for ps, pe in self.protected_ranges)

    def _hard_cut(self, text: str, s: int, e: int) -> List[Tuple[int, int]]:
        """硬切兜底（无任何可分界的超长片段）：按 chunk_size 步进

        切点不得落在原子区间内部（保护区间：表格/代码块/图片；标题行：标题
        文本里的编号点号在句级里也是句界）——顺延到区间末尾（表格宁可整块
        超长、标题宁可整行超长，也不被切开，与项目既有语义一致）。
        """
        spans = self._atomic_spans(text, s, e, _MODE_SENT)
        out: List[Tuple[int, int]] = []
        pos = s
        while pos < e:
            nxt = min(pos + self.chunk_size, e)
            while nxt < e:
                hit = next(((ps, pe) for ps, pe in spans if ps < nxt < pe), None)
                if hit is None:
                    break
                nxt = min(hit[1], e)
            if nxt <= pos:  # 防御：区间异常时按 chunk_size 前进，不得死循环
                nxt = min(pos + self.chunk_size, e) or e
            out.append((pos, nxt))
            pos = nxt
        return out

    def _apply_overlap(self, text: str, pieces: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """overlap 续接（仅在兜底二次切分内部生效，见类 docstring）

        后续片起点回移 overlap 字符与上一片尾部重叠：回移不越过上一片起点
        （避免整段被吞），起点对齐到**下一行行首**（兜底切分的片多为段落/行级
        边界，按字符数硬回移会从词中间开头——如 "V1.0" 只留 "1.0**"，
        块首出现半截标记/半句话），不落入保护区间内部（保护区间不可被 overlap
        切进内部，落进去则放弃本次回移）。回移只改起点偏移，text 仍等于原文切片。
        """
        if self.overlap <= 0 or len(pieces) <= 1:
            return pieces
        out: List[Tuple[int, int]] = [pieces[0]]
        for ps, pe in pieces[1:]:
            prev_s, prev_e = out[-1]
            ns = max(prev_e - self.overlap, prev_s + 1)
            nl = text.find("\n", ns, ps)  # 行内不回移：对齐到下一行行首
            if nl != -1:
                ns = nl + 1
            if ns >= ps or any(qs < ns < qe for qs, qe in self.protected_ranges):
                ns = ps
            out.append((ns, pe))
        return out

    # ---- 成块 ----

    @staticmethod
    def _make_chunk(text: str, s: int, e: int) -> Chunk:
        """原文区间 [s, e) 成块：strip 前后空白并重算偏移
        （保证 text == full[char_start:char_end]，与项目既有切块器一致）"""
        raw = text[s:e]
        stripped = raw.strip()
        cs = s + (len(raw) - len(raw.lstrip()))
        return Chunk(stripped, cs, cs + len(stripped))
