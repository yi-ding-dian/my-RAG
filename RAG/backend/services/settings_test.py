"""档案连接测试（SettingsTester mixin，被 SettingsService 继承）

从 backend/services/settings_service.py 按职责拆分而来（行为零变化）：
逐项测试 LLM / Embedding / MinerU / DeepDoc / MySQL / MinIO，统一返回
{ok, latency_ms, message} 结构。探测逻辑统一在 services/probes.py。

- 以 mixin 形式提供（class SettingsService(SettingsTester)），test_connections
  及其 _test_* 方法仍以实例方法挂载在 SettingsService 上（测试 monkeypatch
  SettingsService._test_* 的既有用法不受影响）；
- OpenAI 客户端类经 backend.services.settings_service.OpenAI 模块属性运行时
  解析（方法体内延迟 import）：既避免循环依赖，也保持连接测试 monkeypatch
  "backend.services.settings_service.OpenAI" 的既有语义（patch 生效）。
"""
from __future__ import annotations

from backend.services.probes import (probe_deepdoc_sync, probe_embedding_sdk,
                                     probe_llm_sdk, probe_mineru_sync,
                                     probe_minio, probe_mysql)
from backend.services.settings_merge import active_llm_item

# 连接测试超时（秒）
LLM_TEST_TIMEOUT = 5.0
EMBEDDING_TEST_TIMEOUT = 5.0
MINERU_TEST_TIMEOUT = 3.0
DEEPDOC_TEST_TIMEOUT = 8.0
MYSQL_TEST_TIMEOUT = 5.0
MINIO_TEST_TIMEOUT = 5.0


class SettingsTester:
    """连接测试 mixin（probes 结果 {ok, latency_ms, reason} → 对外 {ok, latency_ms, message}）"""

    @staticmethod
    def _message(r: dict) -> dict:
        """probes 结果 {ok, latency_ms, reason} → 对外 {ok, latency_ms, message}"""
        return {"ok": r["ok"], "latency_ms": r["latency_ms"],
                "message": f"{r['reason']}（耗时 {r['latency_ms']}ms）"}

    async def test_connections(self, profile: dict) -> dict:
        """逐项测试 LLM / Embedding / MinerU / DeepDoc / MySQL / MinIO，
        统一 {ok, latency_ms, message}"""
        return {
            "llm": self._test_llm(profile.get("llm") or {}),
            "embedding": self._test_embedding(profile.get("embedding") or {}),
            "mineru": self._test_mineru(profile.get("mineru") or {}),
            "deepdoc": await self._test_deepdoc(profile.get("deepdoc") or {}),
            "mysql": await self._test_mysql(profile.get("mysql") or {}),
            "minio": await self._test_minio(profile.get("minio") or {}),
        }

    def _test_llm(self, llm: dict) -> dict:
        """对激活模型条目发最小 chat 请求（max_tokens=1），5s 超时（probes SDK 形态）

        注意：OpenAI 客户端类经 settings_service 模块属性运行时解析
        （方法体内延迟 import 规避循环依赖；monkeypatch
        backend.services.settings_service.OpenAI 可替换实现）
        """
        from backend.services import settings_service as ss
        return self._message(probe_llm_sdk(
            active_llm_item(llm), timeout=LLM_TEST_TIMEOUT, client_cls=ss.OpenAI))

    def _test_embedding(self, embedding: dict) -> dict:
        """发 1 条 embed，5s 超时，返回实际维度（probes SDK 形态）"""
        from backend.services import settings_service as ss
        return self._message(probe_embedding_sdk(
            embedding, timeout=EMBEDDING_TEST_TIMEOUT, client_cls=ss.OpenAI))

    def _test_mineru(self, mineru: dict) -> dict:
        """健康探测（/health → /api/health → 根路径，≤3s，<400 可用）"""
        return self._message(probe_mineru_sync(
            mineru, timeout=min(
                MINERU_TEST_TIMEOUT,
                float(mineru.get("timeout") or MINERU_TEST_TIMEOUT)),
            ok_under=400))

    async def _test_deepdoc(self, deepdoc: dict) -> dict:
        """RAGFlow 登录探测（RSA 加密密码 POST /v1/user/login，≤8s）"""
        return self._message(probe_deepdoc_sync(
            deepdoc, timeout=min(
                DEEPDOC_TEST_TIMEOUT,
                float(deepdoc.get("timeout") or DEEPDOC_TEST_TIMEOUT))))

    async def _test_mysql(self, mysql: dict) -> dict:
        """aiomysql 异步 connect + ping，5s 超时"""
        return self._message(await probe_mysql(mysql, timeout=MYSQL_TEST_TIMEOUT))

    async def _test_minio(self, minio: dict) -> dict:
        """MinIO 桶探测（bucket_exists），5s 超时"""
        return self._message(await probe_minio(minio, timeout=MINIO_TEST_TIMEOUT))
