"""切块质量增强测试（对齐 KnowFlow 方案 A）：表格/代码块完整性、标题智能
回退、连续标题不切、偏移正确性

- 表格不切开：markdown 表格（连续 | 行）作为整体归入某块，无块边界落在
  表格行中间（每块要么含完整表格、要么不含）
- 代码块不切开：``` 围栏代码块整体归入某块（含 HTML <table>）
- 标题智能回退：title 按 split_level 只切出 1 个超长块 → 放宽一级标题重切
  出多块（避免"章节太长只出一个块"；短文档/无更低级标题不触发）
- 连续标题不切：直接相邻标题行（中间无正文）不各自成块，并入后续内容
- 偏移正确性：增强后所有块 full_text[char_start:char_end] == text
覆盖 MarkdownSplitter（title 方式）与 ParentChildChunker（父子分块）。
"""
from __future__ import annotations

import re

import pytest

from backend.chunking.splitter import (Chunk, MarkdownSplitter,
                                       ParentChildChunker, find_protected_ranges)

# 含 markdown 表格的章节（表格 5 行：表头+分隔行+3 数据行）
TABLE_TEXT = (
    "# 表格章节\n\n"
    "表格前说明文字，介绍表格用途。\n\n"
    "| 参数 | 说明 |\n"
    "| --- | --- |\n"
    "| code | 状态码 |\n"
    "| message | 消息内容 |\n"
    "| result | 业务数据 |\n"
    "\n"
    "表格后说明文字，补充使用注意。\n"
)

# 含围栏代码块的章节（代码块内含 "# 假标题" 注释行，不应成为切分边界）
CODE_TEXT = (
    "# 代码章节\n\n"
    "代码前说明文字。\n\n"
    "```python\n"
    "def hello(name):\n"
    "    # 这不是标题，只是注释\n"
    "    return f'hello {name}'\n"
    "```\n\n"
    "代码后说明文字，说明调用方式。\n"
)

# 智能回退样本：单个超长 H2 章节 + 多个 H3 小节（split_level=2 只切出 1 块）
_FALLBACK_TEXT = "\n".join([
    "## 长章节",
    "正文段落" * 20, "正文段落" * 20, "正文段落" * 20,  # ~180 字符
    "",
    "### 小节一",
    "小节一内容。" * 15,
    "",
    "### 小节二",
    "小节二内容。" * 15,
    "",
    "### 小节三",
    "小节三内容。" * 15,
])

# 连续标题样本（直接相邻，中间无正文）
CONSECUTIVE_ONLY = "## 标题A\n## 标题B"
CONSECUTIVE_THEN_BODY = "## 标题A\n## 标题B\n后续正文内容。"
CONSECUTIVE_WITH_PREAMBLE = "前言说明文字。\n\n## 标题A\n## 标题B\n后续正文内容。"


def _assert_offsets(chunks: list[Chunk], full: str) -> None:
    """偏移正确性：每块 text 必须等于 full[char_start:char_end] 且区间不越界"""
    for c in chunks:
        assert 0 <= c.char_start <= c.char_end <= len(full), \
            f"偏移越界: {c.char_start}..{c.char_end} len={len(full)}"
        assert full[c.char_start:c.char_end] == c.text, \
            f"text 与偏移切片不一致: {c.text[:20]!r}"


def _assert_ranges_intact(chunks: list[Chunk], protected: list[tuple[int, int]],
                          full: str) -> None:
    """无块边界落在保护区间内部：表格/代码块被某块完整包含或与块不相交"""
    assert protected, "样本应识别出保护区间"
    for c in chunks:
        for s, e in protected:
            assert not (s < c.char_start < e), \
                f"块起点切入保护区 {s}..{e}: {c.text[:20]!r}"
            assert not (s < c.char_end < e), \
                f"块终点切入保护区 {s}..{e}: {c.text[:20]!r}"


def _title_chunker(**kw):
    defaults = dict(chunk_size=40, overlap=0, split_level=3)
    defaults.update(kw)
    return MarkdownSplitter(**defaults)


def _pc_chunker(**kw):
    defaults = dict(chunk_size=40, overlap=0, parent_chunk_size=200,
                    parent_chunk_overlap=0, parent_split_level=2)
    defaults.update(kw)
    return ParentChildChunker(**defaults)


