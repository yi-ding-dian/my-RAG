"""知识库服务：MySQL（kbs 表）CRUD（async + db session）

- 元数据入 kbs 表（department_id/owner_id/doc_count/chunk_count/tags/created_at）；
- tags 列存 JSON 数组字符串（TEXT，如 ["制度","运维"]），None=无标签；
- documents/chat 仍走 JSON 存储（与方案一致）；
- 路由层"实时重算计数"逻辑保留：列表/详情实时调 document_service 统计，
  refresh_stats 供上传/删除文档后持久化；
- 约束：name 非空（ValueError → 路由层 400）；
- 约束：tags ≤10 个、每个 1-20 字符、去重去空白（ValueError → 路由层 400）。
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.rag_models import KnowledgeBase
from backend.models.user_models import KBORM, gen_id, now_str

logger = logging.getLogger(__name__)

MAX_TAGS = 10
MAX_TAG_LEN = 20


def _load_tags(raw: Optional[str]) -> List[str]:
    """tags 列 → 标签列表（容错：None/非 JSON/非字符串数组 → 空列表）"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, str)]


def _dump_tags(tags: List[str]) -> Optional[str]:
    """标签列表 → tags 列（空列表统一存 None）"""
    return json.dumps(tags, ensure_ascii=False) if tags else None


def _validate_tags(tags: List[str]) -> List[str]:
    """标签校验：strip 去空白 → 空标签报错 → 超长报错 → 去重 → ≤10 个

    空数组合法（=清空标签）；纯空白/空字符串标签报错（1-20 字符要求）。
    返回清洗后的标签列表。
    """
    cleaned: List[str] = []
    for t in tags or []:
        t = (t or "").strip()
        if not t:
            raise ValueError("标签不能为空")
        if len(t) > MAX_TAG_LEN:
            raise ValueError(f"单个标签不能超过 {MAX_TAG_LEN} 字符")
        if t not in cleaned:
            cleaned.append(t)
    if len(cleaned) > MAX_TAGS:
        raise ValueError(f"标签最多 {MAX_TAGS} 个")
    return cleaned


def _to_public(orm: KBORM) -> KnowledgeBase:
    """ORM → 对外模型"""
    return KnowledgeBase(
        id=orm.id,
        name=orm.name,
        description=orm.description or "",
        department_id=orm.department_id,
        owner_id=orm.owner_id,
        doc_count=orm.doc_count or 0,
        chunk_count=orm.chunk_count or 0,
        created_at=orm.created_at,
        tags=_load_tags(orm.tags),
    )


