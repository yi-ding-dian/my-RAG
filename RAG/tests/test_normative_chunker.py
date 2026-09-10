"""规范性文档层级聚合切块（HierarchicalChunker）单元测试

覆盖：自底向上聚合（全小节点并成一块）、不跨一级标题（含文首前言组独立）、
超长节点兜底二次切（段落 → 句子 → 硬切）、图片 URL 不计入长度预算 +
真实长度硬上限、保护区间（表格/代码块/图片）不被切开、块首标题链、偏移契约
（text == full[char_start:char_end]，拼标题链前缀时前缀可剥离）、空文本/无标题
文本/单节点等边界。

全部离线（纯标准库 + 项目内模块，不依赖网络与数据目录）。
"""
from __future__ import annotations

import pytest

from backend.chunking import Chunk
from backend.chunking.common import _iter_headings, find_protected_ranges
from backend.config import get_active_config
from backend.normative import HierarchicalChunker


def _slice_of(chunk: Chunk, full: str) -> str:
    """块原文切片：剥掉标题链前缀（若有）后必须等于 full[char_start:char_end]"""
    slice_ = full[chunk.char_start:chunk.char_end]
    if chunk.text == slice_:
        return chunk.text
    assert chunk.text.endswith("\n" + slice_), \
        f"块文本与前缀+切片不符: {chunk.text[:40]!r}"
    prefix = chunk.text[: -(len(slice_) + 1)]
    assert "\n" not in prefix, f"标题链前缀必须是单行: {prefix!r}"
    return slice_


def _assert_offsets(chunks: list[Chunk], full: str) -> None:
    """偏移契约：区间不越界、升序不重叠、剥前缀后等于原文切片"""
    prev_end = -1
    for c in chunks:
        assert 0 <= c.char_start <= c.char_end <= len(full), \
            f"偏移越界: {c.char_start}..{c.char_end} len={len(full)}"
        assert c.char_start >= prev_end, "块区间应升序且互不重叠"
        prev_end = c.char_end
        _slice_of(c, full)


def _texts(chunks: list[Chunk]) -> list[str]:
    return [c.text for c in chunks]


def _body(chunks: list[Chunk], full: str) -> str:
    """所有块的正文（剥掉标题链前缀）拼接"""
    return "".join(_slice_of(c, full) for c in chunks)


def _assert_coverage(chunks: list[Chunk], full: str) -> None:
    """覆盖完整：原文每个非空白字符都至少落在一个块的区间里（不丢内容）"""
    covered = set()
    for c in chunks:
        covered.update(range(c.char_start, c.char_end))
    for i, ch in enumerate(full):
        if not ch.isspace():
            assert i in covered, f"字符 @{i} ({ch!r}) 未被任何块覆盖"


def _assert_heading_lines_intact(chunks: list[Chunk], full: str) -> None:
    """标题行不被切开：切点不得落在任何标题行内部

    标题文本自带编号点号（"##### 3.3.3.5.6 设备专用术语"），点号在句级切分
    里是句界——实测缺陷：不特殊处理时标题会被切成 "3.3." / "3.5." /
    "6 设备专用术语" 三段残片。
    """
    for off, _lvl, title in _iter_headings(full, find_protected_ranges(full)):
        nl = full.find("\n", off)
        line_end = len(full) if nl == -1 else nl
        for c in chunks:
            for boundary in (c.char_start, c.char_end):
                assert not (off < boundary < line_end), \
                    f"切点 {boundary} 落在标题行 [{off}, {line_end}) {title!r} 内部"


def _assert_no_heading_only_block(chunks: list[Chunk], full: str) -> None:
    """不存在"只有标题"的块（纯标题块检索无意义，必须与正文相伴）"""
    heads = {off for off, _l, _t in _iter_headings(full, find_protected_ranges(full))}
    for c in chunks:
        body = _slice_of(c, full)
        pos = c.char_start  # 块正文起点 = 原文偏移（前缀不在偏移内）
        only_heading = True
        for line in body.split("\n"):
            if line.strip() and pos not in heads:
                only_heading = False
                break
            pos += len(line) + 1
        assert not only_heading, f"纯标题块: {body[:60]!r}"


