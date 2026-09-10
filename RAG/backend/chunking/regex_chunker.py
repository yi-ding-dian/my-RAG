"""按正则匹配位置切块（参考 KnowFlow regex.py 的"按匹配位置切块"思路）

- 用 re.finditer 定位匹配：匹配片段本身与片段之间的文本都作为块，
  保证文本不丢；跳过空匹配（避免 re.split 空匹配导致死循环/丢文本）
- 超长块再递归 RecursiveChunker（chunk_size/overlap）
"""
from __future__ import annotations

import re
from typing import List, Tuple

from backend.chunking.base import Chunk
from backend.chunking.recursive import RecursiveChunker

class RegexChunker:
    """按正则匹配位置切块（参考 KnowFlow regex.py 的"按匹配位置切块"思路）

    - 用 re.finditer 定位匹配：匹配片段本身与片段之间的文本都作为块，
      保证文本不丢；跳过空匹配（避免 re.split 空匹配导致死循环/丢文本）
    - 超长块再递归 RecursiveChunker（chunk_size/overlap）
    - pattern 为空或编译失败 → ValueError（由调用方决定 400 或写回 failed）
    """

    def __init__(self, chunk_size: int | None = None, overlap: int | None = None,
                 pattern: str | None = None):
        self._recursive = RecursiveChunker(chunk_size=chunk_size, overlap=overlap)
        self.pattern = pattern or ""

    def chunk(self, text: str) -> List[Chunk]:
        if not text or not text.strip():
            return []
        if not self.pattern.strip():
            raise ValueError("正则切块需提供 regex_pattern")
        try:
            compiled = re.compile(self.pattern)
        except re.error as e:
            raise ValueError(f"正则表达式无效: {e}") from e
        # 按匹配位置分段：匹配片段之前文本 / 匹配片段本身 / 尾部剩余文本
        parts: List[Tuple[str, int]] = []
        last_end = 0
        for m in compiled.finditer(text):
            if m.start() > last_end:
                parts.append((text[last_end:m.start()], last_end))
            if m.end() > m.start():
                parts.append((text[m.start():m.end()], m.start()))
            last_end = m.end()
        if last_end < len(text):
            parts.append((text[last_end:], last_end))
        chunks: List[Chunk] = []
        for part_text, part_start in parts:
            stripped = part_text.strip()
            if not stripped:
                continue
            start = part_start + (len(part_text) - len(part_text.lstrip()))
            if len(stripped) <= self._recursive.chunk_size:
                chunks.append(Chunk(stripped, start, start + len(stripped)))
            else:
                chunks.extend(self._recursive._split_text(stripped, start))
        return chunks