class KBService:
    """知识库元数据服务（MySQL kbs 表）"""

    async def create(self, db: AsyncSession, name: str, description: str = "",
                     department_id: Optional[str] = None,
                     owner_id: Optional[str] = None,
                     tags: Optional[List[str]] = None) -> KnowledgeBase:
        """创建知识库（department_id 由路由层按角色决策：dept_admin 强制本部门）"""
        name = (name or "").strip()
        if not name:
            raise ValueError("知识库名称不能为空")
        cleaned_tags = _validate_tags(tags or []) if tags is not None else []
        orm = KBORM(
            id=gen_id(),
            name=name,
            description=(description or "").strip(),
            department_id=department_id,
            owner_id=owner_id,
            doc_count=0,
            chunk_count=0,
            tags=_dump_tags(cleaned_tags),
            created_at=now_str(),
        )
        db.add(orm)
        await db.commit()
        await db.refresh(orm)
        logger.info("创建知识库: %s (%s) dept=%s owner=%s tags=%s",
                    orm.name, orm.id, department_id, owner_id, cleaned_tags)
        return _to_public(orm)

    async def list(self, db: AsyncSession,
                   department_id: Optional[str] = None,
                   tags: Optional[List[str]] = None) -> List[KnowledgeBase]:
        """知识库列表（created_at 倒序）；department_id 传 None 返回全部（super_admin）

        tags 传非空列表时按"包含全部给定标签"（交集）过滤。
        过滤在内存完成而非 SQL LIKE：JSON 字符串内精确匹配跨库兼容性差
        （MySQL/SQLite 版本差异），且知识库量级小、列表本就全量返回。
        """
        stmt = select(KBORM)
        if department_id:
            stmt = stmt.where(KBORM.department_id == department_id)
        stmt = stmt.order_by(KBORM.created_at.desc())
        rows = (await db.execute(stmt)).scalars().all()
        kbs = [_to_public(orm) for orm in rows]
        if tags:
            kbs = [kb for kb in kbs if all(t in kb.tags for t in tags)]
        return kbs

    async def get(self, db: AsyncSession, kb_id: str) -> Optional[KnowledgeBase]:
        """按 id 查知识库，不存在返回 None"""
        orm = await db.get(KBORM, kb_id)
        return _to_public(orm) if orm else None

    async def update(self, db: AsyncSession, kb_id: str,
                     name: Optional[str] = None,
                     description: Optional[str] = None,
                     tags: Optional[List[str]] = None) -> Optional[KnowledgeBase]:
        """更新名称/描述/标签（name 非空校验；tags 传 None 不改，传 [] 清空；不存在返回 None）"""
        orm = await db.get(KBORM, kb_id)
        if orm is None:
            return None
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("知识库名称不能为空")
            orm.name = name
        if description is not None:
            orm.description = description.strip()
        if tags is not None:
            orm.tags = _dump_tags(_validate_tags(tags))
        await db.commit()
        await db.refresh(orm)
        logger.info("更新知识库: %s (%s)", orm.name, orm.id)
        return _to_public(orm)

    async def set_tags(self, db: AsyncSession, kb_id: str,
                       tags: List[str]) -> Optional[KnowledgeBase]:
        """覆盖式设置知识库标签（空数组=清空；校验失败 ValueError；不存在返回 None）"""
        orm = await db.get(KBORM, kb_id)
        if orm is None:
            return None
        cleaned = _validate_tags(tags)
        orm.tags = _dump_tags(cleaned)
        await db.commit()
        await db.refresh(orm)
        logger.info("设置知识库标签: %s (%s) tags=%s", orm.name, orm.id, cleaned)
        return _to_public(orm)

    async def count_tags(self, db: AsyncSession,
                         department_id: Optional[str] = None) -> List[tuple]:
        """标签聚合：可见范围内所有 KB 的标签使用计数

        返回 [(name, count)]，count 降序，同 count 按名称升序（输出稳定便于断言）。
        department_id 传 None 统计全部（super_admin）。
        """
        kbs = await self.list(db, department_id=department_id)
        counter: dict = {}
        for kb in kbs:
            for t in kb.tags:
                counter[t] = counter.get(t, 0) + 1
        return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))

    async def delete(self, db: AsyncSession, kb_id: str) -> bool:
        """删除知识库元数据（级联删除文档/向量由路由层处理）"""
        orm = await db.get(KBORM, kb_id)
        if orm is None:
            return False
        await db.delete(orm)
        await db.commit()
        logger.info("删除知识库: %s (%s)", orm.name, orm.id)
        return True

    async def refresh_stats(self, db: AsyncSession, kb_id: str, doc_count: int,
                            chunk_count: int) -> Optional[KnowledgeBase]:
        """刷新 doc_count/chunk_count（上传/删除文档后持久化，供列表快速展示）"""
        orm = await db.get(KBORM, kb_id)
        if orm is None:
            return None
        orm.doc_count = doc_count
        orm.chunk_count = chunk_count
        await db.commit()
        logger.info("刷新知识库统计: %s docs=%d chunks=%d",
                    kb_id, doc_count, chunk_count)
        return _to_public(orm)

    async def count_by_department(self, db: AsyncSession, dept_id: str) -> int:
        """某部门下的知识库数（部门删除前校验用）"""
        result = await db.execute(
            select(func.count()).select_from(KBORM)
            .where(KBORM.department_id == dept_id))
        return int(result.scalar() or 0)


_kb_service: Optional[KBService] = None


def get_kb_service() -> KBService:
    global _kb_service
    if _kb_service is None:
        _kb_service = KBService()
    return _kb_service
