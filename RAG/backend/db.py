"""数据库层：engine 生命周期 + 建库建表 + 种子 + 依赖注入

设计要点：
- **惰性 engine**：模块 import 不创建任何连接（测试/并发环境变量注入优先），
  首次 get_engine()/init_db() 才按当前活跃配置构建；
- **配置 key 比对自动重建**：get_engine() 以（MYSQL_URL 或 host:port/db/user）
  为 key，配置变更（settings 档案切换/测试重置）时先 dispose 旧 engine 再重建，
  无需重启即生效；
- **测试友好**：MYSQL_URL 注入 sqlite+aiosqlite:// 时走内存库
  （StaticPool 共享单连接 + check_same_thread=False），全程离线；
- **建库降级**：MySQL 自动 CREATE DATABASE IF NOT EXISTS，失败仅 warning
  并提示手工执行 SQL，不阻塞启动。
"""
from __future__ import annotations

import logging
import re
from typing import AsyncIterator, Optional
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    create_async_engine)
from sqlalchemy.orm import DeclarativeBase

from backend.config import get_active_config

logger = logging.getLogger(__name__)

# SQLAlchemy 2.0 声明式基类（所有 ORM 模型继承）
class Base(DeclarativeBase):
    pass


# ---------------- engine 生命周期（惰性 + key 比对重建） ----------------

_engine: Optional[AsyncEngine] = None
_engine_key: Optional[str] = None


def _resolve_database_url() -> str:
    """按当前活跃配置解析数据库 URL

    - MYSQL_URL 非空（测试覆盖，如 sqlite+aiosqlite://）→ 直接使用；
    - 否则 MySQL: mysql+aiomysql://user:quote_plus(pass)@host:port/db?charset=utf8mb4
    """
    cfg = get_active_config()
    mysql = cfg.mysql
    if mysql.url and mysql.url.strip():
        return mysql.url.strip()
    return (
        f"mysql+aiomysql://{quote_plus(mysql.user)}:{quote_plus(mysql.password)}"
        f"@{mysql.host}:{mysql.port}/{mysql.database}?charset=utf8mb4"
    )


def _dispose_engine(engine: AsyncEngine):
    """释放旧 engine 连接池

    跨 event loop 场景（如测试 reset 后另起 loop）下，aiosqlite 连接的
    close 需要 async 上下文会抛 MissingGreenlet —— 连接句柄随后被 GC
    回收，忽略即可，不影响新 engine。
    """
    try:
        engine.sync_engine.dispose()
    except Exception as e:  # noqa: BLE001
        logger.debug("释放旧 engine 连接池异常（可忽略）: %s", str(e)[:120])


def _engine_key_for(url: str) -> str:
    """engine 重建比对 key：URL 覆盖时用 URL 本身；MySQL 用 host:port/db/user"""
    if url.startswith("sqlite") or "://" not in url:
        return url
    # 解析 mysql+aiomysql://user:pass@host:port/db → host:port/db/user
    cfg = get_active_config()
    mysql = cfg.mysql
    if mysql.url and mysql.url.strip():
        return mysql.url.strip()
    return f"{mysql.host}:{mysql.port}/{mysql.database}/{mysql.user}"


def get_engine() -> AsyncEngine:
    """获取数据库 engine（惰性创建；活跃配置变更时自动重建）

    注意：旧 engine 用 sync_engine.dispose() 同步释放连接池（AsyncEngine
    的 dispose 为协程，此处调用点可能是非 async 上下文，同步释放足够）。
    """
    global _engine, _engine_key
    url = _resolve_database_url()
    key = _engine_key_for(url)
    if _engine is None or _engine_key != key:
        if _engine is not None:
            logger.info("数据库配置变更，重建 engine: %s -> %s", _engine_key, key)
            _dispose_engine(_engine)
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            # sqlite 内存库：StaticPool 共享单连接（多连接会各持空库）
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                from sqlalchemy.pool import StaticPool
                kwargs["poolclass"] = StaticPool
        _engine = create_async_engine(url, **kwargs)
        _engine_key = key
        logger.info("数据库 engine 就绪: %s", key)
    return _engine