def _eff(text: str) -> int:
    """有效长度（图片引用不计入）——测试内独立实现，避免依赖被测实现"""
    import re
    total = len(text)
    for m in re.finditer(r"!\[[^\]]*\]\([^)]+\)|<img\b[^>]*>", text, re.IGNORECASE):
        total -= len(m.group(0))
    return total


class TestBasicBehaviour:
    """基础行为：空文本 / 无标题 / 单节点 / 全小节点合并"""

    def test_empty_text(self):
        """空文本、纯空白 → 空列表"""
        ch = HierarchicalChunker(chunk_size=100, overlap=0)
        assert ch.chunk("") == []
        assert ch.chunk("   \n \t \n\n ") == []

    def test_no_heading_short(self):
        """无标题短文本 → 整段一块"""
        ch = HierarchicalChunker(chunk_size=100, overlap=0)
        text = "没有标题的纯文本内容。"
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert _texts(chunks) == [text]

    def test_no_heading_long(self):
        """无标题长文本（多段落）→ 段落级切分，每块不超预算且内容不丢"""
        ch = HierarchicalChunker(chunk_size=50, overlap=0)
        text = "\n\n".join(f"第{i}段内容，这里是一些用于填充长度的文字。" for i in range(10))
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) > 1
        assert all(_eff(c.text) <= 50 for c in chunks)
        assert "第0段" in _body(chunks, text) and "第9段" in _body(chunks, text)

    def test_single_small_node(self):
        """单节点（标题 + 短正文）→ 一块，块首保留标题"""
        ch = HierarchicalChunker(chunk_size=512, overlap=0)
        text = "# 标题一\n\n一小段正文。"
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 1
        assert chunks[0].text.startswith("# 标题一")

    def test_all_small_nodes_merge(self):
        """全小节点一路向上聚合：整章并成一个块（哪怕它很小）"""
        ch = HierarchicalChunker(chunk_size=512, overlap=0)
        text = ("# 一、章标题\n\n## 1.1 节\n\n节正文。\n\n"
                "### 1.1.1 小节\n\n小节正文。\n\n#### 1.1.1.1 更深\n\n更深的正文。")
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 1
        for title in ("# 一、章标题", "## 1.1 节", "### 1.1.1 小节", "#### 1.1.1.1 更深"):
            assert title in chunks[0].text

    def test_defaults_read_from_config(self):
        """chunk_size/overlap 缺省时取活跃配置（与其他切块器一致）"""
        cfg = get_active_config().chunking
        ch = HierarchicalChunker()
        assert ch.chunk_size == cfg.chunk_size
        assert ch.overlap == cfg.chunk_overlap

    def test_invalid_chapter_level(self):
        """chapter_level 越界 → 抛错（1~6）"""
        with pytest.raises(ValueError):
            HierarchicalChunker(chapter_level=0)
        with pytest.raises(ValueError):
            HierarchicalChunker(chapter_level=7)


