"""递归字符切块（仿 langchain RecursiveCharacterTextSplitter 逻辑）

naive 切块的字符级切分器；title/parent_child 也用它做段内递归切分。
"""
from __future__ import annotations

import re
from typing import List, Tuple

from backend.chunking.base import Chunk
from backend.chunking.common import find_protected_ranges
from backend.config import get_active_config

# 图片引用（markdown ![alt](src) / HTML <img>）：块长度统计时**不计入**——
# 图片 URL 是资源引用（每张约 100 字符的长哈希路径）而非语义内容，按原始
# 长度计会让含图文档的正文配额被吃光（512 字符的块里 300+ 被 URL 占掉，
# 正文只装 100 余字就被迫切块）。按有效内容（去图片引用）计算 chunk_size
# 预算：图文块能容纳完整正文，视觉上"一块"的内容不会被 URL 拆开。
_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)|<img\b[^>]*>", re.IGNORECASE)

# 块真实长度硬上限（chunk_size 的倍数）：图片引用不计入有效长度，但极端的
# "图片密集"块（几十张图 URL）真实长度可能过大（embedding token / LLM 上下文
# 吃紧）→ 真实长度超过 chunk_size × 本系数时不再合并
_MAX_RAW_LEN_FACTOR = 4

class RecursiveChunker:
    """递归字符切块（仿 langchain RecursiveCharacterTextSplitter 逻辑）

    内部算法基于 (文本, 起始偏移) 元组操作，保证每块都能回溯原文位置；
    overlap 续接时新块 = 当前块尾部 overlap 字符 + 分隔符 + 新段，
    起始偏移 = 原起始 + 原长度 - overlap。

    protected_ranges: 表格/代码块等不可切分区间（全局偏移，默认空）；
    传入时保护区间整体成块（超长也保留完整，不切开），区间外文本照常
    递归切分；naive/regex 不传 → 行为与历史完全一致。
    """

    DEFAULT_SEPARATORS = ["\n\n", "\n", "。", "；", " ", ""]

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None,
                 separators: List[str] | None = None,
                 delimiter: str | None = None,
                 protected_ranges: List[Tuple[int, int]] | None = None,
                 sentence_aware: bool = True):
        cfg = get_active_config().chunking
        self.chunk_size = chunk_size if chunk_size is not None else cfg.chunk_size
        self.overlap = overlap if overlap is not None else cfg.chunk_overlap
        if separators is not None:
            self.separators = separators
        elif delimiter:
            # 自定义分隔符（naive 模式参数，str 或 list）：
            # - str：自定义优先，超长段递归退到更细的默认分隔符（兼容旧行为）
            # - list：用户完整自定义分隔符集（删除默认项=该项不参与）；
            #   空列表/全无效时回退默认，防切分退化
            if isinstance(delimiter, list):
                self.separators = [d for d in delimiter
                                   if isinstance(d, str) and d != ""]
                if not self.separators:
                    self.separators = self.DEFAULT_SEPARATORS
            else:
                self.separators = [delimiter, *self.DEFAULT_SEPARATORS]
        else:
            self.separators = self.DEFAULT_SEPARATORS
        self.protected_ranges = protected_ranges or []
        # 句子感知切分：块边界优先落在句子（。！？）之间，单句超长才句内切
        self._sentence_aware = sentence_aware
        # 自定义分隔符列表（naive 完整替代默认集）：句子感知时也作为强边界
        # （分隔符不进块），实现"删默认项/加自定义项"语义
        self._custom_delimiter_list = delimiter if isinstance(delimiter, list) else None

    def chunk(self, text: str) -> List[Chunk]:
        # 保护区间兜底：调用方未显式传 protected_ranges 时自动识别
        # （表格/代码块/图片引用作为整体成块——naive 直用也吃保护；
        # MarkdownSplitter 已显式传入，此处自动跳过保持行为一致）
        if not self.protected_ranges:
            self.protected_ranges = find_protected_ranges(text)
        chunks = self._split_sentence_aware(text) if self._sentence_aware \
            else self._split_text(text)
        return [c for c in chunks if c.text and c.text.strip()]

    # 句子边界标点（中英文句号/问号/感叹号——强句界；分号/逗号属句内
    # 分隔符，留给句内递归按分隔符优先级切，避免列举文本过度切碎）
    _SENTENCE_BOUNDARY_RE = re.compile(
        r"[^。！？.!?]+[。！？.!?]?")

    def _atomic_units(self, text: str, start: int) -> List[Tuple[int, int]]:
        """原子单元收集（全局偏移，升序互不重叠）：切块的最小不可分单位

        - 保护区间（图片/表格/代码块，含标题扩展）整体为一个单元——保证
          图片标记/表格不被切碎，但**单元之间可贪心合并成块**；
        - 保护区间之外的文本按 \\n\\n 段落切分，自定义分隔符在段内二级切
          （分隔符不进块，作强边界）。

        设计（修复碎片化）：此前"保护区间独立成块 / 图片附着前块"的写法，
        在"图片与短段落交替"的解析产物里会把文本流切碎（图片独占一段隔开
        前后文字 → 每个短段独立成块，产生大量 2~10 字符碎片块，检索命中
        无意义）。改为"单元 + 合并"后，图片与前后短段自然合并为完整块。

        base 语义：text 为片段文本，start 为片段在全文的起始偏移；返回的
        区间为全局偏移，切片时按 s - start 换算回片段内索引。
        """
        total_end = start + len(text)
        # 保护区间（与当前范围相交，裁剪到范围内）
        spans: List[Tuple[int, int]] = []
        for ps, pe in self.protected_ranges:
            if pe <= start or ps >= total_end:
                continue
            spans.append((max(ps, start), min(pe, total_end)))
        spans.sort()
        # 段落单元
        paras: List[Tuple[int, int]] = []
        pattern = ""
        if self._custom_delimiter_list:
            pattern = "|".join(
                re.escape(d) for d in self._custom_delimiter_list if d)
        parts = text.split("\n\n")
        offset = start
        for i, para in enumerate(parts):
            p_start = offset
            p_end = p_start + len(para)
            offset = p_end + (2 if i < len(parts) - 1 else 0)
            if not para.strip():
                continue
            if pattern:
                cursor = p_start
                for m in re.finditer(pattern, para):
                    seg_end = p_start + m.start()
                    if text[cursor - start:seg_end - start].strip():
                        paras.append((cursor, seg_end))
                    cursor = p_start + m.end()
                if text[cursor - start:p_end - start].strip():
                    paras.append((cursor, p_end))
            else:
                paras.append((p_start, p_end))
        # 组装：保护区间占位，段落按序插入（与保护区间重叠的段落已含在区间内）
        units: List[Tuple[int, int]] = []
        pi = 0
        for lo, hi in spans:
            while pi < len(paras) and paras[pi][1] <= lo:
                units.append(paras[pi])
                pi += 1
            while pi < len(paras) and paras[pi][0] < hi:
                pi += 1
            units.append((lo, hi))
        while pi < len(paras):
            units.append(paras[pi])
            pi += 1
        units.sort()
        # 相邻/重叠单元合并（保护区间可能相接）
        merged: List[Tuple[int, int]] = []
        for s, e in units:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return merged

    def _effective_len(self, text: str, base: int, s: int, e: int) -> int:
        """块有效长度：区间文本去掉图片引用后的字符数（图片 URL 不计入
        chunk_size 预算；见 _IMAGE_REF_RE 说明）"""
        if s >= e:
            return 0
        seg = text[s - base:e - base]
        if "![" not in seg and "<img" not in seg.lower():
            return e - s
        url_len = sum(len(m.group(0)) for m in _IMAGE_REF_RE.finditer(seg))
        return (e - s) - url_len

    def _merge_units(self, units: List[Tuple[int, int]], text: str, base: int,
                     merge: bool = True) -> List[Tuple[int, int]]:
        """贪心合并原子单元为块区间：连续单元合并到接近 chunk_size

        - 长度按**有效内容**计（图片引用不计入，见 _effective_len）；
        - 单个超长单元（>= chunk_size）独占一块，由调用方二次切；
        - merge=False（自定义分隔符场景）：单元不跨分隔符合并——分隔符语义
          是"用户指定的强边界"，块不可越过（块 = 子段区间，原文切片天然
          不含分隔符）；
        - overlap 续接：块起始回移 overlap 字符（与上一块重叠，块间语义
          连续；不越过上一块起点，也不越过本块原起点；回移不落入保护区内）。
        """
        if not merge:
            return list(units)
        blocks: List[Tuple[int, int]] = []
        i, n = 0, len(units)
        while i < n:
            s, e = units[i]
            if self._effective_len(text, base, s, e) >= self.chunk_size:
                blocks.append((s, e))  # 超长单元独占（调用方二次切）
                i += 1
                continue
            j = i + 1
            while j < n:
                ns, ne = units[j]
                if (self._effective_len(text, base, s, ne) > self.chunk_size
                        or self._effective_len(text, base, ns, ne)
                        >= self.chunk_size
                        or ne - s > self.chunk_size * _MAX_RAW_LEN_FACTOR):
                    break
                e = ne
                j += 1
            blocks.append((s, e))
            i = j
        if self.overlap > 0 and len(blocks) > 1:
            out: List[Tuple[int, int]] = [blocks[0]]
            for k in range(1, len(blocks)):
                s, e = blocks[k]
                prev_s, prev_e = out[-1]
                ns = max(prev_e - self.overlap, prev_s + 1)
                # 回移起点不得落在保护区间内部（保护区间不可分：图片/表格/
                # 代码块不能被 overlap 切进内部）→ 放弃本次 overlap
                if any(ps < ns < pe for ps, pe in self.protected_ranges):
                    out.append((s, e))
                    continue
                out.append((min(ns, s), e))
            blocks = out
        return blocks

    def _is_protected_unit(self, s: int, e: int) -> bool:
        """单元是否为保护区间（表格/代码块/图片，含标题扩展）：超长保护单元
        整体成块不再二次切（表格/代码块完整性优先，超长也可整体成块）"""
        return any(ps <= s and e <= pe for ps, pe in self.protected_ranges)

    def _split_sentence_aware(self, text: str, start: int = 0) -> List[Chunk]:
        """句子感知切分：原子单元（保护区间 + 段落）贪心合并成块；
        单个超长单元（> chunk_size）走句子感知二次切（块边界在句子之间，
        不切断句子；单句超长递归兜底）；超长保护单元（表格/代码块）整体成块。"""
        if not text:
            return []
        units = self._atomic_units(text, start)
        # 自定义分隔符语义 = 用户指定的强边界：单元不跨分隔符合并
        blocks = self._merge_units(units, text, start,
                                   merge=not self._custom_delimiter_list)
        chunks: List[Chunk] = []
        for s, e in blocks:
            if (self._effective_len(text, start, s, e) <= self.chunk_size
                    or self._is_protected_unit(s, e)):
                chunks.append(self._slice_chunk(text, start, s, e))
            else:
                chunks.extend(
                    self._split_piece_sentences(text, start, s, e))
        return chunks

    @staticmethod
    def _slice_chunk(text: str, base: int, s: int, e: int) -> Chunk:
        """原文区间 [s, e) 成块（base = 片段文本在全文的起始偏移，s/e 为
        全局偏移；切片换算为片段内索引）：strip 前后空白并重算偏移
        （保证 text == full[char_start:char_end] 切片一致）"""
        raw = text[s - base:e - base]
        stripped = raw.strip()
        cs = s + (len(raw) - len(raw.lstrip()))
        return Chunk(stripped, cs, cs + len(stripped))

    def _split_piece_sentences(self, text: str, base: int, s: int,
                               e: int) -> List[Chunk]:
        """单个超长片段（> chunk_size）的句子感知切：按句子边界（。！？.!?）
        切句子单元后贪心合并——块边界永远在句子之间；单句超长（无标点长串）
        走句内递归切（_split_text 兜底）。含 overlap 续接。
        （base = 片段文本在全文的起始偏移；s/e 为全局偏移）"""
        seg_text = text[s - base:e - base]
        sentences = [
            (m.group(), s + m.start())
            for m in self._SENTENCE_BOUNDARY_RE.finditer(seg_text)
            if m.group().strip()]
        chunks: List[Chunk] = []
        buf: str = ""
        buf_start: int = s
        for st, st_start in sentences:
            if len(st) > self.chunk_size:
                # 超长单句（无标点长串）：先封缓冲，再句内递归切
                if buf:
                    chunks.append(Chunk(buf, buf_start, buf_start + len(buf)))
                    buf = ""
                chunks.extend(self._split_text(st, st_start))
                continue
            if buf and len(buf) + len(st) > self.chunk_size:
                # 加下句会超大小 → 封块（块边界在句子之间，不拆句）
                chunks.append(Chunk(buf, buf_start, buf_start + len(buf)))
                if self.overlap > 0:
                    # overlap 续接：取当前块尾部 overlap 字符
                    tail = buf[-self.overlap:]
                    buf_start = buf_start + len(buf) - self.overlap
                    buf = tail + st
                else:
                    buf, buf_start = st, st_start
            else:
                buf += st
        if buf:
            chunks.append(Chunk(buf, buf_start, buf_start + len(buf)))
        return chunks

    def _split_text(self, text: str, start: int = 0) -> List[Chunk]:
        """递归切分（分隔符优先级）：原子单元（保护区间 + 段落）贪心合并成块
        ——保护区间（图片/表格/代码块）不破损、短段落不碎片化；单个超长单元
        走分隔符优先级二次切（硬切兜底）。返回 List[Chunk]（start 为片段在
        全文的起始偏移）。"""
        if not text:
            return []
        units = self._atomic_units(text, start)
        # 自定义分隔符语义 = 用户指定的强边界：单元不跨分隔符合并
        blocks = self._merge_units(units, text, start,
                                   merge=not self._custom_delimiter_list)
        chunks: List[Chunk] = []
        for s, e in blocks:
            if (self._effective_len(text, start, s, e) <= self.chunk_size
                    or self._is_protected_unit(s, e)):
                chunks.append(self._slice_chunk(text, start, s, e))
            else:
                chunks.extend(
                    self._split_block_by_separators(text, start, s, e))
        return chunks

    def _split_block_by_separators(self, text: str, base: int, s: int,
                                   e: int) -> List[Chunk]:
        """超长单元的分隔符优先级切分（全局区间 [s, e)）：选第一个能切出
        多段的分隔符，片段贪心合并到不超过 chunk_size（含 overlap 续接）；
        无可用分隔符时按 chunk_size 硬切兜底；超长片段递归统一入口（换更细
        分隔符）。"""
        block = text[s - base:e - base]
        # 兜底初值必须是 ""（字符级硬切）而非最后一个分隔符：自定义分隔符集
        # （list 形态）会滤掉空串，此时 separators 里没有字符级兜底——若沿用
        # self.separators[-1]，在所有分隔符都切不出多段时会带着一个"切不动"的
        # 分隔符进入 bad_splits 递归，回到完全相同的状态 → 无限递归
        separator = ""
        new_splits: List[Tuple[str, int]] = []
        for sep in self.separators:
            if sep == "":
                new_splits = [(ch, s + i) for i, ch in enumerate(block)]
            else:
                # finditer + re.escape 等价于 str.split(sep)（含连续分隔符的空段），
                # 同时拿到每段的全局起始偏移
                new_splits = []
                cursor = 0
                for m in re.finditer(re.escape(sep), block):
                    new_splits.append((block[cursor:m.start()], s + cursor))
                    cursor = m.end()
                new_splits.append((block[cursor:], s + cursor))
            if len(new_splits) > 1 or sep == "":
                separator = sep
                break

        if separator == "":
            # 无任何可用分隔符：按 chunk_size 硬切
            return [Chunk(block[i:i + self.chunk_size], s + i,
                          s + min(i + self.chunk_size, len(block)))
                    for i in range(0, len(block), self.chunk_size)]

        final_chunks: List[Chunk] = []
        good_splits: List[Tuple[str, int]] = []
        bad_splits: List[Tuple[str, int]] = []
        cur_text = ""
        cur_start = 0
        for split_text, split_start in new_splits:
            if len(split_text) < self.chunk_size:
                if cur_text:
                    candidate = cur_text + separator + split_text
                    if len(candidate) <= self.chunk_size:
                        cur_text = candidate
                    else:
                        good_splits.append((cur_text, cur_start))
                        # 带 overlap 续接：新块 = 当前块尾部 overlap 字符 + 分隔符 + 本段
                        if self.overlap > 0:
                            tail = cur_text[-self.overlap:]
                            cur_start = cur_start + len(cur_text) - self.overlap
                            cur_text = tail + separator + split_text
                        else:
                            cur_text = split_text
                            cur_start = split_start
                else:
                    cur_text = split_text
                    cur_start = split_start
            else:
                if cur_text:
                    good_splits.append((cur_text, cur_start))
                    cur_text = ""
                bad_splits.append((split_text, split_start))
        if cur_text:
            good_splits.append((cur_text, cur_start))

        # 超长片段：统一入口递归（重新收集单元 → 更细分隔符 → 硬切兜底）
        for bad_text, bad_start in bad_splits:
            if len(bad_text) > self.chunk_size:
                final_chunks.extend(self._split_text(bad_text, bad_start))
            else:
                final_chunks.append(Chunk(bad_text, bad_start,
                                          bad_start + len(bad_text)))

        good_chunks = [Chunk(t, cs, cs + len(t)) for t, cs in good_splits]
        return good_chunks + final_chunks