class TestTableIntegrity:
    """增强 1a：markdown 表格不切开（title 切分 + 父子分块）"""

    def test_table_recognized_as_protected_range(self):
        """表格 5 行（表头+分隔行+数据行）识别为 1 个保护区"""
        protected = find_protected_ranges(TABLE_TEXT)
        assert len(protected) == 1, f"应识别 1 个表格区间: {protected}"
        s, e = protected[0]
        assert TABLE_TEXT[s:e].startswith("| 参数"), "表格区间应从表头行开始"
        assert TABLE_TEXT[s:e].endswith("| result | 业务数据 |"), \
            "表格区间应到末行数据结束"

    def test_table_not_cut_in_title_split(self):
        """title 方式：表格整体归入某块，块边界不落在表格行中间"""
        chunks = _title_chunker().chunk(TABLE_TEXT)
        _assert_offsets(chunks, TABLE_TEXT)
        _assert_ranges_intact(chunks, find_protected_ranges(TABLE_TEXT), TABLE_TEXT)
        # 表格内容完整保留在某个块内（表格行不丢失）
        joined = "\n".join(c.text for c in chunks)
        assert "| 参数 | 说明 |" in joined and "| result | 业务数据 |" in joined
        assert len(chunks) > 1, "表格前后应有独立文本块"

    def test_table_not_cut_in_parent_child(self):
        """parent_child：子块与父块都不切开表格（表格=完整章节内容）"""
        chunker = _pc_chunker()
        result = chunker.chunk_parent_child(TABLE_TEXT)
        protected = find_protected_ranges(TABLE_TEXT)
        _assert_offsets(result.children, TABLE_TEXT)
        _assert_offsets(result.parents, TABLE_TEXT)
        _assert_ranges_intact(result.children, protected, TABLE_TEXT)
        _assert_ranges_intact(result.parents, protected, TABLE_TEXT)
        # 父块=完整章节：含完整表格（表头与末行数据在同一父块）
        parent0 = result.parents[0]
        assert "| 参数 | 说明 |" in parent0.text \
            and "| result | 业务数据 |" in parent0.text, \
            "父块应包含完整表格"

    def test_oversized_table_kept_whole(self):
        """超长表格（大于 chunk_size）也整体成块，不切开"""
        big = "# 章\n\n| 列A | 列B |\n| --- | --- |\n" + "\n".join(
            [f"| 值{i} | 说明{i} |" for i in range(20)])
        big += "\n\n正文收尾。\n"
        protected = find_protected_ranges(big)
        assert len(protected) == 1
        chunks = MarkdownSplitter(chunk_size=30, overlap=0).chunk(big)
        _assert_offsets(chunks, big)
        _assert_ranges_intact(chunks, protected, big)
        # 表格整体在一个块内
        table_chunks = [c for c in chunks
                        if "| 列A | 列B |" in c.text]
        assert table_chunks, "表格应完整保留在某个块内"


class TestImageIntegrity:
    """增强 1c：图片引用（markdown ![]()）不切碎

    修复背景：图片引用含英文感叹号 `!`，被句子感知切分（句界集合含 `!`）
    当作英文句界拆碎——块文本变成残缺的 `[](url)` 链接文本（前端不渲染，
    显示为链接文本）。引用作整体保护后与表格同等待遇，原子并入某块。
    """

    IMG_TEXT = ("1.白酒发票\n\n![](/api/files/images/doc/abc.jpg)\n"
                "\n2.内蒙古相关报销服务\n\n![](/api/files/images/doc/def.jpg)\n")

    def test_image_recognized_as_protected_range(self):
        protected = find_protected_ranges(self.IMG_TEXT)
        assert len(protected) == 2, f"应识别 2 个图片区间: {protected}"
        for s, e in protected:
            assert self.IMG_TEXT[s:e].startswith("!["), \
                "保护区间应为完整图片引用（含 !）"

    def test_image_not_cut_in_naive_split(self):
        """naive（RecursiveChunker 兜底保护）：引用完整保留、无残缺链接文本"""
        from backend.chunking.splitter import RecursiveChunker
        chunks = RecursiveChunker(chunk_size=800, overlap=0).chunk(self.IMG_TEXT)
        _assert_offsets(chunks, self.IMG_TEXT)
        _assert_ranges_intact(chunks, find_protected_ranges(self.IMG_TEXT),
                              self.IMG_TEXT)
        joined = "\n".join(c.text for c in chunks)
        assert "![](/api/files/images/doc/abc.jpg)" in joined
        assert "![](/api/files/images/doc/def.jpg)" in joined
        # 残缺引用特征：[](...) 前无 !（完整引用自带 ](/ 子串，须用负向后顾）
        assert not re.search(r"(?<!!)\[\]\(/api/files", joined), \
            "不应残留残缺的 []() 链接引用"

    def test_image_not_cut_in_title_split(self):
        """title 方式：引用同样完整（MarkdownSplitter 显式注入保护区）"""
        chunks = _title_chunker().chunk(self.IMG_TEXT)
        joined = "\n".join(c.text for c in chunks)
        assert "![](/api/files/images/doc/abc.jpg)" in joined
        assert not re.search(r"(?<!!)\[\]\(/api/files", joined), \
            "不应残留残缺的 []() 链接引用"


