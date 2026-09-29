"""系统级故障上报口子：红灯亮起 / 恢复时各报一次。

**两条出口**：
1. **本地落日志**——一条 `[ALERT]` INFO 记录（始终执行，留痕）；
2. **群机器人推送**——钉钉 / 企业微信 / 飞书，配了 `ALERT_WEBHOOK_URL` 才启用。
   平台按 URL 域名自动识别，换平台不用改代码。

**去重状态落盘**（`data/logs/.alert_state.json` + 文件锁），而非存进程内变量：
多 worker 部署时每个进程各有一份 `_last_red`，灯色变化时每个 worker 都会报
一次（N 个 worker = N 条重复告警）——报警疲劳比不报警更危险。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Tuple

from backend.config import DATA_DIR
from backend.config import settings as config_settings

try:
    import fcntl
except ImportError:                     # pragma: no cover - Windows 无 fcntl
    fcntl = None                        # type: ignore[assignment]

_alert_logger = logging.getLogger("backend.alert")

# 去重状态文件：与运行日志同目录（同 .fault_ack.json 的落盘模式）
_STATE_FILE = DATA_DIR / "logs" / ".alert_state.json"

# webhook 请求超时（秒）：推送是尽力而为，不能挂太久
_WEBHOOK_TIMEOUT = 8.0

# 进程内串行：文件锁是进程级的，同一进程内多线程（uvicorn 线程池）仍会竞争
_lock = threading.Lock()


# ==================== 去重状态（跨进程） ====================

def _state_path() -> Path:
    """状态文件路径（动态取，便于测试 monkeypatch DATA_DIR 后生效）"""
    return _STATE_FILE


def _read_last_red() -> Optional[bool]:
    """上次上报的灯色；文件缺失/损坏 → None（= 还没记录过）"""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("is_red") if isinstance(data, dict) else None
    return value if isinstance(value, bool) else None


def _write_last_red(is_red: bool) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"is_red": is_red}), encoding="utf-8")


def _compare_and_set(is_red: bool) -> bool:
    """「读上次灯色 → 比较 → 写新值」，返回是否应上报（调用方持锁）"""
    last = _read_last_red()
    if last is None and not is_red:
        # 首次且为绿：记下但不报（避免每次重启都报一条"平安"）
        _write_last_red(False)
        return False
    if last == is_red:
        return False
    _write_last_red(is_red)
    return True


def _locked_compare_and_set(is_red: bool) -> bool:
    """跨进程原子的 compare-and-set：文件锁保证多 worker 只有一个能上报

    没有文件锁能力（非 POSIX）时退化为无锁版本——单进程部署下语义不变，
    多 worker 会重复上报（与本次改造前的行为一致，不更差）。
    """
    if fcntl is None:
        return _compare_and_set(is_red)
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_name(path.name + ".lock")
    with open(lock_file, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return _compare_and_set(is_red)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# ==================== 上报入口 ====================

def notify_health(*, is_red: bool, summary: str) -> bool:
    """灯色变化时上报一次（绿→红、红→绿），返回本次是否真的上报了

    **只在变化时报**，不是每次轮询都报——故障持续期间每 60s 报一条，很快就
    没人看了。去重状态落盘 + 文件锁，跨 worker 只有一个进程会真正上报。

    重启后状态文件仍在，灯色没变就不会重复报（预期行为）；状态文件丢失时会
    按"首次"处理：为绿则静默记下，为红则报一条。

    调用点：`routers/logs.py` 的 overview / health / ack 接口（都会算灯色）。
    """
    with _lock:
        changed = _locked_compare_and_set(is_red)
    if not changed:
        return False
    _deliver(is_red, summary)
    return True


def _deliver(is_red: bool, summary: str) -> None:
    """上报出口：本地落日志 + 推送群机器人（未配 webhook 则只落日志）

    日志用 INFO 级别：它是一条「通知」，本身不是新故障，别去污染 WARNING 统计；
    logger 名 backend.alert 不带 system. 前缀，也不会被算进系统级故障。
    """
    _alert_logger.info("[ALERT] %s: %s",
                       "红灯（系统级故障）" if is_red else "恢复绿灯", summary)
    _schedule_webhook(is_red, summary)


# ==================== 群机器人推送 ====================

def _webhook_configured() -> bool:
    return bool((config_settings.ALERT_WEBHOOK_URL or "").strip())


def _alert_text(is_red: bool, summary: str) -> str:
    """告警消息文本

    **红灯与恢复消息都必须包含「告警」二字**：钉钉自定义机器人的「自定义
    关键词」安全模式要求消息含关键词才放行（实测不含会返回 errcode 310000
    "关键词不匹配"）。这里两条消息都带上，用户关键词填「告警」或「my-RAG」均可。
    """
    state = "红灯（系统级故障）" if is_red else "已恢复正常"
    return f"【my-RAG 告警】{state}\n{summary}"


def _signed_dingtalk_url(url: str, secret: str) -> str:
    """钉钉「加签」模式：在 URL 上追加 timestamp + sign（官方算法）

    sign = urlencode(base64(HMAC-SHA256(timestamp + "\\n" + secret, secret)))
    """
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"),
                      string_to_sign.encode("utf-8"),
                      hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={timestamp}&sign={sign}"


def _build_payload(url: str, text: str) -> Tuple[str, dict]:
    """按 webhook 域名选择消息结构（换平台不用改代码）

    - **钉钉**（oapi.dingtalk.com）：`{"msgtype":"text","text":{"content":...}}`
      ；配了 `ALERT_WEBHOOK_SECRET` 时按「加签」模式给 URL 追加签名参数
    - **企业微信**（qyapi.weixin.qq.com）：与钉钉同构（无需签名）
    - **飞书**（open.feishu.cn）：`{"msg_type":"text","content":{"text":...}}`
      ——注意字段名是下划线的 `msg_type`，且文本多一层 `content.text`
    - 其他/未知：按钉钉企微的通用结构发送
    """
    if "feishu" in url or "larksuite" in url:
        return url, {"msg_type": "text", "content": {"text": text}}
    if "dingtalk.com" in url:
        secret = (config_settings.ALERT_WEBHOOK_SECRET or "").strip()
        if secret:
            url = _signed_dingtalk_url(url, secret)
        return url, {"msgtype": "text", "text": {"content": text}}
    return url, {"msgtype": "text", "text": {"content": text}}


async def _push_webhook(is_red: bool, summary: str) -> None:
    """推送告警到群机器人（**尽力而为**：失败只记 warning，绝不上抛）

    独立函数便于测试；调用方 `_schedule_webhook` 以 fire-and-forget 方式调度。
    """
    url = (config_settings.ALERT_WEBHOOK_URL or "").strip()
    if not url:
        return
    try:
        import httpx

        final_url, payload = _build_payload(url, _alert_text(is_red, summary))
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
            resp = await client.post(final_url, json=payload)
        status_code = resp.status_code
        detail = ""
        ok = status_code == 200
        if ok:
            try:
                data = resp.json()
                # 钉钉/企微用 errcode，飞书用 code；缺字段时按 HTTP 状态码判
                code = data.get("errcode", data.get("code", 0))
                ok = code == 0
                if not ok:
                    detail = str(data.get("errmsg") or data.get("msg") or data)
            except ValueError:
                detail = resp.text[:150]
        if not ok:
            _alert_logger.warning("告警 webhook 返回异常（消息未送达）: "
                                  "status=%s %s", status_code, detail)
    except Exception as e:  # noqa: BLE001
        _alert_logger.warning("告警 webhook 推送失败（消息未送达）: %s",
                              str(e)[:150])


def _schedule_webhook(is_red: bool, summary: str) -> None:
    """把 webhook 推送丢到事件循环后台执行（**不阻塞调用方**）

    `notify_health` 被 `/api/logs/health` 每 60s 轮询调用，而 webhook 是网络
    IO（慢的话要等几秒）——绝不能让健康检查接口等它。同步脚本/无事件循环的
    场景直接跳过推送（本地日志已落，不受影响）。
    """
    if not _webhook_configured():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _alert_logger.debug("无运行中的事件循环，跳过告警 webhook 推送")
        return
    loop.create_task(_push_webhook(is_red, summary))
