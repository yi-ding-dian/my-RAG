"""父子分块测试：ParentChildChunker 单元 + ingest/检索 API 集成

单元覆盖：子块按 chunk_size、父块按标题聚合（split_level=2 时 ## 也是
父块边界）、子块归属映射（起始偏移落在父块区间 → 归属该父块；跨边界归
起始父块；父块间空隙归前最近父块）、无标题全部归首个父块、父块/子块
偏移切片正确性、chunk() 协议入口与 get_parent_chunks/get_mapping 惰性约定。
API 覆盖：method=parent_child 带完整参数入库（详情 chunks 偏移 + full_text
一致）、retrieval_mode=parent/child 检索返回差异、父块参数/检索模式越界
同步 400、重跑沿用 parent_child 配置、naive 文档 metadata 无 parent 字段
（详情 chunks 偏移仍在）。全部进程内 TestClient + 离线 mock embedding。
"""
from __future__ import annotations

import pytest

from backend.chunking.splitter import (Chunk, ParentChildChunker,
                                       get_chunker)
from conftest import create_kb, upload_doc, wait_for_status


def _ingest(client, kb_id, doc_id, body=None, headers=None):
    """触发入库（body 可选：切块参数），返回原始响应"""
    return client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                       json=body, headers=headers)


def _get_doc(client, kb_id, doc_id, headers=None):
    return client.get(f"/api/kbs/{kb_id}/documents/{doc_id}",
                      headers=headers).json()


