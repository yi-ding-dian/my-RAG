"""解析文本清洗测试（text_cleanup.strip_subsup_tags）

背景：MinerU 把 PDF 里字体/基线不齐的文字误判为上下标输出 <sub>/<sup> 标签
（典型误判：标题编号判成下标、标题文字判成上标、中文引号判成下标），
解析产物落盘前统一去标签留文字。
"""
from backend.services.text_cleanup import strip_subsup_tags


class TestStripSubsupTags:
    def test_heading_number_as_sub(self):
        """标题编号被判成下标：'## <sub>3.1</sub> 工程创建' → '## 3.1 工程创建'"""
        assert strip_subsup_tags("## <sub>3.1</sub> 工程创建") == "## 3.1 工程创建"

    def test_heading_text_as_sup(self):
        """整段标题被判成上标"""
        assert strip_subsup_tags("## <sup>新建工程</sup>") == "## 新建工程"

    def test_quote_as_sub(self):
        """中文引号被判成下标（正文内嵌，两侧文字保留拼接）"""
        assert strip_subsup_tags("实时命令<sub>”</sub>界面") == "实时命令”界面"

    def test_real_subscript_text_kept(self):
        """真上下标：去标签留文字（内容完整，RAG 可检索）"""
        assert strip_subsup_tags("H<sub>2</sub>O") == "H2O"
        assert strip_subsup_tags("10<sup>-3</sup>") == "10-3"

    def test_tag_with_attributes(self):
        """带属性的标签形式同样剥离"""
        assert strip_subsup_tags('<sub style="x">a</sub>') == "a"

    def test_case_insensitive(self):
        """大小写不敏感"""
        assert strip_subsup_tags("<SUB>A</SUB>") == "A"

    def test_plain_text_unchanged(self):
        """无标签文本原样返回（零开销路径）"""
        assert strip_subsup_tags("普通文本无标签") == "普通文本无标签"
        assert strip_subsup_tags("") == ""

    def test_other_html_tags_kept(self):
        """其它 HTML 标签不动（只清洗上下标）"""
        assert strip_subsup_tags("<table><tr><td>x</td></tr></table>") == \
            "<table><tr><td>x</td></tr></table>"
