"""知识图谱查询 API：GET /api/kbs/{kb_id}/graph

- 权限：kb_or_404 读权限（super_admin 全量 / 同部门可读，无权限 404 伪装，
  与文档列表/详情一致）
- doc_id 可选：按单文档过滤（实体=含该文档引用的实体；关系=两端实体都
  在过滤集内才保留；docs 只保留该文档）
- 图谱文件不存在 → 404 中文"该知识库暂无知识图谱"（前端据此显示空状态
  引导"该文档未启用知识图谱，可在解析配置中开启"）
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import get_current_user, kb_or_404
from backend.models.rag_models import KnowledgeGraph
from backend.models.user_models import UserPublic
from backend.services.knowledge_graph_service import (graph_path, load_graph)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/kbs/{kb_id}/graph", tags=["知识图谱"])


def _filter_by_doc(graph: dict, doc_id: str) -> dict:
    """按文档过滤图谱：实体（含该文档引用）、关系（两端都在过滤实体集内）"""
    entity_ids = {
        e["id"] for e in graph.get("entities", [])
        if any(r.get("doc_id") == doc_id for r in e.get("chunk_refs", []))
    }
    entities = [e for e in graph.get("entities", []) if e["id"] in entity_ids]
    relations = [
        r for r in graph.get("relations", [])
        if r.get("source") in entity_ids and r.get("target") in entity_ids
    ]
    docs = {doc_id: graph.get("docs", {}).get(doc_id, {})} \
        if doc_id in graph.get("docs", {}) else {}
    return {"kb_id": graph.get("kb_id", ""),
            "updated_at": graph.get("updated_at", ""),
            "docs": docs, "entities": entities, "relations": relations}


@router.get("", response_model=KnowledgeGraph)
async def get_knowledge_graph(kb_id: str,
                              doc_id: Optional[str] = Query(None, description="按文档过滤（可选）"),
                              db: AsyncSession = Depends(get_db),
                              user: UserPublic = Depends(get_current_user)):
    """查询知识库知识图谱（can_access_kb，无权限 404 伪装）

    - 不带 doc_id：返回全库图谱（docs/entities/relations）
    - 带 doc_id：过滤该文档的实体与关系（文档未构建图谱 → 空结构）
    - 图谱不存在（从未开启知识图谱入库）→ 404"该知识库暂无知识图谱"
    """
    await kb_or_404(db, kb_id, user)
    if not graph_path(kb_id).exists():
        raise HTTPException(status_code=404, detail="该知识库暂无知识图谱")
    graph = load_graph(kb_id)
    if doc_id:
        return _filter_by_doc(graph, doc_id)
    return graph
