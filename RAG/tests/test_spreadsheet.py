"""Excel/CSV 表格读取链路测试（spreadsheet 读取器 + parser_client 集成）

公共基础：fill_merged 合并填充 / pad_rows 等宽 / render_sheet_pipe 管道
渲染（| 转义与换行折叠复用 table_normalizer.pipe_escape）/ 多 sheet 拼接
读取器：xlsx（多 sheet/合并单元格/日期/公式无缓存/空表跳过）、
xls（xlwt 生成样本）、csv（utf-8/gbk 编码探测/引号/分号/空行）
解析链路：parser_client.parse（xlsx/csv/xls → parse_method=spreadsheet）、
空表报错语义、非法扩展名 ValueError、is_spreadsheet_ext 判定。
"""
from __future__ import annotations

import asyncio
import datetime

import pytest

from backend.services import parser_client
from backend.services.spreadsheet_reader import (Sheet, fill_merged,
                                                 is_spreadsheet_ext, pad_rows,
                                                 read_spreadsheet,
                                                 render_sheet_pipe,
                                                 render_sheets_text)


# ------------------- 公共基础 -------------------


def test_fill_merged_repeats_top_left():
    rows = [["A", "x"], ["", "y"], ["", ""]]
    fill_merged(rows, [(0, 0, 2, 0)])
    assert rows == [["A", "x"], ["A", "y"], ["A", ""]]
    # 横向合并
    rows2 = [["h1", "", "h3"]]
    fill_merged(rows2, [(0, 0, 0, 1)])
    assert rows2 == [["h1", "h1", "h3"]]


def test_fill_merged_malformed_range_skipped():
    rows = [["A", "x"]]
    fill_merged(rows, [(-1, 0, 0, 0)])  # 越界坐标不崩溃
    assert rows == [["A", "x"]]


def test_pad_rows_equal_width():
    out = pad_rows([["a", "b"], ["c"]])
    assert out == [["a", "b"], ["c", ""]]
    assert pad_rows([]) == []


def test_render_sheet_pipe_basic_and_escape():
    sheet = Sheet(name="s1", rows=[["名称", "说明"], ["A", "x|y"], ["B", "多\n行"]])
    text = render_sheet_pipe(sheet)
    assert text.startswith("## Sheet: s1")
    assert "| --- | --- |" in text
    assert "| x\\|y |" in text          # | 转义
    assert "| 多 行 |" in text          # \n 折叠为空格
    assert text.splitlines()[2] == "| 名称 | 说明 |"  # 0 标题 /1 空行 /2 表头


def test_large_sheet_split_into_blocks_with_repeated_header():
    """超长表分段（行块+重复表头）：每段表头/分隔行常驻、区间连续无行丢失"""
    import re
    rows = [["列1", "列2"]] + [[f"值{i}", "x" * 150] for i in range(80)]
    text = render_sheet_pipe(Sheet(name="s", rows=rows))
    spans = re.findall(r"## Sheet: s \(第 (\d+)-(\d+) 行\)", text)
    assert len(spans) > 1, "80 行长文本应分段"
    # 区间连续覆盖全部数据行
    assert int(spans[0][0]) == 1 and int(spans[-1][1]) == 80
    for i in range(len(spans) - 1):
        assert int(spans[i][1]) + 1 == int(spans[i + 1][0])
    # 每段都重复表头 + 分隔行
    assert text.count("| 列1 | 列2 |") == len(spans)
    assert text.count("| --- | --- |") == len(spans)
    # 每段数据行数 = 区间宽度
    lines = text.splitlines()
    for a, b in spans:
        seg_lines = [l for l in lines if re.match(r"^\| 值", l)]
        assert len(seg_lines) == 80, "数据行数守恒"
    # 段内完整性抽查：一段的行都在相邻两个标题之间
    titles_idx = [i for i, l in enumerate(lines) if l.startswith("## Sheet: s")]
    first_seg = lines[titles_idx[0]:titles_idx[1]]
    assert first_seg[0].startswith("## Sheet: s (第 1-")
    assert "| --- | --- |" in first_seg and "| 列1 | 列2 |" in first_seg
    # 段数据行数与区间一致
    data_lines = [l for l in first_seg if l.startswith("| 值")]
    a = int(re.search(r"\(第 (\d+)-", first_seg[0]).group(1))
    b = int(re.search(r"-(\d+) 行", first_seg[0]).group(1))
    assert len(data_lines) == b - a + 1


def test_small_sheet_kept_whole():
    """小表不分段（保持单块，标题无区间）"""
    rows = [["a", "b"], ["1", "2"], ["3", "4"]]
    text = render_sheet_pipe(Sheet(name="s", rows=rows))
    assert "## Sheet: s\n" in text
    assert "第 1-" not in text


def test_render_sheets_text_skips_empty():
    sheets = [Sheet(name="空", rows=[]), Sheet(name="s", rows=[["a", "b"], ["1", "2"]])]
    text = render_sheets_text(sheets)
    assert "### 空" not in text and "Sheet: 空" not in text
    assert "## Sheet: s" in text
    assert len(text.splitlines()) == 5  # 标题/空行/表头/分隔/数据
    assert render_sheets_text([]) == ""


