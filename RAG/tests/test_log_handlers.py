"""日志落盘 handler 测试：按天切分 + 三道磁盘护栏

背景：日志目录原先只有「按天保留」一道约束，没有**大小**护栏——某天出 bug
疯狂刷日志（如 httpx 重试死循环）能把单个文件写到 GB 级，撑爆磁盘后 MySQL /
Milvus 写失败，服务直接不可用。现在加两道：

- **单文件上限**：超限后只记 WARNING 及以上（降级而非截断/分片——分片要改
  `/api/logs` 全套读取契约，截断会丢掉最该看的排障线索）；
- **目录总上限**：超限从最旧的文件开始删（当天文件正在写，不动）。

顺带：清理从「每行执行」改为「按时间节流」（原实现每次 emit 都 glob + stat
整个目录，一天几万行就是几万次目录扫描）。
"""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

from backend.logger.handlers import DailyRotatingFileHandler


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _make_handler(tmp_path, **kw) -> DailyRotatingFileHandler:
    h = DailyRotatingFileHandler(tmp_path, **kw)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    return h


@pytest.fixture
def make_logger(tmp_path):
    """返回 (创建 logger 的函数, 读当天文件内容的函数)；自动清理 handler

    **一个 handler 写多条**是必须的：降级状态是 handler 内部状态，若每次写都
    新建 handler 就永远测不到降级——所以 fixture 返回的是「造一个 logger」的
    工厂，由测试自己决定用它写几条。
    """
    created: list[tuple[logging.Logger, DailyRotatingFileHandler]] = []

    def _make(**kw) -> logging.Logger:
        h = _make_handler(tmp_path, **kw)
        logger = logging.getLogger(f"test.loghandlers.{len(created)}")
        logger.handlers = [h]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        created.append((logger, h))
        return logger

    def _read() -> str:
        p = tmp_path / f"kb-{_today()}.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    yield _make, _read
    for logger, h in created:
        logger.handlers = []
        h.close()


class TestSizeGuard:
    """单文件大小护栏：超限降级为「只记 WARNING 及以上」"""

    def test_degrades_after_limit(self, make_logger):
        make, read = make_logger
        log = make(max_file_bytes=10)          # 上限极小：第一条就超限
        log.info("第一条正常日志")
        log.info("这条应被丢弃")
        log.warning("这条应保留")
        content = read()
        assert "[日志降级]" in content, "超限应写入降级标记"
        assert "第一条正常日志" in content
        assert "这条应被丢弃" not in content, "降级后 INFO 应丢弃"
        assert "这条应保留" in content, "降级后 WARNING 仍要保留"

    def test_error_still_recorded(self, make_logger):
        """降级后 ERROR 必须保留（排障最需要的就是它）"""
        make, read = make_logger
        log = make(max_file_bytes=1)
        log.info("填满")
        log.error("系统故障")
        assert "系统故障" in read()

    def test_normal_size_keeps_info(self, make_logger):
        """未超限时 INFO 正常保留"""
        make, read = make_logger
        log = make(max_file_bytes=10 * 1024 * 1024)
        log.info("普通信息")
        content = read()
        assert "普通信息" in content
        assert "[日志降级]" not in content

    def test_cross_day_resets_degraded(self, tmp_path):
        """跨天重置降级状态（新的一天恢复完整记录）"""
        h = _make_handler(tmp_path, max_file_bytes=1)
        logger = logging.getLogger("test.loghandlers.crossday")
        logger.handlers = [h]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.info("触发降级")
        assert h._degraded is True
        # 模拟跨天：把记录中的日期改成昨天，下次 ensure_stream 会重新打开
        h._current_date = "2000-01-01"
        h._ensure_stream()
        assert h._degraded is False, "跨天应重置降级"
        # 追加模式打开已存在的文件，计量从当前大小起（不是 0）
        assert h._written == (tmp_path / f"kb-{_today()}.log").stat().st_size
        logger.handlers = []
        h.close()