def _retrieve(client, kb_id, query, headers=None):
    """检索（POST /api/chat/retrieve），返回 sources 列表"""
    resp = client.post("/api/chat/retrieve", json={
        "kb_id": kb_id, "query": query,
    }, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["sources"]


def _assert_offsets(chunks: list[Chunk], full: str) -> None:
    """偏移正确性：每块 text 必须等于 full[char_start:char_end] 且区间不越界"""
    for c in chunks:
        assert 0 <= c.char_start <= c.char_end <= len(full), \
            f"偏移越界: {c.char_start}..{c.char_end} len={len(full)}"
        assert full[c.char_start:c.char_end] == c.text, \
            f"text 与偏移切片不一致: {c.text[:20]!r}"


# 单元样本：两级标题（#/##），split_level=2 时 # 与 ## 都是父块边界 → 6 个父块
PC_TEXT = """# 第一章 总览

这是第一章的引言内容，介绍背景。包含足够多的文字让子块切分生效。

## 1.1 背景

第一章第一小节的内容，讲述历史沿革与现状分析。

## 1.2 现状

第一章第二小节的内容，分析当前存在的问题与挑战。

# 第二章 方案

第二章的内容，提出整体解决方案与实施路径。

## 2.1 技术选型

第二章第一小节，描述技术选型的依据与对比。

## 2.2 实施计划

第二章第二小节，描述实施步骤与时间规划。
"""

# API 集成样本（与 /tmp/verify_e2e.py 一致，txt/md plain 直读保真）
PC_API_TEXT = """# 第一章 总览

这是第一章的引言内容，介绍背景信息，包含足够多的文字内容。

## 1.1 背景

第一章第一小节的内容，讲述历史沿革与现状分析，内容较长需要切分。

## 1.2 现状

第一章第二小节的内容，分析当前存在的问题与挑战。

# 第二章 方案

第二章的内容，提出整体解决方案与实施路径，是回答问题的关键章节。

## 2.1 技术选型

第二章第一小节，描述技术选型的依据与对比分析。

## 2.2 实施计划

第二章第二小节，描述实施步骤与时间规划安排。
"""


def _pc_chunker(**kw):
    """默认父块参数的 ParentChildChunker（chunk_size=30/overlap=5）"""
    defaults = dict(chunk_size=30, overlap=5, parent_chunk_size=200,
                    parent_chunk_overlap=0, parent_split_level=2)
    defaults.update(kw)
    return ParentChildChunker(**defaults)


class TestParentChildChunkerUnit:
    """ParentChildChunker 单元：子块/父块生成、归属映射、偏移"""

    def test_children_and_parents_generation(self):
        """带 #/## 标题样本：子块按 chunk_size 切、父块按标题聚合

        split_level=2 时 ## 也是父块边界 → 6 个父块（两章四小节），
        每个父块以标题开头。
        """
        chunker = _pc_chunker()
        result = chunker.chunk_parent_child(PC_TEXT)
        assert len(result.children) > 0, "应有子块"
        assert len(result.parents) == 6, f"父块数: {len(result.parents)}"
        assert result.parents[0].text.startswith("# 第一章")
        assert result.parents[1].text.startswith("## 1.1 背景")
        assert result.parents[3].text.startswith("# 第二章")
        assert result.parents[5].text.startswith("## 2.2 实施计划")
        # 子块尺寸受 chunk_size 约束；overlap 续接块 = tail + 分隔符 + 新段，
        # 分隔符（最长 \n\n 2 字符）计入长度，故上界为 chunk_size + 2
        assert all(len(c.text) <= 32 for c in result.children), \
            f"子块超长: {max(len(c.text) for c in result.children)}"

    def test_child_parent_map_by_position(self):
        """子块 char_start 落在哪个父块区间 → child_parent_map 正确"""
        chunker = _pc_chunker()
        result = chunker.chunk_parent_child(PC_TEXT)
        for i, child in enumerate(result.children):
            p = result.child_parent_map[i]
            assert 0 <= p < len(result.parents), f"映射越界: {i}->{p}"
            parent = result.parents[p]
            # 子块起始偏移必须落在其归属父块区间内
            assert parent.char_start <= child.char_start < parent.char_end, \
                f"子块 {i} 起始 {child.char_start} 不在父块 {p} 区间"
        # 父块 3（# 第二章）区间内的子块全部归属 3
        p3 = result.parents[3]
        in_p3 = [i for i, c in enumerate(result.children)
                 if p3.char_start <= c.char_start < p3.char_end]
        assert in_p3, "第二章下应有子块"
        assert all(result.child_parent_map[i] == 3 for i in in_p3)

    def test_child_crossing_parent_boundary_goes_to_start_parent(self):
        """跨父块边界的子块（起始在父块 A、结尾进入父块 B 区域）归起始父块

        直接测 _map_children_to_parents 静态映射规则（含空隙回退分支）：
        - 起始在父块 A 区间内 → 归 A（即使 text 延伸到 B 的区间）
        - 起始落在父块间空白空隙 → 归其前最近父块
        - 起始在首个父块之前且无父块可归 → 归首个父块
        """
        parents = [Chunk("父块A", 0, 100), Chunk("父块B", 102, 200)]
        mapping = ParentChildChunker._map_children_to_parents(
            [Chunk("跨边界子块", 90, 120)], parents)
        assert mapping == {0: 0}, "起始在父块 A 内的子块应归父块 A"
        mapping = ParentChildChunker._map_children_to_parents(
            [Chunk("在父块B内", 102, 130)], parents)
        assert mapping == {0: 1}
        # 起始落在空隙 (100, 102)：归其前最近父块 A
        mapping = ParentChildChunker._map_children_to_parents(
            [Chunk("空隙子块", 101, 101)], parents)
        assert mapping == {0: 0}
        # 无父块（空父块列表）→ -1
        mapping = ParentChildChunker._map_children_to_parents(
            [Chunk("孤儿", 5, 8)], [])
        assert mapping == {0: -1}

    def test_child_before_first_parent_goes_to_first(self):
        """起始偏移早于所有父块的子块（无前最近父块）→ 归首个父块"""
        parents = [Chunk("父块A", 10, 50), Chunk("父块B", 52, 90)]
        mapping = ParentChildChunker._map_children_to_parents(
            [Chunk("起始过早", 5, 8)], parents)
        assert mapping == {0: 0}

    def test_no_heading_all_children_to_first_parent(self):
        """无标题纯文本：父块=整段一块，全部子块归首个父块"""
        plain = "没有标题的纯文本内容。" * 10
        chunker = ParentChildChunker(chunk_size=30, overlap=0)
        result = chunker.chunk_parent_child(plain)
        assert len(result.parents) == 1, f"父块数: {len(result.parents)}"
        assert result.parents[0].text == plain
        assert len(result.children) > 1, "长文本应切出多个子块"
        assert all(result.child_parent_map[i] == 0
                   for i in range(len(result.children)))

    def test_offsets_slice_correct_for_children_and_parents(self):
        """父块/子块偏移正确：text 与全文切片一致（同基准原文）"""
        chunker = _pc_chunker()
        result = chunker.chunk_parent_child(PC_TEXT)
        _assert_offsets(result.children, PC_TEXT)
        _assert_offsets(result.parents, PC_TEXT)

    def test_protocol_chunk_and_lazy_result(self):
        """chunk() 协议入口返回子块；get_parent_chunks/get_mapping 先切后读"""
        chunker = _pc_chunker()
        children = chunker.chunk(PC_TEXT)
        assert len(children) > 0
        assert len(chunker.get_parent_chunks()) == 6
        assert len(chunker.get_mapping()) == len(children)
        # 未执行切块就读取 → RuntimeError
        with pytest.raises(RuntimeError):
            ParentChildChunker().get_parent_chunks()
        with pytest.raises(RuntimeError):
            ParentChildChunker().get_mapping()

    def test_factory_parent_child(self):
        """get_chunker 工厂：parent_child → ParentChildChunker，参数透传"""
        chunker = get_chunker("parent_child", {
            "chunk_size": 50, "overlap": 0,
            "parent_chunk_size": 300, "parent_chunk_overlap": 20,
            "parent_split_level": 2,
        })
        assert isinstance(chunker, ParentChildChunker)
        assert chunker._child_splitter.chunk_size == 50
        assert chunker.parent_split_level == 2
        assert chunker.child_split_level == 2
        # 兜底切分器（仅超长单节 >50_000 字符时按 parent_chunk_size 二次切分）
        assert chunker._fallback_splitter.chunk_size == 300
        assert chunker._fallback_splitter.overlap == 20


class TestParentChildIngest:
    """parent_child 入库 API 集成：配置持久化 / 详情偏移 / 检索 / 校验 400"""

    PC_BODY = {
        "method": "parent_child",
        "chunk_size": 50, "overlap": 5,
        "parent_chunk_size": 300, "parent_chunk_overlap": 10,
        "parent_split_level": 2, "retrieval_mode": "parent",
    }

    def test_ingest_full_params_and_detail_offsets(self, client,
                                                   mock_embedding,
                                                   admin_headers):
        """method=parent_child 带完整参数：入库成功，配置持久化，
        详情 chunks 全部偏移与 full_text 切片一致"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="父子分块.md",
                         content=PC_API_TEXT)
        resp = _ingest(client, kb["id"], doc["id"], body=self.PC_BODY,
                       headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_id"] == "parent_child"
        cfg = final["parser_config"]
        assert cfg["parent_chunk_size"] == 300
        assert cfg["parent_chunk_overlap"] == 10
        assert cfg["parent_split_level"] == 2
        assert cfg["retrieval_mode"] == "parent"
        # 详情：full_text 非空且与上传内容一致（txt/md plain 直读保真）
        detail = _get_doc(client, kb["id"], doc["id"], admin_headers)
        assert detail["full_text"] == PC_API_TEXT
        assert len(detail["chunks"]) > 0
        assert len(detail["chunks"]) == detail["chunk_count"]
        # 抽查全部 chunk：偏移非负且 text 与全文切片一致
        for c in detail["chunks"]:
            assert 0 <= c["char_start"] <= c["char_end"] <= len(PC_API_TEXT), \
                f"偏移越界: {c['char_start']}..{c['char_end']}"
            assert PC_API_TEXT[c["char_start"]:c["char_end"]] == c["text"], \
                f"chunk 文本与偏移切片不一致: {c['text'][:20]!r}"
        # chunk_preview 兼容保留（限 20 条）
        assert "chunk_preview" in detail
        assert len(detail["chunk_preview"]) == min(20, len(detail["chunks"]))

    def test_retrieval_parent_mode_has_parent_text(self, client,
                                                   mock_embedding,
                                                   admin_headers):
        """retrieval_mode=parent：检索命中 source.parent_text 非空且为原文片段"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="父子分块.md",
                         content=PC_API_TEXT)
        _ingest(client, kb["id"], doc["id"], body=self.PC_BODY,
                headers=admin_headers)
        wait_for_status(client, kb["id"], doc["id"])
        sources = _retrieve(client, kb["id"], "技术选型的依据是什么",
                            admin_headers)
        assert sources, "入库后检索应至少命中一条"
        with_parent = [s for s in sources if s.get("parent_text")]
        assert with_parent, "parent 模式检索应携带父块上下文"
        s = with_parent[0]
        assert s["parent_text"] in PC_API_TEXT, "parent_text 应为原文片段"
        # 父块=完整章节：以标题行开头（章节短于 chunk_size 时子块即父块，文本可相同）
        assert s["parent_text"].lstrip().startswith("#"), \
            "parent_text 应为完整章节（含标题行）"

    def test_retrieval_child_mode_no_parent_text(self, client,
                                                 mock_embedding,
                                                 admin_headers):
        """retrieval_mode=child：重跑入库后检索 source.parent_text 为 None"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="父子分块.md",
                         content=PC_API_TEXT)
        body = {**self.PC_BODY, "retrieval_mode": "child"}
        _ingest(client, kb["id"], doc["id"], body=body, headers=admin_headers)
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["parser_config"]["retrieval_mode"] == "child"
        sources = _retrieve(client, kb["id"], "技术选型的依据是什么",
                            admin_headers)
        assert sources, "入库后检索应至少命中一条"
        assert all(s.get("parent_text") is None for s in sources), \
            "child 模式不应携带父块上下文"

    @pytest.mark.parametrize("body,msg", [
        ({"method": "parent_child", "parent_chunk_size": 100},
         "parent_chunk_size"),   # < 200
        ({"method": "parent_child", "parent_chunk_size": 5000},
         "parent_chunk_size"),   # > 4000
        ({"method": "parent_child", "parent_chunk_overlap": 501},
         "parent_chunk_overlap"),  # > 500
        ({"method": "parent_child", "parent_split_level": 7},
         "parent_split_level"),   # > 6
        ({"method": "parent_child", "retrieval_mode": "both"},
         "retrieval_mode"),       # 非法值
    ])
    def test_parent_child_params_invalid_400(self, client, mock_embedding,
                                             admin_headers, body, msg):
        """父块参数/检索模式越界 → 同步 400，任务不启动（状态不变）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        resp = _ingest(client, kb["id"], doc["id"], body=body,
                       headers=admin_headers)
        assert resp.status_code == 400
        assert msg in resp.text
        # 同步校验失败：文档仍 uploaded，未进入任务
        assert _get_doc(client, kb["id"], doc["id"], admin_headers)[
            "status"] == "uploaded"

    def test_reingest_keeps_parent_child_config(self, client,
                                                mock_embedding,
                                                admin_headers):
        """重跑不传 body：parser_id 仍 parent_child，父块配置沿用"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="父子分块.md",
                         content=PC_API_TEXT)
        body = {"method": "parent_child", "parent_chunk_size": 300,
                "parent_split_level": 2}
        resp = _ingest(client, kb["id"], doc["id"], body=body,
                       headers=admin_headers)
        assert resp.status_code == 200, resp.text
        first = wait_for_status(client, kb["id"], doc["id"])
        assert first["parser_id"] == "parent_child"
        assert first["parser_config"]["parent_chunk_size"] == 300
        # 第二次无 body → 沿用 parent_child 配置，而非回退默认 naive
        resp = _ingest(client, kb["id"], doc["id"], headers=admin_headers)
        assert resp.status_code == 200, resp.text
        second = wait_for_status(client, kb["id"], doc["id"])
        assert second["status"] == "ingested"
        assert second["parser_id"] == "parent_child"
        assert second["parser_config"]["parent_chunk_size"] == 300
        assert second["parser_config"]["parent_split_level"] == 2

    def test_parent_text_truncated_to_meta_limit(self, client, mock_embedding,
                                                 admin_headers):
        """超长章节父块入库：Chroma metadata parent_text 按 8000 截断防超限
        （父块=完整章节无上限后可能数千字，入库截断为 8000，展示侧另截 2000）"""
        from backend.services.vector_store import get_vector_store
        # 单章 > 8000 字符（父块=完整章节，无大小上限）
        content = "# 长章\n\n" + "长章节内容。" * 1500
        assert len(content) > 8000
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="长章节.md",
                         content=content)
        resp = _ingest(client, kb["id"], doc["id"], body={
            "method": "parent_child", "chunk_size": 200, "overlap": 0,
            "parent_chunk_size": 300, "parent_split_level": 1,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        wait_for_status(client, kb["id"], doc["id"])
        metas = get_vector_store()._get_collection(kb["id"]).get(
            where={"document_id": doc["id"]},
            include=["metadatas"])["metadatas"]
        assert metas, "入库后应有向量"
        for m in metas:
            assert m["parent_text"].startswith("# 长章"), \
                "父块文本应为完整章节开头"
            assert len(m["parent_text"]) <= 8000, \
                f"parent_text 应截断到 8000: {len(m['parent_text'])}"

    def test_naive_metadata_no_parent_fields(self, client, mock_embedding,
                                             admin_headers):
        """naive 文档：Chroma metadata 无 parent 字段，详情 chunks 偏移仍在"""
        from backend.services.vector_store import get_vector_store
        content = "普通文档内容。" * 30
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="普通文档.md",
                         content=content)
        resp = _ingest(client, kb["id"], doc["id"], body={
            "method": "naive", "chunk_size": 200, "overlap": 0,
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final["parser_id"] == "naive"
        # Chroma metadata：无 parent 字段，但 char_start/char_end 保留
        vec = get_vector_store()
        metas = vec._get_collection(kb["id"]).get(
            where={"document_id": doc["id"]},
            include=["metadatas"])["metadatas"]
        assert metas, "naive 入库后应有向量"
        for m in metas:
            assert "parent_text" not in m, "naive 不应有 parent_text"
            assert "parent_chunk_index" not in m, "naive 不应有 parent_chunk_index"
            assert "retrieval_mode" not in m, "naive 不应有 retrieval_mode"
            assert "char_start" in m and "char_end" in m
        # 详情 chunks 偏移仍在（与全文切片一致）
        detail = _get_doc(client, kb["id"], doc["id"], admin_headers)
        assert detail["full_text"] == content
        assert detail["chunks"], "详情应有 chunks"
        for c in detail["chunks"]:
            assert content[c["char_start"]:c["char_end"]] == c["text"]
