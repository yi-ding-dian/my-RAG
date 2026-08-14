"""QA 问答切块测试：QaChunker 单元 + 规范性检测纯函数 + 入库链路 API 集成

单元覆盖：中文格式（问：/答：）、英文格式（Q:/A:）、全角/半角冒号、
大小写 q/Q 与 a/A、跨多段答案、文档头无标记内容兜底成普通块、全文无
问题标记整文一块、char 偏移正确性（text == full[char_start:char_end]）；
规范性检测 analyze_qa_format/is_qa_format_valid（≥50% 合格 / <50% 不合格 /
50% 边界合格 / 空文档不合格）；get_chunker('qa') 工厂分支与
resolve_parser_engine 引擎联动（auto+PlainText→plain / auto+DeepDOC→deepdoc /
显式引擎优先）。

API 覆盖：method=qa 问答对占比不足 50% → 任务 failed 且 error 带检测详情
（占比/对数/段数，前端确认弹窗依据）；qa_force_continue=true → 跳过检测
强制入库成功（问答对整块含问/答标记、偏移正确）；合格文档直接入库。
全部进程内 TestClient + 离线 mock embedding（txt 直读，无需 mock parser）。
"""
from __future__ import annotations

import time

import pytest

from backend.chunking.splitter import (Chunk, QaChunker, QaStats,
                                       analyze_qa_format, get_chunker,
                                       is_qa_format_valid)
from backend.services.ingestion_service import resolve_parser_engine
from conftest import (_resolve_headers, create_kb, upload_doc,
                      wait_for_status)


def _texts(chunks: list[Chunk]) -> list[str]:
    """取切块文本列表（Chunk.text）"""
    return [c.text for c in chunks]


def _assert_offsets(chunks: list[Chunk], full: str) -> None:
    """偏移正确性：每块 text 必须等于 full[char_start:char_end] 且区间不越界"""
    for c in chunks:
        assert 0 <= c.char_start <= c.char_end <= len(full), \
            f"偏移越界: {c.char_start}..{c.char_end} len={len(full)}"
        assert full[c.char_start:c.char_end] == c.text, \
            f"text 与偏移切片不一致: {c.text[:20]!r}"


# 中文问答样本：2 对问答，答案跨多段（第 3 段无"答："标记），文档头有杂项
QA_CN_TEXT = """本文档是系统常见问题说明。

问：什么是 RAG？

答：RAG 是检索增强生成。

它结合检索与生成两大能力，答案可跨多段。

问：支持哪些文档格式？

答：支持 txt、pdf、docx。"""

# 英文问答样本：半角冒号 + 小写 q/a + 大写 Q/A，开头有杂项段
QA_EN_TEXT = """Intro paragraph.

q: how to deploy?

a: run docker compose up.

It is that simple.

Q: and upgrade?

A: pull the latest image."""


