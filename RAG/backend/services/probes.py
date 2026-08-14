"""统一探测服务：LLM / Embedding / MinerU / DeepDoc / MySQL / MinIO 连接探测

集中注册六个探测，统一返回 {ok: bool, latency_ms: int, reason: str}
（reason 为中文可读描述；成功时"连接成功（…）"，失败时含具体原因）。
调用方按各自对外契约映射文案（如 settings 的 {ok, latency_ms, message}、
stats precheck / parser_probe 的 {available, reason}）。

- 探测失败一律不抛异常（=不可用 + 中文 reason），调用方直接消费结果；
- 超时 / 健康端点 / 判定阈值单点维护：MINERU_ENDPOINTS、默认超时、
  ok_under（MinerU 健康判定：<500 视为服务在，settings 同步形态沿用 <400）；
- 形态说明（历史契约兼容，行为零变化）：
  - 异步形态 probe_mineru/probe_deepdoc（httpx.AsyncClient）：parser_probe、
    parser_client 使用（其测试 mock httpx.AsyncClient）；
  - 同步形态 probe_mineru_sync/probe_deepdoc_sync（httpx.get/post）：
    settings_service 连接测试使用（其测试 mock httpx.get/post）；
  - probe_llm/probe_embedding：httpx 轻量探测（GET /models、POST /embeddings），
    stats precheck 使用；
  - probe_llm_sdk/probe_embedding_sdk：OpenAI SDK 形态（设置页连接测试，
    返回实际向量维度；client_cls 注入以便调用方测试替换）；
- cfg 入参兼容 dict 与配置对象（SimpleNamespace/pydantic），防御式取值；
- 循环导入规避：依赖的服务模块（deepdoc_client/aiomysql/minio）在函数内
  局部导入。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx
from openai import OpenAI

# MinerU 官方 API 服务可能的健康端点（按序探测；与历史 parser_client 端点一致）
MINERU_ENDPOINTS = ("/health", "/api/health", "/")

# 默认超时（秒；调用方可显式传 timeout 覆盖）
DEFAULT_MINERU_TIMEOUT = 3.0
DEFAULT_DEEPDOC_TIMEOUT = 8.0
DEFAULT_LLM_TIMEOUT = 5.0
DEFAULT_EMBEDDING_TIMEOUT = 5.0
DEFAULT_MYSQL_TIMEOUT = 5.0
DEFAULT_MINIO_TIMEOUT = 5.0


def _cfg_get(cfg, *keys, default=None):
    """cfg（dict 或对象）防御式取字段值（None 时按序尝试后续键）"""
    for key in keys:
        try:
            value = cfg.get(key) if isinstance(cfg, dict) else getattr(cfg, key)
        except (AttributeError, TypeError):
            value = None
        if value is not None:
            return value
    return default


def _cfg_str(cfg, *keys, default: str = "") -> str:
    return str(_cfg_get(cfg, *keys, default=default) or default)


def _cfg_timeout(cfg, default: float) -> float:
    raw = _cfg_get(cfg, "timeout")
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _coerce_bool(value) -> bool:
    """布尔宽松转换（"false"/"0"/False → False，其余真值 → True）"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _join_errors(errors: list, fallback: str) -> str:
    return "；".join(errors)[:200] or fallback


# ==================== MinerU ====================


async def probe_mineru(cfg=None, *, timeout: Optional[float] = None,
                       ok_under: int = 500) -> dict:
    """MinerU 健康探测（异步形态，httpx.AsyncClient；parser_probe/parser_client 用）

    - 端点：/health → /api/health → 根路径，任一响应 < ok_under 即可用
      （默认 <500：非服务器错误即视为服务在，与 parser_client 历史语义一致）
    - 超时默认 3s（可显式传 timeout；未传则取 cfg.timeout）
    - 失败不抛异常：reason 为各端点错误拼接（超时后不再试后续端点）
    """
    api_url = _cfg_str(cfg, "api_url", "url").strip().rstrip("/")
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_MINERU_TIMEOUT))
    t0 = time.monotonic()
    if not api_url:
        return {"ok": False, "latency_ms": 0, "reason": "MinerU 服务地址未配置"}
    errors: list = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for ep in MINERU_ENDPOINTS:
                try:
                    resp = await client.get(f"{api_url}{ep}")
                    if resp.status_code < ok_under:
                        return {
                            "ok": True,
                            "latency_ms": int((time.monotonic() - t0) * 1000),
                            "reason": f"连接成功（HTTP {resp.status_code}）",
                        }
                    errors.append(f"HTTP {resp.status_code}")
                except httpx.TimeoutException:
                    errors.append(f"连接超时（{int(timeout)}s）")
                    break  # 超时通常各端点一致，不再逐个尝试
                except Exception as e:
                    errors.append(f"连接失败: {str(e)[:120]}")
    except Exception as e:
        errors.append(f"连接失败: {str(e)[:120]}")
    return {"ok": False,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "reason": _join_errors(errors, "无响应")}


