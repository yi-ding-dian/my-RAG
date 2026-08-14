"""配置档案基础设施测试：mysql/minio 段（契约第 5 章）

覆盖：
- profile 含 mysql/minio 段（字段齐全，mysql 段含 url 字段）
- 密钥脱敏：password/secret_key 前4****后4（如 infi****flow）
- 脱敏值回传不覆盖原值（激活后 get_active_config 可见原值）
- 连接测试响应含 mysql/minio 键（mock aiomysql.connect 与 minio.Minio，离线）
"""
from __future__ import annotations

import asyncio

from backend.config import get_active_config
from backend.services.settings_service import get_settings_service


def _make_profile(client, admin_headers, name="基建档案", **sections):
    """创建配置档案并返回 public dict"""
    resp = client.post("/api/settings/profiles", json={"name": name, **sections},
                       headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestInfraSections:
    """mysql/minio 段存在与字段完整性"""

    def test_default_profile_has_infra_sections(self, client, admin_headers):
        """默认档案含 mysql/minio 段，字段齐全"""
        items = client.get("/api/settings/profiles", headers=admin_headers).json()
        active = next(p for p in items if p["active"])
        mysql = active["mysql"]
        assert isinstance(mysql, dict)
        for field in ("host", "port", "user", "password", "database", "url"):
            assert field in mysql, f"mysql 段缺字段: {field}"
        minio = active["minio"]
        assert isinstance(minio, dict)
        for field in ("endpoint", "access_key", "secret_key", "bucket",
                      "secure", "region"):
            assert field in minio, f"minio 段缺字段: {field}"
        # 测试环境 URL 覆盖注入已透传到档案
        assert "sqlite" in mysql["url"]

    def test_password_masked(self, client, admin_headers):
        """mysql.password / minio.secret_key 脱敏（前4****后4）"""
        profile = _make_profile(client, admin_headers)
        # 测试注入值 mysql-test-pass（15 字符）→ mysq****pass
        assert profile["mysql"]["password"] == "mysq****pass"
        assert profile["minio"]["secret_key"] == "mini****cret"
        assert "****" in profile["mysql"]["password"]
        assert "mysql-test-pass" not in profile["mysql"]["password"], "明文不得回传"
        # access_key 非密钥后缀字段不清洗
        assert profile["minio"]["access_key"] == "test-access-key"

    def test_masked_roundtrip_keeps_original(self, client, admin_headers):
        """PUT 回传脱敏 password → 不覆盖原值（激活后可见原值）"""
        profile = _make_profile(client, admin_headers)
        resp = client.put(f"/api/settings/profiles/{profile['id']}", json={
            "mysql": {"password": "infi****flow", "host": "127.0.0.1"},
        }, headers=admin_headers)
        assert resp.status_code == 200
        client.post(f"/api/settings/profiles/{profile['id']}/activate",
                    headers=admin_headers)
        cfg = get_active_config()
        assert cfg.mysql.password == "mysql-test-pass", "脱敏回传不应覆盖原值"
        assert cfg.mysql.host == "127.0.0.1", "非密钥字段正常更新"


# ==================== 连接测试（离线 mock 网络） ====================

class _FakeCursor:
    """伪 aiomysql cursor"""

    async def execute(self, sql):
        return 0

    async def fetchone(self):
        return (1,)

    async def close(self):
        pass


class _FakeMySQLConn:
    """伪 aiomysql 连接：SELECT 1 成功"""

    async def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


class _FakeMinio:
    """伪 minio 客户端：bucket_exists 可控（存在/抛错）"""

    def __init__(self, *args, **kwargs):
        self._exists = True

    def bucket_exists(self, bucket):
        if not self._exists:
            raise ConnectionError("mock: MinIO 不可达")
        return True


class TestConnectionInfra:
    """连接测试的 mysql/minio 段（服务层直测 + API 层键存在）"""

    def test_api_response_contains_mysql_minio(self, client, admin_headers):
        """POST /profiles/{id}/test → 响应含 mysql/minio 键（{ok, latency_ms, message}）"""
        profile = _make_profile(client, admin_headers)
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        for key in ("llm", "embedding", "mineru", "mysql", "minio"):
            assert key in data, f"连接测试响应缺键: {key}"
        for key in ("mysql", "minio"):
            assert set(data[key]) == {"ok", "latency_ms", "message"}, \
                f"{key} 段结构不符: {data[key]}"
        # 测试环境 mysql 为 URL 覆盖模式 → 明确提示且不崩溃
        assert data["mysql"]["ok"] is False
        assert "URL 覆盖模式" in data["mysql"]["message"]

    def test_mysql_connect_ok_mocked(self, monkeypatch):
        """_test_mysql 直连成功路径（mock aiomysql.connect）"""
        import aiomysql

        async def fake_connect(**kwargs):
            return _FakeMySQLConn()

        monkeypatch.setattr(aiomysql, "connect", fake_connect)
        svc = get_settings_service()
        result = asyncio.run(svc._test_mysql({
            "host": "127.0.0.1", "port": 3306, "user": "u",
            "password": "p", "database": "db", "url": "",
        }))
        assert result["ok"] is True, result
        assert "连接成功" in result["message"]

    def test_mysql_connect_fail_still_result(self, monkeypatch):
        """_test_mysql 连接失败 → ok=False + 失败信息（接口不抛）"""
        import aiomysql

        async def fake_connect(**kwargs):
            raise ConnectionError("mock: 连接被拒")

        monkeypatch.setattr(aiomysql, "connect", fake_connect)
        svc = get_settings_service()
        result = asyncio.run(svc._test_mysql({
            "host": "127.0.0.1", "port": 3306, "user": "u",
            "password": "p", "database": "db", "url": "",
        }))
        assert result["ok"] is False
        assert "连接失败" in result["message"]

    def test_minio_bucket_ok_mocked(self, monkeypatch):
        """_test_minio 桶探测成功路径（mock minio.Minio）"""
        import minio
        monkeypatch.setattr(minio, "Minio", _FakeMinio)
        svc = get_settings_service()
        result = asyncio.run(svc._test_minio({
            "endpoint": "127.0.0.1:9000", "access_key": "ak",
            "secret_key": "sk", "bucket": "my-rag", "secure": False,
        }))
        assert result["ok"] is True, result
        assert "桶" in result["message"]

    def test_minio_endpoint_missing(self):
        """_test_minio endpoint 未配置 → ok=False 明确提示"""
        svc = get_settings_service()
        result = asyncio.run(svc._test_minio({
            "endpoint": "", "access_key": "ak", "secret_key": "sk",
            "bucket": "my-rag", "secure": False,
        }))
        assert result["ok"] is False
        assert "endpoint 未配置" in result["message"]
