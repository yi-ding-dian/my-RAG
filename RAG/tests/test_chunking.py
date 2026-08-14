"""切块单元测试：MarkdownSplitter / RecursiveChunker / RegexChunker / get_chunker

覆盖：标题切分（标题保留块首）、split_level 层级控制、超长段递归切
（分隔符优先级）、中文标点边界（。；，）、overlap 行为、chunk_size 边界、
RegexChunker（匹配片段与间隔文本不丢 / 空 pattern 抛错 / 超长递归）、
自定义 delimiter、get_chunker 工厂（method → 类实例 / 非法 method 抛错）。
切块器统一返回 List[Chunk]（text + 相对全文的 char_start/char_end），
每个构造样本额外断言偏移正确性：full[char_start:char_end] == chunk.text。
全部离线（纯标准库，不依赖网络与数据目录）。
"""
from __future__ import annotations

import pytest

from backend.chunking.splitter import (Chunk, MarkdownSplitter,
                                       RecursiveChunker, RegexChunker,
                                       get_chunker)


def _texts(chunks: list[Chunk]) -> list[str]:
    """取切块文本列表（Chunk.text）"""
    return [c.text for c in chunks]


def _assert_offsets(chunks: list[Chunk], full: str) -> None:
    """偏移正确性：每块 text 必须等于 full[char_start:char_end] 且区间不越界"""
    for c in chunks:
        assert 0 <= c.char_start <= c.char_end <= len(full), \
            f"偏移越界: {c.char_start}..{c.char_end} len={len(full)}"
        assert full[c.char_start:c.char_end] == c.text, \
            f"text 与偏移切片不一致: {c.text[:20]!r} != " \
            f"{full[c.char_start:c.char_end][:20]!r}"


class TestMarkdownSplitter:
    """按 Markdown 标题（#/##/###）切分"""

    def test_title_split_two_sections(self):
        """# 与 ## 两级标题 → 两块，块首保留标题"""
        text = "# 标题一\n\n第一段内容。\n\n## 标题二\n\n第二段内容。"
        chunks = MarkdownSplitter().chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 2
        assert chunks[0].text.startswith("# 标题一")
        assert chunks[1].text.startswith("## 标题二")

    def test_title_kept_at_head(self):
        """标题必须保留在块首（前瞻式切分，而非被切掉）"""
        text = "# 标题\n正文内容\n\n## 子标题\n子内容"
        chunks = MarkdownSplitter().chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 2
        assert all(c.text.startswith("#") for c in chunks)

    def test_heading_levels(self):
        """# / ## / ### 三级标题都参与切分"""
        text = "# 一级\n\n## 二级\n\n### 三级\n\n### 三级之二"
        chunks = MarkdownSplitter().chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 4

    def test_no_heading_single_chunk(self):
        """无标题的纯文本整段一块"""
        text = "没有标题的纯文本内容。" * 3
        chunks = MarkdownSplitter().chunk(text)
        _assert_offsets(chunks, text)
        assert _texts(chunks) == [text.strip()]

    def test_empty_text(self):
        """空文本/纯空白 → 空列表"""
        assert MarkdownSplitter().chunk("") == []
        assert MarkdownSplitter().chunk("   \n \t ") == []

    def test_long_section_recursive_chunk(self):
        """标题下超长段递归切成多块，内容完整保留，首块仍带标题"""
        text = "# 概述\n" + "这是一段非常长的内容。" * 200  # > 800 字符
        chunks = MarkdownSplitter(chunk_size=200, overlap=20).chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) > 1
        assert "这是一段非常长的内容。" in "".join(_texts(chunks))
        assert chunks[0].text.startswith("# 概述")


