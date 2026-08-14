"""用户头像 API 集成测试

覆盖：
- 上传成功（200 + {avatar: avatars/{uid}.ext} + 字段更新 + LocalBackend
  路径断言 data/storage/avatars/{uid}.ext 文件存在、字节一致）
- 重复上传替换旧文件（旧文件删除，新文件生效）
- 校验：非图片扩展名/空文件/超 1MB → 400；未登录 → 401
- 普通用户可上传自己的头像（登录即可，不要求管理员）
- 删除：文件删除 + users.avatar 清空（回默认）；无头像时幂等 200
- 代理 GET /api/files/avatars/{user_id}：200（content-type + 字节一致，
  query token 与 Bearer 均可）；404 伪装（无头像/用户不存在/文件缺失）
- /me、用户列表/详情含 avatar 字段
- 存量 users 表无 avatar 列 → ensure_user_avatar_column 补列迁移（幂等）
"""
from __future__ import annotations

import asyncio

from backend.config import STORAGE_DIR

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 128
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"1" * 128


def _upload(client, headers, filename="avatar.png", content=b"", ctype="image/png"):
    """上传头像并返回响应"""
    files = {"file": (filename, content, ctype)}
    return client.post("/api/users/me/avatar", files=files, headers=headers)


def _admin_uid(client, admin_headers):
    """当前 admin 用户 id（/me）"""
    return client.get("/api/auth/me", headers=admin_headers).json()["id"]


class TestUploadAvatar:
    """头像上传"""

    def test_upload_success_and_file_exists(self, client, admin_headers):
        """上传成功 → 返回 avatar key；/me 字段更新；LocalBackend 文件存在"""
        uid = _admin_uid(client, admin_headers)
        resp = _upload(client, admin_headers, content=PNG_BYTES)
        assert resp.status_code == 200, resp.text
        key = resp.json()["avatar"]
        assert key == f"avatars/{uid}.png"
        # LocalBackend 路径断言：data/storage/avatars/{uid}.png 字节一致
        p = STORAGE_DIR / "avatars" / f"{uid}.png"
        assert p.is_file(), f"头像文件应落在 {p}"
        assert p.read_bytes() == PNG_BYTES
        # /me 含 avatar
        me = client.get("/api/auth/me", headers=admin_headers).json()
        assert me["avatar"] == key

    def test_upload_webp_and_gif_ok(self, client, admin_headers):
        """webp/gif 扩展名同样接受（白名单）"""
        uid = _admin_uid(client, admin_headers)
        for ext, ctype in ((".webp", "image/webp"), (".gif", "image/gif")):
            resp = _upload(client, admin_headers, filename=f"a{ext}",
                           content=b"x" * 16, ctype=ctype)
            assert resp.status_code == 200, resp.text
            assert resp.json()["avatar"] == f"avatars/{uid}{ext}"

    def test_upload_replace_old_file(self, client, admin_headers):
        """重复上传：旧文件删除、新文件生效（先删旧再传，防残留）"""
        uid = _admin_uid(client, admin_headers)
        assert _upload(client, admin_headers, filename="a.png",
                       content=PNG_BYTES).status_code == 200
        assert _upload(client, admin_headers, filename="b.jpg",
                       content=JPG_BYTES).status_code == 200
        assert not (STORAGE_DIR / "avatars" / f"{uid}.png").exists(), \
            "旧 png 文件应被删除"
        p = STORAGE_DIR / "avatars" / f"{uid}.jpg"
        assert p.is_file() and p.read_bytes() == JPG_BYTES
        me = client.get("/api/auth/me", headers=admin_headers).json()
        assert me["avatar"] == f"avatars/{uid}.jpg"

    def test_upload_invalid_type_400(self, client, admin_headers):
        """非白名单扩展名 → 400；无扩展名 → 400"""
        resp = _upload(client, admin_headers, filename="avatar.txt", content=b"x")
        assert resp.status_code == 400
        assert "jpg/png/webp/gif" in resp.json()["detail"]
        resp = _upload(client, admin_headers, filename="noext", content=b"x")
        assert resp.status_code == 400
        # 大写扩展名归一后接受
        resp = _upload(client, admin_headers, filename="AVATAR.PNG",
                       content=PNG_BYTES)
        assert resp.status_code == 200

    def test_upload_empty_400(self, client, admin_headers):
        """空文件 → 400"""
        resp = _upload(client, admin_headers, content=b"")
        assert resp.status_code == 400
        assert "为空" in resp.json()["detail"]

    def test_upload_too_large_400(self, client, admin_headers):
        """超 1MB → 400"""
        resp = _upload(client, admin_headers,
                       content=b"x" * (1024 * 1024 + 1))
        assert resp.status_code == 400
        assert "1MB" in resp.json()["detail"]
        # 恰好 1MB 通过
        resp = _upload(client, admin_headers, content=b"x" * (1024 * 1024))
        assert resp.status_code == 200

    def test_upload_unauthorized_401(self, client):
        """未登录 → 401"""
        resp = _upload(client, None)
        assert resp.status_code == 401

    def test_normal_user_can_upload_own_avatar(self, client, user_headers):
        """普通用户登录即可上传自己的头像（头像接口不要求管理员）"""
        resp = _upload(client, user_headers, content=PNG_BYTES)
        assert resp.status_code == 200, resp.text
        me = client.get("/api/auth/me", headers=user_headers).json()
        assert me["avatar"] == resp.json()["avatar"]


