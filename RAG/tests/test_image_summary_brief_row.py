"""入库图片摘要的「简介行」契约

背景:图片摘要在切块时回填进 chunk(`> 图片说明:…`),它同时承担两个检索职责——
**一句话简介给向量检索当语义锚点**(短文本聚焦),**类型/文字/画面给关键词匹配
补实体词**。简介行必须:

- 恒在提示词首位,**不随内容选项开关**(一个选项都不勾时也要有)
- 经 `_normalize` 白名单后仍在(字段名必须进 `_FIELD_ORDER`,否则被丢弃)
- 回填时**接到 `> 图片说明:` 后面**,而不是另起一行
- 超长截断(提示词写 30 字,但模型常不守)

老格式(部门自定义提示词、历史产物)没有简介行——回填行为必须保持原样。
"""
from backend.config import ImageSummaryConfig
from backend.services.image_summary import (_normalize, _render_block,
                                            default_prompt)

FMT = "fields"
OPTS = {"label_type": True, "read_text": True, "describe_scene": True,
        "describe_layout": False}


class TestBriefRowInPrompt:
    def test_first_and_before_type(self):
        """简介行排在类型之前(它是整段的语义锚点)"""
        lines = [l for l in default_prompt(OPTS, FMT).split("\n") if l.strip()]
        idx_brief = next(i for i, l in enumerate(lines)
                         if l.startswith("简介："))
        idx_type = next(i for i, l in enumerate(lines) if l.startswith("类型："))
        assert idx_brief < idx_type

    def test_survives_empty_options(self):
        """一个内容选项都不勾时简介仍在——它不受选项控制"""
        assert "简介：" in default_prompt({}, FMT)

    def test_prose_asks_for_leading_summary(self):
        """prose 档首句也要先概括(自然段没有字段行,靠首句承当锚点)"""
        p = default_prompt(OPTS, "prose")
        assert "首句" in p and "概括" in p


class TestBriefRowNormalize:
    def test_kept_and_first(self):
        out = _normalize("简介：这是一份验收证明。\n类型：证明文件\n"
                         "文字：防误闭锁系统\n画面：铭牌照片",
                         ImageSummaryConfig(output_format=FMT))
        assert out.split("\n")[0] == "简介：这是一份验收证明。"

    def test_truncated(self):
        """简介超长截断到 50 字(一句话的语义上限,与 brief 模式同口径)"""
        out = _normalize("简介：" + "很" * 200 + "\n类型：x",
                         ImageSummaryConfig(output_format=FMT))
        first = out.split("\n")[0]
        assert first.endswith("…") and len(first) <= 60

    def test_missing_when_model_omits_it(self):
        """模型没吐简介行时不补——与"未启用的字段不补"的既有契约一致"""
        out = _normalize("类型：证明文件\n文字：防误闭锁系统",
                         ImageSummaryConfig(output_format=FMT))
        assert "简介" not in out


class TestRenderBlock:
    def test_brief_row_joins_prefix(self):
        """★ 首行简介接到 `> 图片说明：` 之后"""
        block = _render_block("简介：这是一份验收证明。\n类型：证明文件", FMT)
        assert block.split("\n")[0] == "> 图片说明：这是一份验收证明。"
        assert "> 类型：证明文件" in block

    def test_legacy_without_brief_row(self):
        """老格式(无简介行)行为不变:标记独占一行"""
        block = _render_block("类型：证明文件\n文字：防误闭锁系统", FMT)
        assert block.split("\n")[0] == "> 图片说明："
        assert "> 类型：证明文件" in block

    def test_prose_and_brief_unchanged(self):
        assert _render_block("一段描述", "prose") == "> 图片说明：一段描述"
        assert _render_block("一句话", "brief") == "> 图片说明：一句话"

    def test_empty_text(self):
        assert _render_block("", FMT) == "> 图片说明："

    def test_full_pipeline(self):
        """端到端:模型原始输出 → 回填块"""
        cfg = ImageSummaryConfig(output_format=FMT, text_max_chars=200)
        raw = ("简介：这是一份关于防误闭锁系统设备运行情况的证明文件。\n"
               "类型：证明文件\n"
               "文字：防误闭锁系统、2024年6月18日\n"
               "画面：设备铭牌照片、红色公章两枚\n")
        assert _render_block(_normalize(raw, cfg), FMT) == (
            "> 图片说明：这是一份关于防误闭锁系统设备运行情况的证明文件。\n"
            "> 类型：证明文件\n"
            "> 文字：防误闭锁系统、2024年6月18日\n"
            "> 画面：设备铭牌照片、红色公章两枚")
