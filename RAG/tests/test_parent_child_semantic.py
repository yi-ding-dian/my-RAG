"""父子分块语义测试（对齐 KnowFlow）：子块不跨标题章节、父块=完整章节

核心语义断言（对照用户验收反馈"父子分块不对"的修复）：
① 子块不跨 H1/H2（子块边界标题层级）边界
② 子块 char 区间完整落在父块区间内（不再"起点归属"导致 overlap 尾巴越出）
③ 父块=完整章节（章节内多子块共享同一父块，父块不受 parent_chunk_size
   大小限制，仅超长单节 >50_000 字符走兜底）
④ 无标题文本（文档头）为独立父块
⑤ overlap 只在章节段内部生效，不跨章节
⑥ 超长无标题文档走 parent_chunk_size 兜底切分
⑦ 标题注入（add_heading_paths）在章节内子块上生效
⑧ parent_split_level>3 时父块按更高级别聚合、子块边界封顶 H3
"""
from __future__ import annotations

import re

import pytest

from backend.chunking.splitter import (Chunk, ParentChildChunker,
                                       _HEADING_RE, add_heading_paths)

# 语义样本：多级标题 + 长短章节（1.1 节内容足够长以切出多子块）
SEM_TEXT = """# 第一章 总览

这是第一章的引言内容，介绍背景。包含足够多的文字让子块切分生效。继续补充一些内容使段落变长以便切出多个子块，例如这里的背景描述与目录说明文字。

## 1.1 背景

第一章第一小节的内容，讲述历史沿革与现状分析，内容较长需要切分为多个子块。历史沿革部分描述了系统的发展脉络，现状分析部分罗列了当前存在的问题与挑战，这段文字的长度足以让子块切分生效并验证章节边界约束。补充说明技术路线的选择依据，以及后续章节的组织方式，使本节内容超过父块大小上限，验证完整章节父块不受 parent_chunk_size 限制的语义，从而保证父块上下文完整、不因大小限制被切碎，这是本重构的核心目标之一。

## 1.2 现状

第一章第二小节的内容，分析当前存在的问题与挑战，包含较长的分析文字与总结性描述。

# 第二章 方案

第二章的内容，提出整体解决方案与实施路径，是回答问题的关键章节。

## 2.1 技术选型

第二章第一小节，描述技术选型的依据与对比分析。

## 2.2 实施计划

第二章第二小节，描述实施步骤与时间规划安排。
"""

# 无标题文档头样本：前置说明文字 + 两个章节
DOC_HEAD_TEXT = """前置说明文字，不属于任何章节，应作为独立父块存在。

# 第一章

第一章内容，介绍背景。

## 1.1 小节

小节内容，说明细节。
"""

# 超长无标题文本（> 50_000 字符触发兜底）
_NO_HEADING_LONG = "无标题纯文本内容。" * 7000

# H4 场景样本：父块边界到 H4，子块边界封顶 H3
H4_TEXT = """# 章

## 节1

### 小节1

#### 子节A

AAAA 内容，说明子节 A 的细节。

#### 子节B

BBBB 内容，说明子节 B 的细节。
"""


def _heading_starts(text: str, level: int) -> list[int]:
    """全部 <= level 级标题行起点（偏移升序）"""
    return [m.start() for m in _HEADING_RE.finditer(text) if len(m.group(1)) <= level]


def _assert_text_slices(chunks: list[Chunk], full: str) -> None:
    """偏移正确性：text == full[char_start:char_end] 且区间不越界"""
    for c in chunks:
        assert 0 <= c.char_start <= c.char_end <= len(full), \
            f"偏移越界: {c.char_start}..{c.char_end} len={len(full)}"
        assert full[c.char_start:c.char_end] == c.text, \
            f"text 与偏移切片不一致: {c.text[:20]!r}"