class TestCodeBlockIntegrity:
    """增强 1b：围栏代码块不切开（含代码块内假标题）"""

    def test_code_fence_not_cut_in_title_split(self):
        chunks = _title_chunker().chunk(CODE_TEXT)
        _assert_offsets(chunks, CODE_TEXT)
        _assert_ranges_intact(chunks, find_protected_ranges(CODE_TEXT), CODE_TEXT)
        joined = "\n".join(c.text for c in chunks)
        assert "```python" in joined and "```" in joined
        assert "def hello(name):" in joined, "代码内容不应丢失"

    def test_code_fence_not_cut_in_parent_child(self):
        chunker = _pc_chunker()
        result = chunker.chunk_parent_child(CODE_TEXT)
        protected = find_protected_ranges(CODE_TEXT)
        _assert_offsets(result.children, CODE_TEXT)
        _assert_offsets(result.parents, CODE_TEXT)
        _assert_ranges_intact(result.children, protected, CODE_TEXT)
        _assert_ranges_intact(result.parents, protected, CODE_TEXT)

    def test_heading_inside_code_fence_not_split_boundary(self):
        """代码块内的 "# 假标题" 不是标题：不产生切分边界、不进标题链"""
        text = ("# 真标题\n\n正文。\n\n```\n# 假标题\n内容\n```\n\n"
                "```\n# 另一个假标题\n```\n")
        chunks = _title_chunker(chunk_size=40, overlap=0).chunk(text)
        _assert_offsets(chunks, text)
        # 假标题不是边界：块数远少于"5 个标题"情形，代码块整体保留
        for c in chunks:
            assert "# 假标题" not in c.text or "```" in c.text, \
                "假标题只能出现在代码块块内"
        joined = "\n".join(c.text for c in chunks)
        assert "# 假标题" in joined, "第一个假标题内容不丢失"
        assert "# 另一个假标题" in joined, "第二个假标题内容不丢失"
        assert joined.count("```") == 4, "围栏完整保留"

    def test_html_table_not_cut(self):
        """HTML <table> 表格同样保护（MinerU 解析产物中常见）"""
        text = "# 章\n\n<html>\n<table>\n<tr><td>甲</td><td>乙</td></tr>\n" \
               "<tr><td>丙</td><td>丁</td></tr>\n</table>\n</html>\n\n收尾。\n"
        protected = find_protected_ranges(text)
        assert any(text[s:e].startswith("<table>") for s, e in protected), \
            "应识别 HTML 表格区间"
        chunks = MarkdownSplitter(chunk_size=20, overlap=0).chunk(text)
        _assert_offsets(chunks, text)
        _assert_ranges_intact(chunks, protected, text)

    def test_unclosed_fence_kept_whole(self):
        """未闭合的 ``` 围栏：保护到文末，内部内容不切开"""
        text = "# 章\n\n```\n未闭合代码\n" + "内容。" * 50
        chunks = MarkdownSplitter(chunk_size=50, overlap=0).chunk(text)
        _assert_offsets(chunks, text)
        _assert_ranges_intact(chunks, find_protected_ranges(text), text)


