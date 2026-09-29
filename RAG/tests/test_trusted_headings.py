"""标题识别「信任模式」测试：docx_struct 产物只认 `#` 标题

背景（实测事故）：一份用结构解析（docx_struct）入库的 docx，Word 里有 495 个
真标题（Heading 样式，docx_struct 输出为 `#`），另外还有 50 处**排版加粗的
普通正文**（Word 里 style=Normal、outlineLvl=None，docx_struct 忠实输出为
`**...**`）。`_iter_headings` 的「整行加粗式」规则把它们猜成标题——那条规则
本是为 MinerU 产物准备的（MinerU 解析 Office 时标题会退化成加粗），在
docx_struct 这条链路上 100% 制造误判：切块详情/文档预览的「目录」里因此塞满
"注：其他内容默认即可"，545 个标题里 50 个是假的。

修复：`_iter_headings` 加 `trusted` 参数——`parser_engine=docx_struct` 时只认
ATX `#` 标题，关闭全部启发式规则（结构解析给的是**确定信息**，不该再猜）。

本文件守住两条：
1. `trusted=True` 只认 `#`：加粗式/包裹式/前导符号式/setext/裸编号一律不认
2. `trusted=False` **行为逐字节不变**（MinerU/DeepDOC/plain 仍走启发式兜底）
"""
from __future__ import annotations

from backend.chunking import get_chunker, trusted_headings
from backend.chunking.common import _iter_headings

# 五种启发式规则各来一行（trusted=True 时全不该认）
SAMPLE = """# 第一章 概述

正文内容。

**注：其他内容默认即可**

正文内容。

【包裹式标题】

正文内容。

■ 前导符号式标题

正文内容。

setext 标题
===========

正文内容。

1.1 裸编号标题

正文内容。
"""

ATX_ONLY = ["第一章 概述"]


def _titles(text: str, trusted: bool) -> list:
    return [t for _, _, t in _iter_headings(text, None, None, trusted=trusted)]


class TestTrustedMode:

    def test_trusted_keeps_only_atx(self):
        """★ 信任模式：只有 `#` 算标题，五种启发式一个都不认"""
        assert _titles(SAMPLE, trusted=True) == ATX_ONLY

    def test_untrusted_still_recognizes_heuristics(self):
        """★ 非信任模式行为不变：启发式照常兜底（MinerU 等引擎依赖它）

        这是本次改动的**回归护栏**——MinerU/DeepDOC/plain 的产物质量不稳
        （标题可能退化成加粗、裸编号），那条规则必须留着。
        """
        titles = _titles(SAMPLE, trusted=False)
        assert titles[0] == "第一章 概述"
        assert "注：其他内容默认即可" in titles, "加粗式应照认"
        assert "包裹式标题" in titles
        assert "前导符号式标题" in titles
        assert "setext 标题" in titles
        assert len(titles) >= 5, f"启发式应认出多条，实际 {titles}"

    def test_empty_text(self):
        for trusted in (True, False):
            assert _titles("", trusted=trusted) == []

    def test_protected_ranges_still_respected(self):
        """信任模式不影响保护区间（代码块里的 `#` 行仍不是标题）"""
        text = "# 真标题\n\n```\n# 代码块里的井号\n```\n"
        from backend.chunking.common import find_protected_ranges
        hs = _iter_headings(text, find_protected_ranges(text), None,
                            trusted=True)
        assert [t for _, _, t in hs] == ["真标题"]


class TestTrustedHeadingsFlag:

    def test_only_docx_struct_is_trusted(self):
        """★ 只有 docx_struct 走信任模式，其余引擎一律不信任"""
        assert trusted_headings({"parser_engine": "docx_struct"}) is True
        for eng in ("mineru", "deepdoc", "plain", "auto", "", None):
            assert trusted_headings({"parser_engine": eng}) is False, \
                f"{eng!r} 不该启用信任模式"

    def test_case_and_space_tolerant(self):
        """配置值大小写/空格容错（与 resolve_parser_engine 口径一致）"""
        assert trusted_headings({"parser_engine": " DOCX_STRUCT "}) is True

    def test_missing_key(self):
        assert trusted_headings({}) is False


class TestChunkerWiring:
    """工厂把 trusted 透传到切块器（切块/目录/标题链三处必须同源）"""

    def test_parent_child(self):
        # parent_split_level 是必填语义（get_chunker 直接透传 config 值，
        # 真实入库时 parser_config 里必有），测试里显式给出
        c = get_chunker("parent_child",
                        {"parser_engine": "docx_struct", "parent_split_level": 2})
        assert c.trusted is True
        c2 = get_chunker("parent_child",
                         {"parser_engine": "mineru", "parent_split_level": 2})
        assert c2.trusted is False

    def test_title_splitter(self):
        c = get_chunker("title", {"parser_engine": "docx_struct"})
        assert c.trusted is True

    def test_hierarchical(self):
        c = get_chunker("hierarchical", {"parser_engine": "docx_struct"})
        assert c.trusted is True

    def test_trusted_chunker_drops_bold_fake_headings(self):
        """★ 端到端：结构解析产物切块时不再把加粗正文当标题

        同一段文本，两种引擎配置切出来的块数不同——信任模式下那行加粗正文
        归入上一节，不再自成一节。
        """
        text = "# 章标题\n\n正文一。\n\n**注：其他内容默认即可**\n\n正文二。\n"
        trusted = get_chunker("title", {"parser_engine": "docx_struct"})
        legacy = get_chunker("title", {"parser_engine": "mineru"})
        assert len(trusted.chunk(text)) < len(legacy.chunk(text)), \
            "信任模式不应把加粗正文切成分节"