class TestNoCrossChapter:
    """合并不跨一级标题（章级边界）"""

    def test_short_chapters_not_merged(self):
        """两个短章（合计远小于 chunk_size）仍各自成块，永不合并"""
        ch = HierarchicalChunker(chunk_size=512, overlap=0)
        text = "# 一、第一章\n\n第一章正文。\n\n# 二、第二章\n\n第二章正文。"
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 2
        assert chunks[0].text.startswith("# 一、第一章")
        assert chunks[1].text.startswith("# 二、第二章")
        assert "第二章" not in chunks[0].text
        assert "第一章" not in chunks[1].text

    def test_no_chunk_contains_other_chapter_heading(self):
        """长章内部的多个块：任何块内部都不含另一个一级标题行"""
        ch = HierarchicalChunker(chunk_size=120, overlap=0)
        text = ("# 一、长章\n\n" + "\n\n".join(f"长章第{i}段。" * 3 for i in range(6))
                + "\n\n# 二、短章\n\n短章正文。\n\n# 三、另一章\n\n另一章正文。")
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        h1 = [off for off, lvl, _ in _iter_headings(text, []) if lvl == 1]
        assert len(h1) == 3
        for c in chunks:
            for off in h1:
                assert not (c.char_start < off < c.char_end), \
                    f"块 {c.char_start}-{c.char_end} 跨越了一级标题 @{off}"
        # 短章各自成块（各 1 块，不互相并、也不并进长章）
        assert sum(1 for c in chunks if "二、短章" in c.text) == 1
        assert sum(1 for c in chunks if "三、另一章" in c.text) == 1

    def test_preamble_separate_from_first_chapter(self):
        """文首第一个一级标题之前的内容（封面）独立成块，不并入第一章"""
        ch = HierarchicalChunker(chunk_size=1024, overlap=0)
        text = "封面标题文字。\n\n修订记录说明。\n\n# 一、第一章\n\n第一章正文。"
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 2
        assert chunks[0].text.startswith("封面标题文字。")
        assert "第一章" not in chunks[0].text
        assert chunks[1].text.startswith("# 一、第一章")

    def test_chapter_level_param(self):
        """chapter_level=2：合并不跨二级标题（默认 1 级时全文可一路聚合）"""
        text = "## 甲节\n\n甲正文。\n\n## 乙节\n\n乙正文。"
        default_chunks = HierarchicalChunker(chunk_size=1024, overlap=0).chunk(text)
        _assert_offsets(default_chunks, text)
        assert len(default_chunks) == 1  # 无 # 一级标题 → 整篇一个聚合组

        lvl2 = HierarchicalChunker(chunk_size=1024, overlap=0,
                                   chapter_level=2).chunk(text)
        _assert_offsets(lvl2, text)
        assert len(lvl2) == 2  # ## 成为不可跨越的章级边界
        assert lvl2[0].text.startswith("## 甲节")
        assert lvl2[1].text.startswith("## 乙节")