class TestSmartFallback:
    """增强 2：标题智能回退（title 按 split_level 只切 1 个超长块 → 放宽重切）"""

    def test_h2_only_one_block_falls_back_to_h3(self):
        """长文档只有 1 个 H2 章节且超 chunk_size：H2 方式 1 块 → H3 重切多块"""
        # 对照：chunk_size 大到整篇不超长 → 不触发回退 → 1 块
        control = MarkdownSplitter(chunk_size=999_999, overlap=0,
                                   split_level=2).chunk(_FALLBACK_TEXT)
        assert len(control) == 1, f"H2 方式应只切出 1 块: {len(control)}"
        # 增强：超长 → 回退到 H3 重切出多个块
        chunks = MarkdownSplitter(chunk_size=200, overlap=0,
                                  split_level=2).chunk(_FALLBACK_TEXT)
        assert len(chunks) > 1, "回退到 H3 后应切出多个块"
        _assert_offsets(chunks, _FALLBACK_TEXT)
        # 回退后块以标题行开头（标题保留块首）
        assert chunks[0].text.lstrip().startswith("## 长章节")
        starts = [c.text.lstrip() for c in chunks if c.text.lstrip().startswith("#")]
        assert any(s.startswith("### 小节一") for s in starts), \
            "H3 小节应成为切分边界"

    def test_fallback_stops_when_no_lower_heading(self):
        """只有 1 个 H2 且无 H3：回退到 6 级也无新边界 → 正常递归切（不空转）"""
        text = "## 唯一章节\n\n" + "正文内容。" * 300
        chunks = MarkdownSplitter(chunk_size=100, overlap=0,
                                  split_level=2).chunk(text)
        assert len(chunks) > 1, "超长单章递归切成多个块"
        _assert_offsets(chunks, text)
        assert chunks[0].text.lstrip().startswith("## 唯一章节")

    def test_short_document_no_fallback(self):
        """1 块但不超长 → 不触发回退（仍 1 块）"""
        text = "## 短章节\n\n简短内容。"
        chunks = MarkdownSplitter(chunk_size=200, overlap=0,
                                  split_level=2).chunk(text)
        assert len(chunks) == 1
        assert chunks[0].text == text.strip()


class TestContinuousHeadings:
    """增强 3：连续标题不切（直接相邻标题行并入后续内容）"""

    def test_adjacent_headings_no_body_single_chunk(self):
        """'## A\\n## B' 无正文 → 不产生 2 个空块（整篇 1 块）"""
        chunks = _title_chunker().chunk(CONSECUTIVE_ONLY)
        assert len(chunks) == 1, f"应整篇 1 块: {len(chunks)}"
        assert chunks[0].text == CONSECUTIVE_ONLY.strip()
        _assert_offsets(chunks, CONSECUTIVE_ONLY)

    def test_adjacent_headings_merge_into_body(self):
        """'## A\\n## B\\n正文' → 连续标题并入后续内容（1 块）"""
        chunks = _title_chunker().chunk(CONSECUTIVE_THEN_BODY)
        assert len(chunks) == 1, "A、B 连续标题应并入正文块"
        assert chunks[0].text == CONSECUTIVE_THEN_BODY.strip()
        _assert_offsets(chunks, CONSECUTIVE_THEN_BODY)

    def test_adjacent_headings_with_preamble_two_chunks(self):
        """'前言\\n\\n## A\\n## B\\n正文' → 前言块并入 A，B 为边界 → 2 块"""
        chunks = _title_chunker().chunk(CONSECUTIVE_WITH_PREAMBLE)
        assert len(chunks) == 2, f"应有 2 块: {[c.text for c in chunks]}"
        assert chunks[0].text.startswith("前言说明文字")
        assert "## 标题A" in chunks[0].text, "A 应并入前一块（不单独成块）"
        assert chunks[1].text.lstrip().startswith("## 标题B")
        _assert_offsets(chunks, CONSECUTIVE_WITH_PREAMBLE)

    def test_adjacent_headings_not_merged_in_parent_child(self):
        """parent_child：连续标题不各自成父块（无纯标题父块）"""
        chunker = _pc_chunker()
        result = chunker.chunk_parent_child(CONSECUTIVE_THEN_BODY)
        # 父块=1 个（标题A 并入标题B 的内容父块），子块归属正常
        assert len(result.parents) == 1, f"父块数: {len(result.parents)}"
        assert "## 标题A" in result.parents[0].text
        assert "## 标题B" in result.parents[0].text
        assert "后续正文内容" in result.parents[0].text
        _assert_offsets(result.children, CONSECUTIVE_THEN_BODY)
        _assert_offsets(result.parents, CONSECUTIVE_THEN_BODY)
        assert all(result.child_parent_map[i] == 0
                   for i in range(len(result.children)))

    def test_blank_line_separated_headings_keep_split(self):
        """空行间隔的标题不合并（如 '### 三级\\n\\n### 三级之二' 各成块）"""
        text = "# 一级\n\n## 二级\n\n### 三级\n\n### 三级之二"
        chunks = MarkdownSplitter(split_level=3).chunk(text)
        assert len(chunks) == 4, "空行间隔标题保持独立成块"


