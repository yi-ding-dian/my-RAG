"""后台健康自检：**不依赖前端轮询**的告警触发源

**要解决的问题**：红灯判定（`routers/logs._health_state`）由 `/api/logs/health`
等接口驱动，而这些接口靠**前端每 60 秒轮询**——没人打开页面就不检查、不告警。
凌晨两点 LLM 挂了，要等到早上有人登录才被发现，钉钉更是一声不响。接了 webhook
只是把"上报出口"接通了，"什么时候触发"这一环还缺着，本模块补的就是它。

启动时起一个后台协程，每 `INTERVAL` 秒自己跑一轮：

1. **主动探测关键依赖**（LLM / Embedding / MinerU / 数据库 / 对象存储）——能发现
   "服务挂了但这段时间没人调用，所以日志里根本没有故障记录"的情况。探测结果按
   **状态变化**记录：由好变坏 → `system_error`（点亮红灯）；由坏变好 → `info`。
   每轮都记的话，服务持续不可用时会 60 秒刷一条把日志淹掉。
2. **算灯色并上报**——复用 `routers.logs._health_state`（保证与页面看到的口径
   完全一致），灯色变化时 `notify_health` 落日志 + 推 webhook。

**多 worker 下每个进程都会跑一份探测**（uvicorn 没有"主 worker"的概念）。代价是
几份重复的探测请求和几条重复日志；**告警本身不会重复**——`notify_health` 的
去重状态在文件里且加文件锁。这个取舍远比引入选主逻辑简单。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from backend.config import get_active_config
from backend.config import settings as config_settings
from backend.logger import AppLog
from backend.logger.alert import notify_health
from backend.services.parsers.probes import (probe_embedding, probe_llm,
                                             probe_mineru, probe_minio,
                                             probe_mysql)

logger = logging.getLogger(__name__)
log = AppLog(__name__)

# 自检间隔（秒）：与前端轮询周期一致；故障最长在这么久之后被发现
INTERVAL = 60
# 首轮自检延迟（秒）：不在 start() 里同步跑（会让启动多等几秒），也不等满
# INTERVAL（那样"启动时就有故障"要一分钟才发现）
STARTUP_DELAY = 10
# 单项探测超时（秒）：探测不能让自检卡住，也不能拖垮被探测的服务
PROBE_TIMEOUT = 5.0
# 灯色回看窗口（分钟）：与 /api/logs/health 默认值一致
WINDOW_MINUTES = 60

# 视为"未配置/未启用"的提示词：这类不算故障（可选功能没配是正常状态）
_SKIP_HINTS = ("未配置", "尚未启用", "未启用", "not configured")

_task: Optional[asyncio.Task] = None
# 依赖名 → 上一轮是否可用（进程内；多 worker 各一份，只影响日志重复度）
_probe_state: Dict[str, bool] = {}


# ==================== 生命周期 ====================

def start() -> None:
    """启动后台自检（幂等；由 lifespan 调用）"""
    global _task
    if not config_settings.HEALTH_WATCH_ENABLED:
        logger.info("后台健康自检已关闭（HEALTH_WATCH_ENABLED=false）")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    logger.info("后台健康自检已启动（每 %d 秒探测一次依赖并复核灯色）", INTERVAL)


def stop() -> None:
    """停止后台自检（由 lifespan 退出时调用）"""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None


async def _loop() -> None:
    """自检循环：启动后延迟一小段跑首轮，之后每 INTERVAL 秒一轮

    首轮不放在 `start()` 里同步跑（会让启动多等几秒），也不等满 INTERVAL
    （那样"启动时就有故障"要一分钟才发现）——取 `STARTUP_DELAY` 折中。
    """
    await asyncio.sleep(STARTUP_DELAY)
    while True:
        try:
            await check_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # 单轮异常不能让自检停摆（下一轮继续）
            logger.warning("健康自检异常（继续下一轮）: %s", str(e)[:150])
        await asyncio.sleep(INTERVAL)


# ==================== 单轮自检 ====================

async def check_once() -> None:
    """跑一轮：探测依赖 → 复核灯色 → 变化时上报（测试直接调它）"""
    await _probe_dependencies()
    _report_health()


def _report_health() -> None:
    """复核灯色并触发上报

    复用 `routers.logs._health_state`——口径必须与「日志查看」页面看到的一致，
    否则会出现"页面显示红灯但没告警"（或反之）的诡异现象。
    """
    from backend.routers.logs import _health_state
    health = _health_state(WINDOW_MINUTES)
    notify_health(is_red=health["level"] == "red", summary=health["summary"])


def _probe_specs() -> List[Tuple[str, object]]:
    """本轮要探测的 (显示名, 探测函数) 列表

    取活跃配置（`get_active_config` 不缓存启动值），用户改完配置下一轮即生效。
    """
    cfg = get_active_config()
    return [
        ("LLM 服务", (probe_llm, cfg.llm)),
        ("Embedding 服务", (probe_embedding, cfg.embedding)),
        ("MinerU 解析服务", (probe_mineru, cfg.mineru)),
        ("数据库", (probe_mysql, cfg.mysql)),
        ("对象存储", (probe_minio, cfg.minio)),
    ]


async def _safe_probe(fn, cfg) -> dict:
    """单项探测：把异常归一成 {ok: False, reason}

    探测函数本身已经吞了网络异常，这里再兜一层——自检绝不能因为某个探测
    抛出而整轮失败。
    """
    try:
        return await fn(cfg, timeout=PROBE_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e)[:120]}


async def _probe_dependencies() -> None:
    """并发探测全部依赖（串行的话最坏要等 5×5=25 秒）"""
    specs = _probe_specs()
    results = await asyncio.gather(
        *[_safe_probe(*args) for _, args in specs])
    for (name, _), result in zip(specs, results):
        _record(name, result)


def _is_skipped(result: dict) -> bool:
    """未配置 / 未启用的可选依赖不算故障

    很多探测函数在"地址没配"时返回 ok=False + "…未配置"——那是用户的正常选择
    （比如纯检索场景不配 LLM），不该点亮红灯。真配了却连不上才报。
    """
    if result.get("skipped"):
        return True
    reason = str(result.get("reason") or "")
    return any(h in reason for h in _SKIP_HINTS)


def _record(name: str, result: dict) -> None:
    """按**状态变化**记录：由好变坏点亮红灯，由坏变好记恢复，不变则不动"""
    if _is_skipped(result):
        return
    ok = bool(result.get("ok"))
    was = _probe_state.get(name, True)      # 首次视为"本来正常"→ 真故障会立刻报
    _probe_state[name] = ok
    if ok == was:
        return
    if ok:
        logger.info("依赖已恢复: %s", name)
    else:
        log.system_error("依赖探测失败: %s —— %s", name,
                         str(result.get("reason") or "")[:200])


def reset_probe_state() -> None:
    """清空探测状态（测试用：让下一轮按"首轮"处理）"""
    _probe_state.clear()
