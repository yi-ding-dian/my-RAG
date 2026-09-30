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

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from backend.services.parsers.probes import (probe_deepdoc_sync, probe_embedding_sdk,
                                     probe_gotenberg_sync,
                                     probe_llm_sdk, probe_mineru_sync,
                                     probe_minio, probe_mysql,
                                     probe_rerank_sync)

# 连接测试超时（秒）
LLM_TEST_TIMEOUT = 5.0
EMBEDDING_TEST_TIMEOUT = 5.0
MINERU_TEST_TIMEOUT = 3.0
GOTENBERG_TEST_TIMEOUT = 5.0
DEEPDOC_TEST_TIMEOUT = 8.0
MYSQL_TEST_TIMEOUT = 5.0
MINIO_TEST_TIMEOUT = 5.0
RERANK_TEST_TIMEOUT = 5.0
VISION_TEST_TIMEOUT = 5.0


class SettingsTester:
    """连接测试 mixin（parsers.probes 结果 {ok, latency_ms, reason} → 对外 {ok, latency_ms, message}）"""

    @staticmethod
    def _message(r: dict) -> dict:
        """parsers.probes 结果 {ok, latency_ms, reason} → 对外 {ok, latency_ms, message}

        skipped=True 原样透传：调用方据此把"未配置的可选功能"排除在
        「全部就绪」判定之外（否则新建档案永远测不通过）。
        """
        out = {"ok": r["ok"], "latency_ms": r["latency_ms"],
               "message": f"{r['reason']}（耗时 {r['latency_ms']}ms）"}
        if r.get("skipped"):
            out["skipped"] = True
        return out

    @staticmethod
    def _append(r: dict, detail: str) -> dict:
        """成功/失败都附加地址/对象细节（IP、端口、桶、数据库等）——
        失败时也要能看到"连的是哪个配置"，便于排查"""
        if detail:
            return {**r, "reason": f"{r['reason']} · {detail}"}
        return r

    @staticmethod
    def _probe_each(models: list, probe) -> list:
        """逐个探测一组模型条目（线程池并发），返回每条的明细

        `probe` 收一个条目、返回 parsers.probes 的 {ok, latency_ms, reason}。

        条目内要并发：LLM/图片模型列表可能有四五个，顺序测最坏是
        4×5s=20s（远超"点一下看看通不通"的耐心），并发后仍是一个超时窗口。

        每条带上 `url`：同名模型可能指向不同地址，光看名字分不清是哪个连不上。
        """
        def one(m: dict) -> dict:
            r = probe(m)
            return {"name": str(m.get("name") or ""),
                    "url": str(m.get("base_url") or ""),
                    "ok": bool(r["ok"]), "latency_ms": int(r["latency_ms"]),
                    "message": f"{r['reason']}（耗时 {r['latency_ms']}ms）"}

        with ThreadPoolExecutor(max_workers=max(1, min(8, len(models)))) as ex:
            return list(ex.map(one, models))

    @staticmethod
    def _aggregate(items: list, unit: str) -> dict:
        """一组模型的探测结果 → 单段结果 {ok, latency_ms, message, items}

        段级 ok = 全部通过；失败时 message 把每条的原因列出来（超长截断），
        前端还会按 items 逐条展开显示。
        """
        bad = [it for it in items if not it["ok"]]
        if not bad:
            msg = f"全部通过（{len(items)} {unit}）"
        else:
            detail = "；".join(
                f"{it['name'] or '未命名'}：{it['message']}" for it in bad)
            msg = f"{len(bad)}/{len(items)} 个连不上 —— {detail}"[:300]
        return {
            "ok": not bad,
            "latency_ms": max((it["latency_ms"] for it in items), default=0),
            "message": msg,
            "items": items,
        }

    @staticmethod
    def _probe_vision_single(item: dict) -> dict:
        """单个图片解析模型探活：GET {base_url}/models（只探活不推图）"""
        t0 = time.time()
        try:
            import httpx

            url = str(item["base_url"]).rstrip("/") + "/models"
            with httpx.Client(timeout=VISION_TEST_TIMEOUT) as client:
                resp = client.get(url, headers={
                    "Authorization":
                        f"Bearer {item.get('api_key') or 'EMPTY'}"})
            ms = int((time.time() - t0) * 1000)
            if resp.status_code >= 400:
                return {"ok": False, "latency_ms": ms,
                        "reason": f"服务返回 HTTP {resp.status_code}"}
            return {"ok": True, "latency_ms": ms, "reason": "连接正常"}
        except Exception as e:
            return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                    "reason": str(e)[:120]}

    @staticmethod
    async def _run(fn, *args):
        """统一探测入口：同步探测丢线程池，协程直接 await

        两件事都得在这儿办：
        1. 同步探测（httpx.Client / requests 那批）不能直接在事件循环里跑，
           会整段阻塞、连其它段的超时计时都跟着失真；
        2. 把调用包进协程，**同步抛出的异常才落在这个协程里**——否则
           `gather(return_exceptions=True)` 兜不住（它在参数求值阶段就炸了，
           一个坏段会把整次测试带崩）。
        """
        if asyncio.iscoroutinefunction(fn):
            return await fn(*args)
        return await asyncio.to_thread(fn, *args)

    def _probe_calls(self, profile: dict) -> dict:
        """段名 → 零参协程工厂（调用它才开始跑该段探测）"""
        return {
            "llm": lambda: self._run(
                self._test_llm, profile.get("llm") or {}),
            "embedding": lambda: self._run(
                self._test_embedding, profile.get("embedding") or {}),
            "mineru": lambda: self._run(
                self._test_mineru, profile.get("mineru") or {}),
            "gotenberg": lambda: self._run(
                self._test_gotenberg, profile.get("gotenberg") or {}),
            "rerank": lambda: self._run(
                self._test_rerank,
                (profile.get("retrieval") or {}).get("rerank") or {}),
            "vision": lambda: self._run(
                self._test_vision, profile.get("vision") or {}),
            "deepdoc": lambda: self._run(
                self._test_deepdoc, profile.get("deepdoc") or {}),
            "mysql": lambda: self._run(
                self._test_mysql, profile.get("mysql") or {}),
            "minio": lambda: self._run(
                self._test_minio, profile.get("minio") or {}),
            "vector_store": lambda: self._run(
                self._test_vector_store, profile.get("vector_store") or {}),
        }

    async def test_connections(self, profile: dict,
                               sections: Optional[Iterable[str]] = None) -> dict:
        """逐项测试 LLM / Embedding / MinerU / DeepDoc / MySQL / MinIO /
        Rerank / 向量存储，统一返回 {ok, latency_ms, message}

        - `sections` 只测点名的段（面板标题上的「测试」用，点「向量存储」不该
          还要等 LLM / DeepDoc 的超时）；不传 = 全测。段名拼错只是没人被点名，
          不报错——调用方的段名来自前端白名单，不靠这里兜底。
        - **并发**跑：各段互不依赖，顺序跑最坏要等各段超时之和（5+5+3+5+8+5+5
          +5+5 ≈ 46s），并发后最坏只等于最慢的那一段。
        - 单段抛异常不再拖垮整次测试：记成该段失败，其余段的结论照常返回
          （原来顺序执行时一个异常会让整个接口 500，白等一场还什么都看不到）。
        """
        calls = self._probe_calls(profile)
        keys = [k for k in calls if sections is None or k in set(sections)]
        results = await asyncio.gather(
            *(calls[k]() for k in keys), return_exceptions=True)
        out = {}
        for key, r in zip(keys, results):
            if isinstance(r, BaseException):
                out[key] = {"ok": False, "latency_ms": 0,
                            "message": f"探测异常: {str(r)[:120]}"}
            else:
                out[key] = r
        return out

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
        """测 llm 段里**每一个**已配置的模型条目（max_tokens=1，5s 超时）

        不只测激活的那个：模型列表本就是给切换用的，只测激活的会漏掉
        "切过去才发现连不通"。条目间线程池并发（见 _probe_each）。

        注意：OpenAI 客户端类经 settings.service 模块属性运行时解析
        （方法体内延迟 import 规避循环依赖；monkeypatch
        backend.services.settings.service.OpenAI 可替换实现）
        """
        from backend.services.settings import service as ss
        models = [m for m in (llm.get("models") or []) if isinstance(m, dict)]
        if not models:
            return self._message({"ok": False, "latency_ms": 0,
                                  "reason": "未配置 LLM 模型"})
        return self._aggregate(self._probe_each(
            models, lambda m: probe_llm_sdk(
                m, timeout=LLM_TEST_TIMEOUT, client_cls=ss.OpenAI)),
            "个模型")

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

    def _test_gotenberg(self, gotenberg: dict) -> dict:
        """Gotenberg 文档转换（Office → PDF）：GET {base_url}/health 探活（≤5s）

        只探活不试转换——转换要传实际文件、代价大；真正可用性由调用时兜底。
        地址未配置时返回 ok=False 提示（与 Rerank/图片解析模型同款语义）。
        """
        r = probe_gotenberg_sync(
            gotenberg,
            timeout=min(GOTENBERG_TEST_TIMEOUT,
                        float(gotenberg.get("timeout") or GOTENBERG_TEST_TIMEOUT)))
        # 成败都带地址：失败时最需要看清"连的是哪个地址"（_append 的约定）
        return self._message(
            self._append(r, str(gotenberg.get("base_url") or "")))

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

    def _test_vision(self, vision: dict) -> dict:
        """测 vision 段里**每一个**图片解析模型（GET {base_url}/models 探活）

        **不试推图**是刻意的——推图要传图片、代价大；这里只确认服务可达，
        真正的可用性由解析时的实际结果兜底（单图失败会跳过、不阻塞入库）。

        与 LLM 同理，不只测激活的那个。整个段没配（或条目都缺地址/模型名）
        → ok=False + skipped 提示（与 rerank 的"尚未启用"语义一致）。
        """
        models = [m for m in (vision.get("models") or [])
                  if isinstance(m, dict) and m.get("base_url") and m.get("model")]
        if not models:
            # skipped：未配置是**可选功能的正常状态**，不算连接失败——
            # 调用方的"全部就绪"判定应把它排除（否则新建档案永远测不通过）
            return self._message({
                "ok": False, "latency_ms": 0, "skipped": True,
                "reason": "未配置图片解析模型（需先添加，图片摘要才可用）"})
        return self._aggregate(
            self._probe_each(models, self._probe_vision_single), "个模型")

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
