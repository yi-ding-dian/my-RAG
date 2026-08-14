"""P2-9 回收站图片代理拦截测试

问题：/api/files/images/{doc_id}/{name} 未检查 doc.deleted，回收站文档的
解析图片仍可经代理加载（与 raw 预览的 404 伪装不一致）。
修复：图片代理加载文档时检查 deleted → 404。
"""
from __future__ import annotations

import asyncio

from backend.services.storage_service import get_storage_service
from conftest import create_kb, upload_doc


class TestTrashImageProxy:

    def test_image_available_before_trash(self, client, admin_headers):
        """正常文档图片可加载（200）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        key = f"images/{doc['id']}/test.png"
        asyncio.run(get_storage_service().upload_bytes(key, b"\x89PNG\x0d\x0a"))

        resp = client.get(f"/api/files/images/{doc['id']}/test.png",
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.content == b"\x89PNG\x0d\x0a"

    def test_image_blocked_after_soft_delete(self, client, admin_headers):
        """文档移入回收站后 → 图片代理 404（与 raw 预览一致）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        key = f"images/{doc['id']}/test.png"
        asyncio.run(get_storage_service().upload_bytes(key, b"\x89PNG\x0d\x0a"))
        url = f"/api/files/images/{doc['id']}/test.png"
        assert client.get(url, headers=admin_headers).status_code == 200

        client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                      headers=admin_headers)
        assert client.get(url, headers=admin_headers).status_code == 404

    def test_image_after_restore_available_again(self, client, admin_headers):
        """恢复后图片代理重新可用（与 raw 语义一致）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        key = f"images/{doc['id']}/test.png"
        asyncio.run(get_storage_service().upload_bytes(key, b"\x89PNG\x0d\x0a"))
        url = f"/api/files/images/{doc['id']}/test.png"

        client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                      headers=admin_headers)
        assert client.get(url, headers=admin_headers).status_code == 404
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/restore",
                           headers=admin_headers)
        assert resp.status_code == 200
        assert client.get(url, headers=admin_headers).status_code == 200
