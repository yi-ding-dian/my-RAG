"""对象存储服务（StorageBackend 抽象 + MinIO / 本地文件实现）

设计要点：
- **抽象接口**：upload_bytes / upload_path / download_to / delete / delete_prefix / url /
  ensure_bucket，文档上传、解析图片、图片代理路由统一走此层，存储实现可插拔；
- **MinIOBackend**：minio 同步 SDK 用 asyncio.to_thread 跑（不阻塞事件循环）；
  连接配置运行时读 get_active_config().minio（配置档案改动即时生效）；
  配置 key（endpoint/access_key/secret_key/secure/region）变化时自动重建 client；
  桶惰性 ensure_bucket：bucket_exists → make_bucket，失败仅 warning 并给出
  mc mb 命令提示，不抛错阻断启动；
- **LocalBackend**：存 data/storage/{key}（与 MinIO 桶 key 同构），测试/降级用；
- **get_storage_service()**：按 STORAGE_BACKEND（minio/local）选实现，
  配置 key 比对后重建单例。
"""
from __future__ import annotations

import asyncio
import io
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from backend.config import STORAGE_DIR, get_active_config
from backend.config import settings as config_settings

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """对象存储后端抽象（全部方法 async，内部实现可自由使用线程池）"""

    @abstractmethod
    async def upload_bytes(self, key: str, data: bytes,
                           content_type: Optional[str] = None):
        """上传字节内容到 key"""

    @abstractmethod
    async def upload_path(self, key: str, path: Path):
        """上传本地文件到 key"""

    @abstractmethod
    async def download_to(self, key: str, dest_path):
        """下载 key 到本地路径（不存在/失败抛异常，由调用方 fallback）"""

    @abstractmethod
    async def read_bytes(self, key: str) -> bytes:
        """读回 key 的原始字节（文档在线预览用；调用方负责大小限制，
        不存在/失败抛异常）"""

    @abstractmethod
    async def delete(self, key: str):
        """删除单个对象（不存在时静默）"""

    @abstractmethod
    async def delete_prefix(self, prefix: str):
        """删除某前缀下全部对象（如 images/{doc_id}/）"""

    @abstractmethod
    def url(self, key: str) -> str:
        """对象访问 URL（仅诊断展示用；前端一律走 /api/files/... 鉴权代理）"""

    @abstractmethod
    async def ensure_bucket(self) -> bool:
        """确保桶存在（MinIO 实现可能失败，返回 False；local 恒 True）"""


# ---------------- MinIO 实现 ----------------