class TestQaChunker:
    """QA 问答切块：格式识别 / 跨段答案 / 杂项兜底 / 偏移"""

    def test_chinese_format_pairs(self):
        """中文格式：问：/答：问答对整体成块，text 保留原文标记"""
        chunks = QaChunker().chunk(QA_CN_TEXT)
        _assert_offsets(chunks, QA_CN_TEXT)
        assert len(chunks) == 3
        # 开头杂项兜底成独立普通块（内容不丢失）
        assert chunks[0].text == "本文档是系统常见问题说明。"
        # 问答对含问/答标记与跨段答案，整体一块
        assert chunks[1].text.startswith("问：什么是 RAG？")
        assert "答：RAG 是检索增强生成。" in chunks[1].text
        assert "答案可跨多段。" in chunks[1].text
        assert chunks[2].text.startswith("问：支持哪些文档格式？")
        assert "答：支持 txt、pdf、docx。" in chunks[2].text

    def test_english_format(self):
        """英文格式：Q:/A:（半角冒号）+ 小写 q:/a: + 大小写 Q/A 混合"""
        chunks = QaChunker().chunk(QA_EN_TEXT)
        _assert_offsets(chunks, QA_EN_TEXT)
        assert len(chunks) == 3
        assert chunks[0].text == "Intro paragraph."
        assert chunks[1].text.startswith("q: how to deploy?")
        assert "a: run docker compose up." in chunks[1].text
        assert "It is that simple." in chunks[1].text  # 无标记答案段跨段归入
        assert chunks[2].text.startswith("Q: and upgrade?")
        assert "A: pull the latest image." in chunks[2].text

    def test_fullwidth_and_halfwidth_colon(self):
        """全角/半角冒号都支持（问：与问:、Q：与 Q:）"""
        text = "问：全角冒号。\n\n答：答案一。\n\n问:半角冒号。\n\n答:答案二。"
        chunks = QaChunker().chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 2
        assert chunks[0].text.startswith("问：全角冒号。")
        assert chunks[1].text.startswith("问:半角冒号。")

    def test_lowercase_q_a(self):
        """小写 q:/a: 同样识别为问答对"""
        text = "q: first question?\n\na: first answer.\n\nq: second?\n\na: second answer."
        chunks = QaChunker().chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 2
        assert chunks[0].text.startswith("q: first question?")
        assert chunks[1].text.startswith("q: second?")

    def test_answer_crosses_multiple_paragraphs(self):
        """答案跨多段（含无标记段落）全部归入同一问答对"""
        text = (
            "问：问题一？\n\n"
            "答：答案第一段。\n\n"
            "答案第二段，无答标记。\n\n"
            "答案第三段。\n\n"
            "问：问题二？\n\n"
            "答：答案二。"
        )
        chunks = QaChunker().chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 2
        # 第一对包含 4 个段落（问 + 3 段答案），第二对单独一块
        assert chunks[0].text.startswith("问：问题一？")
        assert "答案第一段。" in chunks[0].text
        assert "答案第二段，无答标记。" in chunks[0].text
        assert "答案第三段。" in chunks[0].text
        assert chunks[1].text == "问：问题二？\n\n答：答案二。"

    def test_leading_misc_fallback_chunk(self):
        """文档开头没有问题标记的杂项内容兜底成独立普通块（内容不丢失）"""
        text = (
            "文档标题说明。\n\n"
            "历史背景段落。\n\n"
            "问：第一个问题？\n\n"
            "答：第一个答案。"
        )
        chunks = QaChunker().chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 2
        assert chunks[0].text == "文档标题说明。\n\n历史背景段落。"
        assert chunks[1].text.startswith("问：第一个问题？")

    def test_no_question_marker_whole_text_one_chunk(self):
        """全文无问题标记：整文一个普通块（内容不丢失）"""
        text = "普通段落甲。\n\n普通段落乙。\n\n普通段落丙。"
        chunks = QaChunker().chunk(text)
        _assert_offsets(chunks, text)
        assert len(chunks) == 1
        assert chunks[0].text == text

    def test_empty_text(self):
        """空文本/纯空白 → 空列表"""
        assert QaChunker().chunk("") == []
        assert QaChunker().chunk("   \n \t ") == []

    def test_offsets_slice_correct(self):
        """char 偏移契约：text == full[char_start:char_end]（含中文/换行/空行）"""
        _assert_offsets(QaChunker().chunk(QA_CN_TEXT), QA_CN_TEXT)
        _assert_offsets(QaChunker().chunk(QA_EN_TEXT), QA_EN_TEXT)

    def test_factory_qa(self):
        """get_chunker('qa') 返回 QaChunker 实例（协议入口一致）"""
        chunker = get_chunker("qa", {})
        assert isinstance(chunker, QaChunker)
        assert len(chunker.chunk(QA_CN_TEXT)) == 3