class TestOversizeFallback:
    """超长节点兜底二次切分（段落 → 句子 → 硬切）"""

    def test_oversize_leaf_split_by_paragraph(self):
        """无子节点的超长正文 → 按段落切成多块，标题随首块"""
        ch = HierarchicalChunker(chunk_size=100, overlap=0)
        paras = [f"第{i}段：" + "填充文字" * 12 for i in range(6)]
        text = "# 长节标题\n\n" + "\n\n".join(paras)
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) > 1
        assert all(_eff(_slice_of(c, text)) <= 100 for c in chunks)
        assert chunks[0].text.startswith("# 长节标题")
        for i in range(6):
            assert f"第{i}段" in _body(chunks, text)

    def test_oversize_paragraph_split_by_sentence(self):
        """单段落自身超长（无空行）→ 按句子边界切，不切在句子中间"""
        ch = HierarchicalChunker(chunk_size=60, overlap=0)
        text = "# 标题\n\n" + "".join(f"这是第{i}个句子，用于测试句子感知切分。" for i in range(12))
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) > 1
        # 除末块外，块正文都应以句号结尾（边界落在句子之间）
        for c in chunks[1:-1]:
            assert _slice_of(c, text).rstrip().endswith("。"), c.text[-20:]

    def test_hard_cut_without_separator(self):
        """无标点无空行的超长串 → 按 chunk_size 硬切兜底，每块不超预算"""
        ch = HierarchicalChunker(chunk_size=50, overlap=0)
        text = "# 标题\n\n" + "无标点长串" * 60  # 300 字符，无任何可分界
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        _assert_coverage(chunks, text)
        assert len(chunks) > 1
        assert all(_eff(_slice_of(c, text)) <= 50 for c in chunks)
        # 内容连续性：块首尾相接，除空白外无缺口
        assert _body(chunks, text).replace("\n", "") == text.replace("\n", "")

    def test_oversize_subtree_children_boundary(self):
        """超长子树：块边界落在子节点之间（子标题不被与它的正文拆开）"""
        ch = HierarchicalChunker(chunk_size=150, overlap=0)
        text = ("# 章\n\n"
                + "".join(f"## 小节{i}\n\n" + "小节正文。" * 8 + "\n\n" for i in range(4)))
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        _assert_coverage(chunks, text)
        _assert_no_heading_only_block(chunks, text)
        assert len(chunks) > 1
        # 每个子节点（标题 + 它的正文）完整落在某一个块内，不被切开
        # （块边界去掉首尾空白，故比较"内容尾"而非"节点尾"）
        heads = [(off, t) for off, _lvl, t in _iter_headings(text, [])]
        for i, (off, title) in enumerate(heads):
            if not title.startswith("小节"):
                continue
            end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
            content_end = off + len(text[off:end].rstrip())
            assert any(c.char_start <= off and content_end <= c.char_end
                       for c in chunks), \
                f"子节点 {title!r} 区间 [{off}, {content_end}) 被切开"

    def test_oversize_first_child_keeps_heading_attached(self):
        """首个子孙节点自身超长时：标题行并进其首片，不产生纯标题碎片块"""
        ch = HierarchicalChunker(chunk_size=120, overlap=0)
        text = ("# 三、章\n\n## 3.3 节\n\n"
                + "".join(f"第{i}段：用于填充长度的正文内容。" for i in range(20)))
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        _assert_coverage(chunks, text)
        _assert_heading_lines_intact(chunks, text)
        _assert_no_heading_only_block(chunks, text)
        assert chunks[0].text.startswith("# 三、章")
        assert "3.3 节" in chunks[0].text   # 节标题与正文同片

    def test_heading_with_dotted_numbering_never_split(self):
        """带编号点号的标题行不被句级切分切开（编号里的小圆点是句界）"""
        ch = HierarchicalChunker(chunk_size=16, overlap=0)
        text = ("# 章\n\n##### 3.3.3.5.6 设备专用术语\n\n" + "正文内容。" * 20)
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        _assert_coverage(chunks, text)
        _assert_heading_lines_intact(chunks, text)
        # 标题整行完整出现在某一个块里
        assert any("##### 3.3.3.5.6 设备专用术语" in _slice_of(c, text)
                   for c in chunks)

    def test_heading_with_dotted_numbering_hard_cut(self):
        """无标点长串走到硬切兜底时，标题行同样不被切开"""
        ch = HierarchicalChunker(chunk_size=32, overlap=0)
        text = ("# 章\n\n##### 3.3.3.5.6 设备专用术语\n\n" + "正文内容" * 200)
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        _assert_coverage(chunks, text)
        _assert_heading_lines_intact(chunks, text)
        assert any("##### 3.3.3.5.6 设备专用术语" in _slice_of(c, text)
                   for c in chunks)


class TestImageBudget:
    """图片引用不计入长度预算 + 真实长度硬上限"""

    IMG = "![](images/" + "a" * 120 + ".png)"

    def test_image_url_not_counted(self):
        """图片 URL 占满长度、有效内容很少 → 仍并成一块（URL 不占预算）"""
        ch = HierarchicalChunker(chunk_size=200, overlap=0)
        text = "# 图集\n\n说明文字。\n\n" + "\n\n".join(self.IMG for _ in range(4))
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert len(text) > 200            # 真实长度远超预算
        assert _eff(text) <= 200          # 有效长度在预算内
        assert len(chunks) == 1           # 因此不切

    def test_raw_len_hard_cap(self):
        """图片密集到真实长度超 chunk_size×4 → 不再合并（碎片块兜底）"""
        ch = HierarchicalChunker(chunk_size=100, overlap=0)
        text = "# 图集\n\n" + "\n\n".join(f"图{i}\n\n{self.IMG}" for i in range(6))
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) > 1
        # 除保护区间（单张图片本身）外，块真实长度不超过 chunk_size × 4
        for c in chunks:
            if self.IMG in c.text and _eff(c.text) < 10:
                continue
            assert len(c.text) <= 100 * 4 + 200, f"块真实长度过大: {len(c.text)}"


