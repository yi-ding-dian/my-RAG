"""日志落盘 handler：按天切分 data/logs/kb-YYYY-MM-DD.log，超期自动清理。

当天文件即 kb-今天.log（与 /api/logs/tail 按天契约一致）；跨天时切换新文件。

**三道护栏**，防「日志写满磁盘 → MySQL/Milvus 写失败 → 服务不可用」：

1. 按天保留 `backup_days` 天（原有逻辑）；
2. **目录总大小上限** `max_dir_bytes`：超限时从**最旧**的文件开始删；
3. **单文件大小上限** `max_file_bytes`：超限后**只记 WARNING 及以上**。

第 3 条选「降级」而非「截断/分片」的理由：分片要改 `/api/logs` 的一整套读取
契约（tail/segments/range/download 都按 `kb-YYYY-MM-DD.log` 单文件假设）；截断
则会丢掉最该看的排障线索。降级两全——磁盘可控，错误仍可见，且跨天自动恢复。

清理**不再每行执行**：原实现每次 `emit` 都 glob + stat 整个目录（一天几万行
就是几万次目录扫描），改为按时间间隔触发。

线程安全由 logging 上层保证（emit 由 root logger 加锁调用）。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

# 单文件大小上限（字节）：超过后只记 WARNING 及以上。正常单日约 15MB，给足余量
_MAX_FILE_BYTES = 200 * 1024 * 1024
# 日志目录总大小上限（字节）：超过后从最旧的按天文件开始删。
# 正常 14 天 × 15MB ≈ 210MB，给约 5 倍余量
_MAX_DIR_BYTES = 1024 * 1024 * 1024
# 清理检查间隔（秒）：原实现每行都清理，这里按时间节流
_CLEANUP_INTERVAL_SECONDS = 300


class DailyRotatingFileHandler(logging.Handler):
    """运行日志按天落盘 handler：data/logs/kb-YYYY-MM-DD.log"""

    def __init__(self, log_dir: Path, backup_days: int = 14, encoding: str = "utf-8",
                 max_file_bytes: int = _MAX_FILE_BYTES,
                 max_dir_bytes: int = _MAX_DIR_BYTES):
        super().__init__()
        self.log_dir = log_dir
        self.backup_days = backup_days
        self.encoding = encoding
        self.max_file_bytes = max_file_bytes
        self.max_dir_bytes = max_dir_bytes
        self._current_date = ""
        self._stream: TextIO | None = None
        self._file_re = re.compile(r"^kb-(\d{4}-\d{2}-\d{2})\.log$")
        self._written = 0            # 当前文件已写字节数（避免每行 tell()）
        self._degraded = False       # 当前文件是否已进入降级（只记 WARNING+）
        self._last_cleanup = 0.0

    def _ensure_stream(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._stream is None or today != self._current_date:
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError:
                    pass
            self._stream = open(self.log_dir / f"kb-{today}.log", "a",
                                encoding=self.encoding)
            self._current_date = today
            # 跨天重置：新文件重新计量、降级状态解除
            self._written = self._stream.tell()
            self._degraded = False

    def _cleanup(self) -> None:
        """按天 + 按目录总大小清理（从最旧开始，当天文件不动）"""
        today_file = f"kb-{self._current_date}.log"
        cutoff = (datetime.now() - timedelta(days=self.backup_days)).strftime("%Y-%m-%d")
        dated: list[tuple[str, Path, int]] = []
        for p in self.log_dir.glob("kb-*.log"):
            m = self._file_re.match(p.name)
            if not m:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            dated.append((m.group(1), p, size))
        dated.sort(key=lambda item: item[0])       # 日期升序（最旧在前）

        # 1) 按天保留：超过 backup_days 的删除
        for date, path, _ in dated:
            if date < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass

        # 2) 目录总大小上限：从最旧的开始删（当天文件正在写，不动）
        total = sum(size for _, _, size in dated)
        if total <= self.max_dir_bytes:
            return
        for _, path, size in dated:
            if total <= self.max_dir_bytes:
                break
            if path.name == today_file:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            total -= size

    def _maybe_cleanup(self) -> None:
        """按时间节流清理（原实现每行都清理，一天几万次目录扫描）"""
        now = time.monotonic()
        if now - self._last_cleanup < _CLEANUP_INTERVAL_SECONDS:
            return
        self._last_cleanup = now
        self._cleanup()

    def emit(self, record) -> None:
        try:
            self._ensure_stream()
            assert self._stream is not None
            # 超限降级：只放行 WARNING 及以上（保住排障线索，也保住磁盘）
            if self._degraded and record.levelno < logging.WARNING:
                return
            line = self.format(record) + "\n"
            self._stream.write(line)
            self._stream.flush()
            self._written += len(line.encode(self.encoding, errors="replace"))
            if not self._degraded and self._written > self.max_file_bytes:
                self._degraded = True
                # 直接写流（不经 logger，避免递归回到本 emit）
                note = (f"[日志降级] 当日文件超过 "
                        f"{self.max_file_bytes // 1024 // 1024}MB，"
                        f"后续仅记录 WARNING 及以上（防止写满磁盘）\n")
                self._stream.write(note)
                self._stream.flush()
                self._written += len(note.encode(self.encoding))
            self._maybe_cleanup()
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        super().close()
