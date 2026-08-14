"""统一迁移机制测试：MIGRATIONS 列表化迁移（ensure_columns）冒烟

覆盖：
- 模拟缺列存量库（kbs.tags / users.must_change_password / users.avatar /
  departments.chat_config / departments.department_config 全缺）→
  ensure_columns 一次补全全部列，旧数据不丢；
- 幂等：重复执行不报错、不重复加列；
- 表不存在（首建场景）→ 对应项返回 True 不报错；
- 单项失败不中断后续（首个迁移 DDL 非法 → 其余项仍执行）。
用独立 sqlite 文件库模拟存量表（避免与 TestClient 全局 engine 的
event loop 冲突），直接驱动 db.ensure_columns 统一迁移入口。
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.db import MIGRATIONS, ensure_columns


def _columns(engine, table: str) -> set:
    async def _run():
        from sqlalchemy import inspect
        async with engine.begin() as conn:
            def _inspect(sync_conn):
                return {c["name"]
                        for c in inspect(sync_conn).get_columns(table)}
            return await conn.run_sync(_inspect)
    return asyncio.run(_run())


def _create_legacy_db(engine):
    """建旧版存量表（全缺新列）+ 插入旧数据"""

    async def _run():
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE kbs (id VARCHAR(32) PRIMARY KEY, "
                "name VARCHAR(128), description TEXT, department_id VARCHAR(32), "
                "owner_id VARCHAR(32), doc_count INTEGER NOT NULL DEFAULT 0, "
                "chunk_count INTEGER NOT NULL DEFAULT 0, created_at VARCHAR(32))"))
            await conn.execute(text(
                "CREATE TABLE users (id VARCHAR(32) PRIMARY KEY, "
                "username VARCHAR(64) UNIQUE, password_hash VARCHAR(128), "
                "display_name VARCHAR(64), role VARCHAR(16), "
                "department_id VARCHAR(32), status VARCHAR(16), "
                "created_at VARCHAR(32))"))
            await conn.execute(text(
                "CREATE TABLE departments (id VARCHAR(32) PRIMARY KEY, "
                "name VARCHAR(128), description TEXT, "
                "created_at VARCHAR(32))"))
            await conn.execute(text(
                "INSERT INTO kbs (id, name, doc_count, chunk_count, created_at) "
                "VALUES ('legacy1', '存量库', 0, 0, '2026-01-01 00:00:00')"))
            await conn.execute(text(
                "INSERT INTO users (id, username, password_hash, "
                "display_name, role, status, created_at) "
                "VALUES ('legacy1', 'old_user', 'x', '存量用户', 'user', "
                "'active', '2026-01-01 00:00:00')"))
            await conn.execute(text(
                "INSERT INTO departments (id, name, created_at) "
                "VALUES ('legacy1', '存量部门', '2026-01-01 00:00:00')"))
    asyncio.run(_run())


def _new_columns() -> set:
    """MIGRATIONS 列表声明的全部新列（table.column）"""
    return {(m["table"], m["column"]) for m in MIGRATIONS}


class TestEnsureColumns:
    def test_legacy_db_all_columns_filled(self, tmp_path):
        """缺列存量库 → ensure_columns 一次补全全部新列，旧数据不丢"""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
        try:
            _create_legacy_db(engine)
            results = asyncio.run(ensure_columns(engine))
            assert len(results) == len(MIGRATIONS)
            assert all(results), f"迁移应全部成功: {results}"
            for table, column in _new_columns():
                assert column in _columns(engine, table), \
                    f"缺列未补齐: {table}.{column}"
            # 旧数据不丢
            async def _check():
                async with engine.begin() as conn:
                    rows = (await conn.execute(text(
                        "SELECT tags FROM kbs WHERE id='legacy1'"))).fetchall()
                    assert rows[0][0] is None
                    users = (await conn.execute(text(
                        "SELECT avatar, must_change_password FROM users "
                        "WHERE id='legacy1'"))).fetchall()
                    assert users[0][0] is None
                    assert users[0][1] == 0  # 新列默认值
                    depts = (await conn.execute(text(
                        "SELECT chat_config, department_config FROM departments "
                        "WHERE id='legacy1'"))).fetchall()
                    assert depts[0][0] is None and depts[0][1] is None
            asyncio.run(_check())
        finally:
            asyncio.run(engine.dispose())

    def test_idempotent(self, tmp_path):
        """幂等：重复执行不报错、不重复加列"""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}")
        try:
            _create_legacy_db(engine)
            asyncio.run(ensure_columns(engine))
            results = asyncio.run(ensure_columns(engine))
            assert all(results)
            assert asyncio.run(ensure_columns(engine)) == \
                [True] * len(MIGRATIONS)
        finally:
            asyncio.run(engine.dispose())

    def test_no_table_scenario_ok(self, tmp_path):
        """空库（无任何表，首建前）→ 各项返回 True 不报错"""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
        try:
            results = asyncio.run(ensure_columns(engine))
            assert all(results), "表不存在场景应全部 True（create_all 首建）"
        finally:
            asyncio.run(engine.dispose())

    def test_single_failure_does_not_stop_others(self, tmp_path, monkeypatch):
        """单项迁移失败（DDL 非法）→ 该项 False + warning，其余项仍执行"""
        import backend.db as db_module
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
        try:
            _create_legacy_db(engine)
            bad = dict(MIGRATIONS[0])
            bad["ddl"] = "ALTER TABLE kbs ADD COLUMN"  # 语法非法
            monkeypatch.setattr(db_module, "MIGRATIONS",
                                [bad, *MIGRATIONS[1:]])
            results = asyncio.run(db_module.ensure_columns(engine))
            assert results[0] is False, "非法 DDL 应失败并返回 False"
            assert all(results[1:]), "后续迁移不应被中断"
            for table, column in _new_columns():
                if table == "kbs" and column == "tags":
                    continue  # 该项失败未补列
                assert column in _columns(engine, table)
        finally:
            asyncio.run(engine.dispose())
