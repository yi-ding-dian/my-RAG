"""HTML 表格 → markdown 管道表格转换测试（table_normalizer）

覆盖：基础表格 / rowspan 重复填充 / colspan 展开 / 单元格 | 转义 /
<p>、<br> 折叠 / 图片保留 / 未闭合表格 / 空表丢弃 / 多表混排 /
非表格文本（XML 标签行）不动 / 列宽对齐 / 表头定位 / HTML 实体还原 /
纯 td 首行表头 / 单行表格 / 空行规范化。
"""
from __future__ import annotations

from backend.services.table_normalizer import html_tables_to_pipe


def test_basic_html_table_to_pipe():
    html = ("<table><tr><th>参数</th><th>说明</th></tr>"
            "<tr><td>code</td><td>状态码</td></tr></table>")
    out = html_tables_to_pipe(html)
    assert "| 参数 | 说明 |" in out
    assert "| --- | --- |" in out
    assert "| code | 状态码 |" in out
    # 原始 HTML 标签已消失
    assert "<table" not in out and "<tr>" not in out and "<td>" not in out
    # 表头/分隔/数据行顺序
    assert out.index("| 参数 | 说明 |") < out.index("| --- | --- |") < out.index("| code | 状态码 |")


def test_rowspan_repeated():
    """用户真实样例：rowspan=6 的合并列在后续行重复填充"""
    html = (
        '<table><tr><td><p>产业链环节</p></td><td><p>细分赛道</p></td>'
        '<td><p>2026年市场规模</p></td><td><p>同比增速</p></td></tr>'
        '<tr><td rowspan="6"><p>上游算力与基础设施层</p></td>'
        '<td><p>AI服务器</p></td><td><p>全球650亿美元、中国210亿美元</p></td>'
        '<td><p>全球71%、中国75%</p></td></tr>'
        '<tr><td><p>AI芯片</p></td><td><p>全球400亿美元</p></td>'
        '<td><p>45%</p></td></tr></table>'
    )
    out = html_tables_to_pipe(html)
    # rowspan 列在数据行重复
    assert out.count("| 上游算力与基础设施层 |") == 2
    assert "| 上游算力与基础设施层 | AI服务器 | 全球650亿美元、中国210亿美元 | 全球71%、中国75% |" in out
    assert "| 上游算力与基础设施层 | AI芯片 | 全球400亿美元 | 45% |" in out


def test_colspan_expanded():
    html = ('<table><tr><th>项目</th><th colspan="2">金额(亿元)</th><th>备注</th></tr>'
            '<tr><td>A</td><td>100</td><td>200</td><td>x</td></tr></table>')
    out = html_tables_to_pipe(html)
    assert "| 项目 | 金额(亿元) | 金额(亿元) | 备注 |" in out
    assert "| A | 100 | 200 | x |" in out


def test_cell_pipe_escaped():
    html = ("<table><tr><th>名称</th><th>数值</th></tr>"
            "<tr><td>A|B</td><td>1</td></tr></table>")
    out = html_tables_to_pipe(html)
    assert "| A\\|B | 1 |" in out


def test_cell_p_and_br_folded():
    """单元格内多段 <p>/<br> 折叠为空格（单元格单行化）"""
    html = ("<table><tr><th>x</th></tr>"
            "<tr><td><p>第一段</p><p>第二段</p></td></tr></table>")
    out = html_tables_to_pipe(html)
    assert "| 第一段 第二段 |" in out


def test_cell_img_kept():
    html = ('<table><tr><th>图</th></tr><tr><td>'
            '<img src="/api/files/images/d1/n.png" alt="示意图" />'
            '</td></tr></table>')
    out = html_tables_to_pipe(html)
    assert "![示意图](/api/files/images/d1/n.png)" in out


def test_unclosed_table_converted_to_end():
    html = ("开头\n<table><tr><td>x</td><td>y</td></tr>"
            "<tr><td>1</td><td>2</td>")
    out = html_tables_to_pipe(html)
    assert "| x | y |" in out
    assert "| 1 | 2 |" in out
    assert out.startswith("开头")


