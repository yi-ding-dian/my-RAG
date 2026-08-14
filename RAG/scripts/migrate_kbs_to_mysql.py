#!/usr/bin/env python
"""一次性迁移脚本：data/kbs/*.json → 知识库表（MySQL）

背景：多租户改造后知识库元数据从 JSON 迁入 kbs 表（users/departments/kbs），
历史 data/kbs/*.json 不会自动迁移（方案约定），由本脚本一次性搬运。

行为：
- 目标部门：默认部门（dept_default，init_db 种子）；归属 owner=admin；
- 幂等：目标库已存在同名（name + department）知识库 → 跳过；
- 保留原 id/name/description/doc_count/chunk_count/created_at；
- 完成后提示可手动清理 data/kbs/*.json（不自动删，迁移确认后再删）。

用法（先启动后端完成 init_db 种子，或本脚本自动 init_db）：
    MYSQL_URL=mysql+aiomysql://user:pass@host:port/db  .venv/bin/python scripts/migrate_kbs_to_mysql.py
    # 测试（sqlite 文件库，可重复运行验证幂等）：
    MYSQL_URL=sqlite+aiosqlite:////tmp/migrate_test.db .venv/bin/python scripts/migrate_kbs_to_mysql.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 脚本可独立运行（不在项目根时也能找到 backend 包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from backend.config import KBS_DIR  # noqa: E402
from backend.db import get_session, init_db  # noqa: E402
from backend.models.user_models import (DepartmentORM, KBORM, UserORM)  # noqa: E402


async def main() -> int:
    # 1) 建表 + 种子（默认部门 dept_default + admin；已存在则跳过）
    await init_db()

    kb_files = sorted(KBS_DIR.glob("*.json"))
    if not kb_files:
        print("未发现 data/kbs/*.json，无需迁移")
        return 0

    async with get_session() as db:
        # 2) 定位默认部门与 admin
        dept = (await db.execute(
            select(DepartmentORM).where(DepartmentORM.id == "dept_default")
        )).scalar_one_or_none()
        admin = (await db.execute(
            select(UserORM).where(UserORM.username == "admin")
        )).scalar_one_or_none()
        if dept is None or admin is None:
            print("错误：默认部门或 admin 账号不存在（init_db 种子未执行成功）")
            return 1

        # 3) 已存在同名 kb 集合（幂等判定：name + 目标部门）
        existing = set((await db.execute(
            select(KBORM.name).where(KBORM.department_id == dept.id)
        )).scalars().all())

        migrated = skipped = failed = 0
        print(f"开始迁移 {len(kb_files)} 个知识库 JSON → kbs 表"
              f"（部门={dept.name}，owner=admin）")
        for f in kb_files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                name = (data.get("name") or "").strip()
                if not name:
                    print(f"  [跳过] {f.name}: 无名称")
                    skipped += 1
                    continue
                if name in existing:
                    print(f"  [跳过] {f.name}: 同名知识库已存在（幂等）")
                    skipped += 1
                    continue
                kb = KBORM(
                    id=data.get("id") or f.stem[:32],
                    name=name,
                    description=(data.get("description") or "").strip() or None,
                    department_id=dept.id,
                    owner_id=admin.id,
                    doc_count=int(data.get("doc_count") or 0),
                    chunk_count=int(data.get("chunk_count") or 0),
                    created_at=data.get("created_at") or dept.created_at,
                )
                db.add(kb)
                existing.add(name)
                migrated += 1
                print(f"  [迁移] {f.name}: {name}（doc={kb.doc_count} chunk={kb.chunk_count}）")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  [失败] {f.name}: {e}")
        await db.commit()

    print(f"\n完成：迁移 {migrated}，跳过 {skipped}，失败 {failed}")
    if migrated:
        print("提示：请在确认数据无误后手动清理 data/kbs/*.json（脚本不自动删除）")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
