"""切块（无 langchain，纯标准库实现）

模块结构（一个切块方法一个文件）：
- base.py: Chunk（切块结果）+ Chunker 协议
- common.py: 标题识别 / 表格代码块图片保护区间 / 连续标题过滤 / 标题链注入
- recursive.py: RecursiveChunker（naive 递归字符切块）
- markdown_splitter.py: MarkdownSplitter（title 按标题切块）
- regex_chunker.py: RegexChunker（regex 按正则位置切块）
- parent_child.py: ParentChildChunker（parent_child 父子分块）
- qa_chunker.py: QaChunker（qa 问答对整块）+ 规范性检测纯函数

统一入口：新代码 `from backend.chunking import get_chunker, Chunk, ...`；
内部文件间互相引用走具体子模块。
"""
from __future__ import annotations

from backend.chunking.base import Chunk, Chunker
from backend.chunking.common import (VALID_METHODS, add_heading_paths,
                                     find_protected_ranges)
from backend.chunking.markdown_splitter import MarkdownSplitter
from backend.chunking.parent_child import (ParentChildChunkResult,
                                           ParentChildChunker)
from backend.chunking.qa_chunker import (QaChunker, QaStats, analyze_qa_format,
                                         is_qa_format_valid)
from backend.chunking.recursive import RecursiveChunker
from backend.chunking.regex_chunker import RegexChunker


def get_chunker(method: str, config: dict) -> Chunker:
    """切块器工厂：按 method 构造，config 为入库参数

    - naive → RecursiveChunker（delimiter 可选，为空用默认分隔符列表）
    - title → MarkdownSplitter（split_level 标题层级，默认 3）
    - regex → RegexChunker（regex_pattern 必填，为空 chunk 时抛 ValueError）
    - parent_child → ParentChildChunker（父块参数 parent_chunk_size/
      parent_chunk_overlap/parent_split_level，子块参数 chunk_size/overlap）
    - qa → QaChunker（问答对整块，chunk_size/overlap 兼容参数暂不生效）
    """
    if method == "naive":
        return RecursiveChunker(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
            delimiter=config.get("delimiter") or None,
        )
    if method == "title":
        return MarkdownSplitter(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
            split_level=config.get("split_level"),
        )
    if method == "regex":
        return RegexChunker(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
            pattern=config.get("regex_pattern"),
        )
    if method == "parent_child":
        return ParentChildChunker(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
            parent_chunk_size=config.get("parent_chunk_size"),
            parent_chunk_overlap=config.get("parent_chunk_overlap"),
            parent_split_level=config.get("parent_split_level"),
        )
    if method == "qa":
        return QaChunker(
            chunk_size=config.get("chunk_size"),
            overlap=config.get("overlap"),
        )
    raise ValueError(f"未知切块方式: {method}（支持: {'/'.join(VALID_METHODS)}）")


# 默认切块器（通用切块 + 超长递归），env 可配
def default_chunker() -> Chunker:
    return RecursiveChunker()