class TestRecursiveChunker:
    """递归字符切块：分隔符优先级 / 中文标点 / overlap / 边界"""

    def test_separator_priority_newline(self):
        """优先用 \n\n 切分（第一可用多段分隔符），块边界干净"""
        para_a = "段落甲的内容，包含一些逗号与句号。" * 2  # ~30 字符
        para_b = "段落乙的内容，同样是三十个字符左右的长度。"
        text = f"{para_a}\n\n{para_b}"
        chunks = RecursiveChunker(chunk_size=50, overlap=0).chunk(text)
        _assert_offsets(chunks, text)
        # 块在 \n\n 处断开，而不是切到句中
        assert _texts(chunks) == [para_a, para_b]
        assert all("\n\n" not in c.text for c in chunks)

    def test_separator_priority_chinese_period(self):
        """无换行时用中文句号切，边界总在句子之间"""
        sentences = "".join(f"第{i}句话。" for i in range(20))
        chunks = RecursiveChunker(chunk_size=40, overlap=0).chunk(sentences)
        _assert_offsets(chunks, sentences)
        assert len(chunks) > 1
        # 句号是分隔符：除首块外，每块都从完整句子开始（不在句中切入）
        assert all(c.text.startswith("第") for c in chunks[1:])

    def test_separator_priority_semicolon_comma(self):
        """分隔符优先级：句号缺失时退到分号；超长段递归用更细的逗号再切"""
        text = "短句A；短句B，短句C，短句D"
        chunks = RecursiveChunker(chunk_size=10, overlap=0).chunk(text)
        _assert_offsets(chunks, text)
        # 一级：；切出 "短句A"（good）与超长段（bad，递归用 ，再切）
        assert _texts(chunks) == ["短句A", "短句B，短句C", "短句D"]

    def test_character_split_fallback(self):
        """无任何标点/空白时退化为按字符硬切"""
        chunks = RecursiveChunker(chunk_size=10, overlap=0).chunk("a" * 25)
        _assert_offsets(chunks, "a" * 25)
        assert _texts(chunks) == ["a" * 10, "a" * 10, "a" * 5]

    def test_chinese_punctuation_boundary(self):
        """中文标点边界：切分点总在句子之间（非首块从完整句首开始）"""
        text = "".join(f"这是第{i}个完整句子。" for i in range(50))
        chunks = RecursiveChunker(chunk_size=100, overlap=0).chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) > 1
        for c in chunks[1:]:
            assert c.text.startswith("这是第"), f"块从句子中间切入: {c.text[:12]}"

    def test_overlap(self):
        """overlap>0 时相邻块开头包含上一块尾部内容"""
        text = "句子内容。" * 30
        chunks = RecursiveChunker(chunk_size=40, overlap=8).chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) > 1
        for prev, cur in zip(chunks, chunks[1:]):
            assert cur.text.startswith(prev.text[-8:]), \
                f"相邻块无重叠: {prev.text[-10:]} / {cur.text[:10]}"

    def test_chunk_size_boundary(self):
        """恰好等于 chunk_size → 单块"""
        text = "x" * 50
        chunks = RecursiveChunker(chunk_size=50, overlap=0).chunk(text)
        _assert_offsets(chunks, text)
        assert _texts(chunks) == [text]

    def test_chunk_size_plus_one(self):
        """超出 1 字符 → 两块（第一块 size 字符）"""
        text = "x" * 51
        chunks = RecursiveChunker(chunk_size=50, overlap=0).chunk(text)
        _assert_offsets(chunks, text)
        assert _texts(chunks) == ["x" * 50, "x"]

    def test_empty_and_whitespace(self):
        """空文本/纯空白 → 空列表"""
        assert RecursiveChunker().chunk("") == []
        assert RecursiveChunker().chunk("   \n  ") == []

    def test_single_short_text(self):
        """短文本（< chunk_size）整段一块"""
        text = "这是一个很短的段落。"
        chunks = RecursiveChunker(chunk_size=800, overlap=100).chunk(text)
        _assert_offsets(chunks, text)
        assert _texts(chunks) == [text]


class TestMarkdownSplitterSplitLevel:
    """split_level 控制参与切分的标题层级（1 仅 #，2 仅 #/##，3 全部）"""

    TEXT = "前言段\n\n# 一级\n\n## 二级\n\n### 三级"

    def test_split_level_1_only_h1(self):
        """split_level=1：仅 # 参与切分 → 2 块，##/### 归入其后块"""
        chunks = MarkdownSplitter(split_level=1).chunk(self.TEXT)
        _assert_offsets(chunks, self.TEXT)
        assert len(chunks) == 2
        assert chunks[0].text == "前言段"
        assert chunks[1].text.startswith("# 一级")
        assert "## 二级" in chunks[1].text and "### 三级" in chunks[1].text

    def test_split_level_2_h1_h2(self):
        """split_level=2：仅 #/## 参与切分 → 3 块，### 归入 ## 块"""
        chunks = MarkdownSplitter(split_level=2).chunk(self.TEXT)
        _assert_offsets(chunks, self.TEXT)
        assert len(chunks) == 3
        assert chunks[0].text == "前言段"
        assert chunks[1].text.startswith("# 一级")
        assert chunks[2].text.startswith("## 二级")
        assert "### 三级" in chunks[2].text

    def test_split_level_3_all(self):
        """split_level=3（默认）：#/##/### 全部参与切分 → 4 块"""
        chunks = MarkdownSplitter(split_level=3).chunk(self.TEXT)
        _assert_offsets(chunks, self.TEXT)
        assert len(chunks) == 4
        assert chunks[0].text == "前言段"
        assert chunks[1].text.startswith("# 一级")
        assert chunks[2].text.startswith("## 二级")
        assert chunks[3].text.startswith("### 三级")

    def test_split_level_out_of_range(self):
        """split_level 越界（0 / 7）→ ValueError（上限已放宽至 6）"""
        with pytest.raises(ValueError):
            MarkdownSplitter(split_level=0)
        with pytest.raises(ValueError):
            MarkdownSplitter(split_level=7)


