"""日志落盘 handler：按天切分 data/logs/kb-YYYY-MM-DD.log，超期自动清理。

当天文件即 kb-今天.log（与 /api/logs/tail 按天契约一致）；跨天时切换新文件；
每次写入顺带清理超过 backup_days 天的旧文件。线程安全由 logging 上层保证
（emit 由 root logger 加锁调用）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO


class DailyRotatingFileHandler(logging.Handler):
    """运行日志按天落盘 handler：data/logs/kb-YYYY-MM-DD.log"""

    def __init__(self, log_dir: Path, backup_days: int = 14, encoding: str = "utf-8"):
        super().__init__()
        self.log_dir = log_dir
        self.backup_days = backup_days
        self.encoding = encoding
        self._current_date = ""
        self._stream: TextIO | None = None
        self._file_re = re.compile(r"^kb-(\d{4}-\d{2}-\d{2})\.log$")

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

    def _cleanup(self) -> None:
        cutoff = (datetime.now() - timedelta(days=self.backup_days)).strftime("%Y-%m-%d")
        for p in self.log_dir.glob("kb-*.log"):
            m = self._file_re.match(p.name)
            if m and m.group(1) < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass

    def emit(self, record) -> None:
        try:
            self._ensure_stream()
            assert self._stream is not None
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
            self._cleanup()
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
