"""解析参数化（ingest 切块方式/参数）API 集成测试

覆盖：默认无 body（naive 落库）、method=title（split_level 持久化 + 按标题
切块）、regex（带 pattern 成功 / 无 pattern 400）、非法 method 400、
chunk_size 越界 400、重跑沿用上次配置、列表/详情返回 parser_id/parser_config、
新文档配置隔离（不受旧文档影响）。
全部进程内 TestClient + 离线 mock embedding（txt/md 走 plain 提取，无需 mock parser）。
注意：RegexChunker 对 pattern 原样编译（不带 re.MULTILINE），跨行匹配需
pattern 自带 (?m) 前缀，与业务实现一致。
"""
from __future__ import annotations

import pytest

from conftest import create_kb, upload_doc, wait_for_status


def _ingest(client, kb_id, doc_id, body=None, headers=None):
    """触发入库（body 可选：切块参数），返回原始响应"""
    return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                       json=body, headers=headers)


def _get_doc(client, kb_id, doc_id, headers=None):
    return client.get(f"/api/kbs/{kb_id}/documents/{doc_id}",
                      headers=headers).json()


class TestIngestParams:
    """ingest 切块参数：默认 / 显式方式 / 校验 400 / 沿用 / 字段 / 隔离"""

    def test_ingest_default_no_body_naive(self, client, mock_embedding,
                                          admin_headers):
        """默认无 body：ingest 成功，parser_id=naive，parser_config 取活跃配置"""
        from backend.config import get_active_config
        active = get_active_config().chunking
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        # 无 body → 默认 naive，且配置已落库（chunk_size/overlap 与活跃配置一致）
        assert final["parser_id"] == "naive"
        assert final["parser_config"]["chunk_size"] == active.chunk_size
        assert final["parser_config"]["overlap"] == active.chunk_overlap

    def test_ingest_method_title_split_level(self, client, mock_embedding,
                                             admin_headers):
        """method=title：成功，parser_config 含 split_level，按标题切块"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])  # SAMPLE_TEXT 含 # 与 ## 两级标题
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "title"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_id"] == "title"
        assert final["parser_config"]["split_level"] == 3  # 缺省 3 级全切
        # 标题切块：3 个标题段各成一块，首块保留标题
        assert final["chunk_count"] >= 3
        assert final["chunk_preview"][0].startswith("# Python 简介")

    def test_ingest_method_regex_with_pattern(self, client, mock_embedding,
                                              admin_headers):
        """method=regex + regex_pattern：成功，匹配片段与间隔文本都成块"""
        content = "开始部分\n\n# 章节一\n\n中间内容\n\n# 章节二\n\n结束部分"
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], content=content)
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "regex",
                             "regex_pattern": r"(?m)^# .*$"},
                       headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_id"] == "regex"
        assert final["parser_config"]["regex_pattern"] == r"(?m)^# .*$"
        # 间隔文本与匹配片段都成块：前言 / # 章节一 / 中间内容 / # 章节二 / 结束
        assert final["chunk_count"] == 5
        assert final["chunk_preview"][1] == "# 章节一"
        assert "中间内容" in final["chunk_preview"][2]
        assert final["chunk_preview"][3] == "# 章节二"

    def test_ingest_regex_without_pattern_400(self, client, mock_embedding,
                                              admin_headers):
        """method=regex 无 regex_pattern → 同步 400，任务不启动（状态不变）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "regex"}, headers=admin_headers)
        assert resp.status_code == 400
        assert "regex_pattern" in resp.text
        # 同步校验失败：文档仍 uploaded，未进入任务
        assert _get_doc(client, kb["id"], doc["id"], admin_headers)[
            "status"] == "uploaded"

    def test_ingest_invalid_method_400(self, client, mock_embedding,
                                       admin_headers):
        """非法 method → 同步 400"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "keyword"}, headers=admin_headers)
        assert resp.status_code == 400
        assert "非法切块方式" in resp.text

    @pytest.mark.parametrize("bad_size", [10, 20001])
    def test_ingest_chunk_size_out_of_range_400(self, client, mock_embedding,
                                                admin_headers, bad_size):
        """chunk_size 越界（<50 或 >20000）→ 同步 400"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "naive", "chunk_size": bad_size},
                       headers=admin_headers)
        assert resp.status_code == 400
        assert "chunk_size" in resp.text

    def test_reingest_keeps_parser_config(self, client, mock_embedding,
                                          admin_headers):
        """重跑不传 body：沿用上次配置（parser_id 与 split_level 保持不变）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        # 第一次：显式 title + split_level=1
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "title", "split_level": 1},
                       headers=admin_headers)
        assert resp.status_code == 200, resp.text
        first = wait_for_status(client, kb["id"], doc["id"])
        assert first["parser_id"] == "title"
        assert first["parser_config"]["split_level"] == 1
        # 第二次：无 body 重跑 → 沿用 title/split_level=1，而非回退默认 naive
        resp = _ingest(client, kb["id"], doc["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        second = wait_for_status(client, kb["id"], doc["id"])
        assert second["status"] == "ingested"
        assert second["parser_id"] == "title"
        assert second["parser_config"]["split_level"] == 1

    def test_list_and_detail_parser_fields(self, client, mock_embedding,
                                           admin_headers):
        """文档列表/详情返回 parser_id/parser_config 字段"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"],
                       body={"method": "naive", "delimiter": "\n"},
                       headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["parser_id"] == "naive"
        # 列表
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        assert len(docs) == 1
        assert docs[0]["parser_id"] == "naive"
        assert docs[0]["parser_config"]["delimiter"] == "\n"
        assert docs[0]["parser_config"]["chunk_size"] > 0
        # 详情
        detail = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                            headers=admin_headers).json()
        assert detail["parser_id"] == "naive"
        assert detail["parser_config"]["delimiter"] == "\n"

    def test_new_doc_uses_default_config(self, client, mock_embedding,
                                         admin_headers):
        """新文档不受旧文档配置影响：doc1 用 title 入库，doc2 默认 naive"""
        kb = create_kb(client)
        doc1 = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc1["id"],
                       body={"method": "title"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final1 = wait_for_status(client, kb["id"], doc1["id"])
        assert final1["parser_id"] == "title"

        doc2 = upload_doc(client, kb["id"], filename="另一文档.md")
        resp = _ingest(client, kb["id"], doc2["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final2 = wait_for_status(client, kb["id"], doc2["id"])
        assert final2["status"] == "ingested"
        assert final2["parser_id"] == "naive"
        # 两文档共存，各自配置互不影响
        docs = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers).json()
        by_id = {d["id"]: d for d in docs}
        assert by_id[doc1["id"]]["parser_id"] == "title"
        assert by_id[doc2["id"]]["parser_id"] == "naive"
