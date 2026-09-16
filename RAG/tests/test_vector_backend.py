"""向量存储可插拔后端测试（VectorBackend 抽象 + Chroma/Milvus 实现 + 门面工厂）

覆盖：
- 工厂：默认 chroma；未知后端 ValueError；milvus 已注册（MockClient 不真连）
- 门面委托：get_vector_store() 返回 VectorStore 且内部后端为 Chroma；add/
  search/count/delete_by_document/update_metadata/get_all/drop_collection
  行为与原先语义一致（随机 kb collection 隔离，不污染真实数据）；
  门面方法为 async（内部 to_thread，不阻塞事件循环）
- **后端契约**（接入新后端时逐条满足，见 VectorBackend 的契约 docstring）：
  能力声明 supports_full_text、不支持的全文检索返回空而非报错、
  ensure_collection 可重复调用（且 add 会隐式建表）、
  where 过滤的"字段缺失即不匹配"语义、写入侧必带 doc_active
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.services.vector_store import (
    ChromaVectorBackend, VectorBackend, MilvusVectorBackend, VectorStore,
    _create_backend, get_vector_store)


def _new_kb() -> str:
    return "vbt_" + uuid.uuid4().hex[:8]


def test_default_backend_is_chroma():
    from backend.config import settings
    assert settings.VECTOR_BACKEND == "chroma"
    vs = get_vector_store()
    assert isinstance(vs, VectorStore)
    assert isinstance(vs._backend, ChromaVectorBackend)
    assert isinstance(vs._backend, VectorBackend)


def test_factory_unknown_backend_raises():
    with pytest.raises(ValueError, match="未知向量后端"):
        _create_backend("walrus")


def test_factory_milvus_registered(monkeypatch):
    """milvus 后端已接入：工厂返回 MilvusVectorBackend（mock client，不真连）"""
    import pymilvus

    fake = object()
    monkeypatch.setattr(pymilvus, "MilvusClient",
                        lambda uri=None, **kw: fake)
    backend = _create_backend("milvus")
    assert isinstance(backend, MilvusVectorBackend)
    assert backend._client is fake


def test_facade_chroma_roundtrip():
    """门面走 Chroma 后端：入库/检索/计数/删除/清库完整语义（async 门面）"""
    kb = _new_kb()
    vs = get_vector_store()
    chunks = ["第一条内容", "第二条内容", "第三条内容"]
    emb = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
    ]

    async def run():
        await vs.add(kb, "doc1", "测试文档.md", chunks, emb)
        assert await vs.count(kb) == 3
        assert await vs.get_embedding_dimension(kb) == 4

        # 检索：查向量 [1,0,0,0] 应命中"第一条内容"（余弦 1.0）
        hits = await vs.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=3)
        assert hits, "应命中至少一条"
        assert hits[0][1] == "第一条内容"
        assert hits[0][3] > 0.9
        # id 格式 doc_{i}
        assert hits[0][0] == "doc1_0"
        assert hits[0][2]["document_id"] == "doc1"

        # metadata 过滤：where 等值过滤
        hits_active = await vs.search(
            kb, [1.0, 0.0, 0.0, 0.0], top_k=3,
            where={"doc_active": True})
        assert len(hits_active) == 3
        hits_ghost = await vs.search(
            kb, [1.0, 0.0, 0.0, 0.0], top_k=3,
            where={"doc_active": False})
        assert hits_ghost == []

        # update_metadata：软删标志生效
        assert await vs.update_metadata(kb, "doc1", doc_active=False)
        assert await vs.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=3,
                               where={"doc_active": True}) == []
        assert await vs.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=3,
                               where={"doc_active": False}), \
            "软删后应命中 False 过滤"
        # 恢复
        assert await vs.update_metadata(kb, "doc1", doc_active=True)

        # get_all（BM25 用）：全部文本+元数据
        all_data = await vs.get_all(kb)
        assert len(all_data) == 3
        assert ("doc1_0", "第一条内容") in [(i, t) for i, t, _ in all_data]

        # delete_by_document：删除后剩 0，collection 保留
        await vs.delete_by_document(kb, "doc1")
        assert await vs.count(kb) == 0
        assert await vs.get_embedding_dimension(kb) is None
        assert await vs.get_all(kb) == []

        # drop_collection：级联清库原子化
        await vs.add(kb, "doc1", "测试文档.md", chunks, emb)
        await vs.drop_collection(kb)
        assert await vs.count(kb) == 0

    asyncio.run(run())


def test_unknown_collection_operations_are_noop():
    """collection 不存在的合法态：count 0 / search 空 / update 视为成功"""
    kb = _new_kb()
    vs = get_vector_store()

    async def run():
        assert await vs.count(kb) == 0
        assert await vs.search(kb, [1.0, 0.0, 0.0, 0.0]) == []
        assert await vs.update_metadata(kb, "ghost_doc", doc_active=False) is True

    asyncio.run(run())


# ==================== 后端契约（接入新后端必须逐条满足） ====================

def test_full_text_capability_declared_per_backend():
    """能力声明：Chroma 无服务端全文检索，Milvus 有（调用方据此选数据源）"""
    assert ChromaVectorBackend.supports_full_text is False
    assert MilvusVectorBackend.supports_full_text is True
    # 门面透传（同步属性：调用方不必 await）
    assert get_vector_store().supports_full_text is False


def test_unsupported_full_text_returns_empty_not_error():
    """不支持的 back：search_full_text 返回空列表（而不是抛异常）→ 调用方降级"""
    kb = _new_kb()
    backend = get_vector_store()._backend
    assert backend.search_full_text(kb, "任意查询", top_k=5) == []
    assert backend.search_full_text(kb, "任意查询", top_k=5,
                                    where={"doc_active": True}) == []


def test_ensure_collection_is_callable_and_idempotent():
    """建表入口在契约内：可重复调用，且 add 会隐式建表（不要求调用方先建）"""
    kb = _new_kb()
    backend = get_vector_store()._backend
    backend.ensure_collection(kb, 4)
    backend.ensure_collection(kb, 4)  # 幂等
    assert backend.count(kb) == 0
    # add 内部会自己确保集合存在（调用方不必先 ensure）
    backend.add(kb, "doc_x", "x.txt", ["内容"], [[1.0, 0.0, 0.0, 0.0]])
    assert backend.count(kb) == 1
    backend.drop_collection(kb)


def test_legacy_rows_without_flag_get_repaired():
    """历史数据缺 doc_active 键：**查询前补齐**，而不是让过滤容忍缺键

    两者结果看着像（都能命中），但语义完全不同：补齐是"把数据修成符合过滤契约
    的样子"，容忍缺键则是把过滤语义放宽——后者会让软删过滤被绕过（缺键数据在
    回收站状态下仍被召回）。这里断言"补齐"：命中结果里必须带 doc_active=True。

    Chroma 走 _ensure_doc_active 惰性补齐；Milvus 侧无缺键历史数据（写入侧与
    迁移脚本都保证带键），故此处只验 Chroma 的行为。
    """
    kb = _new_kb()
    backend = get_vector_store()._backend
    backend.ensure_collection(kb, 4)
    backend._get_collection(kb).add(
        ids=["no_flag_1"], embeddings=[[1.0, 0.0, 0.0, 0.0]],
        documents=["这条数据没有 doc_active 键"],
        metadatas=[{"document_id": "doc_no_flag"}])

    hits = backend.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=5,
                          where={"doc_active": True})
    assert len(hits) == 1, "历史数据应被视为活跃（补齐后命中）"
    assert hits[0][2].get("doc_active") is True, "缺键应在查询前被补齐为 True"
    # 补齐后是 True：doc_active=False 不命中（说明不是"怎么过滤都匹配"）
    assert backend.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=5,
                          where={"doc_active": False}) == []
    backend.drop_collection(kb)


def test_written_rows_always_carry_doc_active():
    """写入侧契约：add 落库的每条数据都必须带 doc_active（不给上面的语义留坑）"""
    kb = _new_kb()
    backend = get_vector_store()._backend
    backend.add(kb, "doc_y", "y.txt", ["甲", "乙"],
                [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
                metadatas=[{"document_id": "doc_y", "chunk_index": 0},
                           {"document_id": "doc_y", "chunk_index": 1}])
    rows = backend.get_all(kb)
    assert len(rows) == 2
    assert all((meta or {}).get("doc_active") is True for _, _, meta in rows), \
        "add 未显式传 doc_active 时应默认补 True"
    backend.drop_collection(kb)
