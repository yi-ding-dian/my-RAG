"""超管系统运行日志 API：/api/logs（仅 super_admin）

日志文件：data/logs/kb-YYYY-MM-DD.log（main.py TimedRotatingFileHandler 按天
轮转落盘，保留 14 天）。

- GET  /api/logs/overview?days=&window_minutes=
  系统日志总览（前端「总览」Tab）：近 N 天每天的行数/分级别计数/系统级故障数
  + 合计 + 今天 + 红绿灯 + 最近系统级故障列表（最新在前）。每天统计按
  (路径, mtime, 大小) 缓存——历史天不变则长期命中，只有当天文件重算
  （否则菜单红绿灯每 60s 轮询都要重扫 7 天约 2.5 万行）。
  `faults` 只数**系统级**（`system.` 前缀，见 backend/logger/），与级别正交：
  用户级的 ERROR（如「文档超阈值入库失败」）计入 error 但**不计入 faults**。
- GET  /api/logs/health?window_minutes=
  红绿灯（green/yellow/red），菜单红点与外部监控轮询用；只扫窗口内的日志，
  比 overview 轻。两个接口都会在灯色**变化**时触发一次上报（去重，见
  backend/logger/alert.notify_health）。
- POST /api/logs/ack?window_minutes=
  确认系统级故障已解决：body `{"ids": ["<recent_faults[].id>"], "note": "处理备注"}`，
  **按条**确认（列表里逐条标记「已解决」），全部确认完（未确认数为 0）红灯才灭；
  之后新产生的故障是新 id、会重新亮灯。备注随确认存下，overview 的
  `recent_faults[].ack_note` 会带回。记录落盘 data/logs/.fault_ack.json（保留 14 天）。
  响应与 GET /health 同构。
- GET  /api/logs/tail?date=&offset=&limit=&hide_http=
  读指定天日志（date 缺省=今天）。字节游标增量读取，前端每 5s 轮询驱动
  （本接口只服务端读文件，不做轮询状态）：
  - offset >= 0：从该字节读到文件尾；offset 超文件大小自动归位尾部
    （文件轮转/重建后自然对齐）；
  - offset < 0：尾部模式，直接返回最近 limit 行（首次加载/切换日期用），
    offset 归位文件尾；
  - 行数超 limit 时取最后 limit 行（tail 语义，中间行丢弃）；
  - 游标落在行中间时首行视为残片丢弃（不返回半个行）。
- GET  /api/logs/files
  按日期倒序列出全部日志文件
  [{date, filename, size_bytes, mtime, line_count, line_count_estimated}]
  （行数：≤ 阈值精确计数，超阈值大文件采样估算并置 line_count_estimated，
  结果按 (路径, mtime, 大小) 缓存——避免每次列表全量读大文件）。
- GET  /api/logs/segments?date=&gap_seconds=&max_bytes=&max_segments=
  按「时间空档」把日志切分输出段（相邻两行间隔超 gap_seconds 视为新段），
  返回每段起止时间/条数/分级别小计（前端时间段导航，点击查看该区间）。
  只扫描文件尾部 max_bytes（大文件保护）。
- GET  /api/logs/range?date=&start=&end=&limit=
  按时间段查询日志行（时间戳秒级前缀比较，含起止边界；超 limit 取该区间
  最后 limit 行，返回 total/truncated 供前端提示）。
- GET  /api/logs/files/download?date=YYYY-MM-DD
  下载指定天日志文件（attachment，文件名 kb-YYYY-MM-DD.log；文件不存在 404）。
- DELETE /api/logs/files?date=YYYY-MM-DD
  删除指定天日志文件（不存在静默成功，返回 deleted=0）。
- DELETE /api/logs/files
  清空所有运行日志：今天文件截断（TimedRotatingFileHandler 持有写句柄，
  保留文件并继续写入），其余天文件删除。

三个读取接口（tail / segments / range）共用 hide_http 参数：置 true 时跳过第三方
HTTP 连接层的「正常」噪音行（httpx 的 2xx/3xx 请求回显、httpcore 与
urllib3.connectionpool 的连接池提示），**4xx/5xx 保留**——httpx 对失败请求同样
打 INFO，按级别过滤抓不到 HTTP 异常，所以按响应状态码判。只影响返回内容与
统计口径（条数/级别小计一致扣除），日志文件始终全量落盘、下载内容不变。

行解析对齐 main.py 的 format：`%(asctime)s [%(levelname)s] %(name)s: %(message)s`；
非标准行（多行消息续行等）level/ts 置空、整行作 message，不报错。
"""
from __future__ import annotations

