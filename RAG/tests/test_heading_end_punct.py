"""标题末尾标点白名单测试（_iter_headings 统一出口判定）

背景：解析器（MinerU/DeepDoc）会把正文句、表格单元格文本、列表项标成 `##`。
这类伪标题进了标题树会把真实章节链整段弹空——实测某检测方案里表格单元格
"电气设备如有异常则在图元标注黄色背景。"被标成 `##`，其 L2 级别把栈里的
"4.3 / 4.3.4 / 4.3.4.1" 全部弹出，后续章节只能挂到它名下。

规则（GB/T 15834—2011《标点符号用法》："文章标题的末尾通常不用标点符号，
但有时根据需要**可用问号、叹号或省略号**"）：
- 标题末尾若是**句末标点**（。？！；… 及半角 .?!;）→ 须在白名单内才认；
- **非句末标点**（冒号/顿号/逗号/右括号…）不受管辖——"注："、"1）xx"、
  "一、总则" 实测都是真标题，纳入管辖会误杀；
- 空白名单**回退默认值**（而非"什么都不认"，后者会让所有问句标题消失）。
"""
from __future__ import annotations

import pytest

from backend.chunking.common import (_DEFAULT_END_PUNCT_WHITELIST,
                                     _END_PUNCT_CHARS, _iter_headings,
                                     passes_end_punct)

DEFAULT = list(_DEFAULT_END_PUNCT_WHITELIST)


def _headings(body: str, whitelist=DEFAULT, systems=None):
    """把单行标题裹进上下文后取识别结果"""
    return _iter_headings(f"前一行\n\n{body}\n\n后一行", None, systems, whitelist)


class TestDefaultWhitelist:
    """默认白名单 = 国标列举的问号/叹号/省略号"""

    @pytest.mark.parametrize("body", [
        "## 什么是 RAG？", "## 注意！", "## 省略号结尾……",
        "## 半角问号?", "## 半角叹号!", "## 生效！！", "## 真的吗？？",
    ])
    def test_allowed_suffix(self, body):
        assert _headings(body), f"应认作标题: {body}"

    @pytest.mark.parametrize("body", [
        "## 这是正文句。", "## 甲；乙；", "## 半角句号.", "## 半角分号;",
    ])
    def test_blocked_suffix(self, body):
        assert not _headings(body), f"不该认作标题: {body}"


class TestNotGoverned:
    """非句末标点不受白名单管辖（这些是真标题的常见形态）"""

    @pytest.mark.parametrize("body", [
        "## 注意事项：",          # 标签式标题
        "## 第二步：",
        "## 1）母线保护装置光字牌",  # 列表项写法，但作为标题形态真实存在
        "## 一、总则",
        "## 【设备容器模型】",
        "## 以普通汉字结尾",
    ])
    def test_not_governed_in_default(self, body):
        assert _headings(body), f"非句末标点不该被管辖: {body}"

    def test_not_governed_even_with_empty_whitelist(self):
        """即使白名单收窄到只剩逗号，冒号结尾照样认（不受管辖）"""
        assert _headings("## 注意事项：", whitelist=["、"])


class TestCustomWhitelist:
    """白名单可配置"""

    def test_add_semicolon(self):
        """把分号加进白名单 → 分号结尾认"""
        assert not _headings("## 甲；乙；")
        assert _headings("## 甲；乙；", whitelist=["；"])

    def test_remove_question_mark(self):
        """把问号移出白名单 → 问号结尾不认"""
        assert _headings("## 什么是 RAG？")
        assert not _headings("## 什么是 RAG？", whitelist=["！"])

    def test_empty_falls_back_to_default(self):
        """空白名单回退默认：句号不认、问号认（而非"什么都不认"）"""
        assert not _headings("## 句。", whitelist=[])
        assert _headings("## 问？", whitelist=[])

    def test_none_reads_config_then_defaults(self):
        """whitelist=None → 读配置（测试环境无配置 → 回退默认）"""
        assert not _headings("## 句。", whitelist=None)
        assert _headings("## 问？", whitelist=None)


class TestAllPathsGoverned:
    """四条识别路径统一过闸（此前前导符号式/包裹式/setext 完全不查标点）"""

    @pytest.mark.parametrize("body", [
        "■ 这是正文句。",           # 前导符号式
        "===== 标题。 =====",      # 包裹式
        "这是内容行。\n========",   # setext
        "【正文句。】",             # 方括号包裹式
    ])
    def test_period_blocked_on_all_paths(self, body):
        assert not _headings(body), f"该路径也应挡句号: {body}"

    @pytest.mark.parametrize("body", [
        "■ 什么是 RAG？",
        "===== 什么是 RAG？ =====",
        "什么是RAG？\n========",
    ])
    def test_question_allowed_on_all_paths(self, body):
        assert _headings(body), f"该路径应放行问号: {body}"


class TestBoldStaysStrict:
    """整行加粗式保持从严（猜测性识别，连问号都不认）——白名单不放宽它"""

    def test_bold_question_still_blocked(self):
        assert not _headings("**什么是 RAG？**")

    def test_bold_plain_still_recognized(self):
        assert _headings("**普通加粗标题**")


class TestPassesEndPunct:
    """判定函数直测"""

    @pytest.mark.parametrize("text,expected", [
        ("注：", True), ("1）xx", True), ("一、总则", True), ("结尾是字", True),
        ("问题？", True), ("警告！", True), ("省略……", True),
        ("句子。", False), ("并列；", False), ("半角.", False), ("半角;", False),
    ])
    def test_table(self, text, expected):
        assert passes_end_punct(text, DEFAULT) is expected

    def test_empty_text(self):
        assert passes_end_punct("", DEFAULT) is True
        assert passes_end_punct("   ", DEFAULT) is True

    def test_end_punct_chars_scope(self):
        """管辖范围只含句末标点，不含冒号/顿号/逗号"""
        for ch in "。？！；…?!;.":
            assert ch in _END_PUNCT_CHARS, f"{ch} 应在管辖范围内"
        for ch in "：、，,）】【":
            assert ch not in _END_PUNCT_CHARS, f"{ch} 不该被管辖"


class TestRegressionRealDoc:
    """回归：某检测方案里把章节链弹空的伪标题"""

    def test_cell_text_sentence_blocked(self):
        """表格单元格正文句被标成 ## → 不认"""
        assert not _headings("## 异常则在图元标注黄色背景。")

    def test_section_headings_kept(self):
        """真章节标题照常认"""
        for body in ["## 4.3.4.1 间隔分图区域界面检测", "## 4.3.4.7 遥控操作界面检测"]:
            assert _headings(body), f"真章节标题被误杀: {body}"

    def test_list_items_kept(self):
        """列表项形态的标题（1）/2））照常认——它们的问题在层级不在有无"""
        assert _headings("## 1）母线相关间隔测控光字牌")
        assert _headings("## 2）母线保护装置光字牌")
