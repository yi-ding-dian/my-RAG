"""全局文档管理 API：/api/admin/documents（super_admin 或 dept_admin）

- GET ""：跨部门/知识库查询所有文档（部门 → 知识库 → 文档层级组装），
  支持可选过滤 department_id / kb_id / status / keyword（文件名模糊），
  先过滤后分页（page/page_size，默认 50，上限 200），total 为过滤后数量。
- 删除/重命名不在此模块重复实现：前端直接调现有
  /kbs/{kb_id}/documents/{doc_id}/rename 与 DELETE（软删）——
  can_manage_kb 对 super_admin 恒放行、对 dept_admin 限本部门
  （deps.can_manage_kb），无需新接口。

数据组装方案：
- 文档元数据：data/documents/{doc_id}.json（DocumentService.list_all 全局扫描，
  每文档带 kb_id）；知识库：MySQL kbs 表（department_id）；部门：departments 表。
- 一次取全部 kbs 与部门建映射（量级小，与 kb 列表全量返回一致），
  再逐文档组装 kb_name / department_id / department_name；文档的 kb 已删除
  （正常不会发生，delete_kb 级联）时 kb_name 回退 kb_id 兜底，不报错。

权限（require_super_or_dept_admin）：
- super_admin：全量（可带任意 department_id / kb_id 筛选）。
- dept_admin：强制 department_id = 自身部门（覆盖/忽略请求参数，防越权
  探测其他部门数据）；未归属部门（department_id 为空）→ 403。
- user：403（与部门内文档列表的 404 伪装策略不同——本接口是管理员
  专属管理面，不暴露存在性信息价值）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import require_super_or_dept_admin
from backend.models.user_models import UserPublic
from backend.services import department_service
from backend.services.document_service import get_document_service
from backend.services.kb_service import get_kb_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/documents", tags=["超管文档管理"])

# 状态筛选合法值（与 documents.py 的 _VALID_LIST_STATUS 语义完全一致：
# parsed 为历史中间态归入「待解析」；unparsed 映射 uploaded+parsed 两态；
# pending_confirm=Agentic 超限待确认，归入「失败」筛选组）
_VALID_LIST_STATUS = {"uploaded", "parsing", "parsed", "ingested", "failed",
                      "pending_confirm", "unparsed", "all"}

# 未分配部门的分组标识（department_id 为 null 的知识库）
UNASSIGNED_DEPT_KEY = "__unassigned__"


def _match_status(doc_status: str, status: str) -> bool:
    """状态筛选匹配（unparsed = uploaded + parsed，failed 组含
    pending_confirm 待确认，与部门内文档列表语义一致）"""
    if status in ("uploaded", "unparsed"):
        return doc_status in ("uploaded", "parsed")
    if status == "failed":
        return doc_status in ("failed", "pending_confirm")
    return doc_status == status


@router.get("")
async def list_admin_documents(
    department_id: Optional[str] = Query(None, description="部门 ID 过滤"),
    kb_id: Optional[str] = Query(None, description="知识库 ID 过滤"),
    status: Optional[str] = Query(None, description="状态筛选（uploaded/parsing/parsed/ingested/failed/unparsed/all）"),
    keyword: Optional[str] = Query(None, description="文件名模糊搜索"),
    page: Optional[int] = Query(None, description="页码（>=1，缺省 1）"),
    page_size: Optional[int] = Query(None, description="每页条数（1~200，缺省 50）"),
    db: AsyncSession = Depends(get_db),
    user: UserPublic = Depends(require_super_or_dept_admin),
):
    """跨部门全部文档查询（super_admin 全量 / dept_admin 限本部门）

    dept_admin 强制 department_id = 自身部门（覆盖请求参数，防越权探测），
    未归属部门 → 403。响应契约: {total, page, page_size, items:
    [文档字段 + kb_name, department_id, department_name]}；先过滤后分页，
    total 为过滤后数量（语义与部门内文档列表一致，避免"筛选只作用于当前页"
    的误导）。默认按创建时间倒序（与部门内列表一致）。
    """
    if user.role == "dept_admin":
        if not user.department_id:
            raise HTTPException(
                status_code=403,
                detail="部门管理员未归属部门，无法查看文档")
        department_id = user.department_id  # 强制覆盖，忽略请求参数

    if status:
        if status not in _VALID_LIST_STATUS:
            raise HTTPException(
                status_code=400,
                detail=f"非法状态筛选: {status}"
                f"（支持: uploaded/parsing/parsed/ingested/failed/"
                f"pending_confirm/unparsed/all）")
        if status == "all":
            status = None  # all = 全部，等同不传

    docs = get_document_service().list_all()
    if status:
        docs = [d for d in docs if _match_status(d.status, status)]
    if kb_id:
        docs = [d for d in docs if d.kb_id == kb_id]

    # 知识库与部门映射（量级小全量加载；kb 已删除的文档 kb_name 回退 kb_id）
    kbs = {k.id: k for k in await get_kb_service().list(db)}
    dept_names = {d.id: d.name
                  for d in await department_service.list_departments(db)}
    if department_id:
        target = department_id if department_id != UNASSIGNED_DEPT_KEY else None
        docs = [d for d in docs
                if (kbs.get(d.kb_id).department_id if d.kb_id in kbs
                    else None) == target]

    keyword = (keyword or "").strip().lower()
    if keyword:
        docs = [d for d in docs if keyword in d.original_name.lower()]

    # 组装：文档字段 + kb_name / department_id / department_name
    items = []
    for d in docs:
        kb = kbs.get(d.kb_id)
        item = d.model_dump(mode="json")
        item["kb_id"] = d.kb_id
        item["kb_name"] = kb.name if kb else d.kb_id
        item["department_id"] = kb.department_id if kb else None
        item["department_name"] = (
            dept_names.get(kb.department_id) if kb and kb.department_id else None)
        items.append(item)

    # 分页（先过滤后分页；page_size 上限 200，与部门内列表一致）
    size = min(page_size or 50, 200)
    total = len(items)
    start = (max(page or 1, 1) - 1) * size
    return {
        "total": total,
        "page": page or 1,
        "page_size": size,
        "items": items[start:start + size],
    }