import json
import logging
import re
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from backend.config import DATA_DIR
from backend.deps import require_super_admin
from backend.logger import SYSTEM_PREFIX
from backend.logger.alert import notify_health

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/logs", tags=["系统日志"],
    dependencies=[Depends(require_super_admin)],
)

LOG_DIR = DATA_DIR / "logs"
_LOG_FILE_RE = re.compile(r"^kb-(\d{4}-\d{2}-\d{2})\.log$")

# 行格式与 main.py logging.basicConfig 一致（asctime 默认带逗号毫秒）
_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?) \[(\w+)\] ([^:]+): (.*)$")


# ==================== 第三方 HTTP 噪音行过滤（hide_http） ====================

# 实测单日日志 86% 是 httpx 请求回显（图片摘要并发时一次刷几百行），把业务
# 日志整个淹没。hide_http 生效时跳过这些「正常」噪音行。
# httpx 单独判断：只隐藏成功回显，**4xx/5xx 保留**——httpx 对失败请求同样打
# INFO（见 httpx/_client.py 的 logger.info），按级别过滤抓不到 HTTP 异常，
# 必须按响应状态码判。
_NOISE_LOGGERS = ("httpcore", "urllib3.connectionpool")
_HTTP_STATUS_RE = re.compile(r'"HTTP/\d\.\d (\d{3})')


def _is_http_noise(raw: str) -> bool:
    """是否为「正常」的第三方 HTTP 连接层噪音行（hide_http 时跳过）

    - httpx：仅 2xx/3xx 请求回显算噪音；4xx/5xx 保留（排障要看的就是它）
    - httpcore / urllib3.connectionpool：连接层提示整类算噪音
    - 非标准行（续行等）不判噪音，一律保留
    """
    m = _LINE_RE.match(raw)
    if not m:
        return False
    name = m.group(3)
    if name == "httpx":
        status = _HTTP_STATUS_RE.search(raw)
        return status is not None and status.group(1)[0] in "23"
    return name in _NOISE_LOGGERS


def _is_system_fault(raw: str) -> bool:
    """是否「系统级故障」行（判定依据是日志的**域**，不是级别）

    系统级 / 用户级的区分见 backend/logger/：系统级的记录由 SystemFaultFilter
    给 logger 名加了 `system.` 前缀（LLM 崩溃、依赖连不上……）；用户级（文档超
    阈值这类只影响单次操作的失败）不加。级别与域正交——用户级照样可能是 ERROR。
    """
    if SYSTEM_PREFIX not in raw:        # 快速预筛：绝大多数行不含，省一次正则
        return False
    m = _LINE_RE.match(raw)
    return m is not None and m.group(3).startswith(SYSTEM_PREFIX)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _log_path(date: str) -> Path:
    return LOG_DIR / f"kb-{date}.log"