class TestProtectedRanges:
    """保护区间（表格/代码块/图片）不被切开"""

    def test_table_not_cut(self):
        """长表格整体成块，切点不落在表格内部"""
        ch = HierarchicalChunker(chunk_size=120, overlap=0)
        rows = "\n".join(f"| 行{i} | 值{i} | 说明{i} |" for i in range(8))
        table = f"| 列A | 列B | 列C |\n| --- | --- | --- |\n{rows}"
        text = ("# 章\n\n" + "前置正文。" * 10 + "\n\n" + table
                + "\n\n" + "后置正文。" * 10)
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        _assert_coverage(chunks, text)
        # 表格保护区间（common 定位，与被测实现同源）
        from backend.chunking.common import _find_table_ranges
        ranges = _find_table_ranges(text)
        assert ranges, "样本里应识别出表格"
        for ts, te in ranges:
            for c in chunks:
                for boundary in (c.char_start, c.char_end):
                    assert not (ts < boundary < te), \
                        f"切点 {boundary} 落在表格内部 [{ts}, {te})"
        assert any("| 行7 |" in _slice_of(c, text) for c in chunks)

    def test_oversize_table_kept_whole(self):
        """超长表格整块保留（宁可超预算，也不切开）"""
        ch = HierarchicalChunker(chunk_size=50, overlap=0)
        rows = "\n".join(f"| 行{i} | 值{i} |" for i in range(20))
        text = f"# 章\n\n| 列A | 列B |\n| --- | --- |\n{rows}\n\n尾注。"
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        table_text = "\n".join(f"| 行{i} | 值{i} |" for i in range(20))
        assert any(table_text in _slice_of(c, text) for c in chunks)

    def test_code_fence_kept_whole(self):
        """围栏代码块整体成块（块内 # 行不当作标题）"""
        ch = HierarchicalChunker(chunk_size=80, overlap=0)
        code = "```bash\n" + "\n".join(f"echo 第{i}行" for i in range(10)) + "\n```"
        text = "# 章\n\n" + "前置正文。" * 8 + "\n\n" + code + "\n\n尾注。"
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert any(code in _slice_of(c, text) for c in chunks)