def probe_mineru_sync(cfg=None, *, timeout: Optional[float] = None,
                      ok_under: int = 400) -> dict:
    """MinerU 健康探测（同步形态，httpx.get；settings_service 连接测试用）

    - ok_under 默认 400（历史 _test_mineru 契约：4xx 视为不可用并继续试
      下一端点）；probe_mineru 异步形态默认 500（解析前可用性探测契约）
    - 其余语义与 probe_mineru 一致（端点/超时/错误拼接）
    """
    api_url = _cfg_str(cfg, "api_url", "url").strip().rstrip("/")
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_MINERU_TIMEOUT))
    t0 = time.monotonic()
    if not api_url:
        return {"ok": False, "latency_ms": 0, "reason": "MinerU 服务地址未配置"}
    errors: list = []
    for ep in MINERU_ENDPOINTS:
        try:
            resp = httpx.get(f"{api_url}{ep}", timeout=timeout)
            if resp.status_code < ok_under:
                return {
                    "ok": True,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "reason": f"连接成功（HTTP {resp.status_code}）",
                }
            errors.append(f"HTTP {resp.status_code}")
        except httpx.TimeoutException:
            errors.append(f"连接超时（{int(timeout)}s）")
            break
        except Exception as e:
            errors.append(f"连接失败: {str(e)[:120]}")
    return {"ok": False,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "reason": _join_errors(errors, "无响应")}


# ==================== DeepDoc ====================


def _deepdoc_login_payload(cfg) -> dict:
    from backend.services.deepdoc_client import _rsa_encrypt_password
    return {"email": _cfg_str(cfg, "email").strip(),
            "password": _rsa_encrypt_password(_cfg_str(cfg, "password"))}


def _deepdoc_result(t0, base_url, status_code, token) -> dict:
    """登录响应判定 → 统一结果（成功 = 200 + 响应头 token）"""
    if status_code == 200 and token:
        return {"ok": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接成功（{base_url}）"}
    return {"ok": False,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "reason": f"登录失败（HTTP {status_code}）"}


def _deepdoc_timeout_result(t0, timeout: float) -> dict:
    return {"ok": False,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "reason": f"连接超时（{int(timeout)}s）"}


async def probe_deepdoc(cfg=None, *, timeout: Optional[float] = None) -> dict:
    """DeepDoc 登录探测（异步形态，httpx.AsyncClient；parser_probe 用）

    与 deepdoc_client 实际解析登录同契约（RSA 加密密码 + 响应头 token），
    探测成功 = 登录 200 且响应头带 HTTP_AUTHORIZATION/Authorization。
    """
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_DEEPDOC_TIMEOUT))
    base_url = _cfg_str(cfg, "base_url").strip().rstrip("/")
    t0 = time.monotonic()
    if not base_url:
        return {"ok": False, "latency_ms": 0, "reason": "DeepDoc 服务地址未配置"}
    from backend.services.deepdoc_client import _rsa_encrypt_password
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/v1/user/login", json=_deepdoc_login_payload(cfg))
        token = (resp.headers.get("HTTP_AUTHORIZATION")
                 or resp.headers.get("Authorization") or "")
        return _deepdoc_result(t0, base_url, resp.status_code, token)
    except httpx.TimeoutException:
        return _deepdoc_timeout_result(t0, timeout)
    except Exception as e:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接失败: {str(e)[:150]}"}


