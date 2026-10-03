"""文档指纹服务：重复上传识别 / 内容变更判定

**两级指纹各司其职**（实测依据：record.md 2026-10-03 17:15 那条）：

- ``file_hash``（源文件字节 sha256）——只负责抓"**字节完全相同的重复上传**"。
  这是强判断：字节相同则内容必然相同，命中即可确定重复，**无需解析**。
  但它判断不了"内容是否变化"：Office 文档重新保存会让字节全变（实测连
  ``word/document.xml`` 的 hash 都变，差异只是 XML 引号风格），只改
  ``docProps/core.xml`` 时间戳同样让整文件 hash 变。
- ``content_hash``（解析产物文本 sha256）——判断"**实质内容是否变化**"的
  可靠口径，对重新保存、换序列化工具、换压缩方式均稳定（实测项目自己的
  ``parse_docx`` 对四个版本输出完全一致）。代价是必须先解析，因此只用于
  同名文件的待确认流程。

**为什么单独成模块**：重复检测不只在 HTTP 上传接口需要——后续接入外部
文件入库（共享目录同步 / 第三方推送）时是同一套判断。故本模块不依赖路由层，
对外只暴露纯函数与结构化结果，调用方按 ``DupCheck.kind`` 分支即可，
不必复制比对逻辑，也不必解析提示文案字符串。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from backend.models.rag_models import DocumentItem
from backend.services.document_service import get_document_service

logger = logging.getLogger(__name__)

# ---- 判定类型（调用方按此分支，勿用 message 文案做判断）----

#: 字节完全相同的重复上传 → 拦截（强判断，已确定重复）
EXACT_DUP = "exact_dup"
#: 同名且既有文档正在转换/解析 → 拦截（后台任务在跑，此刻上传更新版会撞车）
SAME_NAME_BUSY = "same_name_busy"
#: 同名但字节不同 → 放行，由调用方打 pending_update_of 标记走待确认流程
SAME_NAME = "same_name"
#: 全新文档 → 正常上传
NONE = "none"

#: 同名文档处于这些状态时不允许上传新版本：任务正在跑（转换/解析中），
#: 此时既无法比对内容、也不该并发改写同一文档。注意 uploaded（已传未解析）
#: **不算**——用户可能刚传错想重传，放行走待确认流程更少意外；
#: 此时旧文档没有 content_hash，比对会降级为"无法确认"（verdict=unknown）。
_BUSY_STATUS = {"converting", "parsing"}


def compute_file_hash(content: bytes) -> str:
    """源文件字节 sha256

    上传接口本就要把整个文件读进内存做大小校验，直接对已读入的 content
    计算即可，**不额外读盘**。
    """
    return hashlib.sha256(content).hexdigest()


def compute_content_hash(text: str) -> str:
    """解析产物文本 sha256（判断实质内容是否变化的可靠口径）"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class DupCheck:
    """重复检测结果（结构化返回，避免调用方匹配提示文案）

    - kind      : EXACT_DUP / SAME_NAME_BUSY / SAME_NAME / NONE
    - existing  : 命中的既有文档（NONE 时为 None）
    - message   : 面向用户的提示文案（blocked 时供接口直接回 409 detail）
    - file_hash : 本次上传文件的字节 sha256（调用方直接存进文档元数据，
                  避免再算一遍）
    - blocked   : 是否应直接拒绝本次上传（属性，由 kind 推导）
    """

    kind: str
    existing: Optional[DocumentItem] = None
    message: Optional[str] = None
    file_hash: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return self.kind in (EXACT_DUP, SAME_NAME_BUSY)