class TestHeadingChain:
    """块首标题链（祖先 > 父；自己的标题即块首第一行）"""

    def test_ancestors_prefixed(self):
        """以子标题开头的块补祖先链，链为单行、自己的标题在第二行"""
        ch = HierarchicalChunker(chunk_size=100, overlap=0)
        text = ("# 一、章\n\n## 1.1 节\n\n" + "节正文。" * 40
                + "\n\n### 1.1.1 小节\n\n" + "小节正文。" * 40)
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        tail = [c for c in chunks
                if c.text.startswith("一、章 > 1.1 节\n### 1.1.1 小节")]
        assert tail, [c.text[:40] for c in chunks]
        assert len(tail[0].text.split("\n")[0].split(" > ")) == 2  # 祖先两级

    def test_own_heading_not_duplicated(self):
        """块首就是一级标题 → 无祖先可补，块首不出现标题链前缀"""
        ch = HierarchicalChunker(chunk_size=512, overlap=0)
        text = "# 一、章\n\n## 1.1 节\n\n正文。"
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert chunks[0].text.startswith("# 一、章")
        assert " > " not in chunks[0].text.split("\n")[0]

    def test_add_heading_path_disabled(self):
        """add_heading_path=False → 不拼标题链前缀"""
        ch = HierarchicalChunker(chunk_size=120, overlap=0, add_heading_path=False)
        text = ("# 一、章\n\n## 1.1 节\n\n" + "正文。" * 40
                + "\n\n## 1.2 节\n\n后一节正文。")
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        assert all(" > " not in c.text for c in chunks)
        for c in chunks:
            assert c.text == text[c.char_start:c.char_end]

    def test_chain_follows_deep_nesting(self):
        """深层标题的链按层级递增（祖先 > 父 > …的顺序不串级）"""
        ch = HierarchicalChunker(chunk_size=100, overlap=0)
        text = ("# 甲\n\n## 乙\n\n### 丙\n\n" + "丙正文。" * 30
                + "\n\n#### 丁\n\n" + "丁正文。" * 30)
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        deep = [c for c in chunks if c.text.startswith("甲 > 乙 > 丙\n#### 丁")]
        assert deep, [c.text[:40] for c in chunks]
        # 链按层级递增，最后一级即块首标题的父级
        assert deep[0].text.split("\n")[0] == "甲 > 乙 > 丙"


class TestOffsetsAndOrder:
    """偏移契约与顺序（综合样本）"""

    def test_offset_contract_mixed_doc(self):
        """混合文档（多章/多级标题/表格/代码/图片/超长段）偏移契约全通过"""
        ch = HierarchicalChunker(chunk_size=200, overlap=0)
        text = (
            "# 一、章一\n\n章一前言。\n\n## 1.1 节\n\n" + "节正文。" * 30 + "\n\n"
            + "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n\n"
            + "![图](images/" + "b" * 80 + ".png)\n\n"
            + "```python\nprint('代码块')\n```\n\n"
            + "### 1.1.1 小节\n\n" + "小节正文。" * 10 + "\n\n"
            + "# 二、章二\n\n章二正文。"
        )
        chunks = ch.chunk(text)
        _assert_offsets(chunks, text)
        _assert_coverage(chunks, text)
        _assert_heading_lines_intact(chunks, text)
        assert len(chunks) > 2
        h1 = [off for off, lvl, _ in _iter_headings(text, find_protected_ranges(text))
              if lvl == 1]
        for c in chunks:
            for off in h1:
                assert not (c.char_start < off < c.char_end)

    def test_overlap_only_inside_fallback(self):
        """overlap 只在兜底二次切分内部生效：块可重叠，但不跨一级标题"""
        ch = HierarchicalChunker(chunk_size=80, overlap=30)
        text = ("# 一、章一\n\n" + "".join(f"第{i}句，用于测试。" for i in range(10))
                + "\n\n# 二、章二\n\n章二正文。")
        chunks = ch.chunk(text)
        for c in chunks:  # 偏移契约（含 overlap 回移的块）
            assert 0 <= c.char_start <= c.char_end <= len(text)
            assert c.text == text[c.char_start:c.char_end] or \
                c.text.endswith("\n" + text[c.char_start:c.char_end])
        # 同一章内的兜底切分块之间存在 overlap 续接
        assert any(chunks[i].char_start < chunks[i - 1].char_end
                   for i in range(1, len(chunks)))
        # overlap 回移不得越过章边界
        off2 = text.index("# 二、章二")
        for c in chunks:
            assert not (c.char_start < off2 < c.char_end)
        _assert_no_heading_only_block(chunks, text)

    def test_chunk_matches_slice_without_prefix(self):
        """不拼标题链时 text 严格等于原文切片（逐字节）"""
        ch = HierarchicalChunker(chunk_size=100, overlap=0, add_heading_path=False)
        text = "# 一、章\n\n" + "正文内容。" * 40
        for c in ch.chunk(text):
            assert c.text == text[c.char_start:c.char_end]
