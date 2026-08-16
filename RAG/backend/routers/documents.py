"""文档管理 API：/api/kbs/{kb_id}/documents

- POST /upload         multipart 上传（UUID 文件名，原名存 JSON，原文件存对象存储）
- POST /{doc_id}/ingest  触发后台入库（asyncio.create_task；可带切块参数，
  method/chunk_size/overlap/delimiter/split_level/regex_pattern，不传用默认或沿用上次）
- GET 列表 / 详情（切块预览限 20 条；返回 parser_id/parser_config 供前端展示解析方式）
- DELETE              删元数据 + 存储对象 + uploads/parsed 文件 + 向量

权限矩阵：
- 上传 / 入库 / 删除 = can_manage_kb（否则 403）
- 列表 / 详情 = can_access_kb（无权限 404 伪装）
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import get_db
from backend.deps import get_current_user, kb_or_404
from backend.models.rag_models import (ChunkInfo, DocumentDetail,
                                       DocumentItem, IngestRequest,
                                       RenameDocumentRequest, UrlImportRequest)
from backend.models.user_models import UserPublic
from backend.services import audit_service
from backend.services.document_service import (SUPPORTED_EXTS,
                                               get_document_service)
from backend.services.ingestion_service import (get_ingestion_service,
                                                resolve_parser_config,
                                                resolve_parser_engine)
from backend.services.kb_service import get_kb_service
from backend.services.parser_probe import probe_parsers
from backend.services.retrieval_service import get_retrieval_service
from backend.services.storage_service import get_storage_service
from backend.services.vector_store import get_vector_store
from backend.services.web_importer import (WebFetchError, build_filename,
                                           fetch_webpage)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/kbs/{kb_id}/documents", tags=["文档"])

_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB
_MAX_PREVIEW_PDF_BYTES = 50 * 1024 * 1024  # PDF 在线预览上限 50MB

# 文档列表状态筛选合法值（空/缺省 = 全部；parsed 为历史中间态，归入
# 「待解析」；unparsed 为前端「未入库」筛选 value，映射 uploaded+parsed 两态）
_VALID_LIST_STATUS = {"uploaded", "parsing", "parsed", "ingested", "failed",
                      "unparsed", "all"}


def _get_doc_or_404(kb_id: str, doc_id: str) -> DocumentItem:
    doc = get_document_service().get_by_kb(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


async def _refresh_kb_stats(db: AsyncSession, kb_id: str):
    """上传/删除文档后持久化 kb 计数（列表/详情仍实时算，双保险）"""
    doc_svc = get_document_service()
    await get_kb_service().refresh_stats(
        db, kb_id, doc_svc.count_by_kb(kb_id), doc_svc.chunk_count_by_kb(kb_id))


@router.post("/upload", response_model=DocumentItem)
async def upload_document(request: Request, kb_id: str,
                          file: UploadFile = File(...),
                          db: AsyncSession = Depends(get_db),
                          user: UserPublic = Depends(get_current_user)):
    """上传文档（txt/md/pdf/docx），初始状态 uploaded（can_manage_kb）"""
    await kb_or_404(db, kb_id, user, manage=True)
    original_name = file.filename or "unnamed"
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if f".{ext}" not in SUPPORTED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: .{ext}（支持 {sorted(SUPPORTED_EXTS)}）")

    # 分块读取，限制大小
    content = b""
    while chunk := await file.read(8 * 1024 * 1024):
        content += chunk
        if len(content) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件超过 100MB 限制")

    # 创建元数据（UUID 内部文件名），原文件写对象存储 + 本地副本（ingest 解析 / 存储不可用 fallback）
    doc_svc = get_document_service()
    doc = doc_svc.create(
        kb_id=kb_id, original_name=original_name, size=len(content))
    storage = get_storage_service()
    try:
        await storage.upload_bytes(
            f"uploads/{doc.name}", content, content_type=file.content_type)
    except Exception as e:
        doc_svc.delete(doc.id)  # 存储写入失败 → 回滚元数据
        raise HTTPException(status_code=502, detail=f"文件存储失败: {e}")
    upload_path = doc_svc.get_upload_path(doc)
    # P2-11: 本地副本写盘放线程池，避免阻塞事件循环（大文件上传时卡住其他请求）
    await asyncio.to_thread(upload_path.write_bytes, content)
    await _refresh_kb_stats(db, kb_id)
    await audit_service.record_action(
        user, action="doc.upload", target_type="doc",
        target_id=doc.id, target_name=original_name,
        detail={"size": len(content), "file_type": f".{ext}"}, request=request)
    logger.info("文档上传: %s (%s) %d 字节", original_name, doc.id, len(content))
    return doc


@router.post("/{doc_id}/ingest")
async def ingest_document(request: Request, kb_id: str, doc_id: str,
                          req: Optional[IngestRequest] = None,
                          db: AsyncSession = Depends(get_db),
                          user: UserPublic = Depends(get_current_user)):
    """触发入库（后台任务），防重复触发（can_manage_kb）

    请求体可选（IngestRequest，全字段可缺省）：
    - 传了 method/参数 → 本次入库使用并持久化到文档元数据（重跑沿用）
    - 不传 → 沿用文档已有 parser_config，都没有用活跃配置默认（naive）
    同步校验（400）：method 非法 / regex 无 pattern / chunk_size 超范围；
    任务内校验失败写回 status=failed + error（双保险，防路由绕过）
    """
    await kb_or_404(db, kb_id, user, manage=True)
    doc = _get_doc_or_404(kb_id, doc_id)
    doc_svc = get_document_service()
    ing_svc = get_ingestion_service()
    if ing_svc.is_running(doc_id):
        return {"message": "入库任务执行中", "status": doc.status, "doc_id": doc_id}
    if not doc_svc.is_ingestable(doc_id):
        raise HTTPException(status_code=409, detail=f"当前状态不可触发入库: {doc.status}")

    # 同步预校验（参数解析与任务内同源；失败 → 400，任务不会启动）
    params = req.model_dump(exclude_none=True) if req else {}
    try:
        _, resolved_cfg = resolve_parser_config(doc, params.get("method"),
                                                params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 解析前可用性检测（并行探测 ≤8s，仅 pdf/docx 且引擎可能用外部解析器时）：
    # 所选解析器不可用 → 响应带 degrade 提示（前端 warning），探测结果随任务
    # 传递（_probe），任务内复用不重复探测，按降级链自动切换
    # （deepdoc→mineru→plain / mineru→plain），降级说明记录到
    # parser_config["degrade"]（随文档元数据返回）
    probe = None
    degrade = None
    file_type = (doc.file_type or "").lower().lstrip(".")
    # 引擎联动（与任务内 _ingest 同源，见 resolve_parser_engine）：显式引擎优先，
    # auto+DeepDOC→deepdoc / auto+PlainText→plain（plain 纯文本直提无需探测）
    engine = resolve_parser_engine(resolved_cfg)
    if engine in ("auto", "mineru", "deepdoc") and file_type in ("pdf", "docx"):
        probe = await probe_parsers()
        if engine == "deepdoc":
            if not probe["deepdoc"]["available"]:
                degrade = (f"DeepDoc 服务不可用（{probe['deepdoc']['reason']}），"
                           f"将自动切换 MinerU/纯文本解析")
        elif not probe["mineru"]["available"]:
            degrade = (f"MinerU 服务不可用（{probe['mineru']['reason']}），"
                       f"将自动切换纯文本解析")

    asyncio.create_task(ing_svc.run_ingestion(doc_id, **params, _probe=probe))
    await audit_service.record_action(
        user, action="doc.ingest", target_type="doc",
        target_id=doc_id, target_name=doc.original_name,
        detail={"method": params.get("method") or doc.parser_id or "naive"},
        request=request)
    logger.info("触发入库: %s (%s) method=%s", doc.original_name, doc_id,
                params.get("method") or doc.parser_id or "naive")
    resp = {"message": "入库任务已启动", "status": "parsing", "doc_id": doc_id}
    if degrade:
        resp["degrade"] = degrade
    return resp


@router.post("/from-url", response_model=DocumentItem)
async def import_from_url(request: Request, kb_id: str, req: UrlImportRequest,
                          db: AsyncSession = Depends(get_db),
                          user: UserPublic = Depends(get_current_user)):
    """URL 网页导入为文档（can_manage_kb）

    抓取约束：仅 http/https；超时 30s；浏览器 UA；响应体 >5MB 拒绝。
    提取 <title>/首个 <h1> 做文件名（截断 80、重名加序号），正文纯文本
    落盘为 .md（内部名 UUID），file_type="url"；状态 uploaded，
    由用户在列表选择解析方式后点击解析入库（与文件上传一致）。
    抓取失败（超时/4xx/网络错误/非 http）→ 400 中文错误。
    """
    await kb_or_404(db, kb_id, user, manage=True)
    url = (req.url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="仅支持 http/https 网址导入")
    try:
        title, text = await fetch_webpage(url)
    except WebFetchError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("URL 导入异常 %s: %s", url, str(e)[:200])
        raise HTTPException(status_code=400, detail=f"网页导入失败: {e}")

    doc_svc = get_document_service()
    existing = [d.original_name for d in doc_svc.list_by_kb(kb_id)]
    filename = build_filename(title, url, existing)
    content = text.encode("utf-8")
    doc = doc_svc.create(kb_id=kb_id, original_name=filename,
                         size=len(content), file_type="url")
    storage = get_storage_service()
    try:
        await storage.upload_bytes(
            f"uploads/{doc.name}", content, content_type="text/markdown")
    except Exception as e:
        doc_svc.delete(doc.id)  # 存储写入失败 → 回滚元数据
        raise HTTPException(status_code=502, detail=f"文件存储失败: {e}")
    upload_path = doc_svc.get_upload_path(doc)
    # P2-11: 本地副本写盘放线程池，避免阻塞事件循环
    await asyncio.to_thread(upload_path.write_bytes, content)
    await _refresh_kb_stats(db, kb_id)
    await audit_service.record_action(
        user, action="doc.from-url", target_type="doc",
        target_id=doc.id, target_name=filename,
        detail={"url": url}, request=request)
    logger.info("URL 导入文档: %s (%s) %d 字节", filename, doc.id, len(content))
    return doc


@router.post("/{doc_id}/rename", response_model=DocumentItem)
async def rename_document(request: Request, kb_id: str, doc_id: str,
                          req: RenameDocumentRequest,
                          db: AsyncSession = Depends(get_db),
                          user: UserPublic = Depends(get_current_user)):
    """重命名文档（can_manage_kb）

    只改展示名 original_name（内部存储名/向量/chunk 不变，改名即时生效，
    列表/详情引用跟着变；历史会话里的引用是落盘快照，不回溯）。
    校验（400）：1~255 字符；扩展名必须与原文件一致（无扩展名自动补，
    带了不同扩展名报错）；同知识库内与其他文档重名报错。
    """
    await kb_or_404(db, kb_id, user, manage=True)
    # 先取旧名（rename 原地修改共享元数据对象，必须提前拷贝字符串）
    old_name = _get_doc_or_404(kb_id, doc_id).original_name
    try:
        doc = get_document_service().rename_document(kb_id, doc_id, req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit_service.record_action(
        user, action="doc.rename", target_type="doc",
        target_id=doc_id, target_name=doc.original_name,
        detail={"old_name": old_name}, request=request)
    return doc


@router.get("")
async def list_documents(kb_id: str, page: Optional[int] = Query(None),
                         page_size: Optional[int] = Query(None),
                         status: Optional[str] = Query(None),
                         db: AsyncSession = Depends(get_db),
                         user: UserPublic = Depends(get_current_user)):
    """文档列表（can_access_kb，无权限 404 伪装；P2-10 服务端分页）

    - 不传 page/page_size（或 page_size=0）→ 返回全量数组（旧调用兼容）
    - 传 page（>=1）与 page_size（>0，上限 200）→ 返回
      {total, page, page_size, items}，page 越界返回空 items
    - status 可选状态筛选（uploaded/parsing/parsed/ingested/failed/
      unparsed/all，空或缺省 = 全部；parsed 历史中间态与 unparsed 筛选
      均归入「待解析/未入库」两态），先过滤后分页：total 为过滤后数量
      （前端不再本地筛选，避免"筛选只作用于当前页"的误导）
    """
    await kb_or_404(db, kb_id, user)
    docs = get_document_service().list_by_kb(kb_id)
    if status:
        if status not in _VALID_LIST_STATUS:
            raise HTTPException(
                status_code=400,
                detail=f"非法状态筛选: {status}"
                       f"（支持: uploaded/parsing/parsed/ingested/failed/"
                       f"unparsed/all）")
        if status != "all":
            if status in ("uploaded", "unparsed"):
                # 「待解析/未入库」= uploaded + 历史「已解析」中间态，
                # 两者均可触发入库解析（前端筛选 value 用 unparsed）
                docs = [d for d in docs if d.status in ("uploaded", "parsed")]
            else:
                docs = [d for d in docs if d.status == status]
    if page is None or page_size is None or page_size <= 0:
        return docs
    size = min(page_size, 200)
    total = len(docs)
    start = (max(page, 1) - 1) * size
    return {
        "total": total,
        "page": page,
        "page_size": size,
        "items": docs[start:start + size],
    }


def _purge_graph_refs(kb_id: str, doc_id: str) -> None:
    """彻底删除文档时清理知识图谱中该文档的实体/关系引用（失败仅 warning 不阻塞）

    - 引用移除（chunk_refs 清空、count 重算、无引用的实体/关系删除，复用
      remove_doc_refs）+ docs 条目移除；无任何变化不落盘（不产生空文件）
    - 软删除（回收站）不清——恢复免重建，与 MinIO 对象清理逻辑一致
    - 图谱文件不存在时 load_graph 返回空结构、remove 0 条 → 不 save
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
    """purge 的纯同步部分（Chroma 向量删除 + BM25 失效 + 图谱引用清理
    + 元数据/文件删除）

    放线程池执行（asyncio.to_thread）：空回收站批量 purge 时同步阻塞
    不占用事件循环，其他请求（列表/检索）不被卡住。
    """
    get_vector_store().delete_by_document(kb_id, doc_id)
    # 向量删除后 count 变化，BM25 自动重建（显式失效双保险）
    get_retrieval_service().invalidate_bm25(kb_id)
    # 图谱引用清理（失败仅 warning，不阻塞删除主流程）
    _purge_graph_refs(kb_id, doc_id)
    return get_document_service().delete(doc_id)


