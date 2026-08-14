"""A4 P1 JWT 弱密钥拒绝启动测试

问题：默认弱密钥仅 warning 不拒启，未配 .env 可伪造任意用户 token。
修复：lifespan 启动时 JWT_SECRET 为默认值/空/过短（<16 字符）→
抛 RuntimeError 拒绝启动（中文说明）。
"""
from __future__ import annotations

import pytest

from backend.config import settings as config_settings


class TestJWTSecretStartupCheck:

    def _try_start(self) -> None:
        """进入 TestClient（触发 lifespan）；异常向上抛"""
        from backend.main import app
        from fastapi.testclient import TestClient
        with TestClient(app):
            pass

    def test_default_secret_refuses_startup(self, monkeypatch):
        """出厂默认值 my-rag-dev-secret-change-me → 拒启"""
        monkeypatch.setattr(config_settings, "JWT_SECRET",
                            "my-rag-dev-secret-change-me")
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            self._try_start()

    def test_empty_secret_refuses_startup(self, monkeypatch):
        """空值 → 拒启"""
        monkeypatch.setattr(config_settings, "JWT_SECRET", "")
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            self._try_start()

    def test_short_secret_refuses_startup(self, monkeypatch):
        """过短（<16 字符）→ 拒启"""
        monkeypatch.setattr(config_settings, "JWT_SECRET", "weak-secret")
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            self._try_start()

    def test_error_message_guides_config(self, monkeypatch):
        """错误信息含中文配置指引"""
        monkeypatch.setattr(config_settings, "JWT_SECRET", "short")
        with pytest.raises(RuntimeError) as exc_info:
            self._try_start()
        assert "JWT_SECRET 未配置或过弱" in str(exc_info.value)
        assert ".env" in str(exc_info.value)
        assert "16 字符" in str(exc_info.value)

    def test_strong_secret_starts_normally(self, monkeypatch):
        """强密钥（≥16 随机）→ 正常启动，health 可用"""
        monkeypatch.setattr(config_settings, "JWT_SECRET", "s" * 32)
        from backend.main import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            assert c.get("/api/health").status_code == 200
