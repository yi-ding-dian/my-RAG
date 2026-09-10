"""父子分块（对齐 KnowFlow parent_child 语义）

父块按标题聚合完整章节（无大小上限，超长单节按 parent_chunk_size 兜底）；
子块按标题边界断章后段内递归字符切（不跨章节、overlap 不跨章）。
入库只存子块（metadata 带父块全文），检索按 retrieval_mode 决定返回
子块或附带父块上下文。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from backend.chunking.base import Chunk
from backend.chunking.common import (_filter_continuous_headings, _iter_headings,
                                     find_protected_ranges)
from backend.chunking.recursive import RecursiveChunker

@dataclass
class ParentChildChunkResult:
    """父子分块结果：子块（入向量库）、父块（上下文）、子块→父块归属映射
    （child_parent_map 值为父块索引，-1 表示无父块）"""

    children: List[Chunk]
    parents: List[Chunk]
    child_parent_map: Dict[int, int]


# 父块超长兜底阈值：单节超过该字符数视为极端情况（如无标题长文），
# 按 parent_chunk_size 二次切分兜底（正常章节不受 parent_chunk_size 限制）
_PARENT_SECTION_FALLBACK_CHARS = 50_000
# 子块标题边界最大层级（KnowFlow 用 H1~H3；与父块层级取 min，见类 docstring）
_CHILD_HEADING_MAX_LEVEL = 3

# 标题行匹配（判断"段内是否只有标题行"用；含纯文本编号式标题特征）
_TITLE_LINE_RE = re.compile(r"^#{1,6}\s+\S")


def _is_heading_only(seg: str) -> bool:
    """段是否"只有标题行"（无正文内容）

    用于空标题段合并：段 strip 后仅一行（或纯标题行序列无正文）→ True。
    例："## 一、章标题"（其后直接跟下一层级标题、无正文）→ True。
    """
    lines = [ln.strip() for ln in seg.strip().split("\n") if ln.strip()]
    if not lines:
        return True
    return all(_TITLE_LINE_RE.match(ln) for ln in lines)


class ParentChildChunker:
    """父子分块（对齐 KnowFlow parent_child 语义）

    1. 父块：按 markdown 标题（1~parent_split_level 级）聚合完整章节，
       无大小上限（章节完整，标题行包含在块内）；首个标题前的无标题
       文本（文档头）作为独立父块；防极端情况：单节超过 50_000 字符
       （如无标题长文）按 parent_chunk_size 二次切分兜底——
       parent_chunk_size 是"兜底上限"而非目标大小，正常章节不受其限制
    2. 子块：标题边界层级 = min(3, parent_split_level)（KnowFlow 用
       H1~H3；父块层级 <3 时随父块收紧，保证章节段粒度不粗于父块）。
       全文先按该层级标题切成章节段，段内 RecursiveChunker(chunk_size,
       overlap) 递归切：任何子块不跨章节边界，overlap 只在章节段内部
       生效；章节段以标题行开头 → 段内首个子块自带标题行（便于识别
       章节归属，与 KnowFlow"标题注入"思路一致）
    3. 归属：子块按章节段切分、父块按章节聚合 → 子块 char 区间完整
       落在父块区间内；仅超长单节兜底的字符级父块（无章节语义）边界
       可能被子块 overlap 尾巴跨过 → 回退"起始偏移归属"
    4. 增强（对齐 KnowFlow AST 语义）：
       - 表格/代码块完整性：标题边界避开表格（连续 | 行）与 ``` 围栏
         代码块内部（块内 # 行不是标题）；段内递归字符切分同样避开，
         表格/代码块作为整体归入某块（超长可整体成块）；
       - 连续标题不切：直接相邻的标题行（中间无空行/无正文）不各自成
         段，并入后续内容段（不产生纯标题空父块/空子块）
    """

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None,
                 parent_chunk_size: int = 1024, parent_chunk_overlap: int = 100,
                 parent_split_level: int = 2,
                 heading_systems: List[str] | None = None):
        self._child_splitter = RecursiveChunker(chunk_size=chunk_size,
                                                overlap=overlap)
        # 兜底切分器：仅超长单节（>50_000 字符）按 parent_chunk_size 二次切分时用
        self._fallback_splitter = RecursiveChunker(
            chunk_size=parent_chunk_size, overlap=parent_chunk_overlap)
        if not 1 <= parent_split_level <= 6:
            raise ValueError(f"parent_split_level 超出范围: {parent_split_level}（需 1~6）")
        self.parent_split_level = parent_split_level
        # 标题编号体系（如"一、"/"1.1"等推断真实层级；None=仅按 # 数量）
        self.heading_systems = heading_systems or []
        # 子块边界层级：
        # - 有编号体系（级别已归一化为文档内相对层级 1=章/2=节/3=小节…）：
        #   子块比父块深一级（父 2 级聚合章 → 子切到 3 级节段粒度）
        # - 无体系（# 层级语义）：沿用历史 min(3, parent)（KnowFlow H1~H3，
        #   父块层级 <3 时随父块收紧，保证章节段粒度不粗于父块）
        self.child_split_level = (
            min(parent_split_level + 1, 6) if self.heading_systems
            else min(_CHILD_HEADING_MAX_LEVEL, parent_split_level))
        self._result: ParentChildChunkResult | None = None

    def chunk(self, text: str) -> List[Chunk]:
        """协议兼容入口：返回子块（同时计算并缓存父块与映射）"""
        result = self.chunk_parent_child(text)
        return result.children

    def chunk_parent_child(self, text: str) -> ParentChildChunkResult:
        """执行父子分块，返回完整结果（子块/父块/映射）"""
        if not text or not text.strip():
            self._result = ParentChildChunkResult(children=[], parents=[],
                                                  child_parent_map={})
            return self._result
        # 表格/代码块保护区间：标题边界避开（代码块内的 # 行不是标题），
        # 段内递归字符切分避开（表格/代码块作为整体归入某块，不切开）
        protected = find_protected_ranges(text)
        # 标题 = (偏移, 推断级别)：_iter_headings 统一识别（ATX + 纯文本样式），
        # heading_systems 提供时按编号推断级别（MinerU 全 ## 扁平输出恢复层级）
        headings = [(off, lvl) for off, lvl, _ in
                    _iter_headings(text, protected, self.heading_systems)]
        self._protected_ranges = protected
        self._child_splitter.protected_ranges = protected
        self._fallback_splitter.protected_ranges = protected
        parents = self._build_parents(text, headings)
        children = self._build_children(text, headings)
        self._result = ParentChildChunkResult(
            children=children, parents=parents,
            child_parent_map=self._map_children_to_parents(children, parents),
        )
        return self._result

    def get_parent_chunks(self) -> List[Chunk]:
        """父块列表（须先调用 chunk/chunk_parent_child，否则抛错）"""
        if self._result is None:
            raise RuntimeError("尚未执行切块，无法获取父块（请先调用 chunk/chunk_parent_child）")
        return self._result.parents

    def get_mapping(self) -> Dict[int, int]:
        """子块索引 → 父块索引 映射（须先调用 chunk/chunk_parent_child，否则抛错）"""
        if self._result is None:
            raise RuntimeError("尚未执行切块，无法获取映射（请先调用 chunk/chunk_parent_child）")
        return self._result.child_parent_map

    @staticmethod
    def _split_sections(text: str, headings: List[Tuple[int, int]], level: int,
                        start: int = 0, end: int | None = None,
                        merge_heading_only: bool = False) -> List[Tuple[int, int]]:
        """按标题层级切章节段：返回 [(start, end)] 半开区间（段含标题行）

        - headings = [(偏移, 级别)]（_iter_headings 产物：含编号体系推断级别）；
        - 边界 = level 级以内标题行的起点（连续标题不切：直接相邻的标题
          行不各自成段，并入后续内容段）；段 = [边界标题起点, 下一边界起点)
        - 首个边界前的文本（文档头/节内前置正文，strip 后非空）为独立段
        - 区间 [start, end) 内无边界 → 整段为唯一章节段
        - merge_heading_only：空标题段合并开关（**仅编号体系启用时开**）：
          某段只有标题行（无正文）时并入其后一段——"## 一、章标题\n\n
          ## 1.1 节标题\n正文…"（MinerU 扁平输出常见：章标题后直接
          跟节标题，章标题不应孤悬成独立空块）。标准 markdown
          文档（#/##/### 层级本身表意）不合并，每级标题都是独立章节边界。
        """
        end = len(text) if end is None else end
        bounds = [off for off, lvl in headings
                  if start <= off < end and lvl <= level]
        bounds = _filter_continuous_headings(text, bounds, start=start)
        if not bounds:
            return [(start, end)]
        sections: List[Tuple[int, int]] = []
        if bounds[0] > start and text[start:bounds[0]].strip():
            sections.append((start, bounds[0]))
        for i, b in enumerate(bounds):
            b_end = bounds[i + 1] if i + 1 < len(bounds) else end
            # 空标题段（段内仅标题行、无正文）→ 并入后一段（仅编号体系启用时）
            if (merge_heading_only and i + 1 < len(bounds)
                    and _is_heading_only(text[b:b_end])):
                continue
            sections.append((b, b_end))
        return sections

    @staticmethod
    def _make_chunk(text: str, s: int, e: int) -> Chunk:
        """原文区间 [s, e) 切块：strip 前后空白并重算偏移
        （保证 text == full[char_start:char_end] 切片一致）"""
        raw = text[s:e]
        stripped = raw.strip()
        start = s + (len(raw) - len(raw.lstrip()))
        return Chunk(stripped, start, start + len(stripped))

    @staticmethod
    def _clamp_chunk(chunk: Chunk, lo: int, hi: int, full: str) -> Chunk | None:
        """子块区间夹回章节段边界 [lo, hi)

        段首块长度 < overlap 时，RecursiveChunker 的 overlap 续接会把起点
        回移到段起点之前（吞掉段前空白甚至上一段内容），且该块文本与
        偏移不一致——按原文切片重建，保证子块完整落在段内且
        text == full[char_start:char_end]；夹空（段前内容整块被吞）返回 None
        """
        cs, ce = chunk.char_start, chunk.char_end
        if cs >= lo and ce <= hi:
            return chunk
        cs2, ce2 = max(cs, lo), min(ce, hi)
        if cs2 >= ce2:
            return None
        return Chunk(full[cs2:ce2], cs2, ce2)

    def _build_parents(self, text: str, headings: List[Tuple[int, int]]) -> List[Chunk]:
        """父块：完整章节（无大小上限）；超长单节按 parent_chunk_size 兜底"""
        parents: List[Chunk] = []
        for s, e in self._split_sections(
                text, headings, self.parent_split_level,
                merge_heading_only=bool(self.heading_systems)):
            if not text[s:e].strip():
                continue
            if e - s > _PARENT_SECTION_FALLBACK_CHARS:
                parents.extend(self._fallback_split_section(text, headings, s, e))
            else:
                parents.append(self._make_chunk(text, s, e))
        return parents

    def _fallback_split_section(self, text: str, headings: List[Tuple[int, int]],
                                s: int, e: int) -> List[Chunk]:
        """超长单节兜底：节内子段（子块章节边界）按 parent_chunk_size 贪心聚合

        - 子段 = 节内 child_split_level 级标题边界切出的完整章节段，
          聚合父块由完整子段构成 → 子块仍完整落在父块内；
        - 单个子段本身超 parent_chunk_size（如无标题长文的唯一段）：
          交给 _fallback_splitter 字符级切分（其边界无章节语义，
          归属回退起始偏移）
        """
        sub_sections = self._split_sections(text, headings, self.child_split_level,
                                            start=s, end=e,
                                            merge_heading_only=bool(self.heading_systems))
        parents: List[Chunk] = []
        bad: List[Tuple[int, int]] = []
        cur_start, cur_len = -1, 0
        for ss, se in sub_sections:
            seg_len = se - ss
            if seg_len > self._fallback_splitter.chunk_size:
                # 单子段超长：封当前聚合块，该子段走字符级切分
                if cur_start >= 0:
                    parents.append(self._make_chunk(text, cur_start, ss))
                    cur_start, cur_len = -1, 0
                bad.append((ss, se))
            elif cur_start < 0 or cur_len + seg_len <= self._fallback_splitter.chunk_size:
                if cur_start < 0:
                    cur_start, cur_len = ss, seg_len
                else:
                    cur_len += seg_len
            else:
                parents.append(self._make_chunk(text, cur_start, ss))
                cur_start, cur_len = ss, seg_len
        if cur_start >= 0:
            parents.append(self._make_chunk(text, cur_start, e))
        for bs, be in bad:
            seg = text[bs:be]
            stripped = seg.strip()
            start = bs + (len(seg) - len(seg.lstrip()))
            end = start + len(stripped)
            for c in self._fallback_splitter._split_text(stripped, start):
                clamped = self._clamp_chunk(c, start, end, text)
                if clamped and clamped.text and clamped.text.strip():
                    parents.append(clamped)
        return parents

    def _build_children(self, text: str, headings: List[Tuple[int, int]]) -> List[Chunk]:
        """子块：章节段内递归字符切（overlap 只在段内生效，不跨章节）"""
        children: List[Chunk] = []
        for s, e in self._split_sections(
                text, headings, self.child_split_level,
                merge_heading_only=bool(self.heading_systems)):
            seg = text[s:e]
            stripped = seg.strip()
            if not stripped:
                continue
            start = s + (len(seg) - len(seg.lstrip()))
            end = start + len(stripped)
            for c in self._child_splitter._split_text(stripped, start):
                # clamp：段首块过短时 overlap 续接起点会越过段起点，夹回段内
                clamped = self._clamp_chunk(c, start, end, text)
                if clamped and clamped.text and clamped.text.strip():
                    children.append(clamped)
        return children

    @staticmethod
    def _map_children_to_parents(children: List[Chunk],
                                 parents: List[Chunk]) -> Dict[int, int]:
        """子块归属父块：区间完整包含优先（子块按章节切分，天然完整落在
        其章节聚合的父块内）；兜底字符级父块（无章节语义）边界可能被
        子块 overlap 尾巴跨过 → 回退"起始偏移归属"；再回退前最近父块"""
        mapping: Dict[int, int] = {}
        for idx, child in enumerate(children):
            p_idx = -1
            for pi, parent in enumerate(parents):
                if parent.char_start <= child.char_start and child.char_end <= parent.char_end:
                    p_idx = pi
                    break
            if p_idx == -1:
                # 回退：起始偏移落在父块区间（兜底字符级父块边界无章节语义）
                for pi, parent in enumerate(parents):
                    if parent.char_start <= child.char_start < parent.char_end:
                        p_idx = pi
                        break
            if p_idx == -1 and parents:
                # 子块起始落在父块间空白空隙：归其前最近父块；无则归首个父块
                for pi in range(len(parents) - 1, -1, -1):
                    if parents[pi].char_start <= child.char_start:
                        p_idx = pi
                        break
                if p_idx == -1:
                    p_idx = 0
            mapping[idx] = p_idx
        return mapping

