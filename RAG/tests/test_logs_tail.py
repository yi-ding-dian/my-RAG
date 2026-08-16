"""系统运行日志接口测试：GET/POST/DELETE /api/logs

覆盖：
- 权限：无 token 401；非 super_admin（dept_admin/user）403
- tail：文件不存在 → 空；offset=0 读全部 + 行解析；增量读取；offset 超文件大小
  归位；offset 落在行中间残片丢弃；limit 截断（tail 语义）；offset<0 尾部模式；
  非标准行 level/ts 置空；date 非法 400
- files：列表按日期倒序（含大小/mtime）；只认 kb-YYYY-MM-DD.log
- files/download：下载返回文件内容 + attachment 文件名；404/非法日期/权限
- 删除：删单天（不存在静默成功 deleted=0）；清空全部（今天文件截断保留，
  其余天删除）

隔离策略：测试手工写"昨天/前天"的文件（logging handler 只写当天文件，不冲突），
当天文件的断言只验证结构不验证内容。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from backend.routers.logs import LOG_DIR, _log_path, _valid_date


def _d(days_ago: int) -> str:
    """N 天前的日期字符串 YYYY-MM-DD"""
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _write(date: str, content: str):
    """覆写指定天的日志文件（先删再写，不留上一测试的旧内容）"""
    p = _log_path(date)
    p.unlink(missing_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _tail(client, headers, date=None, offset=0, limit=200):
    params = {"offset": offset, "limit": limit}
    if date is not None:
        params["date"] = date
    resp = client.get("/api/logs/tail", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _standard_line(ts, level, name, msg):
    """构造一条与 main.py logging 格式一致的标准行"""
    return f"{ts} [{level}] {name}: {msg}"


# ==================== 权限 ====================

class TestLogsPermission:
    """仅 super_admin 可查/可删运行日志"""

    def test_no_token_401(self, client):
        assert client.get("/api/logs/tail").status_code == 401
        assert client.get("/api/logs/files").status_code == 401
        assert client.delete("/api/logs/files").status_code == 401

    def test_non_super_admin_forbidden(self, client, dept_admin_headers,
                                       user_headers):
        for headers in (dept_admin_headers, user_headers):
            assert client.get("/api/logs/tail", headers=headers).status_code == 403
            assert client.get("/api/logs/files", headers=headers).status_code == 403
            assert client.delete("/api/logs/files", headers=headers).status_code == 403


# ==================== tail 行为（按天） ====================

class TestLogsTail:

    def test_invalid_date_400(self, client, admin_headers):
        resp = client.get("/api/logs/tail",
                          params={"date": "2026/01/01"}, headers=admin_headers)
        assert resp.status_code == 400

    def test_file_not_exists(self, client, admin_headers):
        """指定天无文件 → 空 lines、offset=0、eof=true（不报错）"""
        data = _tail(client, admin_headers, date=_d(1))
        assert data == {"lines": [], "offset": 0, "eof": True}

    def test_read_all_from_zero(self, client, admin_headers):
        """offset=0 读全部行：解析 ts/level/message，offset=文件尾"""
        date = _d(1)
        _write(date,
               _standard_line("2026-08-15 10:00:00,123", "INFO", "uvicorn", "启动") + "\n"
               + _standard_line("2026-08-15 10:00:01,456", "WARNING", "backend.main", "连接异常") + "\n"
               + _standard_line("2026-08-15 10:00:02,789", "ERROR", "backend.services", "请求失败") + "\n")
        data = _tail(client, admin_headers, date=date)
        assert data["eof"] is True
        assert data["offset"] == _log_path(date).stat().st_size
        assert len(data["lines"]) == 3
        first = data["lines"][0]
        assert first["ts"] == "2026-08-15 10:00:00,123"
        assert first["level"] == "INFO"
        assert first["message"] == "uvicorn: 启动"

    def test_incremental_read(self, client, admin_headers):
        """续写后从上次 offset 读只返回新行"""
        date = _d(1)
        _write(date, _standard_line("2026-08-15 10:00:00,000", "INFO", "a", "第一行") + "\n")
        data = _tail(client, admin_headers, date=date)
        assert [l["message"] for l in data["lines"]] == ["a: 第一行"]
        offset = data["offset"]

        with _log_path(date).open("a", encoding="utf-8") as f:
            f.write(_standard_line("2026-08-15 10:00:05,000", "INFO", "b", "第二行") + "\n")
            f.write(_standard_line("2026-08-15 10:00:06,000", "ERROR", "c", "第三行") + "\n")
        data2 = _tail(client, admin_headers, date=date, offset=offset)
        assert [l["message"] for l in data2["lines"]] == ["b: 第二行", "c: 第三行"]
        assert data2["offset"] == _log_path(date).stat().st_size

        # 无新写入 → 增量读取返回空
        data3 = _tail(client, admin_headers, date=date, offset=data2["offset"])
        assert data3["lines"] == []

    def test_offset_beyond_file_size(self, client, admin_headers):
        """offset 超文件大小 → 自动归位尾部（返回空，不报错）"""
        date = _d(1)
        _write(date, _standard_line("2026-08-15 10:00:00,000", "INFO", "a", "行") + "\n")
        data = _tail(client, admin_headers, date=date, offset=10 ** 9)
        assert data["lines"] == []
        assert data["offset"] == _log_path(date).stat().st_size

    def test_offset_mid_line_drops_fragment(self, client, admin_headers):
        """offset 落在行中间 → 首行残片丢弃，只返回完整行"""
        date = _d(1)
        _write(date, "AAA\nBBB\n")
        data = _tail(client, admin_headers, date=date, offset=2)  # "AA|A" 中间
        assert [l["line"] for l in data["lines"]] == ["BBB"]

    def test_limit_truncates_tail(self, client, admin_headers):
        """行数超 limit → 取最后 limit 行（tail 语义），offset 仍指向文件尾"""
        date = _d(1)
        lines = [_standard_line(f"2026-08-15 10:00:{i:02d},000", "INFO",
                                "mod", f"第{i}行")
                 for i in range(10)]
        _write(date, "\n".join(lines) + "\n")
        data = _tail(client, admin_headers, date=date, offset=0, limit=3)
        assert [l["message"] for l in data["lines"]] == [
            "mod: 第7行", "mod: 第8行", "mod: 第9行"]
        assert data["offset"] == _log_path(date).stat().st_size

    def test_limit_2000_returns_all(self, client, admin_headers):
        """limit 上限放宽到 2000（详情视图一次拉取）：行数不足时全量返回"""
        date = _d(1)
        lines = [_standard_line(f"2026-08-15 10:00:{i:02d},000", "INFO",
                                "mod", f"第{i}行")
                 for i in range(10)]
        _write(date, "\n".join(lines) + "\n")
        data = _tail(client, admin_headers, date=date, offset=-1, limit=2000)
        assert len(data["lines"]) == 10
        assert [l["message"] for l in data["lines"]] == [
            f"mod: 第{i}行" for i in range(10)]
        assert data["offset"] == _log_path(date).stat().st_size

    def test_tail_mode_negative_offset(self, client, admin_headers):
        """offset < 0 尾部模式：返回最近 limit 行，offset 归位文件尾"""
        date = _d(1)
        lines = [_standard_line(f"2026-08-15 10:00:{i:02d},000", "INFO",
                                "mod", f"第{i}行")
                 for i in range(10)]
        _write(date, "\n".join(lines) + "\n")
        data = _tail(client, admin_headers, date=date, offset=-1, limit=3)
        assert [l["message"] for l in data["lines"]] == [
            "mod: 第7行", "mod: 第8行", "mod: 第9行"]
        assert data["offset"] == _log_path(date).stat().st_size

    def test_non_standard_line(self, client, admin_headers):
        """非标准行（续行/纯文本）：level/ts 置空，整行作 message，不报错"""
        date = _d(1)
        _write(date, "2026-08-15 10:00:00,000 [INFO] mod: 开始\n"
                     "这是多行消息的续行内容\n"
                     "2026-08-15 10:00:01,000 [ERROR] mod2: 结束\n")
        data = _tail(client, admin_headers, date=date)
        assert len(data["lines"]) == 3
        assert data["lines"][0]["level"] == "INFO"
        assert data["lines"][1] == {
            "line": "这是多行消息的续行内容",
            "level": None,
            "ts": None,
            "message": "这是多行消息的续行内容",
        }
        assert data["lines"][2]["level"] == "ERROR"

    def test_empty_lines_skipped(self, client, admin_headers):
        """空行跳过不计入 lines"""
        date = _d(1)
        _write(date, "A\n\n\nB\n")
        data = _tail(client, admin_headers, date=date)
        assert [l["line"] for l in data["lines"]] == ["A", "B"]

    def test_today_default_and_structure(self, client, admin_headers):
        """缺省 date=今天：当天文件被 logging handler 持续写入，只验证结构"""
        data = _tail(client, admin_headers)
        assert data["eof"] is True
        assert data["offset"] == _log_path(_d(0)).stat().st_size
        assert isinstance(data["lines"], list)


# ==================== 文件管理 ====================

class TestLogFiles:

    def test_list_sorted_desc_with_size(self, client, admin_headers):
        """files 按日期倒序，含 filename/size_bytes/mtime"""
        yesterday, day_before = _d(1), _d(2)
        _write(day_before, "x" * 100 + "\n")
        _write(yesterday, "y" * 50 + "\n")
        resp = client.get("/api/logs/files", headers=admin_headers)
        assert resp.status_code == 200
        files = resp.json()["files"]
        dates = [f["date"] for f in files]
        assert yesterday in dates and day_before in dates
        assert dates == sorted(dates, reverse=True)  # 整体倒序
        by_date = {f["date"]: f for f in files}
        assert by_date[yesterday]["size_bytes"] == 51
        assert by_date[yesterday]["filename"] == f"kb-{yesterday}.log"
        assert by_date[day_before]["size_bytes"] == 101
        assert "mtime" in by_date[yesterday]

    def test_delete_single_day(self, client, admin_headers):
        """删单天：文件删除；不存在静默成功 deleted=0"""
        date = _d(1)
        _write(date, "内容\n")
        resp = client.delete("/api/logs/files", params={"date": date},
                             headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1
        assert not _log_path(date).exists()
        # 再删一次：不存在，静默成功
        resp = client.delete("/api/logs/files", params={"date": date},
                             headers=admin_headers)
        assert resp.json()["deleted"] == 0

    def test_delete_invalid_date_400(self, client, admin_headers):
        resp = client.delete("/api/logs/files", params={"date": "2026/01/01"},
                             headers=admin_headers)
        assert resp.status_code == 400

    def test_clear_all(self, client, admin_headers):
        """清空全部：非当天文件删除；当天文件截断保留（写句柄继续有效）"""
        _write(_d(1), "昨天内容\n")
        _write(_d(2), "前天内容\n")
        today = _d(0)
        today_path = _log_path(today)
        today_path.write_text("今天内容\n", encoding="utf-8")

        resp = client.delete("/api/logs/files", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2
        assert not _log_path(_d(1)).exists()
        assert not _log_path(_d(2)).exists()
        # 当天文件保留（写句柄继续有效），但旧内容已被截断（后续日志不会含测试文本）
        assert today_path.exists()
        assert "今天内容" not in today_path.read_text(encoding="utf-8")


# ==================== 文件下载 ====================

class TestLogFileDownload:
    """GET /api/logs/files/download：内容 + attachment 文件名 + 404/400/权限"""

    def test_no_token_401(self, client):
        assert client.get("/api/logs/files/download",
                          params={"date": _d(1)}).status_code == 401

    def test_non_super_admin_forbidden(self, client, dept_admin_headers,
                                       user_headers):
        for headers in (dept_admin_headers, user_headers):
            assert client.get("/api/logs/files/download",
                              params={"date": _d(1)},
                              headers=headers).status_code == 403

    def test_download_content(self, client, admin_headers):
        """下载返回文件全部内容 + attachment + filename（kb-YYYY-MM-DD.log）"""
        date = _d(1)
        content = (_standard_line("2026-08-15 10:00:00,123", "INFO",
                                  "uvicorn", "启动") + "\n"
                   + _standard_line("2026-08-15 10:00:01,456", "ERROR",
                                    "backend.main", "连接异常") + "\n")
        _write(date, content)
        resp = client.get("/api/logs/files/download", params={"date": date},
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.content.decode("utf-8") == content
        assert "text/plain" in resp.headers.get("content-type", "")
        cd = resp.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert f"kb-{date}.log" in cd

    def test_download_not_found_404(self, client, admin_headers):
        """指定天无文件 → 404 + 中文 detail"""
        date = _d(5)  # 本文件用例只写 _d(1)/_d(2)/_d(0)，5 天前必不存在
        resp = client.get("/api/logs/files/download", params={"date": date},
                          headers=admin_headers)
        assert resp.status_code == 404
        assert "无日志文件" in resp.json()["detail"]

    def test_download_invalid_date_400(self, client, admin_headers):
        resp = client.get("/api/logs/files/download",
                          params={"date": "2026/01/01"}, headers=admin_headers)
        assert resp.status_code == 400
        assert "日期格式" in resp.json()["detail"]


# ==================== 内部辅助 ====================

class TestHelpers:

    def test_valid_date(self):
        assert _valid_date("2026-08-15") is True
        assert _valid_date("2026-13-99") is False
        assert _valid_date("2026/08/15") is False
        assert _valid_date("abc") is False
