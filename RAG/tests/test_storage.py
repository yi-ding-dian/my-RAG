"""本地存储后端单测（LocalBackend）

覆盖：upload_bytes/download_to 往返、upload_path 文件上传、delete 单对象、
delete_prefix 前缀删除、key 路径映射（与 MinIO 桶 key 同构：
uploads/{uuid}.ext / images/{doc_id}/{name} 落在 data/storage/ 下）。
StorageBackend 方法均为 async，测试用 asyncio.run 驱动（本地实现无共享
状态，独立 event loop 安全）。全部离线（STORAGE_BACKEND=local，不连 MinIO）。
"""
from __future__ import annotations

import asyncio

from backend.services.storage_service import LocalBackend


class TestLocalBackend:
    """LocalBackend 单元往返"""

    def test_upload_download_roundtrip(self):
        """upload_bytes → download_to 内容一致"""
        backend = LocalBackend()
        data = "你好，MinIO 兼容 key".encode("utf-8")
        asyncio.run(backend.upload_bytes("uploads/abc123.txt", data,
                                         content_type="text/plain"))
        dest = backend._root / "tmp" / "download.bin"
        asyncio.run(backend.download_to("uploads/abc123.txt", dest))
        assert dest.read_bytes() == data

    def test_key_path_mapping(self):
        """key 映射到 data/storage/{key}，目录自动创建"""
        backend = LocalBackend()
        asyncio.run(backend.upload_bytes("images/doc456/pic.png", b"\x89PNG"))
        p = backend._root / "images" / "doc456" / "pic.png"
        assert p.exists(), "key 应落在 STORAGE_DIR/images/doc456/pic.png"
        assert p.read_bytes() == b"\x89PNG"

    def test_upload_path_file(self):
        """upload_path 上传本地文件"""
        backend = LocalBackend()
        src = backend._root / "src" / "origin.docx"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"docx-content")
        asyncio.run(backend.upload_path("uploads/doc789.docx", src))
        assert (backend._root / "uploads" / "doc789.docx").read_bytes() \
            == b"docx-content"

    def test_download_missing_raises(self):
        """下载不存在的对象抛异常（调用方 fallback 依赖此行为）"""
        backend = LocalBackend()
        try:
            asyncio.run(backend.download_to("uploads/nonexist.bin", "/tmp/x.bin"))
            raise AssertionError("应抛 FileNotFoundError")
        except FileNotFoundError:
            pass

    def test_delete_single(self):
        """delete 删除单对象；不存在时静默"""
        backend = LocalBackend()
        asyncio.run(backend.upload_bytes("uploads/a.txt", b"a"))
        asyncio.run(backend.delete("uploads/a.txt"))
        assert not (backend._root / "uploads" / "a.txt").exists()
        asyncio.run(backend.delete("uploads/a.txt"))  # 不存在不抛

    def test_delete_prefix(self):
        """delete_prefix 删除某前缀下全部对象（images/{doc_id}/）"""
        backend = LocalBackend()
        for name in ("images/doc1/1.png", "images/doc1/2.png",
                     "images/doc2/3.png", "uploads/keep.txt"):
            asyncio.run(backend.upload_bytes(name, b"x"))
        asyncio.run(backend.delete_prefix("images/doc1/"))
        assert not (backend._root / "images" / "doc1").exists()
        assert (backend._root / "images" / "doc2" / "3.png").exists(), \
            "其他前缀不受影响"
        assert (backend._root / "uploads" / "keep.txt").exists()
        # 不存在的前缀静默
        asyncio.run(backend.delete_prefix("images/nonexist/"))

    def test_ensure_bucket_always_true(self):
        """local 后端 ensure_bucket 恒 True"""
        assert asyncio.run(LocalBackend().ensure_bucket()) is True

    def test_url_diagnostic(self):
        """url 仅诊断展示（local:{key}）"""
        assert LocalBackend().url("uploads/a.txt") == "local:uploads/a.txt"
