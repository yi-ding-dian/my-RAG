"""切块基础类型：Chunk（切块结果）与 Chunker 切块器协议

归于此文件的原因：所有切块器（recursive/markdown/regex/parent_child/qa）共用，
独立小文件避免公共类型挂在某个具体切块器文件里（职责单一、无循环 import）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol


@dataclass
class Chunk:
    """切块结果：text 为该块文本，char_start/char_end 为相对输入全文的字符区间
    （半开区间 [char_start, char_end)，overlap 时区间为块文本实际覆盖范围）"""

    text: str
    char_start: int
    char_end: int


class Chunker(Protocol):
    """切块协议：输入全文，输出带偏移的切块列表（阶段2 可扩展语义切块实现）"""

    def chunk(self, text: str) -> List[Chunk]:
        ...
