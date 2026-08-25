"""引用溯源程序性保障单元测试：CitationGuard 流式过滤器 + 覆盖率统计

覆盖：合法/越界引用标的判定、跨 chunk 拆分（待定缓冲）、句尾特征规则
（与前端 renderCitationContent 同源）、非引用 [标（链接/多编号/汉字后缀）
原样保留、flush 行尾（$）语义、异常路径幂等、stats 统计（句子/引用数/
覆盖率）。
全部离线（纯标准库正则，无网络与数据依赖）。
"""
from __future__ import annotations

from backend.services.citation_guard import CitationGuard


def _feed_all(guard: CitationGuard, texts: list[str]) -> str:
    """依次 feed，返回全部 sanitized 输出"""
    return "".join(guard.feed(t) for t in texts)


def _stream(guard: CitationGuard, s: str) -> str:
    """按单字符/双字符切块喂入（模拟跨 chunk 拆分）"""
    return "".join(guard.feed(s[i:i + 2]) for i in range(0, len(s), 2))


class TestRefJudgement:
    """引用标判定（合法 / 越界 / 非引用）"""

    def test_valid_ref_passes(self):
        """合法编号原样通过"""
        guard = CitationGuard(max_ref=3)
        out = guard.feed("Python 是一门语言[1]。")
        assert out == "Python 是一门语言[1]。"
        assert guard.stats().refs == 1

    def test_invalid_ref_stripped(self):
        """越界编号（> max_ref）被剥离，正文保留"""
        guard = CitationGuard(max_ref=2)
        out = guard.feed("内容[5]。）\n再一段[2]。")
        # [5] 越界 → 剥离；[2] 合法 → 保留；"）" 是普通正文不受影响
        assert out == "内容。）\n再一段[2]。"
        stats = guard.stats()
        assert stats.refs == 1 and stats.refs_invalid == 1

    def test_multiple_valid_refs(self):
        """多个合法引用标都保留（每个 [n] 后须跟句尾特征）"""
        guard = CitationGuard(max_ref=3)
        out = _feed_all(guard, ["A[1]。B[2]。C[3]。"])
        assert out == "A[1]。B[2]。C[3]。"
        assert guard.stats().refs == 3

    def test_ref_boundary_valid_equals_max(self):
        """编号 == max_ref 合法（> 才越界）"""
        guard = CitationGuard(max_ref=3)
        out = guard.feed("x[3]。")
        assert out == "x[3]。"
        assert guard.stats().refs == 1 and guard.stats().refs_invalid == 0

    def test_ref_zero_invalid(self):
        """[0] 越界剥离（编号从 1 起）"""
        guard = CitationGuard(max_ref=3)
        out = guard.feed("x[0]。")
        assert out == "x。"
        stats = guard.stats()
        assert stats.refs == 0 and stats.refs_invalid == 1


class TestTailFeature:
    """句尾特征规则（[n] 后必须行尾/空白/标点，与前端同源）"""

    def test_ref_with_chinese_punct(self):
        """后接中文标点（句段号前）→ 是引用标"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("用了[3]。") == "用了[3]。"
        assert guard.stats().refs == 1

    def test_ref_followed_by_hanzi_kept_as_text(self):
        """"见[3]附录"（后接汉字）→ 不识别，整段原样"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("见[3]附录") == "见[3]附录"
        assert guard.stats().refs == 0

    def test_ref_followed_by_letter_kept(self):
        """后接字母/数字 → 不识别"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("x[1]y") == "x[1]y"
        assert guard.feed("x[1]2") == "x[1]2"
        assert guard.stats().refs == 0

    def test_ref_followed_by_space_is_ref(self):
        """后接空白 → 是引用标（前端空白匹配语义）"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("x[1] y") == "x[1] y"
        assert guard.stats().refs == 1

    def test_ref_followed_by_paren_url_kept(self):
        """[1](url) 极端形式：后接 "(" 非句尾特征 → 原样"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("x[1](url)") == "x[1](url)"
        assert guard.stats().refs == 0


class TestNonRefBrackets:
    """非引用 [ ] 内容保持原样"""

    def test_link_text(self):
        """markdown 链接 [text](url)：text 非纯数字 → 原样"""
        guard = CitationGuard(max_ref=5)
        out = _stream(guard, "见[文档标题](http://x)详述[1]。")
        assert out == "见[文档标题](http://x)详述[1]。"
        assert guard.stats().refs == 1

    def test_multi_number_bracket(self):
        """[1,2] 多编号：前端正则不含逗号 → 不识别，原样"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("见[1,2]。") == "见[1,2]。"
        assert guard.stats().refs == 0

    def test_bracket_with_spaces(self):
        """[ 1 ] 含空格 → 不识别，原样"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("见[ 1 ]。") == "见[ 1 ]。"
        assert guard.stats().refs == 0

    def test_bracket_alpha_inside(self):
        """[x1] / [1x] 非纯数字 → 原样"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("[x1]。") == "[x1]。"
        assert guard.stats().refs == 0