class MinIOBackend(StorageBackend):
    """MinIO 对象存储（minio 同步 SDK + asyncio.to_thread）"""

    def __init__(self):
        self._client = None
        self._client_key: Optional[tuple] = None
        self._bucket_ensured = False

    def _cfg(self):
        """运行时读取活跃配置（配置档案切换即时生效）"""
        return get_active_config().minio

    def _get_client(self):
        """配置 key 比对：连接信息变化时重建 client

        minio SDK 7.2.x 无 timeout 参数，超时经 http_client
        （urllib3.PoolManager，timeout 10s）注入。
        """
        cfg = self._cfg()
        key = (cfg.endpoint, cfg.access_key, cfg.secret_key,
               cfg.secure, cfg.region)
        if self._client is None or self._client_key != key:
            import urllib3
            from minio import Minio
            self._client = Minio(
                cfg.endpoint,
                access_key=cfg.access_key,
                secret_key=cfg.secret_key,
                secure=cfg.secure,
                region=cfg.region or None,
                http_client=urllib3.PoolManager(
                    timeout=urllib3.Timeout(connect=10.0, read=10.0)),
            )
            self._client_key = key
            self._bucket_ensured = False  # 连接变化后重新 ensure
            logger.info("MinIO 客户端已重建: %s (secure=%s)",
                        cfg.endpoint, cfg.secure)
        return self._client

    async def ensure_bucket(self) -> bool:
        """惰性建桶：失败仅 warning（给 mc 命令提示），不抛错阻断"""
        if self._bucket_ensured:
            return True
        cfg = self._cfg()
        bucket = cfg.bucket or "my-rag"
        try:
            def _run():
                client = self._get_client()
                if not client.bucket_exists(bucket):
                    client.make_bucket(bucket, location=cfg.region or None)
            await asyncio.to_thread(_run)
            self._bucket_ensured = True
            logger.info("MinIO 桶已就绪: %s/%s", cfg.endpoint, bucket)
            return True
        except Exception as e:
            logger.warning(
                "MinIO 建桶失败（%s/%s）: %s。若账号无建桶权限，可手工执行: "
                "mc mb %s/%s 或使用已有桶（MINIO_BUCKET 环境变量）",
                cfg.endpoint, bucket, str(e)[:200], cfg.endpoint, bucket)
            return False

    async def upload_bytes(self, key: str, data: bytes,
                           content_type: Optional[str] = None):
        await self.ensure_bucket()
        cfg = self._cfg()
        try:
            def _run():
                self._get_client().put_object(
                    cfg.bucket, key, io.BytesIO(data), len(data),
                    content_type=content_type or "application/octet-stream")
            await asyncio.to_thread(_run)
            logger.info("MinIO 上传: %s (%d 字节)", key, len(data))
        except Exception as e:
            raise RuntimeError(f"MinIO 上传失败 {key}: {e}") from e

    async def upload_path(self, key: str, path: Path):
        await self.ensure_bucket()
        cfg = self._cfg()
        size = Path(path).stat().st_size
        try:
            def _run():
                self._get_client().fput_object(cfg.bucket, key, str(path))
            await asyncio.to_thread(_run)
            logger.info("MinIO 上传文件: %s (%d 字节)", key, size)
        except Exception as e:
            raise RuntimeError(f"MinIO 上传失败 {key}: {e}") from e

    async def download_to(self, key: str, dest_path):
        cfg = self._cfg()
        try:
            def _run():
                resp = self._get_client().get_object(cfg.bucket, key)
                try:
                    with open(dest_path, "wb") as f:
                        for chunk in resp.stream(64 * 1024):
                            f.write(chunk)
                finally:
                    resp.close()
                    resp.release_conn()
            await asyncio.to_thread(_run)
        except Exception as e:
            raise RuntimeError(f"MinIO 下载失败 {key}: {e}") from e

    async def read_bytes(self, key: str) -> bytes:
        cfg = self._cfg()
        try:
            def _run():
                resp = self._get_client().get_object(cfg.bucket, key)
                try:
                    return resp.read()
                finally:
                    resp.close()
                    resp.release_conn()
            return await asyncio.to_thread(_run)
        except Exception as e:
            raise RuntimeError(f"MinIO 读取失败 {key}: {e}") from e

    async def delete(self, key: str):
        cfg = self._cfg()
        try:
            def _run():
                self._get_client().remove_object(cfg.bucket, key)
            await asyncio.to_thread(_run)
            logger.info("MinIO 删除对象: %s", key)
        except Exception as e:
            logger.warning("MinIO 删除对象失败 %s: %s", key, str(e)[:150])

    async def delete_prefix(self, prefix: str):
        cfg = self._cfg()
        try:
            def _run():
                # 7.2.x 需 DeleteObject 迭代器（参数名 bypass_governance_mode）
                from minio.deleteobjects import DeleteObject
                client = self._get_client()
                names = [o.object_name for o in
                         client.list_objects(cfg.bucket, prefix=prefix)]
                if names:
                    errors = client.remove_objects(
                        cfg.bucket, [DeleteObject(n) for n in names])
                    list(errors)  # 消费迭代器以触发实际删除请求
            await asyncio.to_thread(_run)
            logger.info("MinIO 删除前缀: %s*", prefix)
        except Exception as e:
            logger.warning("MinIO 删除前缀失败 %s*: %s", prefix, str(e)[:150])

    def url(self, key: str) -> str:
        cfg = self._cfg()
        return f"{cfg.endpoint}/{cfg.bucket}/{key}"


# ---------------- 本地文件实现（测试/降级） ----------------

class LocalBackend(StorageBackend):
    """本地文件存储：data/storage/{key}（与 MinIO 桶 key 同构）"""

    def __init__(self):
        self._root = STORAGE_DIR

    def _path(self, key: str) -> Path:
        """key 转本地路径（key 由内部生成: uploads/{uuid}.ext / images/{id}/{name}）"""
        return self._root.joinpath(*key.split("/"))

    async def ensure_bucket(self) -> bool:
        return True

    async def upload_bytes(self, key: str, data: bytes,
                           content_type: Optional[str] = None):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    async def upload_path(self, key: str, path: Path):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(path), p)

    async def download_to(self, key: str, dest_path):
        src = self._path(key)
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(f"对象不存在: {key}")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    async def read_bytes(self, key: str) -> bytes:
        src = self._path(key)
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(f"对象不存在: {key}")
        return src.read_bytes()

    async def delete(self, key: str):
        p = self._path(key)
        if p.exists():
            p.unlink()

    async def delete_prefix(self, prefix: str):
        base = self._root.joinpath(*prefix.split("/"))
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)

    def url(self, key: str) -> str:
        return f"local:{key}"


# ---------------- 单例（配置 key 比对重建） ----------------

_storage: Optional[StorageBackend] = None
_storage_key: Optional[tuple] = None


def get_storage_service() -> StorageBackend:
    """按 STORAGE_BACKEND（minio/local）选实现；配置 key 变化时重建"""
    global _storage, _storage_key
    backend = (config_settings.STORAGE_BACKEND or "minio").strip().lower()
    cfg = get_active_config().minio
    key = (backend, cfg.endpoint, cfg.access_key, cfg.secret_key,
           cfg.bucket, cfg.secure, cfg.region)
    if _storage is None or _storage_key != key:
        _storage = MinIOBackend() if backend == "minio" else LocalBackend()
        _storage_key = key
        logger.info("存储后端: %s（%s）", backend,
                    "MinIO " + cfg.endpoint if backend == "minio" else str(STORAGE_DIR))
    return _storage