def test_read_spreadsheet_unknown_ext():
    from pathlib import Path
    with pytest.raises(ValueError, match="不支持的表格文件类型"):
        read_spreadsheet(Path("x.ppt"))


def test_is_spreadsheet_ext():
    assert is_spreadsheet_ext("xlsx") and is_spreadsheet_ext(".XLS") and is_spreadsheet_ext("csv")
    assert not is_spreadsheet_ext("pdf") and not is_spreadsheet_ext("")


# ------------------- xlsx -------------------


def _make_xlsx(tmp_path, name="demo.xlsx"):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "表一"
    ws.append(["细分赛道", "规模", "增速", "备注"])
    ws.append(["AI服务器", "650亿", "71%", "训练"])
    ws.append(["AI芯片", "320亿", "68.2%", None])
    ws.merge_cells("A2:A3")           # 纵向合并
    ws2 = wb.create_sheet("空表")
    ws2.append([None, None])
    ws3 = wb.create_sheet("日期")
    ws3.append(["日期", "事件"])
    ws3.append([datetime.datetime(2026, 8, 22, 12, 30), "发布"])
    ws4 = wb.create_sheet("公式")
    ws4.append(["公式值", "说明"])
    ws4.append(["=SUM(1,2)", "无缓存示例"])  # openpyxl 不计算：data_only 读到 None → ""
    p = tmp_path / name
    wb.save(p)
    return p


def test_read_xlsx_multi_sheet_merge_date(tmp_path):
    sheets = read_spreadsheet(_make_xlsx(tmp_path))
    names = [s.name for s in sheets]
    assert names == ["表一", "日期", "公式"]  # 空表跳过
    assert len(sheets[0].rows) == 3
    # 合并单元格重复填充
    assert sheets[0].rows[1][0] == "AI服务器" and sheets[0].rows[2][0] == "AI服务器"
    # 日期 ISO
    assert "2026-08-22 12:30:00" in sheets[1].rows[1][0]
    # 公式无缓存 → ""
    assert sheets[2].rows[1][0] == ""


# ------------------- csv -------------------


def test_read_csv_gbk_and_quotes(tmp_path):
    p = tmp_path / "demo.csv"
    p.write_bytes("名称,数量,备注\n\"发动机,1型\",42,好\n变速箱,7,\n".encode("gbk"))
    sheets = read_spreadsheet(p)
    assert sheets[0].name == "demo"
    assert sheets[0].rows[1][0] == "发动机,1型"   # 引号内逗号保留
    assert sheets[0].rows[2][2] == ""              # 尾空单元格补空


def test_read_csv_utf8_bom_and_semicolon(tmp_path):
    p = tmp_path / "demo2.csv"
    p.write_bytes("名称;值\nA;1\nB;2\n".encode("utf-8-sig"))
    sheets = read_spreadsheet(p)
    assert sheets[0].rows[0] == ["名称", "值"]
    assert sheets[0].rows[1] == ["A", "1"]


def test_read_csv_blank_lines_skipped(tmp_path):
    p = tmp_path / "demo3.csv"
    p.write_text("a,b\n\n1,2\n\n\n3,4\n", encoding="utf-8")
    sheets = read_spreadsheet(p)
    assert len(sheets[0].rows) == 3


def test_read_csv_empty_is_no_sheets(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("  \n\n", encoding="utf-8")
    assert read_spreadsheet(p) == []


# ------------------- xls（xlwt 生成样本） -------------------


def test_read_xls_basic(tmp_path):
    import xlwt
    wb = xlwt.Workbook()
    sh = wb.add_sheet("Sheet1")
    sh.write(0, 0, "项目")
    sh.write(0, 1, "数值")
    sh.write(1, 0, "A")
    sh.write(1, 1, 123)
    sh.write(2, 0, "B")
    sh.write(2, 1, 45.5)
    p = tmp_path / "demo.xls"
    wb.save(str(p))
    sheets = read_spreadsheet(p)
    assert sheets[0].name == "Sheet1"
    assert sheets[0].rows[1] == ["A", "123"]    # 整数浮点去小数点
    assert sheets[0].rows[2] == ["B", "45.5"]


# ------------------- parser_client 集成（asyncio.run 同步包装，项目惯例） -------------------


def test_parser_parse_xlsx(tmp_path):
    p = _make_xlsx(tmp_path)
    text, images, method = asyncio.run(
        parser_client.get_parser_client().parse(p, "xlsx", engine="auto"))
    assert method == "spreadsheet"
    assert images == []
    assert "## Sheet: 表一" in text
    assert "| 细分赛道 | 规模 | 增速 | 备注 |" in text
    assert "| --- | --- | --- | --- |" in text


def test_parser_parse_csv(tmp_path):
    p = tmp_path / "t.csv"
    p.write_bytes("类别,数量\n发动机,42\n".encode("gbk"))
    text, images, method = asyncio.run(
        parser_client.get_parser_client().parse(p, "csv", engine="auto"))
    assert method == "spreadsheet"
    assert "| 发动机 | 42 |" in text


def test_parser_parse_empty_sheet_raises(tmp_path):
    import openpyxl
    p = tmp_path / "empty.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append([None, None])
    wb.save(p)
    with pytest.raises(RuntimeError, match="所有工作表均无有效数据行"):
        asyncio.run(parser_client.get_parser_client().parse(
            p, "xlsx", engine="auto"))
