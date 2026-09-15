"""Gotenberg 文档转换客户端（Office → PDF）

**用途**：把 .doc / .docx 转成 PDF 后交给 MinerU 解析。

**为什么绕这一圈**：MinerU 的主场是 PDF——它按字号/字体/位置做**版面分析**，
能还原标题层级；而处理 docx 时只提取文本、不做版面分析，**标题会全丢**。
实测同一份文档：直接给 docx → **0 个标题**；先用 Gotenberg 转成 PDF 再给
→ **94 个标题**、层级正确。

**为什么经容器（Gotenberg）而非本机 LibreOffice**：容器可加内存硬上限，
转换大文档内存暴涨时**只有容器被杀、不拖垮整机**——本机裸跑 soffice 曾在
105MB / 795 张扫描图的 .doc 上吃到 27.9GB，把机器拖到 OOM 卡死数分钟（见
docker/Gotenberg/docker-compose.yml 的 mem_limit）。

**调用契约**：`POST {base_url}/forms/libreoffice/convert`（multipart，文件
字段名 `files`），**响应体直接就是 PDF 字节流**（非 JSON 包装）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 转换端点：Gotenberg 的 LibreOffice 路由（doc/docx/xls/ppt 等 Office 格式）
_CONVERT_PATH = "/forms/libreoffice/convert"
# 就绪探测端点（返回 {"status":"up","details":{libreoffice:{status},...}}）
_HEALTH_PATH = "/health"


async def convert_to_pdf(src: Path, cfg, out_dir: Path) -> Optional[Path]:
    """把 Office 文档转成 PDF，返回产物路径；不转换/失败 → None

    返回 None 的三种情况（调用方据此回退到原有解析路径，**不阻塞入库**）：
    - 未配置 Gotenberg 地址（cfg.base_url 为空）；
    - 服务不可达 / 超时 / 返回非 2xx；
    - 响应不是 PDF（上游异常时可能返回 JSON 错误体）。

    成功时不抛异常、失败不抛异常——转换是**增强手段**，不该有否决权。
    """
    base_url = (getattr(cfg, "base_url", "") or "").strip().rstrip("/")
    if not base_url:
        return None
    timeout = float(getattr(cfg, "timeout", 0) or 120.0)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            with open(src, "rb") as f:
                resp = await client.post(
                    f"{base_url}{_CONVERT_PATH}",
                    files={"files": (src.name, f)},
                )
        if resp.status_code >= 400:
            logger.warning(
                "Gotenberg 转换失败 %s: HTTP %d %s",
                src.name, resp.status_code, resp.text[:200])
            return None
        content = resp.content
        # 兜底校验：上游异常时可能返回 JSON 错误体而非 PDF
        if not content.startswith(b"%PDF"):
            logger.warning("Gotenberg 返回的不是 PDF（%s）: %s",
                           src.name, content[:200])
            return None
        out = out_dir / f"{src.stem}.pdf"
        out.write_bytes(content)
        logger.info("Gotenberg 转换完成: %s → %s（%.1f KB）",
                    src.name, out.name, len(content) / 1024)
        return out
    except httpx.TimeoutException:
        logger.warning("Gotenberg 转换超时（%.0fs）: %s", timeout, src.name)
        return None
    except Exception as e:
        logger.warning("Gotenberg 转换异常 %s: %s", src.name, str(e)[:200])
        return None
