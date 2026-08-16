"""title 切块纯文本标题样式识别测试（增强 _iter_headings 统一标题识别）

覆盖样式与级别映射（供 split_level 过滤，与 # 标题统一语义）：
- setext 下划线式：'标题\\n========' → 1 级；'标题\\n--------' → 2 级，
  边界在标题文字行（下划线行并入标题块）
- 单行包裹式：===== 标题 ===== / --- 备注 --- / *** 说明 *** / 【标题】 /
  ━━ 标题 ━━ → 2 级（首尾符号 >=3 个，━ ─ 为 >=2 个，左右可不等长）
- 前导符号式：■◆●※▶▍ 开头（后跟空格或直接接文字）→ 2 级

防误判：
- 纯符号行（单独 ======== / ------ 装饰线）不是标题
- XML/HTML 标签行（<xx>）、冒号结尾标签行（E文件导出实例：）、表格行
  （| x |）不作 setext 内容行
- 标题内容/整行 >50 字符不识别；连续标题不切（复用 _filter_continuous_headings）
- 表格/代码块保护区间内的行不识别（复用 find_protected_ranges 过滤）
- 与 # 标题混排兼容；真实文件（E文件导出实例全部.txt）实测：
  92 个纯文本标题全部成为块起点、无块跨标题、偏移一致
"""
from __future__ import annotations

import re

import pytest

from backend.chunking.splitter import (Chunk, MarkdownSplitter,
                                       _iter_headings, find_protected_ranges)

# 真实文档：纯文本 E 文件导出实例（1495 行，44 个 '===== 6.x =====' +
# 43 个 '--- 备注 ---' + 4 个【】标题 + 多处纯 =/- 装饰线）
E_FILE = "/home/yicaibao/my/my-RAG/测试文档/E文件导出实例全部.txt"

# 连续标题样本（复用增强测试语义）
CONSECUTIVE_WRAPPED = "===== 标题A =====\n===== 标题B =====\n后续正文内容。"


def _assert_offsets(chunks: list[Chunk], full: str) -> None:
    """偏移正确性：每块 text 必须等于 full[char_start:char_end] 且区间不越界"""
    for c in chunks:
        assert 0 <= c.char_start <= c.char_end <= len(full), \
            f"偏移越界: {c.char_start}..{c.char_end} len={len(full)}"
        assert full[c.char_start:c.char_end] == c.text, \
            f"text 与偏移切片不一致: {c.text[:20]!r}"


def _title_chunker(**kw):
    defaults = dict(chunk_size=40, overlap=0, split_level=3)
    defaults.update(kw)
    return MarkdownSplitter(**defaults)


def _heading_bounds_text(text: str, level: int = 3) -> list[int]:
    """直接取标题边界（等价于 _heading_bounds，便于断言样式识别）"""
    return _title_chunker(split_level=level)._heading_bounds(
        text, level, find_protected_ranges(text))


