"""Milvus 真实路径集成测试（真连服务 + 临时 collection，用完即删）

**为什么需要这组测试**：日常单测跑的是 Chroma 后端，Milvus 侧的 schema、字段名、
anns_field 这类问题**只有真连才暴露**。2026-09-16 就出过一次事故——迁到"稠密+稀疏
双向量字段"的 schema 后，`search()` 没指定 `anns_field`，Milvus 报 1100 错、异常被
防御性 except 吞掉，**向量检索整条静默失效、只剩 BM25 在扛**；因为 BM25 结果本身
合理，只看检索结果完全看不出异常，翻服务日志才发现。

连不上 Milvus（或未配置 milvus 后端）时自动 skip，不影响日常离线测试。
临时 collection 一律用 it_ 前缀，测试结束即删，不碰真实 kb_* 数据。

跑法：
    .venv/bin/python -m pytest tests/test_milvus_integration.py -v
    # 指定非默认实例：
    MILVUS_TEST_URI=http://host:19530 .venv/bin/python -m pytest -m milvus
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from backend.services.vector_store import (
    SPARSE_FIELD, TEXT_FIELD, VECTOR_FIELD, MilvusVectorBackend,
    normalize_milvus_uri)

# 测试用小维度：这组测试验证的是"字段/接口接得对不对"，不是召回质量，维度小更快
DIM = 8
VEC_A = [1.0] + [0.0] * (DIM - 1)
VEC_B = [0.0, 1.0] + [0.0] * (DIM - 2)

pytestmark = pytest.mark.milvus

DOCS = [
    ("第一条：防误闭锁系统的三权分立要求", VEC_A),
    ("第二条：IEC104 规约的特性包括 RTU 通讯状态", VEC_B),
]


def _milvus_uri() -> str:
    """取 Milvus 地址：优先环境变量 MILVUS_TEST_URI，否则读项目真实的 settings.json

    不能走 get_active_config()——测试的 _isolated_env 会把 DATA_DIR 重定向到临时
    目录并重建默认档案（chroma），读不到线上实际在用的配置。
    """
    env = os.environ.get("MILVUS_TEST_URI")
    if env:
        return normalize_milvus_uri(env)
    settings_file = Path(__file__).resolve().parent.parent / "data" / "settings.json"
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except Exception:
        return ""
    for profile in data.get("profiles") or []:
        if profile.get("id") != data.get("active_id"):
            continue
        vs = profile.get("vector_store") or {}
        if vs.get("backend") == "milvus" and vs.get("milvus_uri"):
            return normalize_milvus_uri(vs["milvus_uri"])
    return ""


@pytest.fixture(scope="module")
def milvus_backend():
    """真连 Milvus 的后端实例；未配置/连不上 → skip 整个模块

    注意 MilvusClient 构造时就会发起连接，构造本身也要包在 try 里——否则实例
    不可达时是 ERROR（fixture 崩溃）而不是 skip，离线环境下整套测试会红。
    """
    uri = _milvus_uri()
    if not uri:
        pytest.skip("未配置 milvus 后端（可设 MILVUS_TEST_URI 指定实例），跳过集成测试")
    try:
        backend = MilvusVectorBackend(uri=uri)
        backend._client.list_collections()  # 探活
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Milvus 不可用（{str(e)[:80]}），跳过集成测试")
    return backend


@pytest.fixture()
def temp_kb(milvus_backend):
    """本测试专用的临时知识库 id（it_ 前缀），结束即删"""
    kb_id = "it_" + uuid.uuid4().hex[:8]
    yield kb_id
    try:
        milvus_backend.drop_collection(kb_id)
    except Exception:  # noqa: BLE001
        pass


def _seed(backend: MilvusVectorBackend, kb_id: str) -> None:
    """写入两条中文数据并 flush（Milvus 写入有可见性延迟，不 flush 会读到旧状态）"""
    backend.add(kb_id, "doc_it", "集成测试.txt",
                [t for t, _ in DOCS],
                [v for _, v in DOCS],
                metadatas=[{"document_id": "doc_it", "chunk_index": 0},
                           {"document_id": "doc_it", "chunk_index": 1}])
    backend._client.flush(f"kb_{kb_id}")


# ==================== 能力与 schema ====================

def test_declares_full_text_capability(milvus_backend):
    assert milvus_backend.supports_full_text is True


def test_collection_has_expected_schema(milvus_backend, temp_kb):
    """建表产出显式字段：text（挂分词器）+ 稠密/稀疏两个向量字段 + BM25 Function"""
    milvus_backend.ensure_collection(temp_kb, DIM)
    desc = milvus_backend._client.describe_collection(f"kb_{temp_kb}")
    fields = {f["name"] for f in desc.get("fields", [])}
    assert {VECTOR_FIELD, TEXT_FIELD, SPARSE_FIELD} <= fields
    assert desc.get("functions"), "应注册 BM25 Function（由 text 生成稀疏向量）"


# ==================== 两条检索路径都要真的能跑（回归重点） ====================

def test_vector_search_works_with_two_vector_fields(milvus_backend, temp_kb):
    """回归：schema 有两个向量字段时，向量检索必须显式指定 anns_field 才能工作

    2026-09-16 事故：`search()` 漏了 anns_field → Milvus 报 "multiple anns_fields
    exist" → 异常被吞 → 向量候选永远为空，检索静默退化成纯 BM25。本用例若失败，
    说明该路径又断了。
    """
    _seed(milvus_backend, temp_kb)
    hits = milvus_backend.search(temp_kb, VEC_A, top_k=2)
    assert hits, "向量检索在双向量字段 schema 下必须能返回命中"
    assert hits[0][0] == "doc_it_0"
    assert hits[0][3] > 0.9, "VEC_A 与自身余弦相似度应接近 1"
    assert hits[0][2].get("document_id") == "doc_it", "metadata 应随命中返回"


def test_full_text_search_works(milvus_backend, temp_kb):
    """BM25 全文检索：中文分词与倒排索引在服务端生效"""
    _seed(milvus_backend, temp_kb)
    hits = milvus_backend.search_full_text(temp_kb, "防误闭锁", top_k=2)
    assert hits, "中文关键词应能命中（分词器失效时这里会空）"
    assert hits[0][0] == "doc_it_0"
    assert hits[0][3] > 0, "BM25 分数应为正"


def test_both_paths_return_hits(milvus_backend, temp_kb):
    """双路同时有命中 = RRF 融合有料可融（任一路挂了都会在这里暴露）"""
    _seed(milvus_backend, temp_kb)
    vector_hits = milvus_backend.search(temp_kb, VEC_A, top_k=2)
    text_hits = milvus_backend.search_full_text(temp_kb, "IEC104", top_k=2)
    assert vector_hits and text_hits


# ==================== 过滤与 metadata 契约 ====================

def test_soft_delete_filter_applies_to_both_paths(milvus_backend, temp_kb):
    """软删过滤对两条路径都生效（where 语义一致，字段缺失即不匹配）"""
    _seed(milvus_backend, temp_kb)
    assert milvus_backend.update_metadata(temp_kb, "doc_it", doc_active=False)
    milvus_backend._client.flush(f"kb_{temp_kb}")

    where = {"doc_active": True}
    assert milvus_backend.search(temp_kb, VEC_A, top_k=2, where=where) == []
    assert milvus_backend.search_full_text(temp_kb, "防误闭锁", top_k=2,
                                           where=where) == []
    # 不过滤时仍能读到（数据在，只是被过滤）
    assert milvus_backend.search_full_text(temp_kb, "防误闭锁", top_k=2)


def test_metadata_roundtrip_and_count(milvus_backend, temp_kb):
    """写入的 metadata 原样读回、count 与维度检测正确"""
    _seed(milvus_backend, temp_kb)
    rows = milvus_backend.get_all(temp_kb)
    assert len(rows) == 2
    metas = [m for _, _, m in rows]
    assert all(m.get("doc_active") is True for m in metas), "写入侧必带 doc_active"
    assert {m.get("chunk_index") for m in metas} == {0, 1}
    assert milvus_backend.count(temp_kb) == 2
    assert milvus_backend.get_embedding_dimension(temp_kb) == DIM