def probe_deepdoc_sync(cfg=None, *, timeout: Optional[float] = None) -> dict:
    """DeepDoc 登录探测（同步形态，httpx.post；settings_service 连接测试用）

    注意：settings_service.test_connections 在 async 上下文调用，本函数为
    真正同步实现（不可用 asyncio.run 包裹）。
    """
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_DEEPDOC_TIMEOUT))
    base_url = _cfg_str(cfg, "base_url").strip().rstrip("/")
    t0 = time.monotonic()
    if not base_url:
        return {"ok": False, "latency_ms": 0, "reason": "DeepDoc 服务地址未配置"}
    from backend.services.deepdoc_client import _rsa_encrypt_password
    try:
        resp = httpx.post(
            f"{base_url}/v1/user/login", json=_deepdoc_login_payload(cfg),
            timeout=timeout)
        token = (resp.headers.get("HTTP_AUTHORIZATION")
                 or resp.headers.get("Authorization") or "")
        return _deepdoc_result(t0, base_url, resp.status_code, token)
    except httpx.TimeoutException:
        return _deepdoc_timeout_result(t0, timeout)
    except Exception as e:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接失败: {str(e)[:150]}"}


# ==================== LLM / Embedding（httpx 轻量形态，stats precheck 用） ====================


async def probe_llm(cfg=None, *, timeout: Optional[float] = None) -> dict:
    """LLM 轻量探测：GET {base_url}/models（OpenAI 兼容端点，deepseek 支持），
    2xx 即可用；api_key 非空时携带 Authorization"""
    base_url = _cfg_str(cfg, "base_url").strip().rstrip("/")
    if not base_url:
        return {"ok": False, "latency_ms": 0, "reason": "LLM 服务地址未配置"}
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_LLM_TIMEOUT))
    headers = {}
    api_key = _cfg_str(cfg, "api_key").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
        if 200 <= resp.status_code < 300:
            return {"ok": True,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "reason": f"连接成功（HTTP {resp.status_code}）"}
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"服务异常（HTTP {resp.status_code}）"}
    except httpx.TimeoutException:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接超时（{int(timeout)}s）"}
    except Exception as e:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接失败: {str(e)[:150]}"}


async def probe_embedding(cfg=None, *, timeout: Optional[float] = None) -> dict:
    """Embedding 轻量探测：POST {base_url}/embeddings 一条测试文本，
    2xx 且返回向量数组即可用"""
    base_url = _cfg_str(cfg, "base_url").strip().rstrip("/")
    model = _cfg_str(cfg, "model").strip()
    if not base_url:
        return {"ok": False, "latency_ms": 0, "reason": "Embedding 服务地址未配置"}
    if not model:
        return {"ok": False, "latency_ms": 0, "reason": "Embedding 模型未配置"}
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_EMBEDDING_TIMEOUT))
    headers = {}
    api_key = _cfg_str(cfg, "api_key").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base_url}/embeddings",
                                     headers=headers,
                                     json={"model": model, "input": "连接测试"})
        if 200 <= resp.status_code < 300:
            try:
                data = resp.json() or {}
                if data.get("data") and len(data["data"]) > 0:
                    return {"ok": True,
                            "latency_ms": int((time.monotonic() - t0) * 1000),
                            "reason": "连接成功"}
            except ValueError:
                pass
            return {"ok": False,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "reason": "服务响应异常（无向量数据）"}
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"服务异常（HTTP {resp.status_code}）"}
    except httpx.TimeoutException:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接超时（{int(timeout)}s）"}
    except Exception as e:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接失败: {str(e)[:150]}"}


# ==================== LLM / Embedding（OpenAI SDK 形态，设置页连接测试用） ====================