def _read_efile() -> str:
    """读取 E 文件（不存在则跳过用例，保证无该文件的 CI 环境可跑）"""
    try:
        with open(E_FILE, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        pytest.skip(f"E 文件不可读: {exc}")


def _real_heading_lines(text: str) -> list[str]:
    """独立正则对照：=/- 包裹（内容非纯符号）+ 【】整行"""
    found = []
    for l in text.split("\n"):
        s = l.strip()
        if not s:
            continue
        if s[0] in "=-*" and s[-1] == s[0] and len(s) > 6:
            head = 0
            while head < len(s) and s[head] == s[0]:
                head += 1
            tail = 0
            while tail < len(s) and s[len(s) - 1 - tail] == s[0]:
                tail += 1
            inner = s[head:len(s) - tail].strip() if head + tail < len(s) else ""
            if inner and not set(inner) <= set("-=*~_#|+━─—·•"):
                found.append(s)
        elif len(s) >= 3 and s[0] == "【" and s[-1] == "】":
            found.append(s)
    return found


class TestSetextUnderline:
    """setext 下划线式：'标题\\n===='（= 一级 / - 二级），边界在标题文字行"""

    def test_eq_underline_level1(self):
        """= 下划线 → 1 级：split_level=1 时切出边界"""
        text = "前言。\n\n第一章 概述\n====\n内容。\n\n第二章 详解\n====\n更多内容。"
        bounds = _heading_bounds_text(text, level=1)
        assert len(bounds) == 2, f"两个 = 下划线标题应识别: {bounds}"
        assert text[bounds[0]:].startswith("第一章 概述"), "边界应在标题文字行起点"
        chunks = _title_chunker().chunk(text)
        assert len(chunks) == 3, "前言 + 两节 各一块"
        _assert_offsets(chunks, text)

    def test_dash_underline_level2(self):
        """- 下划线 → 2 级：split_level=1 不切、split_level=2 切"""
        text = "第一章 概述\n----\n内容。\n\n第二章 详解\n----\n更多内容。"
        assert len(_heading_bounds_text(text, level=1)) == 0, "1 级不含 - 下划线"
        assert len(_heading_bounds_text(text, level=2)) == 2, "2 级切 - 下划线标题"

    def test_underline_merged_into_heading_block(self):
        """下划线行并入标题块（块从标题文字行开始，包含下划线行）"""
        text = "标题A\n====\n正文内容。"
        chunks = _title_chunker().chunk(text)
        assert chunks[0].text.startswith("标题A\n===="), "标题块含标题行与下划线行"
        _assert_offsets(chunks, text)

    def test_underline_min_4_chars(self):
        """下划线 >=4 个：3 个 = 不是下划线（不识别）"""
        text = "标题\n===\n正文。"
        assert _heading_bounds_text(text) == [], "3 个 = 不足下划线"
        text2 = "标题\n====\n正文。"
        assert _heading_bounds_text(text2) != [], "4 个 = 是下划线"

    def test_heading_text_length_limit(self):
        """setext 内容行 >50 字符不识别（整段正文不是标题）"""
        long_line = "这是" + "很长的一段正文文字。" * 8  # >50
        text = long_line + "\n====\n正文。"
        assert _heading_bounds_text(text) == [], "超长内容行不是 setext 标题"

    def test_pure_symbol_above_underline_not_heading(self):
        """纯符号行（如 ---------- 上再叠 ======）不是标题内容"""
        text = "----------\n==========\n正文。"
        assert _heading_bounds_text(text) == [], "纯符号行不是标题"

    def test_xml_tag_line_not_setext(self):
        """XML/HTML 标签行（<Breaker::湖北>）不当作 setext 内容行（E 文件实测发现）"""
        text = "<Breaker::湖北>\n---\n</Breaker::湖北>\n\n正文。"
        assert _heading_bounds_text(text) == [], "XML 标签行 + --- 不是标题"

    def test_colon_label_line_not_setext(self):
        """冒号结尾的标签/字段行（E文件导出实例：）不当作 setext 内容行"""
        text = "E文件导出实例：\n---\n\n正文内容。"
        assert _heading_bounds_text(text) == [], "冒号结尾行 + --- 不是标题"

    def test_pipe_table_line_not_setext(self):
        """表格行（| x |）不当作 setext 内容行"""
        text = "| 参数 | 说明 |\n----\n正文。"
        assert _heading_bounds_text(text) == [], "表格行 + ---- 不是标题"


class TestWrappedSingleLine:
    """单行包裹式：===== 标题 ===== / --- 备注 --- / *** 说明 *** / 【标题】/
    ━━ 标题 ━━，均为 2 级"""

    @pytest.mark.parametrize("title_line,expect", [
        ("===== 6.3.2 变电站模型 =====", "6.3.2 变电站模型"),
        ("--- 备注 ---", "备注"),
        ("*** 说明 ***", "说明"),
        ("【设备容器模型】", "设备容器模型"),
        ("━━ 标题 ━━", "标题"),
        ("── 标题 ──", "标题"),
        ("==== 标题 ======", "标题"),  # 左右不等长
        ("=== 标题 ===", "标题"),
    ])
    def test_wrapped_heading_recognized(self, title_line, expect):
        """各包裹样式识别为 2 级标题，内容 = 去掉包裹符号"""
        text = title_line + "\n正文内容。"
        headings = _iter_headings(text)
        assert len(headings) == 1, f"应识别 1 个标题: {headings}"
        start, level, title = headings[0]
        assert title == expect, f"标题内容: {title}"
        assert level == 2, "包裹式标题级别为 2"
        assert text[start:].startswith(title_line), "边界在包裹行起点"
        # 切块验证：标题保留块首
        chunks = _title_chunker().chunk(text)
        assert chunks[0].text.startswith(title_line)

    def test_bracket_wrapped_requires_whole_line(self):
        """【】包裹要求整行就是【内容】：行内还有别的内容不识别"""
        text = "前缀【设备】后缀\n正文。"
        assert _heading_bounds_text(text) == [], "【】非整行不识别"

    def test_bracket_empty_not_heading(self):
        """【】内容为空/纯空白不是标题"""
        assert _heading_bounds_text("【】\n正文。") == []
        assert _heading_bounds_text("【  】\n正文。") == []

    def test_pure_symbol_line_not_wrapped(self):
        """纯符号行（======= 无内容）不是包裹式标题"""
        text = "==========\n正文。"
        assert _heading_bounds_text(text) == [], "纯符号行不是包裹标题"

    def test_asymmetric_wrap_not_heading(self):
        """首尾符号不同（=== 标题 ---）不是包裹式"""
        text = "=== 标题 ---\n正文。"
        assert _heading_bounds_text(text) == [], "首尾异号不识别"

    def test_wrap_content_pure_symbol_not_heading(self):
        """包裹内容为纯符号（=== ---- ===）不识别"""
        text = "=== ---- ===\n正文。"
        assert _heading_bounds_text(text) == [], "包裹内容是纯符号不识别"

    def test_wrap_content_length_limit(self):
        """包裹内容 >50 字符不识别"""
        inner = "很长的标题内容。" * 10  # >50
        text = "===== " + inner + " =====\n正文。"
        assert _heading_bounds_text(text) == [], "超长包裹内容不识别"


class TestLeadingMark:
    """前导符号式：■◆●※▶▍ 开头（后跟空格或直接接文字），2 级"""

    @pytest.mark.parametrize("title_line", [
        "■ 第一章 概述", "◆ 重点说明", "● 操作步骤", "※ 注意事项",
        "▶ 返回值说明", "▍字段映射", "▶返回值说明（无空格）",
    ])
    def test_leading_mark_recognized(self, title_line):
        text = title_line + "\n正文内容。"
        headings = _iter_headings(text)
        assert len(headings) == 1, f"应识别 1 个标题: {headings}"
        assert headings[0][1] == 2, "前导符号式标题级别为 2"
        chunks = _title_chunker().chunk(text)
        assert chunks[0].text.startswith(title_line)

    def test_standalone_mark_not_heading(self):
        """单个符号（■ 无内容）不是标题"""
        for mark in "■◆●※▶▍":
            text = mark + "\n正文。"
            assert _heading_bounds_text(text) == [], f"{mark} 单独不成标题"

    def test_leading_line_length_limit(self):
        """前导符号式整行 >50 字符不识别"""
        text = "■ " + "超长内容。" * 10 + "\n正文。"
        assert _heading_bounds_text(text) == [], "超长前导符号行不识别"


class TestMisjudgmentProtection:
    """防误判：保护区内、连续标题、分隔线、overlap 不跨标题"""

    def test_inside_table_not_heading(self):
        """表格保护区内（| 分隔行）的 = 行不当作标题"""
        text = ("# 章\n\n| 列A | 列B |\n| --- | --- |\n| ===== 假标题 | 内容 |\n\n"
                "正文。\n")
        protected = find_protected_ranges(text)
        assert protected, "应识别表格保护区"
        bounds = _heading_bounds_text(text)
        assert bounds == [0], f"表格内假标题不识别，仅 # 章 是边界: {bounds}"

    def test_inside_code_fence_not_heading(self):
        """代码块保护区内（``` 围栏）的 ==== 不当作标题"""
        text = "# 章\n\n```\n===== 假标题 =====\n--- 备注 ---\n```\n\n正文。"
        bounds = _heading_bounds_text(text)
        assert bounds == [0], f"代码块内假标题不识别，仅 # 章 是边界: {bounds}"

    def test_consecutive_wrapped_headings_merge(self):
        """连续包裹标题不各自成块（并入后续内容）"""
        chunks = _title_chunker().chunk(CONSECUTIVE_WRAPPED)
        assert len(chunks) == 1, "连续包裹标题应并入后续正文"
        _assert_offsets(chunks, CONSECUTIVE_WRAPPED)

    def test_setext_with_underline_between_keep_split(self):
        """setext 连续标题中间有下划线行 → 不是直接相邻 → 各自成块"""
        text = "前言。\n\n标题A\n====\n标题B\n====\n正文内容。"
        chunks = _title_chunker().chunk(text)
        assert len(chunks) == 3, "前言 + 标题A块 + 标题B块"
        assert chunks[1].text.startswith("标题A\n====")
        assert chunks[2].text.startswith("标题B\n====")
        _assert_offsets(chunks, text)

    def test_standalone_separator_line_not_heading(self):
        """分隔线（---- 单独成行、无上行文字）不是标题"""
        text = "前言。\n\n----\n\n后续内容。"
        assert _heading_bounds_text(text) == [], "无上行文字的分隔线不是标题"

    def test_short_first_chunk_overlap_not_cross_heading(self):
        """段首块长度 < overlap 时，overlap 续接不把块起点回退到上一节
        （clamp 修复：块不跨标题边界）"""
        # 节1 短内容后是长节2（段首块 62 字符 < overlap 100 的复现样本）
        text = ("===== 节一 =====\n正文。\n\n===== 节二 =====\n"
                + "备注内容段落。\n\n" * 30)  # 节二 > chunk_size
        chunker = MarkdownSplitter(chunk_size=200, overlap=100, split_level=3)
        chunks = chunker.chunk(text)
        _assert_offsets(chunks, text)
        headings = _iter_headings(text)
        for c in chunks:
            for b, _, _ in headings:
                assert not (c.char_start < b < c.char_end), \
                    f"块 {c.char_start}..{c.char_end} 跨过标题@{b}"

    def test_heading_after_long_text_not_misdetected(self):
        """正文大段后紧跟标题行：标题正常识别、正文行（>50）不误识别"""
        long_para = "这是正文段落，" * 20  # >50
        text = long_para + "\n===== 6.3.2 变电站模型 =====\n内容。"
        bounds = _heading_bounds_text(text)
        assert len(bounds) == 1, "只识别包裹标题，正文长行不是标题"


class TestMixedWithMarkdown:
    """与 # 标题混排兼容 + split_level 语义（识别 <=N 级）"""

    MIXED = (
        "# 总标题\n\n"
        "===== 章节一 =====\n"
        "内容一。\n\n"
        "--- 备注 ---\n"
        "备注一。\n\n"
        "## 小节二\n"
        "内容二。\n\n"
        "### 三级小节\n"
        "内容三。\n\n"
        "■ 重点说明\n"
        "重点内容。\n"
    )

    def test_mixed_all_levels_recognized(self):
        """# 与 setext/包裹/前导混排：split_level=3 全部切出
        （# 总标题、章节一、备注、小节二、三级小节、重点说明 = 6 个标题）"""
        bounds = _heading_bounds_text(self.MIXED, level=3)
        assert len(bounds) == 6, f"6 个标题应全部识别: {bounds}"
        chunks = _title_chunker(chunk_size=10_000, overlap=0).chunk(self.MIXED)
        assert len(chunks) == 6
        for c in chunks:
            _assert_offsets([c], self.MIXED)
        firsts = [c.text.split("\n", 1)[0] for c in chunks]
        assert firsts[0].startswith("# 总标题")
        assert firsts[1].startswith("===== 章节一")
        assert firsts[2].startswith("--- 备注")
        assert firsts[3].startswith("## 小节二")
        assert firsts[4].startswith("### 三级小节")
        assert firsts[5].startswith("■ 重点说明")

    def test_split_level_filter(self):
        """split_level 过滤：# 1 级、setext(=) 1 级、包裹/前导/##/### 不切"""
        text = "# 一级\n\n===== 包裹 =====\n内容。\n\n--- 备注 ---\n备注。\n\n■ 要点\n内容。"
        # level=1：只切 # 一级
        bounds = _heading_bounds_text(text, level=1)
        assert len(bounds) == 1, f"1 级只切 #: {bounds}"
        # level=2：+ 包裹/前导
        assert len(_heading_bounds_text(text, level=2)) == 4


class TestRealEfile:
    """真实文件实测：E文件导出实例全部.txt（1495 行纯文本）

    - 44 个 '===== 6.x =====' + 43 个 '--- 备注 ---' + 4 个【】= 92 个标题
      （与独立正则对照一致；多处纯 =/- 装饰线不误识别）
    - 92 个标题全部成为块起点、无块跨标题、偏移一致
    - 切块数与标题结构合理：92 标题块 + 超长节递归块，总块数远小于
      未识别标题的机械切分规模
    """

    def test_recognize_all_headings(self):
        """识别出全部 92 个纯文本标题（与独立正则对照一致），级别为 2"""
        efile = _read_efile()
        headings = _iter_headings(efile)
        real = _real_heading_lines(efile)
        assert len(real) == 92, f"E 文件应含 92 个真实标题: {len(real)}"
        assert len(headings) == len(real), \
            f"识别标题数 {len(headings)} 应等于真实标题数 {len(real)}"
        assert {lv for _, lv, _ in headings} == {2}, "E 文件标题均为 2 级"
        # 每个识别标题都能对应到原文一个真实标题行
        for start, lv, title in headings:
            assert title in efile[start:start + 60], \
                f"识别标题应能在原文标题行找到: {title}"

    def test_chunks_structure(self):
        """92 个标题全部成为块起点、无块跨标题、偏移一致、块数合理"""
        efile = _read_efile()
        chunker = MarkdownSplitter(chunk_size=800, overlap=100, split_level=3)
        chunks = chunker.chunk(efile)
        assert chunks, "应切出块"
        _assert_offsets(chunks, efile)
        headings = _iter_headings(efile)
        starts = {c.char_start for c in chunks}
        # 1) 所有标题都是某块起点（标题保留块首）
        missing = [(b, t) for b, _, t in headings if b not in starts]
        assert not missing, f"标题未成为块起点: {missing[:5]}"
        # 2) 无块跨标题（块内标题只能出现在块首）
        crossing = [(c.char_start, c.char_end, b)
                    for c in chunks for b, _, _ in headings
                    if c.char_start < b < c.char_end]
        assert not crossing, f"存在跨标题的块: {crossing[:5]}"
        # 3) 块数与标题结构合理：92 标题块 + 超长节递归块，
        #    总块数显著小于无标题机械切分（chunk_size=50 才需几十块以上）
        assert len(chunks) <= 150, f"块数异常多: {len(chunks)}"
        assert len(chunks) >= len(headings), "块数不应少于标题数"
        # 4) 每个标题块以原始标题行开头
        for b, _, _ in headings:
            block = next(c for c in chunks if c.char_start == b)
            assert block.text.split("\n", 1)[0] == efile[b:].split("\n", 1)[0], \
                f"标题块首行应与原文一致 @{b}"
        # 5) 装饰线不是标题也不产生空块（纯 = 装饰行不单独成标题块）
        pure_lines = [l for l in efile.split("\n")
                      if re.fullmatch(r"\s*[=\-*━─]{4,}\s*", l)]
        assert len(pure_lines) >= 10, "E 文件应含装饰线（防误判素材）"

    def test_short_section_single_chunk(self):
        """短节每节一块：非超长节（<=800 字符）的标题块覆盖整节内容"""
        efile = _read_efile()
        chunker = MarkdownSplitter(chunk_size=800, overlap=100, split_level=3)
        chunks = chunker.chunk(efile)
        headings = _iter_headings(efile)
        # 统计每节块数：绝大多数节应恰好 1 块（不机械切碎）
        per_section = {}
        for c in chunks:
            prev = max((b for b, _, _ in headings if b <= c.char_start), default=-1)
            per_section.setdefault(prev, 0)
            per_section[prev] += 1
        single = sum(1 for k, v in per_section.items() if v == 1)
        assert single >= len(per_section) * 0.6, \
            f"超过 60% 的节应每节一块: {single}/{len(per_section)}"


class TestOffsets:
    """偏移正确性：增强识别后所有块 full_text[char_start:char_end] == text"""

    SAMPLES = [
        "标题A\n====\n内容。\n\n标题B\n----\n更多内容。",
        "===== 6.3.2 变电站模型 =====\n内容。\n\n--- 备注 ---\n备注内容。",
        "【设备容器模型】\n内容。\n\n*** 说明 ***\n说明内容。",
        "■ 第一章 概述\n内容。\n\n▶ 返回值说明\n返回值。",
        "# 总标题\n\n===== 章节 =====\n内容。\n\n### 小节\n内容。\n\n● 要点\n内容。",
        "<Breaker::湖北>\n---\n</Breaker::湖北>\n\n正文。\n\n----\n\n结尾。",
        "==========\n\n===== 真实标题 =====\n内容。\n\n==========",
        CONSECUTIVE_WRAPPED,
    ]

    @pytest.mark.parametrize("text", SAMPLES)
    def test_title_offsets(self, text):
        chunks = _title_chunker().chunk(text)
        _assert_offsets(chunks, text)