class TestCrossChunk:
    """跨 chunk 拆分（待定缓冲）"""

    def test_split_before_bracket(self):
        """"[" 前一字符与其他字符分 chunk"""
        guard = CitationGuard(max_ref=5)
        out = _feed_all(guard, ["内容", "[1]。"])
        assert out == "内容[1]。"
        assert guard.stats().refs == 1

    def test_split_number_and_quote(self):
        """" [1" + "]。" 分 chunk"""
        guard = CitationGuard(max_ref=5)
        out = _feed_all(guard, ["x[1", "]."])
        assert out == "x[1]."
        assert guard.stats().refs == 1

    def test_split_invalid_number_broken(self):
        """"[9" + "]。" 越界跨 chunk 剥离"""
        guard = CitationGuard(max_ref=2)
        out = _feed_all(guard, ["x[9", "]。"])
        assert out == "x。"
        stats = guard.stats()
        assert stats.refs == 0 and stats.refs_invalid == 1

    def test_split_digits_across_chunks(self):
        """"[1" + "2]。" 数字跨 chunk（编号 12）"""
        guard = CitationGuard(max_ref=20)
        out = _feed_all(guard, ["x[1", "2]。"])
        assert out == "x[12]。"
        assert guard.stats().refs == 1

    def test_split_ref_across_three_chunks(self):
        """"[" + "1" + "]。" 三 chunk 拆分"""
        guard = CitationGuard(max_ref=5)
        out = _feed_all(guard, ["[", "1", "]。"])
        assert out == "[1]。"
        assert guard.stats().refs == 1

    def test_split_followed_by_hanzi(self):
        """"[3" + "]附录" 跨 chunk 后接汉字 → 原样保留"""
        guard = CitationGuard(max_ref=5)
        out = _feed_all(guard, ["见[3", "]附录"])
        assert out == "见[3]附录"
        assert guard.stats().refs == 0

    def test_waiting_pending_not_dropped(self):
        """待定期间输出为空，后续 flush/fed 时补偿（不丢文本）"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("x[1") == "x"  # "[1" 待定 → 只输出 x
        assert guard.feed("]。") == "[1]。"  # 补偿完整输出
        assert guard.stats().refs == 1


class TestFlush:
    """流结束（flush）：行尾 $ 视为句尾特征；不完整尾段原文释放"""

    def test_flush_complete_ref_at_eos(self):
        """"[1]" 以行尾结束 → 是引用标"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("a[1]") == "a"
        assert guard.flush() == "[1]"
        assert guard.stats().refs == 1

    def test_flush_invalid_ref_at_eos(self):
        """"[9]" 以行尾结束越界 → 剥离"""
        guard = CitationGuard(max_ref=2)
        assert guard.feed("a[9]") == "a"
        assert guard.flush() == ""
        stats = guard.stats()
        assert stats.refs == 0 and stats.refs_invalid == 1

    def test_flush_incomplete_bracket_kept(self):
        """"[1" 不完整（无 ]）→ 行尾也不可能构成引用 → 原文释放"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("a[1") == "a"
        assert guard.flush() == "[1"
        assert guard.stats().refs == 0

    def test_flush_lone_bracket(self):
        """"[, "[" 单字符结尾 → 原文释放"""
        guard = CitationGuard(max_ref=5)
        assert guard.feed("a[") == "a"
        assert guard.flush() == "["
        assert guard.stats().refs == 0

    def test_flush_idempotent(self):
        """flush 二次调用返回空（幂等，异常路径已 flush 后再调用安全）"""
        guard = CitationGuard(max_ref=5)
        guard.feed("a[1]")
        assert guard.flush() == "[1]"
        assert guard.flush() == ""

    def test_flush_after_feed_complete(self):
        """flush 前已完整输出 → flush 返回空"""
        guard = CitationGuard(max_ref=5)
        guard.feed("a[1]。")
        assert guard.flush() == ""


class TestStats:
    """覆盖率统计"""

    def test_stats_sentences_and_coverage(self):
        """两句子一句有引用 → sentences=2, cited=1, coverage=0.5"""
        guard = CitationGuard(max_ref=5)
        guard.feed("第一句[1]。第二句。")
        stats = guard.stats()
        assert stats.sentences == 2
        assert stats.cited_sentences == 1
        assert stats.coverage == 0.5

    def test_stats_no_citation(self):
        """无引用 → refs=0, cited=0, coverage=0"""
        guard = CitationGuard(max_ref=5)
        guard.feed("没有引用的一段话。")
        stats = guard.stats()
        assert stats.refs == 0 and stats.cited_sentences == 0
        assert stats.coverage == 0.0

    def test_stats_empty(self):
        """无文本 → 全 0"""
        guard = CitationGuard(max_ref=5)
        stats = guard.stats()
        assert stats.sentences == 0 and stats.coverage == 0.0

    def test_stats_sentence_split_puncts(self):
        """英文句点/问号也切句"""
        guard = CitationGuard(max_ref=2)
        guard.feed("A[1]。B! C[2]?")
        stats = guard.stats()
        assert stats.sentences == 3
        assert stats.cited_sentences == 2


class TestChatCitationIntegration:
    """chat_service 集成：流式越界剥离 + done 事件 citation 统计"""

    @staticmethod
    def _sse_parts(text: str) -> tuple:
        """解析 SSE 文本 → (delta_texts, done_data)"""
        import json
        deltas, done = [], None
        for block in text.split("\n\n"):
            if not block.startswith("event: "):
                continue
            ev = block.split("\n", 1)[0][7:]
            data_line = block.split("data: ", 1)[1].strip()
            if ev == "delta":
                deltas.append(json.loads(data_line)["text"])
            elif ev == "done":
                done = json.loads(data_line)
        return "".join(deltas), done

    def test_out_of_range_ref_stripped_in_stream(self, client,
                                                 mock_embedding, mock_llm,
                                                 admin_headers):
        """越界 [9] 剥离，合法 [1] 保留；done 携带 citation 统计"""
        from conftest import create_kb, upload_and_ingest
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        mock_llm(parts=["回答[9]。完成[1]。"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        delta_text, done = self._sse_parts(resp.text)
        assert "[9]" not in delta_text, "越界引用标应被剥离"
        assert "[1]" in delta_text, "合法引用标应保留"
        assert done is not None, "应有 done 事件"
        citation = done.get("citation")
        assert citation is not None, "done 应携带 citation 统计"
        assert citation["refs"] == 1
        assert citation["refs_invalid"] == 1

    def test_no_citation_reported(self, client, mock_embedding, mock_llm,
                                  admin_headers):
        """模型完全不标 [n] → citation 提示未标注（refs=0, coverage=0）"""
        from conftest import create_kb, upload_and_ingest
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        mock_llm(parts=["知识库中未找到相关信息。"])
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        _, done = self._sse_parts(resp.text)
        citation = done["citation"]
        assert citation["refs"] == 0
        assert citation["refs_invalid"] == 0
        assert citation["sentences"] >= 1
        assert citation["cited_sentences"] == 0
        assert citation["coverage"] == 0.0
