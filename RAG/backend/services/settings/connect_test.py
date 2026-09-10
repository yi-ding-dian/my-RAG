"""档案连接测试（SettingsTester mixin，被 SettingsService 继承）

从 backend/services/settings/service.py 按职责拆分而来（行为零变化）：
逐项测试 LLM / Embedding / MinerU / DeepDoc / MySQL / MinIO，统一返回
{ok, latency_ms, message} 结构。探测逻辑统一在 services/parsers/probes.py。

- 以 mixin 形式提供（class SettingsService(SettingsTester)），test_connections
  及其 _test_* 方法仍以实例方法挂载在 SettingsService 上（测试 monkeypatch
  SettingsService._test_* 的既有用法不受影响）；
- OpenAI 客户端类经 backend.services.settings.service.OpenAI 模块属性运行时
  解析（方法体内延迟 import）：既避免循环依赖，也保持连接测试 monkeypatch
  "backend.services.settings.service.OpenAI" 的既有语义（patch 生效）。
"""
from __future__ import annotations

from backend.services.parsers.probes import (probe_deepdoc_sync, probe_embedding_sdk,
                                     probe_llm_sdk, probe_mineru_sync,
                                     probe_minio, probe_mysql,
                                     probe_rerank_sync)
from backend.services.settings.merge import active_llm_item

# 连接测试超时（秒）
LLM_TEST_TIMEOUT = 5.0
EMBEDDING_TEST_TIMEOUT = 5.0
MINERU_TEST_TIMEOUT = 3.0
DEEPDOC_TEST_TIMEOUT = 8.0
MYSQL_TEST_TIMEOUT = 5.0
MINIO_TEST_TIMEOUT = 5.0
RERANK_TEST_TIMEOUT = 5.0


