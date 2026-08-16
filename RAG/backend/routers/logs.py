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
  按日期倒序列出全部日志文件 [{date, filename, size_bytes, mtime}]
  （不含 line_count——避免大文件全读拖慢列表）。
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


def _list_files() -> List[dict]:
    """扫描全部按天日志文件，按日期倒序"""
    files = []
    for p in LOG_DIR.glob("kb-*.log"):
        m = _LOG_FILE_RE.match(p.name)
        if not m:
            continue
        st = p.stat()
        files.append({
            "date": m.group(1),
            "filename": p.name,
            "size_bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    files.sort(key=lambda f: f["date"], reverse=True)
    return files


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


@router.get("/files")
async def list_log_files():
    """按日期倒序列出全部运行日志文件（含大小/修改时间，供空间管理展示）"""
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