def probe_llm_sdk(cfg=None, *, timeout: Optional[float] = None,
                  client_cls=None) -> dict:
    """LLM SDK 探测：最小 chat 请求（max_tokens=1），返回 {ok, latency_ms, reason}

    client_cls: OpenAI 客户端类注入（调用方传其模块引用，便于测试替换）"""
    client_cls = client_cls or OpenAI
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_LLM_TIMEOUT))
    t0 = time.monotonic()
    try:
        client = client_cls(
            base_url=_cfg_str(cfg, "base_url").strip(),
            api_key=(_cfg_str(cfg, "api_key") or "lm-studio").strip(),
            timeout=timeout,
        )
        resp = client.chat.completions.create(
            model=_cfg_str(cfg, "model").strip() or "unknown",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        content = (resp.choices[0].message.content or "").strip()
        return {"ok": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接成功（{_cfg_get(cfg, 'model')}）"
                          + (f"，返回: {content[:50]}" if content else "")}
    except Exception as e:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接失败: {str(e)[:200]}"}


def probe_embedding_sdk(cfg=None, *, timeout: Optional[float] = None,
                        client_cls=None) -> dict:
    """Embedding SDK 探测：发 1 条 embed，返回实际向量维度"""
    client_cls = client_cls or OpenAI
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_EMBEDDING_TIMEOUT))
    t0 = time.monotonic()
    try:
        client = client_cls(
            base_url=_cfg_str(cfg, "base_url").strip(),
            api_key=(_cfg_str(cfg, "api_key") or "vllm").strip(),
            timeout=timeout,
        )
        resp = client.embeddings.create(
            model=_cfg_str(cfg, "model").strip() or "unknown",
            input="test",
        )
        dim = len(resp.data[0].embedding)
        return {"ok": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接成功，向量维度: {dim}"}
    except Exception as e:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接失败: {str(e)[:200]}"}


# ==================== MySQL / MinIO（设置页连接测试用） ====================


async def probe_mysql(cfg=None, *, timeout: Optional[float] = None) -> dict:
    """MySQL 直连探测：aiomysql 异步 connect + SELECT 1"""
    import aiomysql
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_MYSQL_TIMEOUT))
    t0 = time.monotonic()
    url = _cfg_str(cfg, "url").strip()
    if url and not url.startswith("mysql"):
        # URL 覆盖模式（如测试注入 sqlite），无直连可测
        return {"ok": False, "latency_ms": 0,
                "reason": "当前为 URL 覆盖模式（非 mysql 直连），跳过直连测试"}
    host = _cfg_str(cfg, "host") or "127.0.0.1"
    try:
        port = int(_cfg_get(cfg, "port") or 3306)
    except (TypeError, ValueError):
        port = 3306
    user = _cfg_str(cfg, "user").strip()
    password = _cfg_str(cfg, "password").strip()
    database = _cfg_str(cfg, "database").strip()
    try:
        async def _connect():
            conn = await aiomysql.connect(
                host=host, port=port, user=user, password=password,
                db=database or None, connect_timeout=timeout)
            try:
                cur = await conn.cursor()
                await cur.execute("SELECT 1")
                await cur.fetchone()
                await cur.close()
            finally:
                conn.close()
        await asyncio.wait_for(_connect(), timeout=timeout)
        return {"ok": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接成功（{host}:{port}/{database}）"}
    except asyncio.TimeoutError:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接超时（{int(timeout)}s）"}
    except Exception as e:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接失败: {str(e)[:200]}"}


async def probe_minio(cfg=None, *, timeout: Optional[float] = None) -> dict:
    """MinIO 桶探测（bucket_exists，urllib3 超时）"""
    from minio import Minio
    timeout = (float(timeout) if timeout is not None
               else _cfg_timeout(cfg, DEFAULT_MINIO_TIMEOUT))
    t0 = time.monotonic()
    endpoint = _cfg_str(cfg, "endpoint").strip()
    access_key = _cfg_str(cfg, "access_key").strip()
    secret_key = _cfg_str(cfg, "secret_key").strip()
    bucket = _cfg_str(cfg, "bucket") or "my-rag"
    secure = _coerce_bool(_cfg_get(cfg, "secure"))
    if not endpoint:
        return {"ok": False, "latency_ms": 0, "reason": "endpoint 未配置"}
    try:
        def _check() -> bool:
            import urllib3
            client = Minio(endpoint, access_key=access_key,
                           secret_key=secret_key, secure=secure,
                           http_client=urllib3.PoolManager(
                               timeout=urllib3.Timeout(connect=timeout,
                                                       read=timeout)))
            return client.bucket_exists(bucket)
        exists = await asyncio.wait_for(asyncio.to_thread(_check),
                                        timeout=timeout)
        msg = (f"连接成功，桶 {bucket} 存在" if exists
               else f"连接成功，桶 {bucket} 不存在（上传时自动创建）")
        return {"ok": True,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": msg}
    except asyncio.TimeoutError:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接超时（{int(timeout)}s）"}
    except Exception as e:
        return {"ok": False,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "reason": f"连接失败: {str(e)[:200]}"}