async def _purge_document(kb_id: str, doc_id: str):
    """彻底删除（purge）：存储对象 + 向量 + 元数据 + 本地文件全清"""
    doc = get_document_service().get(doc_id)
    storage = get_storage_service()
    try:
        await storage.delete(f"uploads/{doc.name}")
        await storage.delete_prefix(f"images/{doc_id}/")
    except Exception as e:
        logger.warning("删除存储对象失败 %s: %s", doc_id, str(e)[:150])
    ok = await asyncio.to_thread(_purge_local, kb_id, doc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="文档不存在")


# 注意：/trash 必须声明在 /{doc_id} 之前，否则 "trash" 会被当成 doc_id 捕获
@router.get("/trash")
async def list_trash(kb_id: str, page: Optional[int] = Query(None),
                     page_size: Optional[int] = Query(None),
                     db: AsyncSession = Depends(get_db),
                     user: UserPublic = Depends(get_current_user)):
    """回收站列表（can_manage_kb；deleted=true 的文档，含删除时间；P2-10 分页可选）

    分页语义与文档列表一致：不传参数返回全量数组，传参返回
    {total, page, page_size, items}。
    """
    await kb_or_404(db, kb_id, user, manage=True)
    docs = get_document_service().list_trash(kb_id)
    if page is None or page_size is None or page_size <= 0:
        return docs
    size = min(page_size, 200)
    total = len(docs)
    start = (max(page, 1) - 1) * size
    return {
        "total": total,
        "page": page,
        "page_size": size,
        "items": docs[start:start + size],
    }