class TestOffsetsAfterEnhancements:
    """偏移正确性：增强后所有块 full_text[char_start:char_end] == text"""

    @pytest.mark.parametrize("text", [
        TABLE_TEXT, CODE_TEXT, CONSECUTIVE_ONLY, CONSECUTIVE_THEN_BODY,
        CONSECUTIVE_WITH_PREAMBLE, _FALLBACK_TEXT,
    ])
    def test_title_offsets(self, text):
        _assert_offsets(_title_chunker().chunk(text), text)

    @pytest.mark.parametrize("text", [
        TABLE_TEXT, CODE_TEXT, CONSECUTIVE_THEN_BODY, _FALLBACK_TEXT,
    ])
    def test_parent_child_offsets(self, text):
        result = _pc_chunker().chunk_parent_child(text)
        _assert_offsets(result.children, text)
        _assert_offsets(result.parents, text)
        # 子块区间完整落在归属父块内（区间完整优先的映射语义保持）
        for i, c in enumerate(result.children):
            p = result.parents[result.child_parent_map[i]]
            assert p.char_start <= c.char_start and c.char_end <= p.char_end, \
                f"子块 {i} 未完整落在父块内"


# ---------- 标题行并入保护区间（防孤儿标题块，见 splitter._extend_to_preceding_heading） ----------

def test_protected_range_includes_preceding_heading():
    """表格前紧邻(可隔 1 空行)的标题行并入保护区间：防止 title 超长回退
    把标题字符窗口切走成"无数据孤儿块"（Excel 分段表场景）"""
    text = ("## 表标题\n\n"
            "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
            "## 表标题2\n\n"
            "| c | d |\n| --- | --- |\n| 3 | 4 |\n")
    protected = find_protected_ranges(text)
    assert protected[0][0] == text.index("## 表标题"), "区间应含紧邻标题行"
    assert protected[1][0] == text.index("## 表标题2")
    # title 切块：标题+表格成块，无"仅标题"孤儿块
    chunks = MarkdownSplitter(chunk_size=800, overlap=100,
                              split_level=2).chunk(text)
    orphans = [c for c in chunks
               if c.text.strip().startswith("## ") and len(c.text) < 30]
    assert not orphans, f"出现孤儿标题块: {[c.text[:20] for c in orphans]}"
    assert any("| a | b |" in c.text and "## 表标题\n" in c.text for c in chunks)


def test_html_table_heading_extension():
    """HTML <table> 前紧邻标题行同样并入（MinerU 产物一致性）"""
    text = ("# HTML 表\n\n"
            "<table><tr><th>x</th></tr><tr><td>1</td></tr></table>")
    protected = find_protected_ranges(text)
    assert len(protected) == 1
    assert protected[0][0] == text.index("# HTML 表")


def test_heading_not_merged_when_text_between():
    """标题与表格之间有说明文字时不扩展（常规边界语义保持）"""
    text = ("## 章节\n\n表格前说明文字。\n\n"
            "| a | b |\n| --- | --- |\n| 1 | 2 |\n")
    protected = find_protected_ranges(text)
    assert text.index("| a |") in [s for s, _ in protected]
    assert protected[0][0] > text.index("## 章节")