class TestQaFormatDetection:
    """QA 规范性检测纯函数：占比判定 / 边界 / 空文档"""

    def test_ratio_above_half_valid(self):
        """问答对占比 >50% → 合格"""
        # 问/答同段（段内换行无空行）构造 2 对 + 开头单块引言（并入第一问答对
        # 段，视为标题）→ 2 对 / 2 段 = 100%
        text = "引言。\n\n问：a？\n答：b。\n\n问：c？\n答：d。"
        stats = analyze_qa_format(text)
        assert stats == QaStats(qa_pairs=2, total_paragraphs=2)
        assert is_qa_format_valid(stats)

    def test_boundary_50_percent_valid(self):
        """恰好 50%（边界值）→ 合格"""
        # 开头多块杂项独立成段：2 杂项段 + 2 问答对段 = 4 段，2/4 = 50%
        text = ("引言一。\n\n引言二。\n\n问：a？\n答：b。\n\n"
                "问：c？\n答：d。")
        stats = analyze_qa_format(text)
        assert stats == QaStats(qa_pairs=2, total_paragraphs=4)
        assert is_qa_format_valid(stats)

    def test_ratio_below_half_invalid(self):
        """问答对占比 <50% → 不合格"""
        # 开头 2 块杂项各自成段 + 1 问答对段 = 3 段，1/3≈33.3%
        text = "普通段落甲。\n\n普通段落乙。\n\n问：唯一的问题？\n\n答：唯一的答案。"
        stats = analyze_qa_format(text)
        assert stats == QaStats(qa_pairs=1, total_paragraphs=3)
        assert not is_qa_format_valid(stats)

    def test_title_plus_three_pairs_100_percent(self):
        """回归：标题+3 问答对 → 3 对 / 3 段 = 100% 合格（段落统计口径 bug）"""
        # 结构 A：标题与第一个"问："无空行（同一文本块，标题并入第一块）
        text_a = ("公司人事制度问答\n问：a？\n答：b。\n\n"
                  "问：c？\n答：d。\n\n问：e？\n答：f。")
        stats_a = analyze_qa_format(text_a)
        assert stats_a == QaStats(qa_pairs=3, total_paragraphs=3)
        assert is_qa_format_valid(stats_a)
        # 结构 B：真实 docx 解析输出（每个 docx 段落被空行分隔，7 行/7 块）——
        # 聚合后 3 问答对段，标题（单块）并入第一问答对，不拉低占比
        text_b = ("公司人事制度问答\n\n问：a？\n\n答：b。\n\n"
                  "问：c？\n\n答：d。\n\n问：e？\n\n答：f。")
        stats_b = analyze_qa_format(text_b)
        assert stats_b == QaStats(qa_pairs=3, total_paragraphs=3)
        assert is_qa_format_valid(stats_b)

    def test_no_qa_pairs_invalid(self):
        """无任何问答对 → 不合格"""
        stats = analyze_qa_format("普通段落。\n\n普通段落。")
        assert stats == QaStats(qa_pairs=0, total_paragraphs=2)
        assert not is_qa_format_valid(stats)

    def test_empty_document_invalid(self):
        """空文档（0 段）→ 不合格（无问答对可入库）"""
        assert analyze_qa_format("") == QaStats(qa_pairs=0, total_paragraphs=0)
        assert not is_qa_format_valid(analyze_qa_format(""))
        assert not is_qa_format_valid(analyze_qa_format("   \n \t "))

    def test_stat_scope_consistent_with_chunker(self):
        """检测统计口径与切块器一致（同一段落切分+问题块判定复用）"""
        text = "问：a？\n\n答：b。\n\n答案延续。\n\n问：c？\n\n答：d。"
        stats = analyze_qa_format(text)
        chunks = QaChunker().chunk(text)
        # 答案/延续段并入所属问答对：2 问块 + 3 答案块 → 2 个聚合段，2/2=100%；
        # 切块同口径为 2 个问答对整块（答案跨段完整保留）
        assert stats == QaStats(qa_pairs=2, total_paragraphs=2)
        assert is_qa_format_valid(stats)
        assert len(chunks) == 2
        assert "答案延续。" in chunks[0].text


class TestResolveParserEngine:
    """解析引擎联动（PlainText/DeepDOC 映射，路由层与任务内同源）"""

    def test_auto_plaintext_maps_to_plain(self):
        """auto + layout_recognize=PlainText → engine=plain（纯文本直提）"""
        assert resolve_parser_engine(
            {"parser_engine": "auto", "layout_recognize": "PlainText"}) == "plain"
        # 缺省 parser_engine 同样走 auto 逻辑
        assert resolve_parser_engine(
            {"layout_recognize": "PlainText"}) == "plain"

    def test_auto_deepdoc_maps_to_deepdoc(self):
        """auto + layout_recognize=DeepDOC → engine=deepdoc（既有行为保持）"""
        assert resolve_parser_engine(
            {"parser_engine": "auto", "layout_recognize": "DeepDOC"}) == "deepdoc"

    def test_explicit_engine_wins(self):
        """显式引擎优先：mineru + PlainText → 仍为 mineru"""
        assert resolve_parser_engine(
            {"parser_engine": "mineru", "layout_recognize": "PlainText"}) == "mineru"
        assert resolve_parser_engine(
            {"parser_engine": "plain", "layout_recognize": "DeepDOC"}) == "plain"
        assert resolve_parser_engine(
            {"parser_engine": "deepdoc", "layout_recognize": "PlainText"}) == "deepdoc"

    def test_auto_without_layout_stays_auto(self):
        """auto + 无联动版面 → 保持 auto（探测降级）"""
        assert resolve_parser_engine({"parser_engine": "auto"}) == "auto"
        assert resolve_parser_engine(
            {"parser_engine": "auto", "layout_recognize": "MinerU"}) == "auto"