@router.post("/trash/empty")
async def empty_trash(request: Request, kb_id: str,
                      db: AsyncSession = Depends(get_db),
                      user: UserPublic = Depends(get_current_user)):
    """清空回收站：批量彻底删除（can_manage_kb；成功记审计含删除数量）"""
    await kb_or_404(db, kb_id, user, manage=True)
    docs = get_document_service().list_trash(kb_id)
    for doc in docs:
        await _purge_document(kb_id, doc.id)
    await _refresh_kb_stats(db, kb_id)
    await audit_service.record_action(
        user, action="doc.trash-empty", target_type="kb",
        target_id=kb_id, target_name=None,
        detail={"count": len(docs)}, request=request)
    logger.info("清空回收站: kb=%s 彻底删除 %d 个文档", kb_id, len(docs))
    return {"message": "回收站已清空", "count": len(docs)}


@router.get("/{doc_id}/raw")
async def get_document_raw(kb_id: str, doc_id: str,
                           db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(get_current_user)):
    """文档原始内容预览/下载（can_access_kb）

    - pdf：返回原始字节（application/pdf，浏览器原生渲染）；上限 50MB（413）
    - txt/md/url：返回 UTF-8 文本（text/plain；url=导入时抓取的 md 文本）
    - docx：P1-5 返回原始字节下载（application/octet-stream +
      Content-Disposition attachment，文件名 original_name，前端"可下载后查看"）
    - 其他类型暂不支持（400）
    - 回收站文档不可预览（404 伪装）
    """
    await kb_or_404(db, kb_id, user)
    doc = _get_doc_or_404(kb_id, doc_id)
    if doc.deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    file_type = (doc.file_type or "").lower()
    if file_type not in ("pdf", "txt", "md", "url", "docx"):
        raise HTTPException(status_code=400,
                            detail="该文件类型暂不支持在线预览")
    if file_type == "pdf" and doc.size > _MAX_PREVIEW_PDF_BYTES:
        raise HTTPException(status_code=413,
                            detail="PDF 超过 50MB，暂不支持在线预览")
    storage = get_storage_service()
    try:
        data = await storage.read_bytes(f"uploads/{doc.name}")
    except Exception as e:
        logger.warning("预览读取失败 %s: %s", doc_id, str(e)[:150])
        raise HTTPException(status_code=500, detail="文件读取失败，请稍后重试")
    if file_type == "pdf":
        return Response(content=data, media_type="application/pdf")
    if file_type == "docx":
        # 附件下载：文件名取展示名（RFC 5987 编码，含中文）
        filename = quote(doc.original_name)
        return Response(
            content=data, media_type="application/octet-stream",
            headers={
                "Content-Disposition":
                    f"attachment; filename*=UTF-8''{filename}"})
    # txt/md/url：网页/文本内容（二进制解码容错，非法字节替换）
    return Response(content=data.decode("utf-8", errors="replace"),
                    media_type="text/plain; charset=utf-8")