class TestRegexChunker:
    """按正则匹配位置切块：匹配片段与间隔文本都保留，文本不丢"""

    def test_regex_match_and_gap_kept(self):
        """匹配片段与间隔文本都成块，块序正确、文本不丢"""
        text = "a1b22c333d"
        chunks = RegexChunker(pattern=r"\d+").chunk(text)
        _assert_offsets(chunks, text)
        assert _texts(chunks) == ["a", "1", "b", "22", "c", "333", "d"]

    def test_regex_heading_pattern(self):
        """多行匹配（pattern 自带 (?m)）切标题：前言/标题/正文都成块"""
        text = "前言\n\n# 甲\n\n正文\n\n# 乙\n\n结尾"
        chunks = RegexChunker(pattern=r"(?m)^# .*$").chunk(text)
        _assert_offsets(chunks, text)
        assert _texts(chunks) == ["前言", "# 甲", "正文", "# 乙", "结尾"]

    def test_regex_empty_pattern_raises(self):
        """空 pattern / 纯空白 pattern → ValueError"""
        with pytest.raises(ValueError):
            RegexChunker().chunk("有一些文本")
        with pytest.raises(ValueError):
            RegexChunker(pattern="   ").chunk("有一些文本")

    def test_regex_invalid_pattern_raises(self):
        """非法正则（编译失败）→ ValueError"""
        with pytest.raises(ValueError):
            RegexChunker(pattern="[不闭合").chunk("有一些文本")

    def test_regex_empty_text(self):
        """空文本/纯空白 → 空列表（不校验 pattern）"""
        assert RegexChunker(pattern="x").chunk("") == []
        assert RegexChunker(pattern="x").chunk("  \n ") == []

    def test_regex_no_match_single_chunk(self):
        """无匹配 → 整段一块"""
        text = "没有任何匹配的纯文本。"
        chunks = RegexChunker(pattern=r"\d+").chunk(text)
        _assert_offsets(chunks, text)
        assert _texts(chunks) == [text]

    def test_regex_long_segment_recursive(self):
        """超长间隔段递归切块，匹配片段保留，内容完整不丢

        注：一级切分点处的分隔符按设计丢弃（块边界干净，与
        test_separator_priority_newline 的 "\n\n" 断言一致），故只断言
        字符级内容完整（每块为原文子串、片段计数不丢），不要求逐字符还原。
        """
        long_text = "无匹配内容。" * 30  # ~210 字符 > chunk_size=50
        text = long_text + "[命中]" + long_text
        chunks = RegexChunker(chunk_size=50, overlap=0,
                              pattern=r"\[命中\]").chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) > 1
        joined = "".join(_texts(chunks))
        # 每块都是原文连续子串（无凭空内容/重排），匹配片段保留
        assert all(c.text in text for c in chunks)
        assert joined.count("[命中]") == 1
        assert joined.count("无匹配内容") == 60, "递归切块后文本不应丢失"


class TestRecursiveChunkerDelimiter:
    """naive 模式自定义 delimiter"""

    def test_custom_delimiter_takes_effect(self):
        """自定义 delimiter 优先切分，块边界干净且块内不残留分隔符"""
        text = ("x" * 30) + ";;" + ("x" * 30) + ";;" + ("x" * 30)
        chunks = RecursiveChunker(chunk_size=50, overlap=0,
                                  delimiter=";;").chunk(text)
        _assert_offsets(chunks, text)
        assert _texts(chunks) == ["x" * 30, "x" * 30, "x" * 30]
        assert all(";;" not in c.text for c in chunks)

    def test_custom_delimiter_vs_default_hardcut(self):
        """对照：无 delimiter 时同一文本按字符硬切（块内含分隔符）"""
        text = ("x" * 30) + ";;" + ("x" * 30) + ";;" + ("x" * 30)
        chunks_default = RecursiveChunker(chunk_size=50, overlap=0).chunk(text)
        _assert_offsets(chunks_default, text)
        assert len(chunks_default) == 2
        assert any(";;" in c.text for c in chunks_default), \
            "无可用分隔符应退化按字符硬切，分隔符残留在块内"


class TestGetChunkerFactory:
    """get_chunker 工厂：method → 对应切块器实例；非法 method 抛错"""

    def test_factory_naive(self):
        chunker = get_chunker("naive", {"chunk_size": 100, "overlap": 10})
        assert isinstance(chunker, RecursiveChunker)
        assert chunker.chunk_size == 100
        assert chunker.overlap == 10

    def test_factory_title(self):
        chunker = get_chunker("title", {"split_level": 2})
        assert isinstance(chunker, MarkdownSplitter)
        assert chunker.split_level == 2

    def test_factory_regex(self):
        chunker = get_chunker("regex", {"regex_pattern": r"\d+"})
        assert isinstance(chunker, RegexChunker)
        assert chunker.pattern == r"\d+"

    def test_factory_unknown_method_raises(self):
        """未知 method → ValueError"""
        with pytest.raises(ValueError):
            get_chunker("keyword", {})