class TestDirSizeGuard:
    """目录总大小护栏：超限从最旧删起，当天文件不动"""

    def test_removes_oldest_until_under_limit(self, tmp_path):
        for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
            (tmp_path / f"kb-{d}.log").write_text("x" * 100, encoding="utf-8")
        today_file = tmp_path / f"kb-{_today()}.log"
        today_file.write_text("y" * 100, encoding="utf-8")
        # 总 400 字节，上限 250 → 需删 2 个（400→300→200）
        h = _make_handler(tmp_path, backup_days=99999, max_dir_bytes=250)
        h._current_date = _today()
        h._cleanup()
        assert not (tmp_path / "kb-2026-01-01.log").exists(), "最旧的先删"
        assert not (tmp_path / "kb-2026-01-02.log").exists(), "删到 <= 上限为止"
        assert (tmp_path / "kb-2026-01-03.log").exists(), "够了就停"
        assert today_file.exists(), "当天文件正在写，不动"
        h.close()

    def test_deletes_only_what_needed(self, tmp_path):
        """刚超一点时只删一个（不做无谓删除）"""
        for d in ("2026-01-01", "2026-01-02"):
            (tmp_path / f"kb-{d}.log").write_text("x" * 100, encoding="utf-8")
        # 总 200，上限 150 → 删 1 个即达标
        h = _make_handler(tmp_path, backup_days=99999, max_dir_bytes=150)
        h._current_date = _today()
        h._cleanup()
        assert not (tmp_path / "kb-2026-01-01.log").exists()
        assert (tmp_path / "kb-2026-01-02.log").exists()
        h.close()

    def test_never_deletes_today(self, tmp_path):
        """极端：当天文件本身就超过目录上限时，也不删它（不删正在写的）"""
        today_file = tmp_path / f"kb-{_today()}.log"
        today_file.write_text("z" * 1000, encoding="utf-8")
        h = _make_handler(tmp_path, backup_days=99999, max_dir_bytes=10)
        h._current_date = _today()
        h._cleanup()
        assert today_file.exists()
        h.close()

    def test_under_limit_keeps_all(self, tmp_path):
        for d in ("2026-01-01", "2026-01-02"):
            (tmp_path / f"kb-{d}.log").write_text("x" * 10, encoding="utf-8")
        h = _make_handler(tmp_path, backup_days=99999,
                          max_dir_bytes=10 * 1024 * 1024)
        h._current_date = _today()
        h._cleanup()
        assert (tmp_path / "kb-2026-01-01.log").exists()
        assert (tmp_path / "kb-2026-01-02.log").exists()
        h.close()


class TestDailyRetention:
    """按天保留（原有行为回归）"""

    def test_old_files_removed(self, tmp_path):
        (tmp_path / "kb-2020-01-01.log").write_text("old", encoding="utf-8")
        (tmp_path / f"kb-{_today()}.log").write_text("now", encoding="utf-8")
        h = _make_handler(tmp_path, backup_days=14)
        h._current_date = _today()
        h._cleanup()
        assert not (tmp_path / "kb-2020-01-01.log").exists()
        assert (tmp_path / f"kb-{_today()}.log").exists()
        h.close()

    def test_unmatched_files_kept(self, tmp_path):
        """命名不合规的文件不参与日期/大小判定，不动它"""
        weird = tmp_path / "not_a_log.txt"
        weird.write_text("x" * 100, encoding="utf-8")
        h = _make_handler(tmp_path, backup_days=1, max_dir_bytes=1)
        h._current_date = _today()
        h._cleanup()
        assert weird.exists()
        h.close()


class TestCleanupThrottle:
    """清理节流：连续写入不重复扫描目录"""

    def test_emit_does_not_cleanup_every_line(self, tmp_path, monkeypatch):
        h = _make_handler(tmp_path)
        calls: list = []
        monkeypatch.setattr(h, "_cleanup", lambda: calls.append(1))
        logger = logging.getLogger("test.loghandlers.throttle")
        logger.handlers = [h]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        for i in range(20):
            logger.info("行 %d", i)
        assert len(calls) <= 1, f"20 行日志只应清理一次，实际 {len(calls)} 次"
        logger.handlers = []
        h.close()
