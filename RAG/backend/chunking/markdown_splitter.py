"""按标题切块（Markdown # 标题 + 纯文本常见标题样式），标题保留在块首；
超长段递归 RecursiveChunker（title 切块实现）

增强：表格/代码块完整性、标题智能回退、连续标题不切（见 common.py）。
"""
from __future__ import annotations

from typing import List, Tuple

from backend.chunking.base import Chunk
from backend.chunking.common import (_bounds_outside_protected,
                                     _filter_continuous_headings, _iter_headings,
                                     find_protected_ranges)
from backend.chunking.recursive import RecursiveChunker

class MarkdownSplitter:
    """按标题切块（Markdown # 标题 + 纯文本常见标题样式），标题保留在块首；
    超长段递归 RecursiveChunker

    split_level: 参与切分的最大标题层级 1-6（默认 3）。ATX '# 标题' 按 #
    数量计级；纯文本样式级别映射（见 _iter_headings）：setext = 下划线 → 1 级、
    - 下划线 → 2 级；单行包裹式（===== 标题 ===== 等）与前导符号式（■ 标题 等）
    → 2 级。如传 2 则仅 1~2 级标题切分，### 归入上层块；6 级为父子分块父块
    聚合预留。

    增强（对齐 KnowFlow title 方式）：
    - 表格/代码块完整性：切分边界避开表格（连续 | 行）与 ``` 围栏代码块
      内部，保护区间作为整体归入某块（超长可整体成块）；
    - 标题智能回退：按 split_level 切只切出 1 个超长块时，放宽一级标题
      重切（直至多块/6 级/无更低级标题），避免"章节太长只出一个块"；
    - 连续标题不切：直接相邻的标题行（中间无空行/无正文）不各自成块，
      并入后续内容块（不产生纯标题空块）。
    """

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None,
                 split_level: int | None = None):
        self._recursive = RecursiveChunker(chunk_size=chunk_size, overlap=overlap)
        level = split_level if split_level is not None else 3
        if not 1 <= level <= 6:
            raise ValueError(f"split_level 超出范围: {level}（需 1~6）")
        self.split_level = level

    @staticmethod
    def _heading_bounds(text: str, level: int,
                        protected: List[Tuple[int, int]]) -> List[int]:
        """level 级内标题切分边界（升序）：ATX # 与纯文本标题样式统一识别
        （setext 下划线式/单行包裹式/前导符号式，级别映射见 _iter_headings），
        过滤保护区内标题 + 连续标题不切"""
        bounds = [start for start, lvl, _ in _iter_headings(text, protected)
                  if lvl <= level]
        bounds = _bounds_outside_protected(bounds, protected)
        return _filter_continuous_headings(text, bounds)

    @staticmethod
    def _sections(text: str, bounds: List[int]) -> List[Tuple[str, int]]:
        """按边界切章节段（前瞻式：边界标题保留在段首），返回 (段文本, 全局起始)"""
        parts: List[Tuple[str, int]] = []
        cursor = 0
        for b in bounds:
            parts.append((text[cursor:b], cursor))
            cursor = b
        parts.append((text[cursor:], cursor))
        return parts

    @staticmethod
    def _clamp_chunk(chunk: Chunk, lo: int, hi: int, full: str) -> Chunk | None:
        """递归切块区间夹回章节段边界 [lo, hi)

        段首块长度 < overlap 时，RecursiveChunker 的 overlap 续接会把起点
        回移到段起点之前（吞掉段前空白甚至上一节内容），且该块文本与
        偏移不一致——按原文切片重建，保证块完整落在段内且
        text == full[char_start:char_end]；夹空（段前内容整块被吞）返回 None
        """
        cs, ce = chunk.char_start, chunk.char_end
        if cs >= lo and ce <= hi:
            return chunk
        cs2, ce2 = max(cs, lo), min(ce, hi)
        if cs2 >= ce2:
            return None
        return Chunk(full[cs2:ce2], cs2, ce2)

    def chunk(self, text: str) -> List[Chunk]:
        if not text or not text.strip():
            return []
        protected = find_protected_ranges(text)
        # 超长段递归切分器（带保护区：表格/代码块作为整体不切开）
        recursive = RecursiveChunker(
            chunk_size=self._recursive.chunk_size,
            overlap=self._recursive.overlap,
            separators=self._recursive.separators,
            protected_ranges=protected)
        level = self.split_level
        bounds = self._heading_bounds(text, level, protected)
        parts = self._sections(text, bounds)
        # 标题智能回退：非空段仅 1 个且超长 → 放宽一级标题重切（直至多段
        # /6 级/无更低级标题——避免无标题长文的无谓循环）
        nonempty = [(t, s) for t, s in parts if t.strip()]
        while (len(nonempty) == 1 and level < 6
               and len(nonempty[0][0]) > self._recursive.chunk_size):
            level += 1
            retry = self._heading_bounds(text, level, protected)
            if len(retry) == len(bounds):
                break  # 无更低级标题可切（或新增标题全部被连续标题过滤）
            bounds = retry
            parts = self._sections(text, bounds)
            nonempty = [(t, s) for t, s in parts if t.strip()]
        # 逐段处理：超长段段内递归字符切（表格/代码块不切开）
        chunks: List[Chunk] = []
        for part_text, part_start in parts:
            stripped = part_text.strip()
            if not stripped:
                continue
            # strip 后重算起始（去掉前导空白）
            start = part_start + (len(part_text) - len(part_text.lstrip()))
            if len(stripped) <= self._recursive.chunk_size:
                chunks.append(Chunk(stripped, start, start + len(stripped)))
            else:
                # clamp：段首块过短时 overlap 续接起点会越过段起点（甚至
                # 吞掉上一节内容），夹回段内保证块不跨标题边界
                for c in recursive._split_text(stripped, start):
                    clamped = self._clamp_chunk(c, start, start + len(stripped), text)
                    if clamped and clamped.text and clamped.text.strip():
                        chunks.append(clamped)
        return chunks

