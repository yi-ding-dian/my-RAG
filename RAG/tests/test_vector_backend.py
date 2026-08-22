"""向量存储可插拔后端测试（VectorBackend 抽象 + Chroma 实现 + 门面工厂）

覆盖：
- 工厂：默认 chroma；未知后端 ValueError；milvus 提示"尚未接入"（接口就位，
  实现待接入）
- 门面委托：get_vector_store() 返回 VectorStore 且内部后端为 Chroma；add/
  search/count/delete_by_document/update_metadata/get_all/drop_collection
  行为与原先语义一致（随机 kb collection 隔离，不污染真实数据）
"""
from __future__ import annotations

import uuid

import pytest

from backend.services.vector_store import (
    ChromaVectorBackend, VectorBackend, VectorStore, _create_backend,
    get_vector_store)


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


def test_factory_milvus_not_implemented_hint():
    """milvus 后端未接入：应给出明确接入指引而非静默失败"""
    with pytest.raises(RuntimeError, match="尚未接入"):
        _create_backend("milvus")


def test_facade_chroma_roundtrip():
    """门面走 Chroma 后端：入库/检索/计数/删除/清库完整语义"""
    kb = _new_kb()
    vs = get_vector_store()
    chunks = ["第一条内容", "第二条内容", "第三条内容"]
    emb = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
    ]
    vs.add(kb, "doc1", "测试文档.md", chunks, emb)
    assert vs.count(kb) == 3
    assert vs.get_embedding_dimension(kb) == 4

    # 检索：查向量 [1,0,0,0] 应命中"第一条内容"（余弦 1.0）
    hits = vs.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=3)
    assert hits, "应命中至少一条"
    assert hits[0][1] == "第一条内容"
    assert hits[0][3] > 0.9
    # id 格式 doc_{i}
    assert hits[0][0] == "doc1_0"
    assert hits[0][2]["document_id"] == "doc1"

    # metadata 过滤：where 等值过滤
    hits_active = vs.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=3,
                            where={"doc_active": True})
    assert len(hits_active) == 3
    hits_ghost = vs.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=3,
                           where={"doc_active": False})
    assert hits_ghost == []

    # update_metadata：软删标志生效
    assert vs.update_metadata(kb, "doc1", doc_active=False)
    assert vs.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=3,
                     where={"doc_active": True}) == []
    assert vs.search(kb, [1.0, 0.0, 0.0, 0.0], top_k=3,
                     where={"doc_active": False}), "软删后应命中 False 过滤"
    # 恢复
    assert vs.update_metadata(kb, "doc1", doc_active=True)

    # get_all（BM25 用）：全部文本+元数据
    all_data = vs.get_all(kb)
    assert len(all_data) == 3
    assert ("doc1_0", "第一条内容") in [(i, t) for i, t, _ in all_data]

    # delete_by_document：删除后剩 0，collection 保留
    vs.delete_by_document(kb, "doc1")
    assert vs.count(kb) == 0
    assert vs.get_embedding_dimension(kb) is None
    assert vs.get_all(kb) == []

    # drop_collection：级联清库原子化
    vs.add(kb, "doc1", "测试文档.md", chunks, emb)
    vs.drop_collection(kb)
    assert vs.count(kb) == 0


def test_unknown_collection_operations_are_noop():
    """collection 不存在的合法态：count 0 / search 空 / update 视为成功"""
    kb = _new_kb()
    vs = get_vector_store()
    assert vs.count(kb) == 0
    assert vs.search(kb, [1.0, 0.0, 0.0, 0.0]) == []
    assert vs.update_metadata(kb, "ghost_doc", doc_active=False) is True
