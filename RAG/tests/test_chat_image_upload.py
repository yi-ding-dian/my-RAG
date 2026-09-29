"""聊天图片上传与读取接口测试

覆盖（`routers/chat.py` 的 upload-image、`routers/files.py` 的 chat-images 代理）：

- 上传：成功返回 key、粘贴无文件名靠 Content-Type 认扩展名、格式不支持 400、
  超过 chat.image_max_mb 400、空文件 400、总开关关闭 403
- 读取：本人 200、他人 404 伪装、超管 200、对象不存在 404、路径穿越 404
- 存储路径规范：chat_images/{user_id}/{uuid}.{ext}

全部离线：图片字节用内存构造的 1×1 PNG（不依赖外部素材），存储走
LocalBackend（_isolated_env 已把 DATA_DIR 指到临时目录）。
"""
from __future__ import annotations

import base64

# 1×1 透明 PNG（最小合法图片）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def upload_image(client, headers, data=None, filename="t.png",
                 content_type="image/png"):
    """调上传接口（默认用最小 PNG）"""
    return client.post(
        "/api/chat/upload-image",
        files={"file": (filename, PNG_BYTES if data is None else data,
                        content_type)},
        headers=headers)


def split_key(key: str):
    """chat_images/{user_id}/{name} → (user_id, name)"""
    prefix, user_id, name = key.split("/")
    assert prefix == "chat_images", f"存储前缀应为 chat_images，实际 {prefix}"
    return user_id, name


class TestUpload:

    def test_upload_ok_returns_key(self, client, admin_headers):
        """上传成功返回 key，路径符合 chat_images/{user_id}/{uuid}.{ext}"""
        resp = upload_image(client, admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        user_id, name = split_key(body["key"])
        assert user_id, "user_id 段不能为空"
        assert name.endswith(".png")
        assert body["name"] == "t.png"

    def test_paste_without_ext_uses_content_type(self, client,
                                                 admin_headers):
        """粘贴的图没有带扩展名的文件名（浏览器里通常叫 blob）：靠
        Content-Type 认出扩展名

        只认文件名会把最常用的"截图直接粘贴"误判成非法格式。注意用
        `blob` 而不是空串——httpx 对空 filename 会当普通表单字段发，
        连 UploadFile 都构造不出来（那是测试客户端的行为，非接口行为）。
        """
        resp = upload_image(client, admin_headers, filename="blob",
                            content_type="image/jpeg")
        assert resp.status_code == 200, resp.text
        assert resp.json()["key"].endswith(".jpg")

    def test_unsupported_format_400(self, client, admin_headers):
        resp = upload_image(client, admin_headers, data=b"not an image",
                            filename="t.txt", content_type="text/plain")
        assert resp.status_code == 400
        assert "格式" in resp.json()["detail"]

    def test_oversize_400(self, client, admin_headers):
        """超过 chat.image_max_mb（默认 5MB）→ 400"""
        big = PNG_BYTES + b"\0" * (6 * 1024 * 1024)
        resp = upload_image(client, admin_headers, data=big)
        assert resp.status_code == 400
        assert "过大" in resp.json()["detail"]

    def test_empty_file_400(self, client, admin_headers):
        resp = upload_image(client, admin_headers, data=b"")
        assert resp.status_code == 400

    def test_disabled_403(self, client, admin_headers, monkeypatch):
        """总开关关闭 → 拒绝上传（前端此时也不显示图片入口）"""
        from types import SimpleNamespace

        from backend.routers import chat as chat_router

        monkeypatch.setattr(
            chat_router, "get_active_config",
            lambda: SimpleNamespace(chat=SimpleNamespace(image_enabled=False)))
        resp = upload_image(client, admin_headers)
        assert resp.status_code == 403
        assert "关闭" in resp.json()["detail"]

    def test_requires_login(self, client):
        resp = upload_image(client, {})
        assert resp.status_code == 401


class TestRead:

    def test_owner_can_read_bytes(self, client, admin_headers):
        """本人读自己的图：200 且字节与上传一致"""
        key = upload_image(client, admin_headers).json()["key"]
        user_id, name = split_key(key)
        resp = client.get(f"/api/files/chat-images/{user_id}/{name}",
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.content == PNG_BYTES

    def test_other_user_404(self, client, admin_headers, user_headers):
        """别人读 → 404 伪装（不区分"没权限"与"不存在"，防存在性探测）"""
        key = upload_image(client, admin_headers).json()["key"]
        user_id, name = split_key(key)
        resp = client.get(f"/api/files/chat-images/{user_id}/{name}",
                          headers=user_headers)
        assert resp.status_code == 404

    def test_super_admin_can_read_others(self, client, admin_headers,
                                         user_headers):
        """超管可读他人的图：回溯被反馈会话时要能看到图（与会话详情同口径）"""
        key = upload_image(client, user_headers).json()["key"]
        user_id, name = split_key(key)
        resp = client.get(f"/api/files/chat-images/{user_id}/{name}",
                          headers=admin_headers)
        assert resp.status_code == 200

    def test_missing_object_404(self, client, admin_headers):
        """对象不存在 → 404（不是 500）"""
        resp = client.get(
            "/api/files/chat-images/nobody/0123456789abcdef.png",
            headers=admin_headers)
        assert resp.status_code == 404

    def test_path_traversal_404(self, client, admin_headers):
        """路径穿越尝试一律 404（与文档图片代理同款白名单）"""
        for path in ("../../etc/passwd",
                     "..%2F..%2Fetc%2Fpasswd",
                     "a/../../b"):
            resp = client.get(f"/api/files/chat-images/{path}",
                              headers=admin_headers)
            assert resp.status_code == 404, f"{path} 应被拒绝"