class TestSemanticChunking:
    """语义核心：子块不跨章节、子块完整落在父块内、父块=完整章节"""

    def test_child_not_cross_heading_boundaries(self):
        """① 子块不跨 H1/H2 边界（child_split_level=2）：子块区间内
        不得包含其他子块边界标题的起点（子块自身以标题行开头合法）"""
        chunker = ParentChildChunker(chunk_size=50, overlap=10,
                                     parent_chunk_size=200, parent_split_level=2)
        result = chunker.chunk_parent_child(SEM_TEXT)
        assert chunker.child_split_level == 2
        h_bounds = _heading_starts(SEM_TEXT, 2)
        assert h_bounds, "样例应含 H1/H2 标题"
        for c in result.children:
            crossed = [h for h in h_bounds if c.char_start < h < c.char_end]
            assert not crossed, f"子块跨标题边界: {c.text[:20]!r} 跨偏移 {crossed}"
        # 至少一个章节被切出多个子块（验证多子块场景真实存在）
        per_section: dict = {}
        for c in result.children:
            key = next((h for h in h_bounds if h <= c.char_start), -1)
            per_section[key] = per_section.get(key, 0) + 1
        assert max(per_section.values()) >= 2, "应存在章节内多子块"

    def test_child_interval_fully_inside_parent(self):
        """② 每个子块 char 区间完整落在其归属父块区间内（含映射一致性）"""
        chunker = ParentChildChunker(chunk_size=50, overlap=10,
                                     parent_chunk_size=200, parent_split_level=2)
        result = chunker.chunk_parent_child(SEM_TEXT)
        for i, c in enumerate(result.children):
            p = result.child_parent_map[i]
            assert 0 <= p < len(result.parents), f"映射越界: {i}->{p}"
            parent = result.parents[p]
            assert parent.char_start <= c.char_start and c.char_end <= parent.char_end, \
                f"子块 {i} ({c.char_start}..{c.char_end}) 未完整落在父块 {p} " \
                f"({parent.char_start}..{parent.char_end})"

    def test_parent_is_complete_section(self):
        """③ 父块=完整章节：与章节段区间一致、章节内多子块共享同一父块、
        父块大小不受 parent_chunk_size 限制（1.1 节是 1024 上限的 2 倍多）"""
        chunker = ParentChildChunker(chunk_size=50, overlap=10,
                                     parent_chunk_size=200, parent_split_level=2)
        result = chunker.chunk_parent_child(SEM_TEXT)
        # 父块数与章节段数一致（无文档头 → 6 段）
        sections = chunker._split_sections(SEM_TEXT, list(_HEADING_RE.finditer(SEM_TEXT)), 2)
        assert len(result.parents) == len(sections) == 6
        for p, (s, e) in zip(result.parents, sections):
            assert p.text == SEM_TEXT[s:e].strip(), "父块应为完整章节（标题行起）"
        # 1.1 节（>200 字符）整体为一个父块：parent_chunk_size 不是大小上限
        h = _heading_starts(SEM_TEXT, 2)
        sec11 = next(i for i, m in enumerate(_HEADING_RE.finditer(SEM_TEXT))
                     if m.group(2).strip() == "1.1 背景")
        p11 = result.parents[sec11]
        assert len(p11.text) > 200, "完整章节父块应超过 parent_chunk_size（无上限语义）"
        # 章节内多子块共享同一父块
        p11_children = [i for i, c in enumerate(result.children)
                        if p11.char_start <= c.char_start < p11.char_end]
        assert len(p11_children) >= 2, "1.1 节内应有多个子块"
        assert all(result.child_parent_map[i] == sec11 for i in p11_children)

    def test_doc_header_is_own_parent(self):
        """④ 无标题文档头作为独立父块（text 不以标题行开头）"""
        chunker = ParentChildChunker(chunk_size=30, overlap=0,
                                     parent_split_level=2)
        result = chunker.chunk_parent_child(DOC_HEAD_TEXT)
        assert len(result.parents) == 3, \
            f"应 3 个父块（文档头+第一章+1.1 小节）: {len(result.parents)}"
        assert result.parents[0].text.startswith("前置说明文字")
        assert not result.parents[0].text.lstrip().startswith("#")
        assert result.parents[1].text.startswith("# 第一章")
        assert result.parents[2].text.startswith("## 1.1 小节")
        # 文档头内的子块归属文档头父块
        for i, c in enumerate(result.children):
            p = result.parents[result.child_parent_map[i]]
            assert p.char_start <= c.char_start and c.char_end <= p.char_end

    def test_overlap_not_cross_section(self):
        """⑤ overlap 只在章节段内部生效：所有子块（含 overlap 续接块）
        完整落在单个章节段区间内"""
        chunker = ParentChildChunker(chunk_size=50, overlap=20,
                                     parent_chunk_size=200, parent_split_level=1)
        result = chunker.chunk_parent_child(SEM_TEXT)
        sections = chunker._split_sections(SEM_TEXT, list(_HEADING_RE.finditer(SEM_TEXT)), 1)
        assert len(sections) == 2, "按 H1 切应有 2 个章节段"
        for c in result.children:
            inside = [s for s in sections
                      if s[0] <= c.char_start and c.char_end <= s[1]]
            assert inside, f"子块 ({c.char_start}..{c.char_end}) 跨章节段边界"

    def test_offsets_slice_correct(self):
        """子块/父块偏移与全文切片一致（同基准原文）"""
        chunker = ParentChildChunker(chunk_size=50, overlap=10,
                                     parent_chunk_size=200, parent_split_level=2)
        result = chunker.chunk_parent_child(SEM_TEXT)
        _assert_text_slices(result.children, SEM_TEXT)
        _assert_text_slices(result.parents, SEM_TEXT)

    def test_empty_and_blank_text(self):
        """空文本/纯空白 → 空结果（子块/父块/映射全空）"""
        chunker = ParentChildChunker(chunk_size=50, overlap=10)
        for t in ("", "   \n\t  "):
            result = chunker.chunk_parent_child(t)
            assert result.children == [] and result.parents == []
            assert result.child_parent_map == {}