@router.get("/{doc_id}", response_model=DocumentDetail)
async def get_document(kb_id: str, doc_id: str,
                       db: AsyncSession = Depends(get_db),
                       user: UserPublic = Depends(get_current_user)):
    """文档详情

    - chunks: 完整切块列表 [{text, index, char_start, char_end}]（来源 chunks_meta，
      偏移相对 full_text；历史数据无 chunks_meta 时用 chunk_preview 兜底，偏移 -1）
    - full_text: 解析后全文（data/parsed/{doc_id}.md，入库时写的是替换图片引用
      后的文本，偏移以该文本为基准）；chunk_preview 保留兼容
    """
    await kb_or_404(db, kb_id, user)
    doc = _get_doc_or_404(kb_id, doc_id)
    if doc.chunks_meta:
        chunks = [ChunkInfo(text=c.get("text", ""), index=i,
                            char_start=c.get("char_start", -1),
                            char_end=c.get("char_end", -1),
                            context=c.get("context"))
                  for i, c in enumerate(doc.chunks_meta)]
    else:
        # 历史数据（无 chunks_meta）：chunk_preview 兜底，偏移未知（-1）
        chunks = [ChunkInfo(text=t, index=i) for i, t in enumerate(doc.chunk_preview)]
    full_text = ""
    parsed_path = get_document_service().get_parsed_path(doc)
    try:
        if parsed_path.exists():
            full_text = parsed_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("读取解析文本失败 %s: %s", doc_id, str(e)[:150])
    return DocumentDetail(**doc.model_dump(mode="json"), chunks=chunks,
                          full_text=full_text)


