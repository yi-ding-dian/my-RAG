"""文档生命周期编排：跨存储的一致性操作

从 `routers/documents/crud.py` 下沉。这些操作要**同时改多个存储**，顺序上有
一致性约束，放在路由层会随端点复制而漂移：四个可见性端点（软删/恢复/禁用/
启用）原本各写一遍同一个三段式，其中 `enable_document` 的"回收站优先"特例
只出现在一处、没有名字——复制它写批量版时极易漏掉。

- `set_deleted` / `set_enabled`：可见性切换。二者是同一个二维状态
  `(deleted, enabled)` 的两个维度，向量侧统一为
  `doc_active = (not deleted) and enabled`（这个公式只在此文件出现）
- `purge_document`：彻底删除（对象存储 + 向量 + 图谱引用 + 元数据/本地文件）
- `check_doc_quota`：知识库文档数配额（上传与 URL 导入共用）

**不抛 HTTPException**：service 层不碰 HTTP（全 backend/services 仅
rate_limit.py 一个中间件例外），调用方按异常类型转状态码。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from backend.config import get_active_config
from backend.models.rag_models import DocumentItem
from backend.services.document.service import get_document_service
from backend.services.knowledge_graph.service import (load_graph,
                                                      remove_doc_refs,
                                                      save_graph)
from backend.services.retrieval_service import get_retrieval_service
from backend.services.storage_service import get_storage_service
from backend.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)


class DocLifecycleError(RuntimeError):
    """文档生命周期操作失败（调用方按具体类型转 HTTP 状态码）"""


class VectorSyncError(DocLifecycleError):
    """向量 metadata 更新失败 → 调用方转 500。

    此时元数据**尚未改动**，两侧保持一致，调用方中止即可。
    """


class QuotaExceeded(DocLifecycleError):
    """知识库文档数超配额 → 调用方转 400"""


async def set_deleted(kb_id: str, doc_id: str,
                      deleted: bool) -> Optional[DocumentItem]:
    """软删除（deleted=True）/ 恢复（deleted=False）

    恢复**不会把"禁用"洗掉**：可见性取 `(not deleted) and enabled`，
    被禁用的文档恢复后仍不参与检索。

    返回更新后的文档；文档不存在返回 None。
    """
    doc_svc = get_document_service()
    doc = doc_svc.get(doc_id)
    if doc is None:
        return None
    if not await get_vector_store().update_metadata(
            kb_id, doc_id, doc_active=(not deleted) and doc.enabled):
        raise VectorSyncError(f"向量状态更新失败: {doc_id}")
    result = doc_svc.soft_delete(doc_id) if deleted else doc_svc.restore(doc_id)
    # 可见性变了但向量 count 没变，BM25 不会自动失效，必须显式
    get_retrieval_service().invalidate_bm25(kb_id)
    logger.info("%s文档: %s (%s)", "软删除" if deleted else "恢复",
                doc.original_name, doc_id)
    return result


async def set_enabled(kb_id: str, doc_id: str,
                      enabled: bool) -> Optional[DocumentItem]:
    """启用（enabled=True）/ 禁用（enabled=False）检索

    **回收站优先级更高**：文档在回收站时"启用"只记标志，向量保持不可见
    （`(not deleted) and enabled` 中 deleted 为真），避免"启用"绕过回收站
    语义；出回收站时由 set_deleted(False) 按 enabled 自动激活。

    返回 / 异常语义同 set_deleted。
    """
    doc_svc = get_document_service()
    doc = doc_svc.get(doc_id)
    if doc is None:
        return None
    if not await get_vector_store().update_metadata(
            kb_id, doc_id, doc_active=enabled and not doc.deleted):
        raise VectorSyncError(f"向量状态更新失败: {doc_id}")
    result = doc_svc.set_enabled(doc_id, enabled)
    get_retrieval_service().invalidate_bm25(kb_id)
    logger.info("%s检索: %s (%s)", "启用" if enabled else "禁用",
                doc.original_name, doc_id)
    return result


def check_doc_quota(kb_id: str, action: str = "上传") -> None:
    """知识库文档数配额校验（kb_doc_limit>0 时启用；0=不限）

    防"单库无限膨胀 / 多用户上传耗尽磁盘"。超限抛 QuotaExceeded；
    action 仅用于文案（上传 / 导入各一处调用）。
    """
    limit = get_active_config().ingestion.kb_doc_limit
    if limit and limit > 0 and \
            get_document_service().count_by_kb(kb_id) >= limit:
        raise QuotaExceeded(
            f"知识库文档数已达上限（{limit} 个），"
            f"请删除部分文档或调大配额后再{action}")


def _purge_graph_refs(kb_id: str, doc_id: str) -> None:
    """清理知识图谱中该文档的实体/关系引用（失败仅 warning 不阻塞）

    - 引用移除（chunk_refs 清空、count 重算、无引用的实体/关系删除，复用
      remove_doc_refs）+ docs 条目移除；无任何变化不落盘（不产生空文件）
    - 软删除（回收站）不清——恢复免重建，与 MinIO 对象清理逻辑一致
    """
    try:
        graph = load_graph(kb_id)
        removed = remove_doc_refs(graph, doc_id)
        had_doc = doc_id in graph.get("docs", {})
        if removed or had_doc:
            graph["docs"].pop(doc_id, None)
            save_graph(kb_id, graph)
    except Exception as e:
        logger.warning("清理知识图谱文档引用失败（不阻塞删除）: %s err=%s",
                       doc_id, str(e)[:150])


def _purge_local(kb_id: str, doc_id: str) -> bool:
    """purge 的纯同步部分（向量删除 + BM25 失效 + 图谱引用清理 + 元数据/文件删除）

    放线程池执行（asyncio.to_thread）：空回收站批量 purge 时同步阻塞
    不占用事件循环，其他请求（列表/检索）不被卡住。
    """
    get_vector_store().sync.delete_by_document(kb_id, doc_id)
    # 向量删除后 count 变化，BM25 自动重建（显式失效双保险）
    get_retrieval_service().invalidate_bm25(kb_id)
    _purge_graph_refs(kb_id, doc_id)
    return get_document_service().delete(doc_id)


async def purge_document(kb_id: str, doc_id: str) -> bool:
    """彻底删除：存储对象 + 向量 + 元数据 + 本地文件 + 图谱引用全清

    返回 False 表示元数据不存在（调用方转 404）。对象存储删除失败仅
    warning——本地文件与向量/元数据仍要清干净，不因存储不可用而残留。
    """
    doc = get_document_service().get(doc_id)
    if doc is None:
        return False
    storage = get_storage_service()
    try:
        await storage.delete(f"uploads/{doc.name}")
        await storage.delete_prefix(f"images/{doc_id}/")
    except Exception as e:
        logger.warning("删除存储对象失败 %s: %s", doc_id, str(e)[:150])
    return await asyncio.to_thread(_purge_local, kb_id, doc_id)
