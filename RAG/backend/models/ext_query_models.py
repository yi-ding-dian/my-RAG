"""外部查询记录 ORM（知识库对外开放查询的访问日志）

与配置（data/ext_queries.json）分开存：配置是小体量、要整体读写的文件，
而记录是持续增长的时序数据——落库才能支撑按时间/链接/IP 的筛选、分页与
聚合（jsonl 得全量扫描，记录上万条后明显变慢）。

建表走 db.py 的 `create_all`（新表自动创建，无需 MIGRATIONS 条目——那里
只用于给存量表补列）；模型需在 db.py 初始化时被 import 才会注册到 metadata。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class ExtQueryLogORM(Base):
    """外部查询访问记录（每完成一次外部查询落一行）

    - created_at 统一 "%Y-%m-%d %H:%M:%S"（与库内其余表一致，字符串字典序
      即时间序，范围过滤与排序可直接比较）
    - config_name 冗余存储：配置被删除后记录仍需可读（审计价值），不靠关联查
    - client_ip / user_agent：外部查询无需账号，IP 是追溯来源的唯一线索
    - source：区分 chat（对外网页）/ query（MCP、Agent 接入）
    """
    __tablename__ = "ext_query_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True,
                                    autoincrement=True)
    config_id: Mapped[str] = mapped_column(String(32), index=True)
    config_name: Mapped[str] = mapped_column(String(64))
    query: Mapped[str] = mapped_column(Text)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16))
    client_ip: Mapped[Optional[str]] = mapped_column(String(64), index=True,
                                                     nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(256),
                                                      nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), index=True)
