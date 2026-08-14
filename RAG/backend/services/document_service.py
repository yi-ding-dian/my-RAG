"""文档元数据服务：data/documents/{doc_id}.json 持久化 + 状态机

状态机: uploaded -> parsing -> parsed -> ingested / failed
- parsing  解析中（MinerU/降级提取）
- parsed   解析完成，文本已落盘 data/parsed/（历史流程中间态；
           新流程解析+入库一步完成，parsing 可直接到 ingested）
- ingested 切块+向量化完成
- failed   任一步骤失败，error 写回（可重试触发）
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from backend.config import DOCUMENTS_DIR, PARSED_DIR, UPLOAD_DIR
from backend.models.rag_models import DocumentItem

logger = logging.getLogger(__name__)

# 支持的文档状态
VALID_STATUS = {"uploaded", "parsing", "parsed", "ingested", "failed"}

# 支持的文件扩展名
SUPPORTED_EXTS = {".txt", ".md", ".pdf", ".docx"}

# 服务重启后残留 parsing 状态文档的错误消息（recover_stuck_parsing 写入）
_RECOVER_ERROR = "服务重启，解析中断，请重新解析"

# 状态机合法迁移
_TRANSITIONS = {
    "uploaded": {"parsing"},
    "parsing": {"parsed", "ingested", "failed"},  # ingested: 新流程解析+入库一步完成（parsed 保留兼容历史数据）
    "parsed": {"ingested", "failed"},
    "ingested": {"parsing"},   # 允许重新入库（先清旧向量）
    "failed": {"parsing"},     # 失败后允许重试
}


class DocumentService:

    def __init__(self):
        self._lock = threading.Lock()
        self._docs: Dict[str, DocumentItem] = {}
        self._load_all()

    # ---------- 持久化 ----------

    def _get_meta_path(self, doc_id: str) -> Path:
        return DOCUMENTS_DIR / f"{doc_id}.json"

    def _load_all(self):
        if not DOCUMENTS_DIR.exists():
            return
        for f in DOCUMENTS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                self._docs[data["id"]] = DocumentItem(**data)
            except Exception as e:
                logger.warning("加载文档 %s 失败: %s", f.name, e)

    def _save_meta(self, doc: DocumentItem):
        self._get_meta_path(doc.id).write_text(
            json.dumps(doc.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- 上传 / 查询 ----------

    def create(self, kb_id: str, original_name: str, size: int,
               file_type: Optional[str] = None) -> DocumentItem:
        """创建文档元数据（文件本身由路由层写入 uploads/）

        file_type 缺省由 original_name 扩展名推导；URL 导入等场景可显式
        指定（如 "url"，此时内部文件名仍带 .md 扩展名保证解析链路可用）。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ext = Path(original_name).suffix.lower() or ""
        doc = DocumentItem(
            id=uuid.uuid4().hex[:12],
            kb_id=kb_id,
            name=f"{uuid.uuid4().hex[:12]}{ext}",
            original_name=original_name,
            file_type=file_type if file_type is not None else ext.lstrip("."),
            size=size,
            status="uploaded",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._docs[doc.id] = doc
        self._save_meta(doc)
        return doc

    def list_by_kb(self, kb_id: str,
                   include_deleted: bool = False) -> List[DocumentItem]:
        """知识库文档列表（默认只返回未删除；include_deleted=True 返回全部，
        级联删除知识库/向量重建等场景需要包含回收站文档）"""
        with self._lock:
            docs = [d for d in self._docs.values()
                    if d.kb_id == kb_id and (include_deleted or not d.deleted)]
        return sorted(docs, key=lambda x: x.created_at, reverse=True)

    def list_all(self, include_deleted: bool = False) -> List[DocumentItem]:
        """全部知识库的文档（超管全局文档管理用；默认排除回收站）

        跨部门聚合视图的数据源：文档元数据 JSON 全局扫描，按创建时间倒序
        （与 list_by_kb 排序一致），知识库/部门归属由路由层组装映射。
        """
        with self._lock:
            docs = [d for d in self._docs.values()
                    if include_deleted or not d.deleted]
        return sorted(docs, key=lambda x: x.created_at, reverse=True)

    def list_trash(self, kb_id: str) -> List[DocumentItem]:
        """回收站列表（deleted=true，按删除时间倒序）"""
        with self._lock:
            docs = [d for d in self._docs.values()
                    if d.kb_id == kb_id and d.deleted]
        return sorted(docs, key=lambda x: x.deleted_at or "", reverse=True)

    def get(self, doc_id: str) -> Optional[DocumentItem]:
        with self._lock:
            return self._docs.get(doc_id)

    def get_by_kb(self, kb_id: str, doc_id: str) -> Optional[DocumentItem]:
        doc = self.get(doc_id)
        if doc and doc.kb_id == kb_id:
            return doc
        return None

    def count_by_kb(self, kb_id: str) -> int:
        with self._lock:
            return sum(1 for d in self._docs.values()
                       if d.kb_id == kb_id and not d.deleted)

    def chunk_count_by_kb(self, kb_id: str) -> int:
        with self._lock:
            return sum(d.chunk_count for d in self._docs.values()
                       if d.kb_id == kb_id and not d.deleted
                       and d.status == "ingested")

    def get_upload_path(self, doc: DocumentItem) -> Path:
        return UPLOAD_DIR / doc.name

    def get_parsed_path(self, doc: DocumentItem) -> Path:
        # 内部文件一律 UUID（无扩展名，避免中文与特殊字符路径问题）
        return PARSED_DIR / f"{doc.id}.md"

    # ---------- 状态机 ----------

    def transition(self, doc_id: str, to_status: str, **extra) -> Optional[DocumentItem]:
        """状态迁移校验 + 写回元数据"""
        with self._lock:
            doc = self._docs.get(doc_id)
            if not doc:
                return None
            if to_status not in VALID_STATUS:
                raise ValueError(f"非法状态: {to_status}")
            if to_status not in _TRANSITIONS.get(doc.status, set()):
                raise ValueError(
                    f"非法状态迁移: {doc.status} -> {to_status}（防重复触发）")
            doc.status = to_status
            doc.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if "error" in extra:
                doc.error = extra.pop("error")
            elif to_status != "failed":
                doc.error = None
            for k, v in extra.items():
                setattr(doc, k, v)
        self._save_meta(doc)
        return doc

    def mark_failed(self, doc_id: str, error: str) -> Optional[DocumentItem]:
        return self.transition(doc_id, "failed", error=str(error)[:1000])

    def recover_stuck_parsing(self) -> List[str]:
        """启动恢复：把解析中断（status=parsing）的文档拨回 failed，可重新解析

        后台解析任务用 asyncio.create_task 执行（无持久化），进程重启后
        parsing 状态文档既无法重新解析（is_ingestable 不含 parsing 会 409），
        前端又会每 2s 无限轮询。启动时（lifespan）调用本函数，将这些文档
        统一标记 failed + 明确 error，用户可点击解析直接重试
        （_TRANSITIONS["failed"] 含 parsing，状态机合法）。
        返回被恢复的文档 ID 列表。
        """
        with self._lock:
            stuck = [d for d in self._docs.values() if d.status == "parsing"]
            for doc in stuck:
                doc.status = "failed"
                doc.error = _RECOVER_ERROR
                doc.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for doc in stuck:
            self._save_meta(doc)
        if stuck:
            logger.warning("启动恢复: %d 个解析中断文档已标记失败（可重新解析）",
                           len(stuck))
        return [d.id for d in stuck]

    def is_ingestable(self, doc_id: str) -> bool:
        """防重复触发：仅 uploaded/failed/ingested 可触发 ingest"""
        doc = self.get(doc_id)
        return doc is not None and doc.status in ("uploaded", "failed", "ingested")

    # ---------- 重命名 ----------

    def rename_document(self, kb_id: str, doc_id: str, new_name: str) -> DocumentItem:
        """重命名文档（只改展示名 original_name，内部名/向量/chunk 不变）

        校验（失败抛 ValueError，路由层转 400）：
        - 1~255 字符（strip 后非空）
        - 扩展名保留：无扩展名自动补原扩展名；带了与原文件不同的扩展名报错
        - 重名检测：同知识库内其他文档 original_name 相同报错（企业场景避免混淆）
        """
        doc = self.get_by_kb(kb_id, doc_id)
        if not doc:
            return None
        name = (new_name or "").strip()
        if not name:
            raise ValueError("文件名不能为空")
        if len(name) > 255:
            raise ValueError("文件名不能超过 255 个字符")
        # 扩展名保留：无扩展名自动补；扩展名不一致（如 .txt 改 .md）明确报错
        old_ext = Path(doc.original_name).suffix
        new_ext = Path(name).suffix
        if not new_ext:
            name = f"{name}{old_ext}"
        elif new_ext.lower() != old_ext.lower():
            raise ValueError(
                f"扩展名不能修改：原文件扩展名为 {old_ext or '（无）'}，"
                f"请保留或省略扩展名（将自动补全）")
        # 重名检测（同知识库内，排除自身）
        with self._lock:
            for other in self._docs.values():
                if other.kb_id == kb_id and other.id != doc_id \
                        and other.original_name == name:
                    raise ValueError(f"知识库中已存在同名文档: {name}")
            doc.original_name = name
            doc.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_meta(doc)
        logger.info("文档重命名: %s (%s)", name, doc_id)
        return doc

    # ---------- 软删除 / 恢复 ----------

    def soft_delete(self, doc_id: str) -> Optional[DocumentItem]:
        """软删除：标记 deleted + deleted_at（元数据保留，向量保留，仅检索排除）"""
        with self._lock:
            doc = self._docs.get(doc_id)
            if not doc:
                return None
            if doc.deleted:
                return doc  # 幂等：已在回收站
            doc.deleted = True
            # 毫秒精度：同一秒多次软删也能稳定排序（回收站按删除时间倒序）
            doc.deleted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            doc.updated_at = doc.deleted_at
        self._save_meta(doc)
        logger.info("软删除文档: %s (%s)", doc.original_name, doc_id)
        return doc

    def restore(self, doc_id: str) -> Optional[DocumentItem]:
        """恢复：取消 deleted 标记（无需重新解析，向量保留直接可检索）"""
        with self._lock:
            doc = self._docs.get(doc_id)
            if not doc:
                return None
            if not doc.deleted:
                return doc  # 幂等：非回收站状态
            doc.deleted = False
            doc.deleted_at = None
            doc.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_meta(doc)
        logger.info("恢复文档: %s (%s)", doc.original_name, doc_id)
        return doc

    # ---------- 删除 ----------

    def delete(self, doc_id: str) -> bool:
        """删除元数据 + uploads/parsed 文件（向量删除由路由层调用 vector_store）"""
        with self._lock:
            doc = self._docs.pop(doc_id, None)
        if not doc:
            return False
        meta_path = self._get_meta_path(doc_id)
        if meta_path.exists():
            meta_path.unlink()
        for p in (self.get_upload_path(doc), self.get_parsed_path(doc)):
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                logger.warning("删除文件失败 %s: %s", p, e)
        logger.info("删除文档: %s (%s)", doc.original_name, doc_id)
        return True

    def delete_by_kb(self, kb_id: str) -> List[str]:
        """级联删除某知识库全部文档（含回收站），返回被删文档 ID 列表"""
        ids = [d.id for d in self.list_by_kb(kb_id, include_deleted=True)]
        for doc_id in ids:
            self.delete(doc_id)
        return ids


_document_service: Optional[DocumentService] = None


def get_document_service() -> DocumentService:
    global _document_service
    if _document_service is None:
        _document_service = DocumentService()
    return _document_service