# ---- 入库链路（API 集成）----

# 不合格样本：2 个杂项段 + 1 问答对段 = 3 段，1/3≈33.3%（< 50%）
QA_WEAK_TEXT = "普通段落甲。\n\n普通段落乙。\n\n问：唯一的问题？\n\n答：唯一的答案。"
# 合格样本：6 段 3 个问答对（50% 边界合格）
QA_GOOD_TEXT = (
    "问：什么是 RAG？\n\n答：RAG 是检索增强生成。\n\n"
    "问：支持哪些格式？\n\n答：支持 txt、pdf、docx。\n\n"
    "问：如何部署？\n\n答：docker compose up。"
)


def _ingest(client, kb_id, doc_id, body=None, headers=None):
    """触发入库（body 可选：切块参数），返回原始响应"""
    return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                       json=body, headers=headers)


def _wait_failed(client, kb_id, doc_id, headers=None, timeout=10.0):
    """轮询文档直到 failed（qa 检测失败场景）；意外 ingested 或超时抛错"""
    hdrs = _resolve_headers(client, headers)
    deadline = time.monotonic() + timeout
    while True:
        doc = client.get(f"/api/kbs/{kb_id}/documents/{doc_id}",
                         headers=hdrs).json()
        if doc["status"] == "failed":
            return doc
        if doc["status"] == "ingested":
            raise AssertionError("预期 QA 规范性检测失败，但文档已入库")
        if time.monotonic() > deadline:
            raise AssertionError(
                f"等待 failed 超时（{timeout}s），当前状态: {doc['status']}")
        time.sleep(0.2)


class TestQaIngestChain:
    """qa 入库链路：不合格失败带详情 / 强制继续 / 合格直接入库"""

    def test_qa_weak_fails_with_detail(self, client, mock_embedding,
                                       admin_headers):
        """method=qa 且问答对占比 <50% → 任务 failed，error 带检测详情
        （占比/对数/段数，前端确认弹窗的解析依据）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], content=QA_WEAK_TEXT)
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "qa"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = _wait_failed(client, kb["id"], doc["id"])
        assert final["status"] == "failed"
        error = final["error"] or ""
        assert "QA 问答格式检测未通过" in error
        assert "占比 33.3%" in error
        assert "1 对 / 3 段" in error
        assert "50%" in error

    def test_qa_force_continue_ingests(self, client, mock_embedding,
                                       admin_headers):
        """qa_force_continue=true → 跳过规范性检测，不合格文档强制入库成功"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], content=QA_WEAK_TEXT)
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "qa", "qa_force_continue": True},
                       headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_id"] == "qa"
        # 2 个开头杂项块合并成普通块 + 问答对块，共 2 块（内容不丢失）
        assert final["chunk_count"] == 2
        assert final["chunk_preview"][0].startswith("普通段落甲。")
        assert "问：唯一的问题？" in final["chunk_preview"][1]

    def test_qa_good_doc_ingests_directly(self, client, mock_embedding,
                                          admin_headers):
        """问答对占比 >=50% 的合格文档直接入库（无强制标记）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], content=QA_GOOD_TEXT)
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "qa"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_id"] == "qa"
        # 3 个问答对各一块，共 3 块
        assert final["chunk_count"] == 3
        preview = final["chunk_preview"]
        assert preview[0].startswith("问：什么是 RAG？")
        assert preview[1].startswith("问：支持哪些格式？")
        assert preview[2].startswith("问：如何部署？")
        # 详情切块偏移与 full_text 一致（问答对整块保留原文标记）
        detail = client.get(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}",
            headers=admin_headers).json()
        full = detail["full_text"]
        for c in detail["chunks"]:
            assert full[c["char_start"]:c["char_end"]] == c["text"]
