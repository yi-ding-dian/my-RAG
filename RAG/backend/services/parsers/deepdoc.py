"""DeepDoc 解析客户端（通过 RAGFlow API，默认本机 9380 ragflow-server）

DeepDoc 是 ragflow-server 内置进程内解析器（ONNX OCR + 版面识别 +
表格结构识别），非独立服务；核心价值：**表格输出为 HTML <table>
文本可检索**（vs MinerU 表格为图片不可检索）。

调用链（DEEPDOC_BASE_URL 配置，默认本机 9380）：
1. POST /v1/user/login {"email", "password": RSA-PKCS1v15(公钥, Base64(密码))}
   → 响应头 HTTP_AUTHORIZATION 拿登录 token（itsdangerous）
2. POST /v1/api/new_token {"dialog_id": "ragflow"}，Authorization 头**不带
   Bearer 前缀**（直接放 token 值）→ {"token": "ragflow-xxx"}
   （/api/v1 接口校验 APIToken，登录 token 无效！）
3. POST /api/v1/datasets {"name"（唯一，prefix+时间戳+随机）,
   "chunk_method": "naive", "parser_config": {"layout_recognize": "DeepDOC"}}
   → dataset_id（parser_config 严格 pydantic 校验，未知字段报错）
4. POST /api/v1/datasets/{id}/documents multipart file → document_id
   （上传不自动解析）
5. POST /api/v1/datasets/{id}/chunks {"document_ids": [...]} → 触发解析
   （解析中重复触发报错）
6. GET /api/v1/datasets/{id}/documents 轮询 run/progress/progress_msg 至
   DONE（FAILED 时 error 带进异常）
7. GET /api/v1/datasets/{id}/documents/{doc_id}/chunks?page=1&page_size=200
   → {"total": n, "chunks": [{content, positions, ...}]}
   ——content 含 HTML <table>（DeepDoc 特征）；无图片内容
8. DELETE /api/v1/datasets {"ids": [dataset_id]} → 清理临时数据集
   （finally 保证，不留垃圾）

失败路径：任一步失败抛 RuntimeError（中文，含步骤名）；解析 FAILED 时
progress_msg/error 带进异常；清理必须 finally 执行。

并发/重入安全：模块级 asyncio.Semaphore(1)。RAGFlow 单文档解析有并发
限制（解析中重复触发报错，实测契约），且多任务并发互相拖慢、进度混淆；
信号量 1 串行化全部 DeepDoc 解析——并发场景排队等待，换来契约可靠与
失败诊断清晰。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

# 轮询间隔（秒；测试 monkeypatch 缩短加速）
_POLL_INTERVAL = 5.0
# chunks 分页大小（RAGFlow 单页上限，实测 200 可拿全量 188）
_PAGE_SIZE = 200

# RAGFlow 登录 RSA 公钥（硬编码，来源：KnowFlow web/src/utils/index.ts 的
# rsaPsw，与 ragflow-server 端登录校验私钥配对；探测实测有效）。
# 登录密码加密算法：先对密码做标准 Base64 编码，再 PKCS1v15 填充 RSA
# 加密（2048 位），最后输出 Base64——与 JSEncrypt.encrypt(Base64.encode(pwd))
# 完全等价。
_RAGFLOW_RSA_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArq9XTUSeYr2+N1h3Afl/"
    "z8Dse/2yD0ZGrKwx+EEEcdsBLca9Ynmx3nIB5obmLlSfmskLpBo0UACBmB5rEjBp"
    "2Q2f3AG3Hjd4B+gNCG6BDaawuDlgANIhGnaTLrIqWrrcm4EMzJOnAOI1fgzJRsOO"
    "UEfaS318Eq9OVO3apEyCCt0lOQK6PuksduOjVxtltDav+guVAA068NrPYmRNabVK"
    "RNLJpL8w4D44sfth5RvZ3q9t+6RTArpEtc5sh5ChzvqPOzKGMXW83C95TxmXqpbK"
    "6olN4RevSfVjEAgCydH6HN6OhtOQEcnrU97r9H0iZOWwbw3pVrZiUkuRD1R56Wzs"
    "2wIDAQAB"
    "-----END PUBLIC KEY-----"
)

# 模块级信号量：串行化全部 DeepDoc 解析（选择 1 的理由见模块 docstring）
_deepdoc_semaphore = asyncio.Semaphore(1)


# ==================== RSA 加密 ====================

def _rsa_encrypt_password(password: str) -> str:
    """RSA-PKCS1v15 加密 Base64(密码)，返回 Base64 密文

    复刻 KnowFlow web/src/utils/index.ts 的 rsaPsw（JSEncrypt）：
    encryptor.encrypt(Base64.encode(password))——先标准 Base64 编码密码，
    再 PKCS1v15 填充 RSA 加密（2048 位），最终 Base64 输出。
    """
    password = (password or "").strip()
    payload = base64.b64encode(password.encode("utf-8"))
    public_key = serialization.load_pem_public_key(
        _RAGFLOW_RSA_PUBLIC_KEY.encode("utf-8"))
    encrypted = public_key.encrypt(payload, padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("utf-8")


# ==================== 响应解析辅助 ====================

def _extract_data(payload) -> object:
    """RAGFlow 响应体 {code, data} 包装解包（兼容探测实测的简化结构）

    data 可能是 dict（登录/建数据集/chunks）或 list（上传文档返回
    文档对象列表），两种都解包。
    """
    if isinstance(payload, dict) and isinstance(payload.get("data"),
                                                (dict, list)):
        return payload["data"]
    return payload


def _error_msg(resp) -> str:
    """RAGFlow 错误响应 message 提取（{code, message} 或纯文本）"""
    try:
        data = resp.json()
        if isinstance(data, dict):
            msg = data.get("message") or data.get("detail")
            if msg:
                return str(msg)
    except Exception:
        pass
    return resp.text[:200]


def _doc_items(payload) -> list:
    """文档列表条目：兼容 data.docs / data.items / data（列表）三种形态"""
    data = _extract_data(payload)
    if isinstance(data, dict):
        for key in ("docs", "items", "documents"):
            val = data.get(key)
            if isinstance(val, list):
                return val
    if isinstance(data, list):
        return data
    return []


def _position_sort_key(chunk: dict, index: int) -> tuple:
    """跨页拼接排序键：按 positions[0] 的页序 + 页内纵坐标

    RAGFlow chunks 接口返回顺序不保证跨页阅读序；DeepDoc 每个切块带
    页面坐标 positions（形如 [{page_idx, top, left, width, height}]，
    page_idx 0 基）。一个切块通常位于单页内，取其首坐标：先按页序、
    再按页内纵向位置（top）排序，即还原文档阅读顺序——简单可靠。
    无 positions（如文本块）回退到接口返回顺序（index 兜底），保证
    排序稳定不丢块。
    """
    positions = chunk.get("positions") or []
    if positions and isinstance(positions[0], dict):
        page = positions[0].get("page_idx")
        if isinstance(page, (int, float)):
            return (int(page), float(positions[0].get("top") or 0), index)
    return (0, 0, index)


# ==================== 调用链各步骤 ====================

async def _login(client: httpx.AsyncClient, base_url: str,
                 cfg) -> str:
    """步骤1：登录，响应头 HTTP_AUTHORIZATION 拿登录 token（itsdangerous）"""
    try:
        password = _rsa_encrypt_password(cfg.password)
        resp = await client.post(
            f"{base_url}/v1/user/login",
            json={"email": cfg.email, "password": password})
        if resp.status_code != 200:
            raise RuntimeError(
                f"登录失败（HTTP {resp.status_code}）: {_error_msg(resp)}")
        token = (resp.headers.get("HTTP_AUTHORIZATION")
                 or resp.headers.get("Authorization") or "")
        token = token.removeprefix("Bearer ").strip()
        if not token:
            raise RuntimeError("登录失败：响应头缺少 HTTP_AUTHORIZATION")
        return token
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"登录 ragflow-server 失败: {e}") from e


async def _create_api_key(client: httpx.AsyncClient, base_url: str,
                          login_token: str) -> str:
    """步骤2：创建 API key（/api/v1 校验 APIToken，登录 token 无效）

    new_token 的 Authorization 头直接放登录 token 值（无 Bearer 前缀）
    """
    try:
        resp = await client.post(
            f"{base_url}/v1/api/new_token",
            json={"dialog_id": "ragflow"},
            headers={"Authorization": login_token})
        if resp.status_code != 200:
            raise RuntimeError(
                f"创建 API key 失败（HTTP {resp.status_code}）: {_error_msg(resp)}")
        data = _extract_data(resp.json())
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError(
                f"创建 API key 失败：响应无 token 字段（{resp.text[:200]}）")
        return str(token)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"创建 RAGFlow API key 失败: {e}") from e


async def _create_dataset(client: httpx.AsyncClient, base_url: str,
                          headers: dict, cfg) -> str:
    """步骤3：建临时数据集（名称唯一：prefix+时间戳+随机，防重名）"""
    name = f"{cfg.dataset_prefix}{int(time.time() * 1000)}{uuid.uuid4().hex[:4]}"
    try:
        resp = await client.post(
            f"{base_url}/api/v1/datasets",
            json={"name": name, "chunk_method": "naive",
                  "parser_config": {"layout_recognize": "DeepDOC"}},
            headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"创建数据集失败（HTTP {resp.status_code}）: {_error_msg(resp)}")
        data = _extract_data(resp.json())
        ds_id = data.get("id") if isinstance(data, dict) else None
        if not ds_id:
            raise RuntimeError(
                f"创建数据集失败：响应无 id（{resp.text[:200]}）")
        logger.info("DeepDoc 临时数据集已创建: %s (%s)", name, ds_id)
        return str(ds_id)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"创建 RAGFlow 数据集失败: {e}") from e


async def _upload_document(client: httpx.AsyncClient, base_url: str,
                           headers: dict, dataset_id: str,
                           file_path: Path) -> str:
    """步骤4：上传文档（multipart file；上传不自动解析）

    实测契约：上传响应 {"code":0,"data":[{...文档对象...,"id":...}]}——
    data 是**列表**，文档 id 取第一项。
    """
    try:
        with open(file_path, "rb") as f:
            resp = await client.post(
                f"{base_url}/api/v1/datasets/{dataset_id}/documents",
                files={"file": (file_path.name, f)},
                headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"上传文档失败（HTTP {resp.status_code}）: {_error_msg(resp)}")
        data = _extract_data(resp.json())
        if isinstance(data, list):
            data = data[0] if data else {}
        doc_id = data.get("id") if isinstance(data, dict) else None
        if not doc_id:
            raise RuntimeError(
                f"上传文档失败：响应无 id（{resp.text[:200]}）")
        return str(doc_id)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"上传文档到 RAGFlow 失败: {e}") from e


async def _trigger_parse(client: httpx.AsyncClient, base_url: str,
                         headers: dict, dataset_id: str, doc_id: str) -> None:
    """步骤5：触发解析（解析中重复触发会报错，靠模块级信号量规避）"""
    try:
        resp = await client.post(
            f"{base_url}/api/v1/datasets/{dataset_id}/chunks",
            json={"document_ids": [doc_id]},
            headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"触发解析失败（HTTP {resp.status_code}）: {_error_msg(resp)}")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"触发 RAGFlow 解析失败: {e}") from e


async def _wait_done(client: httpx.AsyncClient, base_url: str,
                     headers: dict, dataset_id: str, doc_id: str,
                     timeout: float) -> None:
    """步骤6：轮询解析进度至 DONE；FAILED 带 progress_msg/error 抛异常"""
    deadline = time.monotonic() + timeout
    while True:
        resp = await client.get(
            f"{base_url}/api/v1/datasets/{dataset_id}/documents",
            headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"查询解析进度失败（HTTP {resp.status_code}）: {_error_msg(resp)}")
        items = _doc_items(resp.json())
        item = next((d for d in items if str(d.get("id")) == doc_id),
                    items[0] if items else None)
        if item is None:
            raise RuntimeError(
                f"查询解析进度失败：文档 {doc_id} 不在数据集列表中")
        run = str(item.get("run") or "").upper()
        progress = item.get("progress")
        msg = item.get("progress_msg") or ""
        if run == "DONE":
            logger.info("DeepDoc 解析完成（进度 %s）", progress)
            return
        if run == "FAILED":
            raise RuntimeError(
                f"DeepDoc 解析失败: {msg or item.get('error') or '未知原因'}")
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"DeepDoc 解析超时（{int(timeout)}s，进度 {progress} {msg}）")
        logger.info("DeepDoc 解析中: 进度 %s %s", progress, msg or "")
        await asyncio.sleep(_POLL_INTERVAL)


async def _fetch_and_join_chunks(client: httpx.AsyncClient, base_url: str,
                                 headers: dict, dataset_id: str,
                                 doc_id: str) -> Tuple[str, List[dict]]:
    """步骤7：取全部切块（翻页）→ 按 positions 页序拼接全文 → (text, [])

    DeepDoc 图片不进 content（RAGFlow 仅页面快照 image_id），无图片链路，
    故 images 恒为 []。
    """
    chunks: List[dict] = []
    page = 1
    while True:
        resp = await client.get(
            f"{base_url}/api/v1/datasets/{dataset_id}/documents/{doc_id}/chunks",
            params={"page": page, "page_size": _PAGE_SIZE},
            headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"获取切块失败（HTTP {resp.status_code}）: {_error_msg(resp)}")
        data = _extract_data(resp.json())
        items = data.get("chunks") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise RuntimeError(
                f"获取切块失败：响应无 chunks（{resp.text[:200]}）")
        chunks.extend(items)
        total = int(data.get("total") or 0) if isinstance(data, dict) else 0
        if not items or len(chunks) >= total:
            break
        page += 1
    ordered = sorted(
        enumerate(chunks), key=lambda t: _position_sort_key(t[1], t[0]))
    text = "\n\n".join(
        str(c.get("content") or "") for _, c in ordered).strip()
    table_count = text.count("<table")
    logger.info("DeepDoc 获取 %d 个切块，拼接全文 %d 字符，表格 %d 个",
                len(chunks), len(text), table_count)
    return text, []


async def _cleanup_dataset(client: httpx.AsyncClient, base_url: str,
                           headers: dict, dataset_id: str) -> None:
    """步骤8：DELETE 清理临时数据集（finally 保证；失败仅告警不掩盖主异常）

    httpx 的 delete 方法不支持 json body（实测报错），改用
    client.request("DELETE", ..., json=...) 发送 {"ids": [...]}。
    """
    try:
        resp = await client.request(
            "DELETE", f"{base_url}/api/v1/datasets",
            json={"ids": [dataset_id]},
            headers=headers)
        if resp.status_code != 200:
            logger.warning("DeepDoc 临时数据集清理失败（HTTP %s）: %s",
                           resp.status_code, _error_msg(resp))
        else:
            logger.info("DeepDoc 临时数据集已清理: %s", dataset_id)
    except Exception as e:
        logger.warning("DeepDoc 临时数据集清理失败: %s", e)


# ==================== 主入口 ====================

async def parse_via_deepdoc(file_path: Path, cfg) -> Tuple[str, List[dict]]:
    """通过 ragflow-server（RAGFlow API）用 DeepDoc 解析 PDF

    - 返回 (全文 text, images=[])；text 含 HTML <table>（DeepDoc 表格
      为文本可检索，无图片链路）
    - cfg: DeepDocConfig（base_url/email/password/timeout/dataset_prefix）
    - 任一步失败抛 RuntimeError（中文，含步骤名）；临时数据集 finally 清理
    - 模块级信号量（=1）串行化并发调用（RAGFlow 解析中禁重复触发）
    """
    async with _deepdoc_semaphore:
        return await _parse_via_deepdoc_locked(file_path, cfg)


async def _parse_via_deepdoc_locked(file_path: Path, cfg) -> Tuple[str, List[dict]]:
    base_url = str(getattr(cfg, "base_url", "") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("DeepDoc 服务地址未配置（系统设置 → DeepDoc 解析）")
    if not file_path.exists():
        raise RuntimeError(f"DeepDoc 待解析文件不存在: {file_path}")
    timeout = float(getattr(cfg, "timeout", 0) or 300.0)
    dataset_id: Optional[str] = None
    headers: Optional[dict] = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            login_token = await _login(client, base_url, cfg)
            api_key = await _create_api_key(client, base_url, login_token)
            headers = {"Authorization": f"Bearer {api_key}"}
            dataset_id = await _create_dataset(client, base_url, headers, cfg)
            doc_id = await _upload_document(
                client, base_url, headers, dataset_id, file_path)
            await _trigger_parse(client, base_url, headers, dataset_id, doc_id)
            await _wait_done(client, base_url, headers, dataset_id, doc_id,
                             timeout)
            return await _fetch_and_join_chunks(
                client, base_url, headers, dataset_id, doc_id)
        finally:
            # 清理必须 finally 执行：任一步失败都不留临时数据集垃圾
            if dataset_id and headers:
                await _cleanup_dataset(client, base_url, headers, dataset_id)