class TestDeleteAvatar:
    """头像删除（回默认）"""

    def test_delete_removes_file_and_field(self, client, admin_headers):
        """删除 → 200；文件删除；/me avatar 清空"""
        uid = _admin_uid(client, admin_headers)
        assert _upload(client, admin_headers, content=PNG_BYTES).status_code == 200
        resp = client.delete("/api/users/me/avatar", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["avatar"] is None
        assert not (STORAGE_DIR / "avatars" / f"{uid}.png").exists()
        me = client.get("/api/auth/me", headers=admin_headers).json()
        assert me["avatar"] is None

    def test_delete_without_avatar_idempotent(self, client, admin_headers):
        """无头像时删除也 200（幂等，'恢复默认'按钮可重复点击）"""
        resp = client.delete("/api/users/me/avatar", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["avatar"] is None

    def test_delete_unauthorized_401(self, client):
        """未登录删除 → 401"""
        assert client.delete("/api/users/me/avatar").status_code == 401


class TestAvatarProxy:
    """头像鉴权代理 GET /api/files/avatars/{user_id}"""

    def _upload_avatar(self, client, headers):
        uid = _admin_uid(client, headers)
        resp = _upload(client, headers, content=PNG_BYTES)
        assert resp.status_code == 200
        return uid

    def test_proxy_200_with_header(self, client, admin_headers):
        """Bearer 鉴权 → 200，content-type/字节与上传一致"""
        uid = self._upload_avatar(client, admin_headers)
        resp = client.get(f"/api/files/avatars/{uid}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == PNG_BYTES

    def test_proxy_200_with_query_token(self, client, admin_headers):
        """query ?token= 鉴权（<img> 无法带 header，与 markdown 图片代理一致）"""
        uid = self._upload_avatar(client, admin_headers)
        token = admin_headers["Authorization"].replace("Bearer ", "")
        resp = client.get(f"/api/files/avatars/{uid}?token={token}")
        assert resp.status_code == 200
        assert resp.content == PNG_BYTES

    def test_proxy_404_no_avatar(self, client, admin_headers, user_headers):
        """目标用户未上传头像 → 404 伪装"""
        uid = _admin_uid(client, user_headers)
        resp = client.get(f"/api/files/avatars/{uid}", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "头像不存在"

    def test_proxy_404_user_missing(self, client, admin_headers):
        """用户不存在 / user_id 非法 → 404 伪装"""
        for uid in ("000000000000", "a b c", "../../etc"):
            resp = client.get(f"/api/files/avatars/{uid}", headers=admin_headers)
            assert resp.status_code == 404, uid

    def test_proxy_404_file_missing(self, client, admin_headers):
        """DB 有 avatar 字段但文件缺失（如存储被清）→ 404 伪装"""
        uid = self._upload_avatar(client, admin_headers)
        p = STORAGE_DIR / "avatars" / f"{uid}.png"
        p.unlink()
        resp = client.get(f"/api/files/avatars/{uid}", headers=admin_headers)
        assert resp.status_code == 404

    def test_proxy_unauthorized_401(self, client):
        """未登录 → 401"""
        assert client.get("/api/files/avatars/abc").status_code == 401


class TestAvatarField:
    """序列化：/me、用户列表/详情含 avatar"""

    def test_me_and_list_contain_avatar(self, client, admin_headers, user_headers):
        """上传后 /me、用户列表（管理员视角）均含 avatar 字段；无头像为 null"""
        assert _upload(client, admin_headers, content=PNG_BYTES).status_code == 200
        me = client.get("/api/auth/me", headers=admin_headers).json()
        assert me["avatar"].startswith("avatars/")
        # 登录响应也携带 avatar（前端登录即持久化）
        login = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin123"})
        assert login.status_code == 200
        assert login.json()["user"]["avatar"] == me["avatar"]
        # 用户列表：admin 有头像，普通用户为 null
        users = client.get("/api/users", headers=admin_headers).json()
        by_id = {u["id"]: u for u in users}
        assert by_id[_admin_uid(client, admin_headers)]["avatar"] == me["avatar"]
        assert by_id[_admin_uid(client, user_headers)]["avatar"] is None


class TestLegacyTable:
    """存量表容错：users 表无 avatar 列 → 补列迁移（幂等，旧数据不丢）"""

    def test_legacy_table_add_avatar_column(self, tmp_path):
        """独立 sqlite 文件库模拟存量 users 表（无 avatar 列+旧数据）→ 补列"""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from backend.db import ensure_user_avatar_column

        async def _run():
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
            try:
                async with engine.begin() as conn:
                    # 模拟旧版本建表：无 avatar 列 + 一行旧数据
                    await conn.execute(text(
                        "CREATE TABLE users (id VARCHAR(32) PRIMARY KEY, "
                        "username VARCHAR(64) UNIQUE, password_hash VARCHAR(128), "
                        "display_name VARCHAR(64), role VARCHAR(16), "
                        "department_id VARCHAR(32), status VARCHAR(16), "
                        "created_at VARCHAR(32), must_change_password INTEGER NOT NULL DEFAULT 0)"))
                    await conn.execute(text(
                        "INSERT INTO users (id, username, password_hash, "
                        "display_name, role, status, created_at) "
                        "VALUES ('legacy1', 'old_user', 'x', '存量用户', 'user', "
                        "'active', '2026-01-01 00:00:00')"))
                # 迁移补列
                assert await ensure_user_avatar_column(engine) is True
                # 幂等：重复调用不报错
                assert await ensure_user_avatar_column(engine) is True
                async with engine.begin() as conn:
                    rows = (await conn.execute(text(
                        "SELECT avatar FROM users WHERE id='legacy1'"))).fetchall()
                    assert rows[0][0] is None  # 旧数据 avatar 为空，行不丢
            finally:
                await engine.dispose()
        asyncio.run(_run())


class TestAvatarOnUserDelete:
    """删除用户连带删除其头像对象（防孤儿 avatars/{uid}.{ext}）"""

    def test_delete_user_removes_avatar_object(self, client, admin_headers):
        """建用户 → 上传头像 → 删除用户 → 头像对象不存在"""
        from conftest import login_headers
        created = client.post("/api/users", json={
            "username": "avatar_doomed", "password": "pass123456",
            "display_name": "有头像待删", "role": "user",
        }, headers=admin_headers).json()
        user_hdrs = login_headers(client, "avatar_doomed", "pass123456")
        resp = _upload(client, user_hdrs, content=PNG_BYTES)
        assert resp.status_code == 200, resp.text
        assert resp.json()["avatar"] == f"avatars/{created['id']}.png"
        p = STORAGE_DIR / "avatars" / f"{created['id']}.png"
        assert p.is_file(), f"头像文件应落在 {p}"

        # admin 删除该用户 → 200；头像对象连带删除（修复点）
        resp = client.delete(f"/api/users/{created['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200
        assert not p.exists(), "删用户后头像对象应一并删除"
