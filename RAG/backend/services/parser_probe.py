"""解析器可用性探测（解析前检测，供解析弹窗状态徽标 + ingestion 自动降级）

- probe_parsers(cfg=None, *, mineru_timeout, deepdoc_timeout) -> dict：
  {mineru: {available, reason}, deepdoc: {available, reason},
   plain: {available: True, reason: ""}}
  - mineru：GET {url}/health → /api/health → 根路径（与 parser_client
    探测端点一致），任一响应 <500 即可用
  - deepdoc：复用 deepdoc_client 登录探测（RSA 加密密码 POST
    /v1/user/login，响应头 HTTP_AUTHORIZATION 有 token 即可用）
  - plain：本地 pypdf/python-docx 提取，恒可用
- 探测失败不抛异常（探测失败 = 不可用 + 用户可读 reason：连接失败/超时/
  非 2xx），调用方（状态接口/ingestion 降级链）直接消费结果
- 超时：mineru 与 deepdoc 并行探测（asyncio.gather），总耗时 = 两者最大
  超时；默认 5s/8s（解析弹窗打开时探测 ≤8s 不拖 UI），ingestion 任务内
  传 3.0/5.0（解析前检测 ≤5s）
- 探测实现统一在 services/probes.py（probe_mineru/probe_deepdoc），本模块
  仅做 {ok, latency_ms, reason} → {available, reason} 的契约映射
"""
from __future__ import annotations

import asyncio
import logging

from backend.config import get_active_config
from backend.services.probes import probe_deepdoc, probe_mineru

logger = logging.getLogger(__name__)


def _to_available(r: dict) -> dict:
    """probes 结果 → 解析器契约 {available, reason}（成功时 reason 空串）"""
    return {"available": r["ok"], "reason": "" if r["ok"] else r["reason"]}


async def _probe_mineru(api_url: str, timeout: float) -> dict:
    """MinerU 健康探测（薄包装：probes.probe_mineru，<500 可用）"""
    return _to_available(await probe_mineru({"url": api_url}, timeout=timeout))


async def _probe_deepdoc(cfg, timeout: float) -> dict:
    """DeepDoc 登录探测（薄包装：probes.probe_deepdoc，200+token 可用）"""
    return _to_available(await probe_deepdoc(cfg, timeout=timeout))


async def probe_parsers(cfg=None, *, mineru_timeout: float = 5.0,
                        deepdoc_timeout: float = 8.0) -> dict:
    """探测全部解析器可用性（mineru/deepdoc 并行），全部失败不抛异常

    - cfg: 活跃配置（缺省 get_active_config()，运行时读取配置档案即时生效）
    - 超时默认 5s/8s（弹窗状态接口）；ingestion 任务内传 3.0/5.0（≤5s）
    - 返回 {mineru: {...}, deepdoc: {...}, plain: {available: True}}
    """
    cfg = cfg or get_active_config()
    mineru, deepdoc = await asyncio.gather(
        _probe_mineru(str(getattr(cfg.mineru, "api_url", "") or ""),
                      mineru_timeout),
        _probe_deepdoc(cfg.deepdoc, deepdoc_timeout),
    )
    if not mineru["available"]:
        logger.info("MinerU 探测不可用: %s", mineru["reason"])
    if not deepdoc["available"]:
        logger.info("DeepDoc 探测不可用: %s", deepdoc["reason"])
    return {
        "mineru": mineru,
        "deepdoc": deepdoc,
        "plain": {"available": True, "reason": ""},
    }
