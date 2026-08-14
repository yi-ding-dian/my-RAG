"""Embedding 维度状态检测 + 一键重建向量（P0：更换 embedding 模型后的维度冲突）

背景：企业用户更换 embedding 模型（如 bge-m3 1024 维 → 其他维度模型）后，
Chroma collection 里已有旧维度向量，新维度写入/检索会抛错且被吞掉，
问答静默"未检索到相关内容"。本模块提供：
- get_kb_vector_status(kb_id)：检测 collection 实际维度 vs 当前模型实测维度，
  返回 {kb_id, collection_vectors, current_dim, model_dim, compatible, message}
- 重建任务管理（start_rebuild_task / get_rebuild_status / run_rebuild_task）：
  清空 collection 旧向量 → 逐文档重新 embedding（当前激活模型）→ 写回，
  串行执行防内存爆炸，任务状态内存 dict + 落盘 data/rebuild_tasks.json
- VectorDimensionError：检索/入库维度不匹配时的可识别异常（错误信息透传前端）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from backend.config import DATA_DIR, get_active_config
from backend.services import embedding_service as _embedding_module
from backend.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)

_TASK_FILE = DATA_DIR / "rebuild_tasks.json"

# 向量重建后台任务并发上限（默认 2，环境变量 REBUILD_CONCURRENCY 可调）：
# 多知识库同时重建会打爆 embedding API；信号量惰性绑定首次运行事件循环，
# conftest 重置 _rebuild_semaphore 避免跨测试 loop 串用。
_REBUILD_CONCURRENCY = max(1, int(os.environ.get("REBUILD_CONCURRENCY", "2")))
_rebuild_semaphore: Optional[asyncio.Semaphore] = None


def _get_rebuild_semaphore() -> asyncio.Semaphore:
    """惰性获取重建并发信号量（首次调用绑定当前事件循环）"""
    global _rebuild_semaphore
    if _rebuild_semaphore is None:
        _rebuild_semaphore = asyncio.Semaphore(_REBUILD_CONCURRENCY)
    return _rebuild_semaphore


def _chunk_embed_text(c: dict) -> str:
    """chunks_meta 条目 → 向量化文本（与入库一致：有上下文摘要的块用增强文本）

    chunks_meta.text 保持原文（偏移契约），摘要存 context 字段；
    重建/补齐向量时用 "【上下文】摘要\n原文" 与原始入库向量化输入保持一致，
    避免重建后检索语义退化
    """
    ctx = (c.get("context") or "").strip()
    text = c.get("text", "")
    if ctx:
        return f"【上下文】{ctx}\n{text}"
    return text

# ---- 重建任务状态（内存为主，任务完成时落盘，重启后仍可查历史）----
_rebuild_tasks: Dict[str, dict] = {}        # task_id -> 任务状态 dict
_last_task_by_kb: Dict[str, str] = {}       # kb_id -> 最近一次 task_id


class VectorDimensionError(Exception):
    """知识库向量维度与当前模型不匹配（检索/入库时抛出，错误信息可直接展示给用户）"""


# ==================== 模型维度实测（全局缓存） ====================

# 模型维度缓存：配置 key -> 实测维度（embedding 配置变化时 key 变化自动失效）
_model_dim_cache: Dict[str, Optional[int]] = {}

# 维度实测超时（秒）：embedding 服务不可用时不阻塞列表/状态接口
_MODEL_DIM_TEST_TIMEOUT = 10.0


def _model_key() -> str:
    cfg = get_active_config().embedding
    return f"{cfg.base_url}|{cfg.api_key}|{cfg.model}"


def clear_model_dim_cache() -> None:
    """清空模型维度缓存（配置变更或测试用）"""
    _model_dim_cache.clear()


async def get_model_dimension() -> Optional[int]:
    """实测当前激活 embedding 模型输出维度（embed 一条测试文本；失败返回 None）

    - 全局缓存：同一配置只实测一次（避免每次检测都调模型）
    - 失败不抛异常：返回 None，由调用方按"模型未配置/不可用"降级处理
    - 10s 短超时：模型不可用时列表/状态接口不被 60s 长超时拖慢（仅首次）
    """
    key = _model_key()
    if key in _model_dim_cache:
        return _model_dim_cache[key]
    dim: Optional[int] = None
    try:
        # 模块属性引用（而非函数名绑定）：conftest mock_embedding 替换
        # embedding_service 模块属性后此处自动生效（测试离线跑）
        vectors = await asyncio.wait_for(
            _embedding_module.get_embedding_service().embed(["维度检测测试"]),
            timeout=_MODEL_DIM_TEST_TIMEOUT)
        if vectors and vectors[0]:
            dim = len(vectors[0])
        logger.info("Embedding 模型维度实测: %s -> %s", key.split("|")[-1], dim)
    except Exception as e:
        logger.warning("Embedding 模型维度实测失败: %s", str(e)[:150])
    _model_dim_cache[key] = dim
    return dim


# ==================== 维度状态检测 ====================

def _incompatible_message(current_dim: int, model_dim: int) -> str:
    return (f"Embedding 模型维度不匹配（collection {current_dim} 维 vs "
            f"模型 {model_dim} 维），请更换模型或重建向量")


async def get_kb_vector_status(kb_id: str) -> dict:
    """检测知识库向量维度与当前激活模型维度是否兼容

    返回契约: {kb_id, collection_vectors, current_dim, model_dim, compatible, message}
    - compatible: collection 空 / 维度相同 / 模型维度无法检测（未配置或不可用）时 True；
      明确不匹配才 False（避免模型瞬时不可用阻塞问答）
    - dimension 检测失败不抛 500：embedding 调用异常返回 None + 友好 message
    """
    vec = get_vector_store()
    collection_vectors = vec.count(kb_id)
    current_dim = vec.get_embedding_dimension(kb_id)
    model_dim = await get_model_dimension()

    if collection_vectors == 0 or current_dim is None:
        return {
            "kb_id": kb_id,
            "collection_vectors": collection_vectors,
            "current_dim": None,
            "model_dim": model_dim,
            "compatible": True,
            "message": ("知识库暂无向量，维度兼容（入库时将使用当前模型维度）"
                        if collection_vectors == 0
                        else "向量维度未知（collection 为空）"),
        }
    if model_dim is None:
        return {
            "kb_id": kb_id,
            "collection_vectors": collection_vectors,
            "current_dim": current_dim,
            "model_dim": None,
            "compatible": True,
            "message": "Embedding 模型维度检测失败（模型不可用或未配置），无法比对；入库时将实际校验",
        }
    if current_dim == model_dim:
        return {
            "kb_id": kb_id,
            "collection_vectors": collection_vectors,
            "current_dim": current_dim,
            "model_dim": model_dim,
            "compatible": True,
            "message": f"维度匹配（{current_dim} 维）",
        }
    return {
        "kb_id": kb_id,
        "collection_vectors": collection_vectors,
        "current_dim": current_dim,
        "model_dim": model_dim,
        "compatible": False,
        "message": _incompatible_message(current_dim, model_dim),
    }


async def kb_vector_summary(kb_id: str) -> dict:
    """知识库列表/详情附带的轻量摘要（不重复实测模型维度——get_model_dimension 有缓存）"""
    status = await get_kb_vector_status(kb_id)
    return {
        "current_dim": status["current_dim"],
        "model_dim": status["model_dim"],
        "compatible": status["compatible"],
    }


# ==================== 重建任务管理 ====================

def _load_tasks_from_disk() -> None:
    """启动/首次查询时加载历史任务状态（重启后保留最近一次结果）"""
    try:
        if _TASK_FILE.exists():
            data = json.loads(_TASK_FILE.read_text(encoding="utf-8"))
            tasks = data.get("tasks") or {}
            for tid, task in tasks.items():
                if task.get("kb_id"):
                    _last_task_by_kb.setdefault(task["kb_id"], tid)
    except Exception as e:
        logger.warning("加载重建任务历史失败: %s", e)


def _save_tasks_to_disk() -> None:
    """任务完成后落盘（仅保留最近任务，重启丢失可接受的兜底持久化）"""
    try:
        keep = {tid: t for tid, t in _rebuild_tasks.items()
                if t.get("kb_id") in _last_task_by_kb
                and _last_task_by_kb[t["kb_id"]] == tid}
        _TASK_FILE.write_text(
            json.dumps({"tasks": keep}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        logger.warning("保存重建任务历史失败: %s", e)


def start_rebuild_task(kb_id: str) -> str:
    """启动重建任务，返回 task_id（已有 running 任务则复用，幂等防重复触发）"""
    tid = _last_task_by_kb.get(kb_id)
    if tid and _rebuild_tasks.get(tid, {}).get("running"):
        return tid
    task_id = uuid.uuid4().hex[:12]
    _rebuild_tasks[task_id] = {
        "task_id": task_id,
        "kb_id": kb_id,
        "running": True,
        "done": 0,
        "total": 0,
        "failed": 0,
        "current_doc": None,
        "finished_at": None,
        "errors": [],
    }
    _last_task_by_kb[kb_id] = task_id
    logger.info("启动重建向量任务: kb=%s task=%s", kb_id, task_id)
    return task_id


def get_rebuild_status(kb_id: str) -> dict:
    """查询知识库重建任务状态（内存优先，落盘历史兜底）"""
    task_id = _last_task_by_kb.get(kb_id)
    if task_id and task_id in _rebuild_tasks:
        return _rebuild_tasks[task_id]
    if not _rebuild_tasks:
        _load_tasks_from_disk()
    if task_id and task_id in _rebuild_tasks:
        return _rebuild_tasks[task_id]
    return {
        "kb_id": kb_id,
        "task_id": None,
        "running": False,
        "done": 0,
        "total": 0,
        "failed": 0,
        "current_doc": None,
        "finished_at": None,
        "errors": [],
    }


async def _backfill_missing_vectors(kb_id: str, task: dict, vec, emb_svc,
                                    snapshot_ids: set) -> None:
    """增量补齐重建期间新入库文档的向量（快照重建后的收尾阶段调用）

    竞态背景：重建任务启动时生成 ingested 快照，随后 drop 旧 collection；
    此间完成 ingest 的文档，向量写入后随旧 collection 一起被删除，
    且不在快照内 → ingested 但检索永久丢失。收尾时对比当前 DB 中
    ingested 文档集合 vs collection 现有 doc_id 集合，缺失的重新向量化
    写入（文本取自 chunks_meta；软删文档保留 doc_active=False）。
    collection 已有向量（重建期间 ingest 直接写入新 collection）的跳过。
    """
    from backend.services.document_service import get_document_service
    doc_svc = get_document_service()
    all_ingested = [d for d in doc_svc.list_by_kb(kb_id, include_deleted=True)
                    if d.status == "ingested"]
    # total 含增量文档（快照 ⊂ all_ingested，除非重建期间被彻底删除）
    task["total"] = len(all_ingested)
    existing = {meta.get("document_id")
                for _, _, meta in vec.get_all(kb_id)}
    for doc in all_ingested:
        if doc.id in snapshot_ids or doc.id in existing:
            continue
        task["current_doc"] = doc.original_name
        try:
            pairs = [(_chunk_embed_text(c), c) for c in (doc.chunks_meta or [])
                     if c.get("text")]
            if not pairs:
                raise RuntimeError(
                    "增量文档无向量且无 chunks_meta 可重建（可能未走完整入库）")
            texts = [t for t, _ in pairs]
            embeddings = await emb_svc.embed(texts)
            if not embeddings or len(embeddings) != len(texts):
                raise RuntimeError("重新向量化结果为空或数量不一致")
            # 软删文档保持 doc_active=False（与重建快照路径保真语义一致）；
            # parent_text 等父子上下文缺失（chunks_meta 不含父块全文），
            # 检索退化为子块独立命中，可接受（与历史脏数据兜底路径一致）
            metadatas = [{
                "document_id": doc.id,
                "document_name": doc.original_name,
                "chunk_index": i,
                "char_start": c.get("char_start", -1),
                "char_end": c.get("char_end", -1),
                **({"context": c.get("context")} if c.get("context") else {}),
                "doc_active": not doc.deleted,
            } for i, (_, c) in enumerate(pairs)]
            vec.add(kb_id, doc.id, doc.original_name, texts, embeddings,
                    metadatas=metadatas)
            task["done"] += 1
            logger.info("增量补齐向量: kb=%s doc=%s chunks=%d", kb_id,
                        doc.original_name, len(texts))
        except Exception as e:
            task["failed"] += 1
            task["errors"].append({
                "doc_id": doc.id,
                "doc_name": doc.original_name,
                "error": str(e)[:500],
            })
            logger.warning("增量补齐失败: kb=%s doc=%s err=%s", kb_id,
                           doc.original_name, str(e)[:200])
        task["current_doc"] = None


async def run_rebuild_task(kb_id: str, task_id: str) -> None:
    """后台重建任务主流程（路由层 asyncio.create_task 调用，串行防内存爆炸）

    流程：全量拉取 collection 条目（文本+metadata 保留，供重新 embedding）
    → 清空 collection 旧维度向量 → 逐个已入库文档重新 embedding 并写回；
    失败文档跳过继续后续，最后汇总 failed 列表。任务状态实时写入内存 dict。
    并发上限：模块级信号量（REBUILD_CONCURRENCY，默认 2），
    多知识库同时重建不会打爆 embedding API。
    """
    async with _get_rebuild_semaphore():
        await _run_rebuild_locked(kb_id, task_id)


async def _run_rebuild_locked(kb_id: str, task_id: str) -> None:
    """run_rebuild_task 的并发受限主体（快照重建 + 收尾增量补齐）"""
    from backend.services.document_service import get_document_service
    from backend.services.retrieval_service import get_retrieval_service

    task = _rebuild_tasks[task_id]
    doc_svc = get_document_service()
    vec = get_vector_store()
    emb_svc = _embedding_module.get_embedding_service()
    try:
        # 含回收站文档：软删文档的向量需保留（doc_active=False，写回时保真，
        # 恢复后无需重新解析）；只重建已入库的（ingested）
        docs = [d for d in doc_svc.list_by_kb(kb_id, include_deleted=True)
                if d.status == "ingested"]
        task["total"] = len(docs)

        # 0) 清空前先全量拉取条目（文本 + 完整 metadata：parent_text/偏移等保真），
        #    按文档分组；drop 后 collection 为空无法再取
        by_doc: Dict[str, List[tuple]] = {}
        for cid, text, meta in vec.get_all(kb_id):
            by_doc.setdefault(meta.get("document_id", ""), []).append(
                (cid, text, meta))

        # 1) 清空旧向量：维度不一致时无法增量替换（Chroma 单 collection 要求维度一致），
        #    直接 drop 整个 collection 后重建
        vec.drop_collection(kb_id)
        get_retrieval_service().invalidate_bm25(kb_id)

        # 2) 逐文档重新 embedding + 写回（串行执行防内存爆炸）
        for doc in docs:
            task["current_doc"] = doc.original_name
            try:
                pairs = by_doc.get(doc.id, [])
                if pairs:
                    texts = [t for _, t, _ in pairs]
                    metadatas: Optional[List[dict]] = [m for _, _, m in pairs]
                else:
                    # collection 无该文档向量（历史脏数据）：chunks_meta 文本兜底重建
                    # （有上下文摘要的块用增强文本，与入库向量化保持一致）
                    texts = [_chunk_embed_text(c) for c in (doc.chunks_meta or [])
                             if c.get("text")]
                    metadatas = None
                if not texts:
                    raise RuntimeError(
                        "无向量文本可重建（collection 无该文档向量且无 chunks_meta）")
                embeddings = await emb_svc.embed(texts)
                if not embeddings or len(embeddings) != len(texts):
                    raise RuntimeError("重新向量化结果为空或数量不一致")
                vec.add(kb_id, doc.id, doc.original_name, texts, embeddings,
                        metadatas=metadatas)
                task["done"] += 1
                logger.info("重建向量: kb=%s doc=%s chunks=%d", kb_id,
                            doc.original_name, len(texts))
            except Exception as e:
                task["failed"] += 1
                task["errors"].append({
                    "doc_id": doc.id,
                    "doc_name": doc.original_name,
                    "error": str(e)[:500],
                })
                logger.warning("重建向量失败: kb=%s doc=%s err=%s", kb_id,
                               doc.original_name, str(e)[:200])
            task["current_doc"] = None

        # 2.5) 增量补齐：重建快照生成后、旧 collection 被 drop 期间完成 ingest
        #     的文档，其向量已随旧 collection 一起删除且不在快照内
        #     → 重新向量化入库，防"ingested 但检索永久丢失"
        await _backfill_missing_vectors(
            kb_id, task, vec, emb_svc, snapshot_ids={d.id for d in docs})

        # 3) 收尾：BM25 索引失效（重建时已 pop 一次，写回后 count 变化自动重建双保险）
        if task["failed"] == 0 and task["done"] > 0:
            get_retrieval_service().invalidate_bm25(kb_id)
        logger.info("重建向量完成: kb=%s done=%d failed=%d", kb_id,
                    task["done"], task["failed"])
    except Exception as e:
        # 任务级兜底（如 get_all 异常）：任务仍标记结束，信息进 errors
        logger.exception("重建向量任务异常: kb=%s task=%s", kb_id, task_id)
        task["failed"] = task.get("failed", 0) + 1
        task["errors"].append({"doc_id": None, "doc_name": None,
                               "error": f"任务级异常: {str(e)[:500]}"})
    finally:
        task["running"] = False
        task["current_doc"] = None
        task["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_tasks_to_disk()