def _valid_date(date: str) -> bool:
    try:
        datetime.strptime(date, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _parse_line(raw: str) -> dict:
    """单行解析：失败（续行等）时 level/ts 置空，整行作 message"""
    m = _LINE_RE.match(raw)
    if not m:
        return {"line": raw, "level": None, "ts": None, "message": raw}
    ts, level, name, message = m.groups()
    return {"line": raw, "level": level, "ts": ts,
            "message": f"{name}: {message}"}


def _split_lines(content: str, hide_http: bool = False) -> List[dict]:
    """按行切分并解析（空行跳过；hide_http 时跳过第三方 HTTP 正常噪音行）"""
    out: List[dict] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        if hide_http and _is_http_noise(line):
            continue
        out.append(_parse_line(line))
    return out


# ==================== 行数统计（日志文件列表展示） ====================

# 精确计数尺寸上限：≤ 该值全量读计数（毫秒级）；超过改为采样估算，
# 避免几十 MB 大文件每次列表刷新都被全量扫描
_COUNT_EXACT_MAX_BYTES = 2 * 1024 * 1024
# 采样参数：均匀取 _SAMPLE_BLOCKS 块 × _SAMPLE_BLOCK_BYTES 字节
_SAMPLE_BLOCKS = 8
_SAMPLE_BLOCK_BYTES = 64 * 1024
# (路径, mtime_ns, 大小) → (行数, 是否估算)；文件未变化直接命中不重算
_lc_cache: dict = {}
_LC_CACHE_MAX = 256


def _count_lines_exact(path: Path) -> int:
    """全量读计数（末行无换行符时补 1）"""
    data = path.read_bytes()
    n = data.count(b"\n")
    return n + 1 if data and not data.endswith(b"\n") else n


def _count_lines_sampled(path: Path, size: int) -> int:
    """采样估算行数：均匀取块统计「平均行字节」，总行数 ≈ 文件大小 / 平均行字节

    日志行长度分布稳定，采样误差通常在 ±10% 内（调用方以 line_count_estimated
    标注「约」，前端提示为估算值）。
    """
    blocks = min(_SAMPLE_BLOCKS, max(1, size // _SAMPLE_BLOCK_BYTES))
    seen_bytes = seen_lines = 0
    with path.open("rb") as f:
        for i in range(blocks):
            # 块中心对齐到 (i+0.5)/blocks 处，避免只采到文件首尾的极端分布
            center = size * (2 * i + 1) // (2 * blocks)
            f.seek(max(0, center - _SAMPLE_BLOCK_BYTES // 2))
            chunk = f.read(_SAMPLE_BLOCK_BYTES)
            seen_bytes += len(chunk)
            seen_lines += chunk.count(b"\n")
    if seen_bytes <= 0 or seen_lines <= 0:
        return 0
    return round(size * seen_lines / seen_bytes)


def _line_count(path: Path, st) -> tuple:
    """日志文件行数 (行数, 是否估算)：小文件精确、大文件采样，按
    (路径, mtime, 大小) 缓存（当天文件持续写入，mtime/大小变化自然失效重算）"""
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _lc_cache.get(key)
    if hit is not None:
        return hit
    if st.st_size <= _COUNT_EXACT_MAX_BYTES:
        result = (_count_lines_exact(path), False)
    else:
        result = (_count_lines_sampled(path, st.st_size), True)
    if len(_lc_cache) >= _LC_CACHE_MAX:
        _lc_cache.clear()  # 容量满整体清空（纯加速缓存，重建代价可接受）
    _lc_cache[key] = result
    return result


def _list_files() -> List[dict]:
    """扫描全部按天日志文件，按日期倒序（含行数，统计策略见 _line_count）"""
    files = []
    for p in LOG_DIR.glob("kb-*.log"):
        m = _LOG_FILE_RE.match(p.name)
        if not m:
            continue
        st = p.stat()
        count, estimated = _line_count(p, st)
        files.append({
            "date": m.group(1),
            "filename": p.name,
            "size_bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "line_count": count,
            "line_count_estimated": estimated,
        })
    files.sort(key=lambda f: f["date"], reverse=True)
    return files


# ==================== 每天统计与红绿灯（overview / health） ====================

# 每天统计缓存：(路径, mtime_ns, 大小) → 统计结果。历史天的文件不再变化 →
# 长期命中；当天文件持续写入 → mtime/大小变化自然失效重算。
# 不缓存的话，菜单红绿灯每 60s 轮询都要把 7 天日志（约 2.5 万行）重扫一遍。
_day_stat_cache: dict = {}
_DAY_STAT_CACHE_MAX = 64
# 每天最多留多少条故障明细（供「最近系统故障」列表；故障本就稀少，够用）
_MAX_FAULT_LINES = 50


def _scan_day_stat(path: Path) -> dict:
    """扫一天日志：行数 + 分级别计数 + 系统级故障明细

    级别计数用切片解析（_head_level）而非正则，配合上面的缓存，只有当天文件
    （或被改动过的历史文件）会重算。
    """
    stat = {"lines": 0, "info": 0, "warning": 0, "error": 0,
            "faults": 0, "fault_errors": 0,
            "fault_lines": deque(maxlen=_MAX_FAULT_LINES)}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            stat["lines"] += 1
            level = _head_level(raw)
            if level == "INFO":
                stat["info"] += 1
            elif level == "WARNING":
                stat["warning"] += 1
            elif level == "ERROR":
                stat["error"] += 1
            if _is_system_fault(raw):
                stat["faults"] += 1
                if level == "ERROR":
                    # 系统级里的 ERROR（用户级 ERROR = error - fault_errors，
                    # 前端拿这两个口径并排显示：「系统故障 N · 操作失败 M」）
                    stat["fault_errors"] += 1
                stat["fault_lines"].append(_parse_line(raw))
    return stat


def _day_stat(date: str) -> Optional[dict]:
    """取某天统计（带缓存）；文件不存在返回 None"""
    path = _log_path(date)
    if not path.exists():
        return None
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _day_stat_cache.get(key)
    if hit is not None:
        return hit
    result = _scan_day_stat(path)
    if len(_day_stat_cache) >= _DAY_STAT_CACHE_MAX:
        _day_stat_cache.clear()   # 满则整体清空（纯加速缓存，重建代价可接受）
    _day_stat_cache[key] = result
    return result


def _recent_fault_lines(days: int = 2) -> List[dict]:
    """最近 N 天的系统级故障明细（时间正序），供灯色判定与「最近故障」列表"""
    out: List[dict] = []
    for i in range(days - 1, -1, -1):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        stat = _day_stat(date)
        if stat:
            out.extend(stat["fault_lines"])
    return out


# ---- 故障确认（按条 ACK 消警）----

# 已确认故障记录：{故障 id: 确认时间}。放日志目录而不进数据库：日志域的状态就该
# 跟日志文件在一起，且本模块本来只碰文件。路径用函数动态取（测试会 monkeypatch
# LOG_DIR 做隔离）。
_ACK_FILENAME = ".fault_ack.json"
# 确认记录保留天数：日志按天轮转，行都没了的旧确认留着没意义
_ACK_KEEP_DAYS = 14


def _ack_path() -> Path:
    return LOG_DIR / _ACK_FILENAME


def _fault_id(fault: dict) -> str:
    """故障唯一标识（时间戳 + 模块消息）：同一条日志行稳定可复现

    前端拿它调 POST /ack 确认某一条；已确认的 id 存进 .fault_ack.json。
    """
    return f"{fault.get('ts') or ''}|{fault.get('message') or ''}"


def _read_acked() -> dict:
    """读已确认故障 `{id: {at, note}}`；文件缺失/损坏返回空（= 一条都没确认过）

    兼容早期只存确认时间字符串的格式（值直接是 str），读出时统一成 dict。
    """
    try:
        data = json.loads(_ack_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("acked") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        if isinstance(value, str):                      # 旧格式：值就是确认时间
            out[key] = {"at": value, "note": ""}
        elif isinstance(value, dict):
            out[key] = {"at": str(value.get("at") or ""),
                        "note": str(value.get("note") or "")}
    return out


def _write_acked(acked: dict) -> None:
    """落盘确认记录，顺带丢掉超期条目（按确认时间保留最近 _ACK_KEEP_DAYS 天）"""
    cutoff = (datetime.now() - timedelta(days=_ACK_KEEP_DAYS)
              ).strftime("%Y-%m-%d %H:%M:%S")
    kept = {k: v for k, v in acked.items()
            if isinstance(v, dict) and str(v.get("at") or "") >= cutoff}
    _ack_path().write_text(json.dumps({"acked": kept}, ensure_ascii=False),
                           encoding="utf-8")


def _health_state(window_minutes: int = 60) -> dict:
    """红绿灯：窗口内有**未确认**的**系统级故障** → 红；否则绿

    只看域（`system.` 前缀），**不看级别**——本项目里级别与域是错位的：
    「Embedding 服务不可用」打的是 WARNING（其实是全站故障），「文档超阈值
    被拒」打的是 ERROR（其实只影响这一个用户）。按级别判灯会被两头骗。

    用户级的失败一律不点灯：那是「用户自己传大了、系统按设计拒绝」，不该让
    全站亮红灯——但它照样会在分级别计数里算作 error（前端两个口径并排显示）。

    确认（ACK）按**条**走：在「最近系统级故障」列表里逐条标记已解决，全部确认
    完（未确认数为 0）红灯才灭。之后**新产生**的故障是新的 id、自然重新亮灯——
    不是把信号一键按死（否则确认完再崩就看不见了，比不做还危险）。
    """
    cutoff = (datetime.now() - timedelta(minutes=window_minutes)
              ).strftime("%Y-%m-%d %H:%M:%S")
    # 窗口最长 1440 分钟（一天），扫今天+昨天足够覆盖
    faults = [f for f in _recent_fault_lines(2)
              if f["ts"] and f["ts"][:19] >= cutoff]
    acked = _read_acked()
    unacked = [f for f in faults if _fault_id(f) not in acked]
    acked_count = len(faults) - len(unacked)
    is_red = len(unacked) > 0
    errors = sum(1 for f in unacked if f["level"] == "ERROR")
    if is_red:
        summary = f"近 {window_minutes} 分钟有 {len(unacked)} 条未确认的系统级故障"
    elif acked_count:
        summary = f"近 {window_minutes} 分钟无新故障（{acked_count} 条已确认）"
    else:
        summary = f"近 {window_minutes} 分钟无系统级故障"
    return {"level": "red" if is_red else "green",
            "window_minutes": window_minutes,
            "fault_count": len(unacked),
            "acked_count": acked_count,
            "error_count": errors,
            "warning_count": len(unacked) - errors,
            "summary": summary}


# ==================== 时间段切分（前端时间段导航 / 按区间查询） ====================

# 行首时间戳长度（YYYY-MM-DD HH:mm:ss，秒级前缀；毫秒部分不参与切分与比较）
_TS_LEN = 19


def _head_ts(raw: str) -> Optional[str]:
    """取行首秒级时间戳（格式不符返回 None；切片校验，避免逐行正则开销）"""
    if len(raw) < _TS_LEN:
        return None
    ts = raw[:_TS_LEN]
    if not (ts[4] == "-" and ts[7] == "-" and ts[10] == " "
            and ts[13] == ":" and ts[16] == ":"):
        return None
    return ts


def _head_level(raw: str) -> Optional[str]:
    """取行首时间戳后的 [LEVEL]（asctime 的 ,SSS 毫秒可有可无；非标准行 None）"""
    rest = raw[_TS_LEN:].lstrip()
    if rest.startswith(","):
        # 跳过毫秒（logging 默认 asctime 带 ,SSS）
        rest = rest[1:]
        if rest[:3].isdigit():
            rest = rest[3:]
        rest = rest.lstrip()
    if not rest.startswith("["):
        return None
    end = rest.find("]")
    return rest[1:end] if end > 0 else None


def _parse_head_ts(ts: str) -> Optional[datetime]:
    """秒级时间戳解析（失败返回 None：非标准行不参与段切分）

    用 fromisoformat 解析（与 strptime 等价、快一个量级），段扫描逐行调用。
    """
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _scan_segments(path: Path, size: int, max_bytes: int,
                   gap_seconds: int, hide_http: bool = False) -> tuple:
    """扫描文件尾部并按「时间空档」切段，返回 (segments, 实际扫描字节数)

    - 大文件保护：只读尾部 max_bytes；起点落在行中时丢弃残片（不产生半行段）；
    - 相邻标准行时间差 > gap_seconds → 新段（一次连续输出聚为一段）；
    - 非标准行（续行等无时间戳）并入当前段，只计数不参与切分；
    - 每段累计条数与「行首 [LEVEL] → 条数」小计；
    - hide_http：噪音行照常推进段边界（段的划分与不隐藏时一致，只是条数变少），
      但不计入条数与级别小计；起止时间也只由保留行决定。
    """
    start = max(0, size - max_bytes)
    segments: List[dict] = []
    cur: Optional[dict] = None
    last_dt: Optional[datetime] = None
    prev_ts: Optional[str] = None      # 同秒复用解析结果（日志同秒多行，省解析开销）
    prev_dt: Optional[datetime] = None
    with path.open("r", encoding="utf-8", errors="replace") as f:
        if start > 0:
            f.seek(start - 1)
            prev = f.read(1)
            f.seek(start)
            if prev != "\n":
                f.readline()
        for raw in f:
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            noise = hide_http and _is_http_noise(raw)
            ts = _head_ts(raw)
            if ts != prev_ts:
                prev_ts = ts
                prev_dt = _parse_head_ts(ts) if ts else None
            dt = prev_dt
            if cur is None or (dt is not None and last_dt is not None
                               and (dt - last_dt).total_seconds() > gap_seconds):
                # 起止时间留空，由保留行填充（段首是噪音行时不占用它的时间戳）
                cur = {"start_ts": None, "end_ts": None, "count": 0, "levels": {}}
                segments.append(cur)
            if not noise:
                cur["count"] += 1
                if ts is not None:
                    if cur["start_ts"] is None:
                        cur["start_ts"] = ts
                    cur["end_ts"] = ts
                level = _head_level(raw)
                if level:
                    cur["levels"][level] = cur["levels"].get(level, 0) + 1
            if dt is not None:
                last_dt = dt
    # 整段都是噪音（过滤后一条不剩）不返回，避免前端出现「0 条」的空段
    return [s for s in segments if s["count"] > 0], size - start


@router.get("/overview")
async def log_overview(
    days: int = Query(7, ge=1, le=30, description="统计最近多少天"),
    window_minutes: int = Query(60, ge=5, le=1440,
                                description="红绿灯回看窗口（分钟）"),
):
    """系统日志总览：近 N 天分级别计数 + 红绿灯 + 最近系统级故障列表

    响应契约: {days: [{date, lines, info, warning, error, faults}]（时间正序，
    只含实际有日志的天）, totals, today, health, recent_faults（最新在前）}。

    `faults` 数的是**系统级**（`system.` 前缀）条数，与级别正交：用户级的
    ERROR（如「文档超阈值入库失败」）计入 error 但**不计入 faults**，所以会
    出现「有 error 但灯是绿的」——这是设计如此，不是 bug。
    """
    day_list: List[dict] = []
    totals = {"lines": 0, "info": 0, "warning": 0, "error": 0,
              "faults": 0, "fault_errors": 0}
    for i in range(days - 1, -1, -1):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        stat = _day_stat(date)
        if stat is None:
            continue
        item = {"date": date, "lines": stat["lines"], "info": stat["info"],
                "warning": stat["warning"], "error": stat["error"],
                "faults": stat["faults"], "fault_errors": stat["fault_errors"]}
        day_list.append(item)
        for key in totals:
            totals[key] += item[key]
    health = _health_state(window_minutes)
    # 灯色变化时上报一次（内部去重，不会每次轮询都报）
    notify_health(is_red=health["level"] == "red", summary=health["summary"])
    today = _today()
    acked = _read_acked()
    recent_faults = []
    for fault in _recent_fault_lines(min(days, 7))[-_MAX_FAULT_LINES:][::-1]:
        fault_id = _fault_id(fault)
        recent_faults.append({
            **fault,
            "id": fault_id,                                   # 调 POST /ack 用
            "acked": fault_id in acked,                       # 前端据此显示按钮/标签
            "ack_note": (acked.get(fault_id) or {}).get("note", ""),
        })
    return {
        "days": day_list,
        "totals": totals,
        "today": next((d for d in day_list if d["date"] == today),
                      {"date": today, "lines": 0, "info": 0, "warning": 0,
                       "error": 0, "faults": 0, "fault_errors": 0}),
        "health": health,
        "recent_faults": recent_faults,
    }


@router.get("/health")
async def log_health(
    window_minutes: int = Query(60, ge=5, le=1440,
                                description="回看窗口（分钟）"),
):
    """红绿灯状态（轻量：只扫窗口内的，不碰 7 天全量），供菜单红点与外部监控轮询

    响应契约: {level: green|yellow|red, window_minutes, fault_count,
    error_count, warning_count, summary}，并顺带驱动「灯色变化上报」（去重）。

    权限说明：本路由挂在 require_super_admin 下（前端菜单用）。外部监控系统
    （uptime/Prometheus）后续接入时需另开一个带 token 的公开端点——当前先不加。
    """
    health = _health_state(window_minutes)
    notify_health(is_red=health["level"] == "red", summary=health["summary"])
    return health


@router.post("/ack")
async def ack_system_faults(
    ids: List[str] = Body(..., embed=True,
                          description="要确认的故障 id 列表（取 recent_faults[].id）"),
    note: str = Body("", embed=True,
                     description="处理备注（可选；如「压测产生的，非真实故障」）"),
    window_minutes: int = Query(60, ge=5, le=1440,
                                description="回看窗口（分钟）"),
):
    """确认系统级故障已解决（按条 ACK 消警，可附处理备注）

    在「最近系统级故障」列表里**逐条**确认；全部确认完（未确认数为 0）红灯才灭。
    之后**新产生**的故障是新 id、会重新亮灯——不是把信号一键按死（否则确认完
    再崩就看不见了，比不做还危险）。备注随确认一起存，供日后回溯"当时为什么
    确认掉"。记录落盘 data/logs/.fault_ack.json（保留 14 天），重启不丢。

    响应与 GET /health 同构（确认后的最新状态），前端可直接用之刷新灯色。
    未知 id 静默忽略（日志按天轮转后旧 id 自然失效）。
    """
    acked = _read_acked()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean_note = note.strip()[:200]          # 备注限长，避免日志域存大段文本
    for fault_id in ids:
        acked[fault_id] = {"at": now, "note": clean_note}
    _write_acked(acked)
    health = _health_state(window_minutes)
    # 全部确认完转绿时，这里会顺带触发一次「恢复绿灯」上报
    notify_health(is_red=health["level"] == "red", summary=health["summary"])
    return health


@router.get("/tail")
async def tail_logs(
    date: Optional[str] = Query(None, description="日志日期（YYYY-MM-DD，缺省=今天）"),
    offset: int = Query(0, description="字节游标；<0 = 尾部模式取最近 limit 行（首次加载）"),
    limit: int = Query(200, ge=1, le=2000, description="最多返回行数"),
    hide_http: bool = Query(
        False, description="隐藏第三方 HTTP 正常噪音行（httpx 2xx/3xx 回显、"
                           "连接池提示；4xx/5xx 保留）"),
):
    """读指定天运行日志（从 offset 字节读到文件尾，offset 超文件大小自动归位尾部）

    响应契约: {lines: [{line, level, ts, message}], offset: 新字节位置（=文件尾，
    下次轮询传入）, eof: 是否已读到文件尾}。文件不存在返回空 lines（offset=0）。
    """
    if date is None:
        date = _today()
    if not _valid_date(date):
        raise HTTPException(status_code=400, detail="日期格式须为 YYYY-MM-DD")
    path = _log_path(date)
    if not path.exists():
        return {"lines": [], "offset": 0, "eof": True}
    size = path.stat().st_size
    if offset < 0:
        # 尾部模式：直接取最近 limit 行（首次加载），offset 归位文件尾
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = _split_lines(f.read(), hide_http)
        return {"lines": lines[-limit:], "offset": size, "eof": True}
    start = min(offset, size)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        if start > 0:
            # 校验游标是否在行首：前一字节非换行 → 首行为残片，丢弃
            f.seek(start - 1)
            prev = f.read(1)
            f.seek(start)
            if prev != "\n":
                f.readline()
        else:
            f.seek(0)
        lines = _split_lines(f.read(), hide_http)
    return {"lines": lines[-limit:], "offset": size, "eof": True}


@router.get("/segments")
async def list_log_segments(
    date: Optional[str] = Query(None, description="日志日期（YYYY-MM-DD，缺省=今天）"),
    gap_seconds: int = Query(60, ge=1, le=3600,
                             description="相邻两行间隔超过该秒数视为新的输出段"),
    max_bytes: int = Query(4 * 1024 * 1024, ge=64 * 1024, le=64 * 1024 * 1024,
                           description="只扫描文件尾部该字节数（大文件保护）"),
    max_segments: int = Query(200, ge=1, le=1000,
                              description="最多返回段数（超出取最近的段）"),
    hide_http: bool = Query(
        False, description="隐藏第三方 HTTP 正常噪音行（httpx 2xx/3xx 回显、"
                           "连接池提示；4xx/5xx 保留）"),
):
    """按「时间空档」切分日志输出段（时间段导航：起止时间 + 条数 + 级别小计）

    响应契约: {segments: [{start_ts, end_ts, count, levels}], scanned_bytes,
    truncated}。时间段按时间正序；truncated=True 表示只覆盖文件尾部
    （更早内容未统计，前端提示）。文件不存在返回空 segments。
    """
    if date is None:
        date = _today()
    if not _valid_date(date):
        raise HTTPException(status_code=400, detail="日期格式须为 YYYY-MM-DD")
    path = _log_path(date)
    if not path.exists():
        return {"segments": [], "scanned_bytes": 0, "truncated": False}
    size = path.stat().st_size
    segments, scanned = _scan_segments(path, size, max_bytes, gap_seconds, hide_http)
    return {
        "segments": segments[-max_segments:],
        "scanned_bytes": scanned,
        "truncated": scanned < size or len(segments) > max_segments,
    }


@router.get("/range")
async def query_log_range(
    date: Optional[str] = Query(None, description="日志日期（YYYY-MM-DD，缺省=今天）"),
    start: str = Query(..., description="区间起（YYYY-MM-DD HH:mm:ss，含边界）"),
    end: str = Query(..., description="区间止（YYYY-MM-DD HH:mm:ss，含边界）"),
    limit: int = Query(500, ge=1, le=2000, description="最多返回行数"),
    max_bytes: int = Query(8 * 1024 * 1024, ge=64 * 1024, le=64 * 1024 * 1024,
                           description="只扫描文件尾部该字节数（大文件保护）"),
    hide_http: bool = Query(
        False, description="隐藏第三方 HTTP 正常噪音行（httpx 2xx/3xx 回显、"
                           "连接池提示；4xx/5xx 保留）"),
):
    """按时间段查询日志行（时间戳秒级前缀比较，含起止边界）

    超 limit 取该区间最后 limit 行（tail 语义）并置 truncated；total 为区间内
    实际命中行数（前端提示「该时间段共 N 条」）。文件/区间无内容返回空 lines。
    """
    for value, name in ((start, "start"), (end, "end")):
        try:
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"{name} 格式须为 YYYY-MM-DD HH:mm:ss")
    if date is None:
        date = _today()
    if not _valid_date(date):
        raise HTTPException(status_code=400, detail="日期格式须为 YYYY-MM-DD")
    path = _log_path(date)
    if not path.exists():
        return {"lines": [], "total": 0, "truncated": False}
    size = path.stat().st_size
    scan_start = max(0, size - max_bytes)
    matched: List[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        if scan_start > 0:
            f.seek(scan_start - 1)
            prev = f.read(1)
            f.seek(scan_start)
            if prev != "\n":
                f.readline()
        for raw in f:
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            ts = _head_ts(raw)
            # 秒级前缀字典序 = 时间序（同格式 19 字符），含起止边界
            if ts is None or ts < start or ts > end:
                continue
            if hide_http and _is_http_noise(raw):
                continue
            matched.append(_parse_line(raw))
    return {"lines": matched[-limit:], "total": len(matched),
            "truncated": len(matched) > limit}


@router.get("/files")
async def list_log_files():
    """按日期倒序列出全部运行日志文件（含大小/修改时间/行数，供空间管理展示）"""
    return {"files": _list_files()}


@router.get("/files/download")
async def download_log_file(
    date: str = Query(..., description="日志日期（YYYY-MM-DD）"),
):
    """下载指定天日志文件（attachment，文件名 kb-YYYY-MM-DD.log）

    直接读文件返回字节流——当天文件可能正被 logging handler 追加写入，
    只读不锁（读写并发由操作系统保证不损坏）。文件不存在 404。
    """
    if not _valid_date(date):
        raise HTTPException(status_code=400, detail="日期格式须为 YYYY-MM-DD")
    path = _log_path(date)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{date} 无日志文件")
    # 文件名 ASCII（kb-YYYY-MM-DD.log），filename* 编码风格与 documents.py raw 下载一致
    return FileResponse(
        path, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{path.name}"})


@router.delete("/files")
async def delete_log_files(
    date: Optional[str] = Query(None, description="删除指定天日志；缺省=清空全部"),
):
    """删除日志文件：date 指定删单天（不存在静默成功）；缺省清空全部
    （今天文件截断保留写句柄继续写入，其余天文件删除）"""
    if date is not None:
        if not _valid_date(date):
            raise HTTPException(status_code=400, detail="日期格式须为 YYYY-MM-DD")
        path = _log_path(date)
        if path.exists():
            path.unlink()
            return {"message": f"已删除 {date} 的日志文件", "deleted": 1}
        return {"message": f"{date} 无日志文件", "deleted": 0}
    today = _today()
    deleted = 0
    for p in LOG_DIR.glob("kb-*.log"):
        m = _LOG_FILE_RE.match(p.name)
        if not m:
            continue
        if p.name == f"kb-{today}.log":
            # 今天的文件被 logging handler 持有：截断保留（写句柄继续有效）
            with p.open("wb") as f:
                f.truncate(0)
        else:
            p.unlink()
            deleted += 1
    return {"message": "运行日志已清空", "deleted": deleted}
