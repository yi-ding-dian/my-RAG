"""解析图片上传与 markdown 引用重写 mixin（方法实现原样搬移自原 ingestion_service）

- _ImageMixin._upload_images: 有字节的解析图片上传存储（images/{doc_id}/{name}），
  markdown 引用替换为鉴权代理 URL；上传失败仅 warning，不阻断文本入库
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from backend.services.parsers.images import rewrite_image_refs
from backend.services.storage_service import get_storage_service

logger = logging.getLogger(__name__)


class _ImageMixin:
    """解析图片上传与 markdown 引用重写"""

    async def _upload_images(self, doc, text: str, images: List[dict]) -> str:
        """上传有字节的解析图片，替换 markdown 引用为鉴权代理 URL；返回替换后文本

        - images 形态 [{name, data: bytes}]（parsers.client 归一化产物）
        - 无字节的图片（仅文件名/解码失败）不上传、不替换（保留原引用，不阻塞入库）
        - 串行上传（大图批量 201 张/16MB 量级，并发无收益）；逐张上传走 quiet
          （不打单张日志），整批结束只留一条汇总日志（张数 + 总字节）
        - 上传失败仅 warning（图片缺失不阻断文本入库）
        """
        storage = get_storage_service()
        mapping: dict = {}
        uploaded = 0
        uploaded_bytes = 0
        for img in images:
            name = img.get("name") or ""
            data = img.get("data")
            if not data:
                continue
            # 文件名取 basename（MinerU 可能带 images/ 前缀），避免 key 层级逃逸
            base_name = Path(name).name if name else "image"
            if not base_name:
                continue
            key = f"images/{doc.id}/{base_name}"
            try:
                await storage.upload_bytes(key, data, quiet=True)
                mapping[base_name] = f"/api/files/images/{doc.id}/{base_name}"
                uploaded += 1
                uploaded_bytes += len(data)
            except Exception as e:
                logger.warning("解析图片上传失败 %s: %s", key, str(e)[:150])
        if uploaded:
            # 汇总一条（含总占用体积，便于排查存储占用），替代逐张上传日志
            size_text = (f"{uploaded_bytes / 1024 / 1024:.1f} MB"
                         if uploaded_bytes >= 1024 * 1024
                         else f"{uploaded_bytes / 1024:.1f} KB")
            logger.info("解析图片上传完成: %s 共 %d 张 (%s)",
                        doc.id, uploaded, size_text)
        if not mapping:
            return text
        rewritten = rewrite_image_refs(text, mapping)
        if rewritten != text:
            logger.info("图片引用已替换: %s (%d 处)", doc.id, len(mapping))
        return rewritten