class SettingsTester:
    """连接测试 mixin（parsers.probes 结果 {ok, latency_ms, reason} → 对外 {ok, latency_ms, message}）"""

    @staticmethod
    def _message(r: dict) -> dict:
        """parsers.probes 结果 {ok, latency_ms, reason} → 对外 {ok, latency_ms, message}"""
        return {"ok": r["ok"], "latency_ms": r["latency_ms"],
                "message": f"{r['reason']}（耗时 {r['latency_ms']}ms）"}

    @staticmethod
    def _append(r: dict, detail: str) -> dict:
        """成功/失败都附加地址/对象细节（IP、端口、桶、数据库等）——
        失败时也要能看到"连的是哪个配置"，便于排查"""
        if detail:
            return {**r, "reason": f"{r['reason']} · {detail}"}
        return r

    async def test_connections(self, profile: dict) -> dict:
        """逐项测试 LLM / Embedding / MinerU / DeepDoc / MySQL / MinIO /
        Rerank / 向量存储，统一 {ok, latency_ms, message}"""
        return {
            "llm": self._test_llm(profile.get("llm") or {}),
            "embedding": self._test_embedding(profile.get("embedding") or {}),
            "mineru": self._test_mineru(profile.get("mineru") or {}),
            "deepdoc": await self._test_deepdoc(profile.get("deepdoc") or {}),
            "mysql": await self._test_mysql(profile.get("mysql") or {}),
            "minio": await self._test_minio(profile.get("minio") or {}),
            "vector_store": await self._test_vector_store(
                profile.get("vector_store") or {}),
            "rerank": self._test_rerank(
                (profile.get("retrieval") or {}).get("rerank") or {}),
        }

    async def _test_vector_store(self, vs_cfg: dict) -> dict:
        """向量存储：chroma=本地目录探活；milvus=连接探活（≤5s）"""
        import time
        from pathlib import Path
        from backend.config import CHROMA_DIR
        backend = str(vs_cfg.get("backend") or "chroma")
        if backend == "milvus":
            uri = str(vs_cfg.get("milvus_uri") or "")
            t0 = time.time()
            try:
                from pymilvus import MilvusClient
                from backend.services.vector_store import normalize_milvus_uri
                MilvusClient(uri=normalize_milvus_uri(uri)).list_collections()
                ok, reason = True, "连接正常"
            except Exception as e:
                ok, reason = False, f"连接失败: {str(e)[:120]}"
            return self._message({
                "ok": ok, "latency_ms": int((time.time() - t0) * 1000),
                "reason": reason + (f" · Milvus: {uri}" if ok else "")})
        ok = Path(CHROMA_DIR).is_dir()
        return self._message({
            "ok": ok, "latency_ms": 0,
            "reason": ("本地目录可用" if ok else f"本地目录不存在: {CHROMA_DIR}")
                      + (f" · Chroma: {CHROMA_DIR}" if ok else "")})

    def _test_llm(self, llm: dict) -> dict:
        """对激活模型条目发最小 chat 请求（max_tokens=1），5s 超时（parsers.probes SDK 形态）

        注意：OpenAI 客户端类经 settings.service 模块属性运行时解析
        （方法体内延迟 import 规避循环依赖；monkeypatch
        backend.services.settings.service.OpenAI 可替换实现）
        """
        from backend.services.settings import service as ss
        item = active_llm_item(llm)
        r = probe_llm_sdk(
            item, timeout=LLM_TEST_TIMEOUT, client_cls=ss.OpenAI)
        return self._message(self._append(r, str(item.get("base_url") or "")))

    def _test_embedding(self, embedding: dict) -> dict:
        """发 1 条 embed，5s 超时，返回实际维度（parsers.probes SDK 形态）"""
        from backend.services.settings import service as ss
        r = probe_embedding_sdk(
            embedding, timeout=EMBEDDING_TEST_TIMEOUT, client_cls=ss.OpenAI)
        return self._message(self._append(
            r, str(embedding.get("base_url") or "")))

    def _test_mineru(self, mineru: dict) -> dict:
        """健康探测（/health → /api/health → 根路径，≤3s，<400 可用）"""
        r = probe_mineru_sync(
            mineru, timeout=min(
                MINERU_TEST_TIMEOUT,
                float(mineru.get("timeout") or MINERU_TEST_TIMEOUT)),
            ok_under=400)
        return self._message(self._append(r, str(mineru.get("url") or "")))

    def _test_rerank(self, rerank: dict) -> dict:
        """Rerank 探测（POST {base_url}/rerank 最小请求，≤5s；

        未启用（enabled=false）或地址/模型未配置时返回 ok=False 提示——
        前端测试按钮按结果 toast，与"未配置"语义一致
        """
        if not bool(rerank.get("enabled")):
            return self._message({
                "ok": False, "latency_ms": 0,
                "reason": "尚未启用（Rerank 重排序开关为关）"})
        r = probe_rerank_sync(
            rerank, timeout=min(
                RERANK_TEST_TIMEOUT,
                float(rerank.get("timeout") or RERANK_TEST_TIMEOUT)))
        return self._message(self._append(r, str(rerank.get("base_url") or "")))

    async def _test_deepdoc(self, deepdoc: dict) -> dict:
        """RAGFlow 登录探测（RSA 加密密码 POST /v1/user/login，≤8s）"""
        return self._message(probe_deepdoc_sync(
            deepdoc, timeout=min(
                DEEPDOC_TEST_TIMEOUT,
                float(deepdoc.get("timeout") or DEEPDOC_TEST_TIMEOUT))))

    async def _test_mysql(self, mysql: dict) -> dict:
        """数据库探测：配置什么数据库就显示什么数据库（URL 覆盖/直连同等对待）
        ——成功直接显示数据源标识；失败保留错误 + 数据源"""
        r = await probe_mysql(mysql, timeout=MYSQL_TEST_TIMEOUT)
        url = str(mysql.get("url") or "").strip()
        src = (url[:100] if url
               else f"{mysql.get('host') or '127.0.0.1'}:"
                    f"{mysql.get('port') or 3306}/{mysql.get('database') or ''}")
        if url:
            # URL 覆盖模式（sqlite/其他）：无需直连测试，直接展示数据源（视为可用）
            return {"ok": True, "latency_ms": r["latency_ms"],
                    "message": f"{src}（耗时 {r['latency_ms']}ms）"}
        reason = src if r["ok"] else f"{r['reason']} · {src}"
        return {"ok": r["ok"], "latency_ms": r["latency_ms"],
                "message": f"{reason}（耗时 {r['latency_ms']}ms）"}

    async def _test_minio(self, minio: dict) -> dict:
        """MinIO 桶探测（bucket_exists），5s 超时"""
        r = await probe_minio(minio, timeout=MINIO_TEST_TIMEOUT)
        return self._message(self._append(
            r, f"{minio.get('endpoint') or ''} (桶 {minio.get('bucket') or ''})"))