@router.delete("/{doc_id}")
async def delete_document(request: Request, kb_id: str, doc_id: str,
                          db: AsyncSession = Depends(get_db),
                          user: UserPublic = Depends(get_current_user)):
    """软删除：移入回收站（can_manage_kb；成功记审计）

    标记 deleted + 更新向量 metadata（doc_active=False）+ 失效 BM25 索引，
    检索自动排除；向量/存储/切块全部保留，恢复无需重新解析。
    彻底删除请走 POST /{doc_id}/purge（回收站内操作，防误删）。
    """
    await kb_or_404(db, kb_id, user, manage=True)
    doc = _get_doc_or_404(kb_id, doc_id)
    if doc.deleted:
        raise HTTPException(status_code=409, detail="文档已在回收站")
    # 先更新向量标志再标记元数据：失败则中止，保持两侧一致
    if not get_vector_store().update_metadata(kb_id, doc_id, doc_active=False):
        raise HTTPException(status_code=500, detail="向量状态更新失败，请稍后重试")
    get_document_service().soft_delete(doc_id)
    # 软删不改变向量 count，BM25 索引不会自动失效，必须显式失效
    get_retrieval_service().invalidate_bm25(kb_id)
    await _refresh_kb_stats(db, kb_id)
    await audit_service.record_action(
        user, action="doc.delete", target_type="doc",
        target_id=doc_id, target_name=doc.original_name, request=request)
    logger.info("文档移入回收站: %s (%s)", doc.original_name, doc_id)
    return {"message": "文档已移入回收站（可恢复）", "doc_id": doc_id}


