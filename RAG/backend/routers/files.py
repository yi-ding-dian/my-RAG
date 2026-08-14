"""文件代理 API：/api/files

- GET /api/files/images/{doc_id}/{name}  解析图片鉴权代理
- GET /api/files/avatars/{user_id}      用户头像鉴权代理（登录即可；
                                        无头像/用户不存在/文件缺失 → 404 伪装）

背景：MinerU 解析出的图片存对象存储（MinIO/local），不直接暴露预签名 URL
（7 天过期 + 公开桶违背部门隔离）。前端统一经此端点加载：校验登录 +
所属知识库可访问（无权限 404 伪装），再从存储下载返回。头像无部门隔离
（头像本身非敏感），登录即可访问。

鉴权：header Bearer 或 query ?token= 二选一（img 标签无法带 header，
前端渲染 markdown 图片/头像时对 src 自动追加当前 JWT；JWT 24h 有效期内进 URL，
内网企业环境可接受，见 deps.get_current_user_query_or_header 注释）。
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from backend.db import get_db
from backend.deps import get_current_user_query_or_header, kb_or_404
from backend.models.user_models import UserPublic
from backend.services import user_service
from backend.services.document_service import get_document_service
from backend.services.storage_service import get_storage_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["文件"])

# 图片文件名白名单（防路径穿越回归）：仅字母/数字/下划线/连字符/点/中文，
# 拒绝斜杠、空格、引号等特殊字符；".." 显式拒绝（LocalBackend._path 对
# 含 .. 的 key 敏感，joinpath 后 ".." 可跳出 doc_id 目录）。
_IMAGE_NAME_RE = re.compile(r"^[\w.-]+$")


@router.get("/images/{doc_id}/{name}")
async def get_image(doc_id: str, name: str,
                    db: AsyncSession = Depends(get_db),
                    user: UserPublic = Depends(get_current_user_query_or_header)):
    """图片代理：文档所属知识库可访问（404 伪装）→ 存储下载 → FileResponse

    - 鉴权：query ?token= 或 Bearer header 二选一（失败 401）
    - 对象不存在 / 存储不可用 → 404
    - content_type 按扩展名（image/*，未知扩展名回退 octet-stream）
    """
    # P1-5: name 白名单校验（与"图片不存在"同款 404 伪装，防路径穿越）
    if (not name or ".." in name
            or not _IMAGE_NAME_RE.fullmatch(name)):
        raise HTTPException(status_code=404, detail="图片不存在")
    doc = get_document_service().get(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="图片不存在")
    # P2-9: 回收站文档图片同样不可访问（与 raw 预览 404 伪装一致，防绕过）
    if doc.deleted:
        raise HTTPException(status_code=404, detail="图片不存在")
    # 知识库不存在/无权限统一 404 伪装（防图片存在性探测）
    await kb_or_404(db, doc.kb_id, user, detail="图片不存在")

    key = f"images/{doc_id}/{name}"
    storage = get_storage_service()
    # 临时文件（后缀保留便于识别；下载后由 BackgroundTask 清理）
    fd, tmp_path = tempfile.mkstemp(suffix=Path(name).suffix or ".img")
    os.close(fd)
    try:
        await storage.download_to(key, tmp_path)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        logger.warning("图片读取失败 %s: %s", key, str(e)[:150])
        raise HTTPException(status_code=404, detail="图片不存在")

    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    def _cleanup():
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass

    return FileResponse(tmp_path, media_type=content_type,
                        background=BackgroundTask(_cleanup))


# 头像存储 key 白名单（防 DB 脏数据路径穿越）：avatars/{user_id}.{ext}
_AVATAR_KEY_RE = re.compile(r"^avatars/[\w.-]+$")


@router.get("/avatars/{user_id}")
async def get_avatar(user_id: str,
                     db: AsyncSession = Depends(get_db),
                     user: UserPublic = Depends(get_current_user_query_or_header)):
    """头像代理：登录即可 → 用户存在且有头像 → storage 读字节 → image/*

    - user_id 非法/用户不存在/未上传头像/文件缺失/存储不可用 → 统一 404
      「头像不存在」（防探测，与图片代理一致）
    - content_type 按扩展名（jpg/png/webp/gif），未知回退 image/*；
      前端无头像时直接用默认 SVG，不依赖本接口 404 兜底
    """
    if not re.fullmatch(r"[\w-]{1,32}", user_id):
        raise HTTPException(status_code=404, detail="头像不存在")
    target = await user_service.get(db, user_id)
    if target is None or not target.avatar:
        raise HTTPException(status_code=404, detail="头像不存在")
    key = target.avatar
    # key 白名单校验（DB 脏数据防穿越；服务端生成的 key 恒满足）
    if ".." in key or not _AVATAR_KEY_RE.fullmatch(key):
        logger.warning("用户头像 key 非法，拒绝访问: %s", key)
        raise HTTPException(status_code=404, detail="头像不存在")
    try:
        data = await get_storage_service().read_bytes(key)
    except Exception as e:
        logger.warning("头像读取失败 %s: %s", key, str(e)[:150])
        raise HTTPException(status_code=404, detail="头像不存在")
    content_type = mimetypes.guess_type(Path(key).name)[0] or "image/*"
    return Response(content=data, media_type=content_type)