def test_empty_table_dropped():
    html = "前文\n<table><tr></tr></table>\n后文"
    out = html_tables_to_pipe(html)
    assert "前文" in out and "后文" in out
    assert "<table" not in out
    # 空表不出产物行
    assert "|  |" not in out


def test_multiple_tables_with_context():
    html = ("# 标题\n\n<table><tr><th>a</th><th>b</th></tr>"
            "<tr><td>1</td><td>2</td></tr></table>\n\n正文段落。\n\n"
            "<table><tr><th>c</th></tr><tr><td>3</td></tr></table>")
    out = html_tables_to_pipe(html)
    assert "| a | b |" in out and "| c |" in out
    assert "正文段落。" in out
    assert out.index("| a | b |") < out.index("正文段落。") < out.index("| c |")


def test_non_table_text_untouched():
    """XML 标签行（<Breaker::湖北> 等）与普通正文不被误改"""
    html = "<Breaker::湖北>标记\n<table><tr><th>a</th><th>b</th></tr>" \
           "<tr><td>1</td><td>2</td></tr></table>\n结尾"
    out = html_tables_to_pipe(html)
    assert "<Breaker::湖北>标记" in out
    assert "结尾" in out
    assert out.index("<Breaker::湖北>") < out.index("| a | b |") < out.index("结尾")


def test_no_html_table_returns_original():
    text = "| 普通竖线文本 | 不是表格\n无表格内容"
    assert html_tables_to_pipe(text) == text
    assert html_tables_to_pipe("") == ""
    assert html_tables_to_pipe(None) == "" or html_tables_to_pipe("None") == "None"


def test_ragged_rows_padded():
    """列数不齐：按最宽行补空单元格（HTML 语义空列）"""
    html = ("<table><tr><th>a</th><th>b</th></tr>"
            "<tr><td>1</td></tr></table>")
    out = html_tables_to_pipe(html)
    assert "| 1 |  |" in out


def test_th_in_second_row_is_header():
    html = ("<table><tr><td>表外占位</td><td>x</td></tr>"
            "<tr><th>h1</th><th>h2</th></tr>"
            "<tr><td>v1</td><td>v2</td></tr></table>")
    out = html_tables_to_pipe(html)
    # 第一个含 th 的行作为表头（强制排首行），其余行按原顺序为数据行
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    assert lines[0] == "| h1 | h2 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2].startswith("| 表外占位")
    assert lines[3].startswith("| v1 | v2 |")


def test_entity_unescaped():
    html = ("<table><tr><th>a</th></tr>"
            "<tr><td>A &amp; B &lt; C</td></tr></table>")
    out = html_tables_to_pipe(html)
    assert "| A & B < C |" in out


def test_pure_td_first_row_as_header():
    html = ("<table><tr><td>名称</td><td>数值</td></tr>"
            "<tr><td>A</td><td>1</td></tr></table>")
    out = html_tables_to_pipe(html)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    assert lines[0] == "| 名称 | 数值 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| A | 1 |"


def test_single_line_table():
    html = "前文\n<table><tr><th>x</th><th>y</th></tr><tr><td>1</td><td>2</td></tr></table>\n后文"
    out = html_tables_to_pipe(html)
    assert "| x | y |" in out
    assert "| 1 | 2 |" in out
    assert "前文" in out and "后文" in out


def test_blank_lines_normalized():
    """表格转换补的空行与原文换行叠加时塌缩（不产生 4+ 连续空行）"""
    html = "前文\n\n\n<table><tr><th>a</th></tr><tr><td>1</td></tr></table>\n\n\n后文"
    out = html_tables_to_pipe(html)
    assert "\n\n\n\n" not in out
    assert "前文" in out and "后文" in out


def test_table_with_caption_ignored():
    """<caption> 文本不进入表格单元格（转换后不含 caption 内容混淆）"""
    html = ("<table><caption>表 1：示例</caption><tr><th>a</th></tr>"
            "<tr><td>1</td></tr></table>")
    out = html_tables_to_pipe(html)
    assert "| a |" in out
    assert "| 1 |" in out