@router.post("/{doc_id}/restore", response_model=DocumentItem)
async def restore_document(request: Request, kb_id: str, doc_id: str,
                           db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(get_current_user)):
    """恢复回收站文档（can_manage_kb）：取消 deleted 标记 + 向量 doc_active=True

    向量/切块保留，恢复后立即重新进入检索，无需重新解析。
    """
    await kb_or_404(db, kb_id, user, manage=True)
    doc = _get_doc_or_404(kb_id, doc_id)
    if not doc.deleted:
        raise HTTPException(status_code=409, detail="文档不在回收站")
    if not get_vector_store().update_metadata(kb_id, doc_id, doc_active=True):
        raise HTTPException(status_code=500, detail="向量状态更新失败，请稍后重试")
    restored = get_document_service().restore(doc_id)
    get_retrieval_service().invalidate_bm25(kb_id)
    await _refresh_kb_stats(db, kb_id)
    await audit_service.record_action(
        user, action="doc.restore", target_type="doc",
        target_id=doc_id, target_name=restored.original_name, request=request)
    logger.info("恢复文档: %s (%s)", restored.original_name, doc_id)
    return restored


@router.post("/{doc_id}/purge")
async def purge_document(request: Request, kb_id: str, doc_id: str,
                         db: AsyncSession = Depends(get_db),
                         user: UserPublic = Depends(get_current_user)):
    """彻底删除（can_manage_kb）：存储对象 + 向量 + 元数据 + 本地文件全清

    仅回收站内文档操作（前端入口），删除后不可恢复。
    """
    await kb_or_404(db, kb_id, user, manage=True)
    doc = _get_doc_or_404(kb_id, doc_id)
    await _purge_document(kb_id, doc_id)
    await _refresh_kb_stats(db, kb_id)
    await audit_service.record_action(
        user, action="doc.purge", target_type="doc",
        target_id=doc_id, target_name=doc.original_name, request=request)
    return {"message": "文档已彻底删除", "doc_id": doc_id}
