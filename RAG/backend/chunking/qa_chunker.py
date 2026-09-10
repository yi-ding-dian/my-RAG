"""QA 问答切块：问答对整块（问题段起，答案跨多段保留，含原文问/答标记）

analyze_qa_format / is_qa_format_valid 为规范性检测纯函数，入库前按占比
>=50% 判定合格，与切块器统计口径一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from backend.chunking.base import Chunk

# QA 问答切块：问题标记 = 块内行首（允许前导空白）"问/Q/q + 冒号"（全角：/半角: 均可）
_QA_QUESTION_RE = re.compile(r"(?:问|Q|q)\s*[:：]\s*")
# 答案标记（格式识别定义：段首"答/A/a + 冒号"；问答对聚合不依赖它——
# 答案可无标记、可跨多段，直到下一个问题标记为止）
_QA_ANSWER_RE = re.compile(r"(?:答|A|a)\s*[:：]\s*")


@dataclass
class QaStats:
    """QA 规范性统计：问答对数量 / 聚合段数（与 QaChunker 切块共用同一
    段落切分与问题块判定，口径一致）"""

    qa_pairs: int
    total_paragraphs: int


def _split_paragraphs(text: str) -> List[Tuple[str, int]]:
    """按空行/连续换行（\\n\\s*\\n）切分段落，返回 [(段文本, 段起始偏移)]

    - 空行 = 两个换行之间只含空白（含 \\n\\n / \\n \\n / 连续多个空行）；
    - 空白段（连续空行产生的空段）跳过，不计数、不参与切块。
    """
    parts: List[Tuple[str, int]] = []
    cursor = 0
    for m in re.finditer(r"\n\s*\n", text):
        if text[cursor:m.start()].strip():
            parts.append((text[cursor:m.start()], cursor))
        cursor = m.end()
    if text[cursor:].strip():
        parts.append((text[cursor:], cursor))
    return parts


def _block_has_question(seg: str) -> bool:
    """块内是否含问题标记行（逐行行首匹配 (?:问|Q|q)\\s*[:：]）

    - 问题标记允许不在块首：如"标题行\\n问：…？\\n答：…。"同块时，
      标题行不阻断问答对识别（标题并入该块，不单独成段）；
    - 同块多个问标记行按 1 个问题块计（与 QaChunker 块级切块一致）。
    """
    return any(_QA_QUESTION_RE.match(line.lstrip())
               for line in seg.splitlines())


def _count_qa_segments(paragraphs: List[str]) -> int:
    """问答对聚合段数（analyze_qa_format 专用统计口径）：

    - 问题块（块内含问题标记行）与其后所有非问题块合并为 1 个问答对段，
      答案可跨多个原始块（如示例3 答案 5 行连续段落）直至下一问题块；
    - 开头杂项（第一个问题块之前）：
      - 恰 1 块（如标题）→ 并入第一个问答对段，不拉低占比；
      - 多块（叙述文场景）→ 每块独立成段，拉低占比（保证"叙述文夹
        1 个问答对"类文档仍可能不达标）；
    - 全文无问题块 → 每个原始块 1 段（0 对，占比必为 0 不合格）。
    """
    q_idx = [i for i, seg in enumerate(paragraphs) if _block_has_question(seg)]
    if not q_idx:
        return len(paragraphs)
    first = q_idx[0]
    return len(q_idx) if first <= 1 else len(q_idx) + first


def analyze_qa_format(text: str) -> QaStats:
    """QA 规范性统计纯函数（与 QaChunker 共用 _split_paragraphs 段落切分
    与 _block_has_question 问题块判定，口径天然一致）：
    - 总段落数 = 按空行/连续换行切分后，再按问答对聚合的段数（问块+其后
      所有答案块=1 段；开头单块标题并入第一问答对；多块杂项独立成段）；
    - 问答对数量 = 含问题标记行（全角/半角冒号、大小写 Q/q 均可，允许
      块内非首行）的块数；答案跨多段不增加对数。
    """
    paragraphs = [seg for seg, _ in _split_paragraphs(text)]
    qa_pairs = sum(1 for seg in paragraphs if _block_has_question(seg))
    return QaStats(qa_pairs=qa_pairs,
                   total_paragraphs=_count_qa_segments(paragraphs))


def is_qa_format_valid(stats: QaStats, min_ratio: float = 0.5) -> bool:
    """QA 规范性判定：问答对占比（问答对 / 总段落）>= min_ratio（默认 50%）合格；
    空文档（无段落）视为不合格（无问答对可入库）"""
    if stats.total_paragraphs <= 0:
        return False
    return stats.qa_pairs / stats.total_paragraphs >= min_ratio


class QaChunker:
    """QA 问答切块：问答对整块（问题段起，答案跨多段保留，含原文问/答标记）

    - 段落 = 按空行/连续换行（\\n\\s*\\n）切分（与 analyze_qa_format 同口径，
      复用 _split_paragraphs 与 _block_has_question，判定一致）；
    - 问题块 = 块内任意行行首（允许前导空白）匹配问题标记 (?:问|Q|q)\\s*[:：]
      （全角/半角冒号、大小写；允许标题与问题行同块）；问题块之后到下一个
      问题块之前的所有内容（答案段，可带"答："标记也可无标记、可跨多段）
      归入该问答对；
    - 问答对整体成一块：text 保留原文（含"问：/答："标记，不破坏偏移契约），
      char_start/char_end 按原文本偏移（text == full[char_start:char_end]）；
    - 第一个问题段之前的杂项内容合并为独立普通块；全文无问题标记 → 整文
      一个普通块（内容不丢失，QA 对优先、其余兜底）；
    - 超长问答对整体成块，不按 chunk_size 二次切分（问答对完整性优先；
      chunk_size/overlap 为兼容参数，暂不生效）
    """

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None):
        self.chunk_size = chunk_size
        self.overlap = overlap

    @staticmethod
    def _make_chunk(text: str, s: int, e: int) -> Chunk:
        """原文区间 [s, e) 成块：strip 前后空白并重算偏移（切片一致性）"""
        raw = text[s:e]
        stripped = raw.strip()
        start = s + (len(raw) - len(raw.lstrip()))
        return Chunk(stripped, start, start + len(stripped))

    def chunk(self, text: str) -> List[Chunk]:
        if not text or not text.strip():
            return []
        paragraphs = _split_paragraphs(text)
        q_indices = [i for i, (seg, _) in enumerate(paragraphs)
                     if _block_has_question(seg)]
        if not q_indices:
            # 全文无问题标记：整文一个普通块（内容不丢失）
            return [self._make_chunk(text, 0, len(text))]
        chunks: List[Chunk] = []
        # 文档头杂项（第一个问题段之前）：合并为独立普通块
        first_q = q_indices[0]
        if first_q > 0:
            chunks.append(self._make_chunk(
                text, paragraphs[0][1], paragraphs[first_q][1]))
        # 问答对：问题段起 → 下一个问题段前（答案跨段内容完整保留）
        for i, idx in enumerate(q_indices):
            start = paragraphs[idx][1]
            end = (paragraphs[q_indices[i + 1]][1]
                   if i + 1 < len(q_indices) else len(text))
            chunks.append(self._make_chunk(text, start, end))
        return chunks