def ensure_fingerprints(doc: DocumentItem, doc_svc=None) -> Optional[str]:
    """给"本功能上线前入库的老文档"补算指纹并持久化（一次性）

    补两个，各自只读一个**本地已有产物**，都不需要重新解析：

    - ``file_hash``：读 ``uploads/`` 里的源文件（解析用的同一份本地副本）
    - ``content_hash``：读 ``data/parsed/{id}.md``（入库时已落盘的定稿文本）

    为什么必须补 ``content_hash``：不补的话，老文档第一次遇到同名新版本时比对
    结果只能是 ``unknown``（"无法确认内容是否变化"），用户拿不到"其实没变"这个
    关键判断——而那恰恰是最省钱的结论（选"不更新"就不用重跑 embedding）。

    返回补算后的 file_hash；源文件缺失（对象存储模式下本地副本被清理等历史
    遗留）→ None，此时该文档不参与字节比对，检测降级为"只看文件名"。
    """
    svc = doc_svc or get_document_service()
    result = doc.file_hash
    updates: dict = {}

    if not doc.file_hash:
        try:
            path = svc.get_upload_path(doc)
            if path.exists():
                result = compute_file_hash(path.read_bytes())
                updates["file_hash"] = result
            else:
                logger.info("老文档补算 file_hash 跳过（源文件不存在）: %s", doc.id)
        except Exception as e:  # 补算失败不该阻塞上传，降级为不带 hash
            logger.warning("老文档补算 file_hash 失败 %s: %s", doc.id, str(e)[:150])

    if not doc.content_hash:
        try:
            parsed = svc.get_parsed_path(doc)
            if parsed.exists():
                text = parsed.read_text(encoding="utf-8")
                updates["content_hash"] = compute_content_hash(text)
            else:
                # 未入库（uploaded/failed 等）或历史数据没留产物 → 保持 unknown
                logger.info("老文档补算 content_hash 跳过（无解析产物）: %s", doc.id)
        except Exception as e:
            logger.warning("老文档补算 content_hash 失败 %s: %s",
                           doc.id, str(e)[:150])

    if updates:
        svc.update_doc(doc.id, **updates)
        logger.info("老文档补算指纹: %s -> %s", doc.id,
                    ",".join(sorted(updates)))
    return result


def check_upload(kb_id: str, original_name: str, content: bytes,
                 doc_svc=None) -> DupCheck:
    """上传时的一站式重复检测（**推荐调用方用这个**）

    算新文件 hash + 必要时补算老文档 hash + 判定，一次完成。同步函数
    （含读盘补算），async 调用方请用 ``await asyncio.to_thread(check_upload, ...)``
    包装，避免阻塞事件循环。

    判定顺序（只看同名，同名是最贴近用户意图的信号）：

    1. 同名且既有文档正在转换/解析 → SAME_NAME_BUSY（拦）
    2. 同名且字节完全相同         → EXACT_DUP（拦，无需解析）
    3. 同名但字节不同             → SAME_NAME（放行，走待确认流程）
    4. 无同名                     → NONE（正常上传）

    **刻意不做「改名后同内容」的全局查重**（曾实现后撤回）：那会拦下"同一份
    内容以不同文件名入库"的操作，而这类操作是合理且常见的——空文件/占位文件
    （内容都为空，hash 天然相同）、同一模板按用途各存一份、以及大量既有用法。
    实测一次性打挂 19 个用例，全是"用同内容不同名造数据"这一种用法，
    说明误拦风险远大于它挡住的收益。真要防重复内容，那是"内容去重"另一个
    命题（需相似度而非等值 hash），不该塞进上传链路。

    比对范围**仅限本知识库**：同一份文件放进两个知识库是合理需求
    （如通用制度文件），跨库比对会误伤。回收站文档不参与（可由用户恢复）。
    """
    svc = doc_svc or get_document_service()
    file_hash = compute_file_hash(content)
    docs = svc.list_by_kb(kb_id)  # 默认排除回收站

    same_name = next((d for d in docs if d.original_name == original_name), None)
    if same_name is None:
        return DupCheck(NONE, file_hash=file_hash)

    # 老文档缺指纹 → 当场补算（两个都补：file_hash 判决字节是否相同，
    # content_hash 供后续同名版本比对；均只读本地已有文件，见 ensure_fingerprints）
    if not same_name.file_hash or not same_name.content_hash:
        ensure_fingerprints(same_name, svc)

    if same_name.status in _BUSY_STATUS:
        same_bytes = bool(same_name.file_hash == file_hash)
        return DupCheck(
            SAME_NAME_BUSY, same_name, file_hash=file_hash,
            message=("该文件正在解析中，无需重复上传"
                     if same_bytes else
                     f"文档「{original_name}」正在解析中，"
                     "请等待完成后再上传更新版本"))

    if same_name.file_hash and same_name.file_hash == file_hash:
        return DupCheck(
            EXACT_DUP, same_name, file_hash=file_hash,
            message=(f"该文件与文档「{original_name}」内容完全相同"
                     f"（{_ingested_hint(same_name)}），无需重复上传"))

    return DupCheck(SAME_NAME, same_name, file_hash=file_hash)


def _ingested_hint(doc: DocumentItem) -> str:
    """提示文案里的"入库时间"小尾巴（无入库时间时退回创建时间）"""
    when = doc.ingest_finished_at or doc.created_at or ""
    return f"{when} 入库" if when else "已入库"
