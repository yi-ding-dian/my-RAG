"""超管系统运行日志 API：/api/logs（仅 super_admin）

日志文件：data/logs/kb-YYYY-MM-DD.log（main.py TimedRotatingFileHandler 按天
轮转落盘，保留 14 天）。

- GET  /api/logs/tail?date=&offset=&limit=
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

行解析对齐 main.py 的 format：`%(asctime)s [%(levelname)s] %(name)s: %(message)s`；
非标准行（多行消息续行等）level/ts 置空、整行作 message，不报错。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from backend.config import DATA_DIR
from backend.deps import require_super_admin

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


def _split_lines(content: str) -> List[dict]:
    """按行切分并解析（空行跳过）"""
    return [_parse_line(l) for l in content.splitlines() if l.strip()]


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
                   gap_seconds: int) -> tuple:
    """扫描文件尾部并按「时间空档」切段，返回 (segments, 实际扫描字节数)

    - 大文件保护：只读尾部 max_bytes；起点落在行中时丢弃残片（不产生半行段）；
    - 相邻标准行时间差 > gap_seconds → 新段（一次连续输出聚为一段）；
    - 非标准行（续行等无时间戳）并入当前段，只计数不参与切分；
    - 每段累计条数与「行首 [LEVEL] → 条数」小计。
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
            ts = _head_ts(raw)
            if ts != prev_ts:
                prev_ts = ts
                prev_dt = _parse_head_ts(ts) if ts else None
            dt = prev_dt
            if cur is None or (dt is not None and last_dt is not None
                               and (dt - last_dt).total_seconds() > gap_seconds):
                cur = {"start_ts": ts, "end_ts": ts, "count": 0, "levels": {}}
                segments.append(cur)
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
    return segments, size - start


@router.get("/tail")
async def tail_logs(
    date: Optional[str] = Query(None, description="日志日期（YYYY-MM-DD，缺省=今天）"),
    offset: int = Query(0, description="字节游标；<0 = 尾部模式取最近 limit 行（首次加载）"),
    limit: int = Query(200, ge=1, le=2000, description="最多返回行数"),
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
            lines = _split_lines(f.read())
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
        lines = _split_lines(f.read())
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
    segments, scanned = _scan_segments(path, size, max_bytes, gap_seconds)
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
