"""标题末尾标点白名单：文档级覆盖全局

三层覆盖：
1) 默认值接口（/kbs/ingest-defaults）带**全局**白名单 —— 前端据此显示默认值；
2) 切块器工厂把文档级值传给三个切块器（title/parent_child/hierarchical）；
3) 文档级值**真的影响切块结果**（不只存了不用）；
4) API 层落库/沿用/校验。

与全局配置级测试互补：tests/test_heading_end_punct.py 测判定规则本身，
本文件测「本次解析覆盖全局」这条链路。
"""
from __future__ import annotations

from backend.chunking import get_chunker


class TestPublicDefaults:
    """默认值接口带全局白名单（前端解析配置弹窗据此显示默认值）"""

    def test_includes_global_whitelist(self):
        from backend.services.ingestion.params import public_defaults
        d = public_defaults()
        assert "heading_end_punct_whitelist" in d
        # 默认取国标：问号/叹号/省略号（含半角）
        assert set(d["heading_end_punct_whitelist"]) == {"？", "！", "…", "?", "!"}


class TestGetChunkerPassesWhitelist:
    """工厂把文档级白名单交给各切块器（delimiter 只在 naive，这个跨方式通用）"""

    # parent_child 从工厂构造须显式给 parent_split_level（其构造签名默认值
    # 会被 config.get 的 None 覆盖，报 1<=None<=6 —— 既有行为，非本次改动）
    _CASES = (("title", {}),
              ("parent_child", {"parent_split_level": 2}),
              ("hierarchical", {}))

    def test_passes_to_all_chunkers(self):
        wl = ["。", "？"]
        for method, extra in self._CASES:
            chunker = get_chunker(method, {"heading_end_punct_whitelist": wl,
                                           **extra})
            assert chunker.heading_end_punct_whitelist == wl, method

    def test_absent_is_none(self):
        """不传 → None（切块时回退全局配置，而非空列表）"""
        for method, extra in self._CASES:
            chunker = get_chunker(method, extra)
            assert chunker.heading_end_punct_whitelist is None, method


class TestDocLevelTakesEffect:
    """文档级白名单真的改变切块结果（存了就要用）"""

    # 两行标题：一行以问号结尾（默认白名单放行）、一行以句号结尾（默认挡）
    TEXT = ("# 第一章 概述\n\n正文甲。\n\n"
            "## 这是正文句。\n\n正文乙。\n")

    def test_default_blocks_period_heading(self):
        """默认：句号标题不认 → 只有'第一章'一个标题边界"""
        chunker = get_chunker("title", {"split_level": 2})
        chunks = chunker.chunk(self.TEXT)
        assert len(chunks) == 1, [c.text[:20] for c in chunks]

    def test_doc_level_allows_period_heading(self):
        """文档级放行句号 → 该行成为标题 → 多出一个切分边界"""
        chunker = get_chunker("title",
                              {"split_level": 2,
                               "heading_end_punct_whitelist": ["。"]})
        chunks = chunker.chunk(self.TEXT)
        assert len(chunks) == 2, [c.text[:20] for c in chunks]

    def test_question_heading_blocked_when_not_whitelisted(self):
        """反向：文档级只放行句号 → 问号标题不再算标题"""
        text = ("# 第一章 概述\n\n正文甲。\n\n"
                "## 什么是 RAG？\n\n正文乙。\n")
        default_chunks = get_chunker("title", {"split_level": 2}).chunk(text)
        restricted = get_chunker(
            "title", {"split_level": 2,
                      "heading_end_punct_whitelist": ["。"]}).chunk(text)
        assert len(default_chunks) == 2   # 默认放行 ？
        assert len(restricted) == 1       # 收窄后不认 ？


class TestAPIRoundTrip:
    """API 层：落库 / 重跑沿用 / 空列表是有效值 / 非法 400"""

    def _ingest(self, client, kb_id, doc_id, body, headers):
        return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                           json=body, headers=headers)

    def test_not_passed_stays_absent(self, client, mock_embedding,
                                     admin_headers):
        """不传 → parser_config 不含该字段（切块时用全局配置）"""
        from conftest import create_kb, upload_doc, wait_for_status
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        self._ingest(client, kb["id"], doc["id"],
                     {"method": "naive"}, admin_headers)
        final = wait_for_status(client, kb["id"], doc["id"])
        assert "heading_end_punct_whitelist" not in final["parser_config"]

    def test_stored_and_reused(self, client, mock_embedding, admin_headers):
        """显式传 → 落库；重跑不传时沿用"""
        from conftest import create_kb, upload_doc, wait_for_status
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        wl = ["。", "；"]
        self._ingest(client, kb["id"], doc["id"],
                     {"method": "naive", "heading_end_punct_whitelist": wl},
                     admin_headers)
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["parser_config"]["heading_end_punct_whitelist"] == wl
        # 重跑不带该字段 → 沿用文档已存的
        self._ingest(client, kb["id"], doc["id"], {"method": "naive"},
                     admin_headers)
        again = wait_for_status(client, kb["id"], doc["id"])
        assert again["parser_config"]["heading_end_punct_whitelist"] == wl

    def test_empty_list_is_valid(self, client, mock_embedding, admin_headers):
        """空列表是有效值（= 回退全局默认），不是 400"""
        from conftest import create_kb, upload_doc, wait_for_status
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        r = self._ingest(client, kb["id"], doc["id"],
                         {"method": "naive", "heading_end_punct_whitelist": []},
                         admin_headers)
        assert r.status_code == 200, r.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["parser_config"]["heading_end_punct_whitelist"] == []

    def test_non_list_rejected(self, client, mock_embedding, admin_headers):
        """非列表 → 422（Pydantic 类型校验先于业务校验拦截，FastAPI 标准行为；
        业务级校验如"每项须为单字符"走 400，见下个用例）"""
        from conftest import create_kb, upload_doc
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        r = self._ingest(client, kb["id"], doc["id"],
                         {"method": "naive",
                          "heading_end_punct_whitelist": "。？"},
                         admin_headers)
        assert r.status_code == 422, r.text

    def test_multi_char_item_rejected(self, client, mock_embedding,
                                      admin_headers):
        """列表项非单字符 → 400"""
        from conftest import create_kb, upload_doc
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        r = self._ingest(client, kb["id"], doc["id"],
                         {"method": "naive",
                          "heading_end_punct_whitelist": ["。。"]},
                         admin_headers)
        assert r.status_code == 400, r.text
