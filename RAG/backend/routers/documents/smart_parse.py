"""智能解析引导：文档画像分析接口（薄适配层）

- GET /api/kbs/{kb_id}/documents/{doc_id}/analyze（can_manage_kb）：
  对文档做规则画像分析——未解析文档也能分析（轻量本地文本提取：
  txt/md 直读、pdf 用 pypdf、docx 用 python-docx、doc 经 LibreOffice 转
  docx，不调 MinerU/DeepDoc 外部服务），返回画像（格式/引擎建议/标题结构/
  篇幅/QA/指代密集度）+ **入库方案 parse_plan**，供前端 4 步引导向导
  （SmartParseWizard）展示。确定后由向导把 parse_plan.config 提交给现有
  POST /{doc_id}/ingest 解析（ingest 接口零改动）。

**业务逻辑全在 backend/services/smart_parse/ 包里**（画像 / 引擎建议 /
决策矩阵 / 成本预估 / 编排），本文件只做四件事：权限校验 → 取文件路径 →
注入解析器探测 → 调 build_analyze 并序列化。

**probe_parsers 为什么在本模块调用**：解析器探测是网络 IO，且测试通过
monkeypatch 本模块的 probe_parsers 打桩。把**函数引用**（而非调用结果）传给
build_analyze，补丁路径与重构前完全一致。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import get_current_user, kb_or_404
from backend.models.user_models import UserPublic
from backend.services.document_service import get_document_service
from backend.services.parsers.probe import probe_parsers
from backend.services.smart_parse.build import build_analyze

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/kbs/{kb_id}/documents", tags=["智能解析"])


def _get_doc_or_404(kb_id: str, doc_id: str):
    doc = get_document_service().get_by_kb(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


@router.get("/{doc_id}/analyze")
async def analyze_document(request: Request, kb_id: str, doc_id: str,
                           db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(get_current_user)):
    """文档画像分析（can_manage_kb；未解析文档也能分析）

    轻量本地文本提取（不调 MinerU/DeepDoc），规则画像 + 入库方案；
    任何一步失败不影响整体（部分画像 + warnings）。
    """
    await kb_or_404(db, kb_id, user, manage=True)
    doc = _get_doc_or_404(kb_id, doc_id)
    file_type = (doc.file_type or "").lower().lstrip(".")
    path = get_document_service().get_upload_path(doc)
    report = await build_analyze(
        doc_id=doc_id, file_type=file_type, path=path,
        doc_name=doc.original_name, probe_fn=probe_parsers)
    return report.to_dict()