def reset_db_engine():
    """清空全局 engine（测试用）：下一次 get_engine() 按新配置重建"""
    global _engine, _engine_key
    if _engine is not None:
        _dispose_engine(_engine)
        _engine = None
    _engine_key = None


# ---------------- session 依赖 ----------------

def get_session() -> AsyncSession:
    """创建独立 AsyncSession（绑定当前 engine，engine 重建后自动生效）"""
    return AsyncSession(get_engine(), expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级 session"""
    async with get_session() as session:
        yield session


# ---------------- 初始化：建库 → 建表 → 种子 ----------------

async def _ensure_database():
    """MySQL 自动建库（CREATE DATABASE IF NOT EXISTS，utf8mb4）

    - 仅 MySQL 需要；sqlite 直接跳过；
    - 账号无建库权限时失败仅 warning，提示手工执行 SQL，不阻塞启动。
    """
    cfg = get_active_config()
    mysql = cfg.mysql
    if mysql.url and mysql.url.strip():
        return  # URL 覆盖（sqlite 测试）跳过
    db_name = (mysql.database or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", db_name):
        logger.warning("跳过自动建库：数据库名不合法 %r", db_name)
        return
    server_url = (
        f"mysql+aiomysql://{quote_plus(mysql.user)}:{quote_plus(mysql.password)}"
        f"@{mysql.host}:{mysql.port}/?charset=utf8mb4"
    )
    engine = create_async_engine(server_url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4"))
        logger.info("已确认数据库存在: %s", db_name)
    except Exception as e:
        logger.warning(
            "自动建库失败（%s），可手工执行: CREATE DATABASE IF NOT EXISTS %s "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; —— %s",
            db_name, db_name, str(e)[:200])
    finally:
        await engine.dispose()


# 存量表缺列迁移列表（单点维护：未来加列只需在列表加一行）
# - 背景：MySQL 存量表不会随 create_all 自动加列（create_all 对已存在表是
#   no-op），由 ensure_columns() 显式检测缺列 → ALTER TABLE ADD COLUMN
#   （SQL 两后端兼容：MySQL 与 SQLite 均支持）
# - 顺序 = 历史迁移顺序（互不依赖）；单项失败仅 warning（与"自动建库失败
#   降级"风格一致，日志给出手工 SQL 补救路径），不中断后续项
# - 不引入 schema_version 表：迁移均为幂等"加列"，检测缺列即可，无版本依赖
MIGRATIONS: list = [
    {
        "name": "kbs.tags",
        "table": "kbs",
        "column": "tags",
        "ddl": "ALTER TABLE kbs ADD COLUMN tags TEXT NULL",
    },
    {
        "name": "users.must_change_password",
        "table": "users",
        "column": "must_change_password",
        "ddl": "ALTER TABLE users ADD COLUMN must_change_password "
               "INTEGER NOT NULL DEFAULT 0",
    },
    {
        "name": "users.avatar",
        "table": "users",
        "column": "avatar",
        "ddl": "ALTER TABLE users ADD COLUMN avatar TEXT NULL",
    },
    {
        "name": "departments.chat_config",
        "table": "departments",
        "column": "chat_config",
        "ddl": "ALTER TABLE departments ADD COLUMN chat_config TEXT NULL",
    },
    {
        "name": "departments.department_config",
        "table": "departments",
        "column": "department_config",
        "ddl": "ALTER TABLE departments ADD COLUMN department_config TEXT NULL",
    },
]


async def _ensure_column(engine, mig: dict) -> bool:
    """单列迁移：表存在且缺列 → ALTER；成功 True，失败仅 warning 返回 False"""
    from sqlalchemy import inspect, text
    table = mig["table"]
    column = mig["column"]
    try:
        async with engine.begin() as conn:
            def _inspect(sync_conn):
                insp = inspect(sync_conn)
                if not insp.has_table(table):
                    return "no_table"
                # SQLAlchemy 2.0 移除了 Inspector.has_column，用列名集合判断
                columns = {c["name"] for c in insp.get_columns(table)}
                return "ok" if column in columns else "missing"
            status = await conn.run_sync(_inspect)
            if status == "ok":
                return True
            if status == "no_table":
                # 首建场景：表由 create_all 新建（含该列），无需迁移
                return True
            await conn.execute(text(mig["ddl"]))
            logger.info("存量 %s 表已补 %s 列", table, column)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "%s 表补 %s 列失败，请手工执行: %s; —— %s",
            table, column, mig["ddl"], str(e)[:200])
        return False


async def ensure_columns(engine) -> list:
    """统一迁移入口：遍历 MIGRATIONS 补列（顺序执行，单项失败不中断）

    返回每项是否确认列存在（布尔列表，与 MIGRATIONS 同序）。
    """
    return [await _ensure_column(engine, mig) for mig in MIGRATIONS]


# ---- 薄包装（旧调用点兼容；逻辑统一走 MIGRATIONS 列表） ----

async def ensure_kb_tags_column(engine) -> bool:
    """存量 kbs 表补 tags 列（TEXT，存 JSON 数组字符串）"""
    return await _ensure_column(engine, MIGRATIONS[0])


async def ensure_users_must_change_column(engine) -> bool:
    """存量 users 表补 must_change_password 列（Integer 0/1，默认 0）"""
    return await _ensure_column(engine, MIGRATIONS[1])


async def ensure_user_avatar_column(engine) -> bool:
    """存量 users 表补 avatar 列（TEXT，可空，存 avatars/{user_id}.{ext}）"""
    return await _ensure_column(engine, MIGRATIONS[2])


async def ensure_department_chat_config_column(engine) -> bool:
    """存量 departments 表补 chat_config 列（TEXT，可空，存部门级聊天配置 JSON）"""
    return await _ensure_column(engine, MIGRATIONS[3])


async def ensure_department_config_column(engine) -> bool:
    """存量 departments 表补 department_config 列（TEXT，可空，存部门级完整配置 JSON）"""
    return await _ensure_column(engine, MIGRATIONS[4])


async def init_db() -> dict:
    """初始化数据库：建库（MySQL）→ 建表（create_all）→ 存量表迁移 → 种子数据

    返回 {backend: "mysql"/"sqlite", seeded: {...}} 供启动日志展示。
    """
    # 1. 建库（仅 MySQL；失败降级为 warning）
    await _ensure_database()

    # 2. 建表（import ORM 模型确保注册到 Base.metadata）
    from backend.models import user_models  # noqa: F401
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 3. 存量表结构演进：MySQL 存量表缺列时 ALTER（统一迁移列表 MIGRATIONS）
    await ensure_columns(engine)

    # 3. 种子：默认部门 + 超级管理员（表为空时才插入）
    from backend.services.auth_service import hash_password
    from backend.models.user_models import (DepartmentORM, UserORM,
                                            ROLE_SUPER_ADMIN, STATUS_ACTIVE,
                                            gen_id, now_str)
    from sqlalchemy import func, select
    seeded: dict = {}
    async with get_session() as session:
        # 默认部门（id 固定，便于引用）
        dept_count = (await session.execute(
            select(func.count()).select_from(DepartmentORM))).scalar() or 0
        if dept_count == 0:
            session.add(DepartmentORM(
                id="dept_default",
                name="默认部门",
                description="系统默认部门（可重命名/扩展）",
                created_at=now_str(),
            ))
            await session.commit()
            seeded["department"] = "dept_default"
        # 超级管理员 admin / admin123
        user_count = (await session.execute(
            select(func.count()).select_from(UserORM))).scalar() or 0
        if user_count == 0:
            session.add(UserORM(
                id=gen_id(),
                username="admin",
                password_hash=hash_password("admin123"),
                display_name="超级管理员",
                role=ROLE_SUPER_ADMIN,
                department_id=None,
                status=STATUS_ACTIVE,
                created_at=now_str(),
                must_change_password=0,  # 种子管理员不强制改密
            ))
            await session.commit()
            seeded["admin"] = "admin"
    logger.info("数据库初始化完成: 后端=%s, 种子=%s",
                engine.url.drivername.split("+")[0], seeded or "已存在，跳过")
    return {"backend": engine.url.drivername.split("+")[0], "seeded": seeded}