class TestFallbackAndLevels:
    """超长兜底与层级配置"""

    def test_oversized_no_heading_document_fallback(self):
        """⑥ 超长无标题文档（>50_000 字符）走 parent_chunk_size 兜底切分：
        多个父块、每块不超兜底上限；子块数量正常、偏移一致"""
        chunker = ParentChildChunker(chunk_size=100, overlap=0,
                                     parent_chunk_size=500, parent_split_level=2)
        assert len(_NO_HEADING_LONG) > 50_000
        result = chunker.chunk_parent_child(_NO_HEADING_LONG)
        assert len(result.parents) > 1, "超长无标题文档应被兜底切成多个父块"
        for p in result.parents:
            assert len(p.text) <= 500 + 2, \
                f"兜底父块超上限: {len(p.text)}（parent_chunk_size=500 兜底）"
        assert len(result.children) > 10
        _assert_text_slices(result.parents, _NO_HEADING_LONG)
        _assert_text_slices(result.children, _NO_HEADING_LONG)
        # 映射全部存在（兜底字符级父块边界无章节语义，回退起始归属）
        assert all(result.child_parent_map[i] >= 0 for i in range(len(result.children)))

    def test_oversized_section_with_subheadings(self):
        """超长单节含子标题：单 H2 节 >50_000 触发兜底；子块不跨子块
        边界标题（child 级）、完整落在兜底父块内"""
        # 单个超长 H2 节（>50_000），内含 50 个 H3 小段（每个 ~1120 字符）
        big = "# 章\n\n## 超长子节\n\n" + "\n\n".join(
            f"### 小段{i}\n\n" + "小段内容文字。" * 160 for i in range(50))
        assert len(big) > 50_000
        chunker = ParentChildChunker(chunk_size=100, overlap=0,
                                     parent_chunk_size=3000, parent_split_level=2)
        result = chunker.chunk_parent_child(big)
        assert len(result.parents) > 2, "H1 节 + 超长 H2 节兜底为多个父块"
        for p in result.parents:
            assert len(p.text) <= 3000 + 2, \
                f"兜底父块超上限: {len(p.text)}"
        # 子块不跨子块边界标题（child_split_level=2 → H1/H2；H3 不是子块边界）
        h_bounds = _heading_starts(big, 2)
        for c in result.children:
            crossed = [h for h in h_bounds if c.char_start < h < c.char_end]
            assert not crossed, "兜底场景子块也不得跨 H1/H2 边界"
        # 子块完整落在兜底父块内（overlap=0 时字符级父块与子块切分对齐）
        for i, c in enumerate(result.children):
            p = result.parents[result.child_parent_map[i]]
            assert p.char_start <= c.char_start and c.char_end <= p.char_end, \
                f"子块 {i} 未完整落在兜底父块 {result.child_parent_map[i]} 内"

    def test_parent_split_level_gt3_child_boundary_caps_at_3(self):
        """⑧ parent_split_level=4：父块按 H4 聚合（5 个），子块边界封顶 H3

        子块边界封顶 H3 → ### 小节1 段内切出的子块可能跨 H4 边界（H4 不是
        子块边界），此时无父块完整包含它，映射按起始归属（KnowFlow 对这类
        子块为孤儿，我们回退起始归属便于携带上下文）"""
        chunker = ParentChildChunker(chunk_size=50, overlap=0,
                                     parent_split_level=4)
        result = chunker.chunk_parent_child(H4_TEXT)
        assert chunker.child_split_level == 3, "子块边界层级应封顶 3"
        assert chunker.parent_split_level == 4
        assert len(result.parents) == 5, \
            f"H4 场景父块数（按 H1~H4 聚合）应为 5: {len(result.parents)}"
        assert result.parents[0].text.startswith("# 章")
        assert result.parents[3].text.startswith("#### 子节A")
        assert result.parents[4].text.startswith("#### 子节B")
        # 子块归属：完整落在归属父块内，或（跨 H4 边界的子块）起始落在归属父块内
        for i, c in enumerate(result.children):
            p = result.child_parent_map[i]
            assert 0 <= p < len(result.parents), f"映射越界: {i}->{p}"
            parent = result.parents[p]
            assert parent.char_start <= c.char_start < parent.char_end, \
                f"子块 {i} 起始 {c.char_start} 不在归属父块 {p} 区间"
            if parent.char_end < c.char_end:
                # 跨 H4 边界（子块边界封顶 H3，H4 不是子块边界）
                assert any(h < c.char_end for h in _heading_starts(H4_TEXT, 4)
                           if c.char_start < h), "越界只允许跨父块边界标题"

    def test_child_split_level_follows_parent_below_3(self):
        """parent_split_level<3 时子块边界随父块收紧（min 语义）"""
        assert ParentChildChunker(parent_split_level=1).child_split_level == 1
        assert ParentChildChunker(parent_split_level=2).child_split_level == 2
        assert ParentChildChunker(parent_split_level=6).child_split_level == 3

    def test_parent_split_level_invalid(self):
        """parent_split_level 越界抛 ValueError（与旧 MarkdownSplitter 校验一致）"""
        with pytest.raises(ValueError):
            ParentChildChunker(parent_split_level=0)
        with pytest.raises(ValueError):
            ParentChildChunker(parent_split_level=7)


class TestHeadingInjection:
    """标题注入：add_heading_paths 对章节内非标题子块补父标题链（ingestion 调用）"""

    def test_heading_injection_via_add_heading_paths(self):
        chunker = ParentChildChunker(chunk_size=50, overlap=10,
                                     parent_split_level=2)
        result = chunker.chunk_parent_child(SEM_TEXT)
        # 模拟 ingestion_service 的 enable_heading_in_content 流程
        children = add_heading_paths(result.children, SEM_TEXT)
        injected = [c.text for c in children if " > " in c.text[:100]]
        assert injected, "应存在补了父标题链的子块"
        assert any(t.startswith("第一章 总览 > 1.1 背景") for t in injected), \
            "1.1 节内非标题子块应带 '第一章 总览 > 1.1 背景' 前缀"
        # 首个子块自带标题行（章节段以标题行开头）→ 不重复拼接
        assert children[0].text.startswith("# 第一章 总览")
        # 注入只改 text，偏移保持不变
        for c, orig in zip(children, result.children):
            assert (c.char_start, c.char_end) == (orig.char_start, orig.char_end)
