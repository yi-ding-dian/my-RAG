"""A3 P1 files 图片代理 name 白名单测试

问题：GET /api/files/images/{doc_id}/{name} 的 name 未白名单校验直接拼 key；
LocalBackend._path 对含 .. 的 key 敏感（joinpath 后 .. 可跳出 doc_id 目录）。
修复：name 校验 ^[\w.-]+$（Unicode 模式，中文合法）且拒绝 ".."（404 伪装）。
"""
from __future__ import annotations

import asyncio

from backend.services.storage_service import get_storage_service
from conftest import create_kb, upload_doc


class TestImageNameWhitelist:

    def _setup(self, client, admin_headers):
        """建库上传文档并放一张 test.png，返回 (doc_id, url 前缀)"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        key = f"images/{doc['id']}/test.png"
        asyncio.run(get_storage_service().upload_bytes(key, b"\x89PNG\x0d\x0a"))
        return doc["id"], f"/api/files/images/{doc['id']}"

    def test_normal_name_200(self, client, admin_headers):
        """正常文件名（字母数字下划线连字符点/中文）→ 白名单放行 + 对象存在 → 200"""
        doc_id, prefix = self._setup(client, admin_headers)
        for name in ("test.png", "1.jpg", "a-b_c.d.png", "图片1.png"):
            asyncio.run(get_storage_service().upload_bytes(
                f"images/{doc_id}/{name}", b"\x89PNG\x0d\x0a"))
            resp = client.get(f"{prefix}/{name}", headers=admin_headers)
            assert resp.status_code == 200, resp.text

    def test_path_traversal_404(self, client, admin_headers):
        """../ 与 .. → 404（防穿越；与"图片不存在"同款伪装）"""
        doc_id, prefix = self._setup(client, admin_headers)
        for name in ("..", "...", "a..b.png", "../secret.txt", "a/b.png"):
            resp = client.get(f"{prefix}/{name}", headers=admin_headers)
            assert resp.status_code == 404, f"{name} 应 404: {resp.text}"

    def test_special_chars_404(self, client, admin_headers):
        """空格/引号/分号/斜杠等特殊字符 → 404"""
        doc_id, prefix = self._setup(client, admin_headers)
        for name in ("a b.png", 'a"b.png', "a;b.png", "a?b.png",
                     "a\\b.png", "a%b.png", "a@b.png"):
            resp = client.get(f"{prefix}/{name}", headers=admin_headers)
            assert resp.status_code == 404, f"{name!r} 应 404: {resp.text}"

    def test_empty_name_404(self, client, admin_headers):
        """空/纯符号 name → 404"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        # 空串与纯符号（URL 编码）→ 404
        for name in ("", "..", "~"):
            resp = client.get(
                f"/api/files/images/{doc['id']}/{name}", headers=admin_headers)
            assert resp.status_code == 404, resp.text

    def test_invalid_name_does_not_touch_storage(self, client, admin_headers,
                                                 monkeypatch):
        """非法 name 直接 404，不触发存储下载（防探测路径存在性）"""
        doc_id, prefix = self._setup(client, admin_headers)
        touched = []

        async def fake_download(key, dest):
            touched.append(key)

        monkeypatch.setattr(get_storage_service(), "download_to", fake_download)
        resp = client.get(f"{prefix}/bad..name", headers=admin_headers)
        assert resp.status_code == 404
        assert touched == [], "非法 name 不应触发存储下载"
