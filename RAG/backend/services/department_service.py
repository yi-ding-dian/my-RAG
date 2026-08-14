"""部门服务（多租户 departments 表 CRUD + 部门级聊天配置）

约束：
- name 唯一（重复 → ValueError → 路由层 409）
- 删除前校验：部门下存在用户或知识库时拒绝（ValueError → 路由层 409）
- 部门级聊天配置：chat_config 列存 JSON 字符串（{"chat": {...},
  "retrieval": {...}}），字段级合并到全局活跃档案（部门只覆盖它设置的
  字段，其余用全局；None=未设置，全部使用全局）
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.user_models import (DepartmentCreate, DepartmentORM,
                                        DepartmentPublic, DepartmentUpdate,
                                        gen_id, now_str)

logger = logging.getLogger(__name__)


async def list_departments(db: AsyncSession) -> List[DepartmentPublic]:
    """全部部门"""
    rows = (await db.execute(
        select(DepartmentORM).order_by(DepartmentORM.created_at))).scalars().all()
    return [DepartmentPublic(
        id=d.id, name=d.name, description=d.description,
        created_at=d.created_at) for d in rows]


async def get(db: AsyncSession, dept_id: str) -> Optional[DepartmentPublic]:
    """按 id 查部门，不存在返回 None"""
    orm = await db.get(DepartmentORM, dept_id)
    if orm is None:
        return None
    return DepartmentPublic(
        id=orm.id, name=orm.name, description=orm.description,
        created_at=orm.created_at)


async def create(db: AsyncSession, data: DepartmentCreate) -> DepartmentPublic:
    """创建部门（name 唯一性校验失败抛 ValueError → 路由层 409）"""
    name = (data.name or "").strip()
    if not name:
        raise ValueError("部门名称不能为空")
    existing = (await db.execute(
        select(DepartmentORM).where(DepartmentORM.name == name))).scalar_one_or_none()
    if existing:
        raise ValueError("部门名称已存在")
    orm = DepartmentORM(
        id=gen_id(),
        name=name,
        description=(data.description or "").strip() or None,
        created_at=now_str(),
    )
    db.add(orm)
    await db.commit()
    await db.refresh(orm)
    logger.info("创建部门: %s (%s)", orm.name, orm.id)
    return DepartmentPublic(
        id=orm.id, name=orm.name, description=orm.description,
        created_at=orm.created_at)


async def update(db: AsyncSession, dept_id: str,
                 data: DepartmentUpdate) -> Optional[DepartmentPublic]:
    """更新部门（改名时校验唯一性；不存在返回 None）"""
    orm = await db.get(DepartmentORM, dept_id)
    if orm is None:
        return None
    payload = data.model_dump(exclude_unset=True)
    if "name" in payload:
        new_name = (payload["name"] or "").strip()
        if not new_name:
            raise ValueError("部门名称不能为空")
        if new_name != orm.name:
            dup = (await db.execute(
                select(DepartmentORM)
                .where(DepartmentORM.name == new_name,
                       DepartmentORM.id != dept_id))).scalar_one_or_none()
            if dup:
                raise ValueError("部门名称已存在")
        payload["name"] = new_name
    if "description" in payload:
        payload["description"] = (payload["description"] or "").strip() or None
    for key, value in payload.items():
        setattr(orm, key, value)
    await db.commit()
    await db.refresh(orm)
    logger.info("更新部门: %s", orm.name)
    return DepartmentPublic(
        id=orm.id, name=orm.name, description=orm.description,
        created_at=orm.created_at)


# ==================== 部门级聊天配置（chat_config 列） ====================

def _parse_chat_config(raw: Optional[str]) -> dict:
    """chat_config JSON 字符串 → dict（脏数据/非法 JSON 容错为空 dict）"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("部门 chat_config 解析失败（脏数据，按空处理）: %r",
                       (raw or "")[:200])
        return {}
    if not isinstance(data, dict):
        return {}
    # 段内仅保留对象结构（chat/retrieval 段必须为 dict）
    out: dict = {}
    for section in ("chat", "retrieval"):
        if isinstance(data.get(section), dict):
            out[section] = data[section]
    return out


# ==================== 部门级完整配置（department_config 列） ====================

def _parse_config(raw: Optional[str]) -> dict:
    """department_config JSON 字符串 → dict（脏数据/非法 JSON 容错为空 dict）"""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("部门 department_config 解析失败（脏数据，按空处理）: %r",
                       (raw or "")[:200])
        return {}
    if not isinstance(data, dict):
        return {}
    # 段内仅保留对象结构（llm/chat/retrieval 段必须为 dict）
    out: dict = {}
    for section in ("llm", "chat", "retrieval"):
        if isinstance(data.get(section), dict):
            out[section] = data[section]
    return out


def _merge_legacy_config(new_cfg: dict, legacy: dict) -> dict:
    """department_config 与旧 chat_config 列的读取合并（纯函数）

    - chat/retrieval 段：新列显式设置的字段优先；新列未设置的字段回退
      旧列同名字段；新列显式置空（脏数据防御）→ 清除（跟随全局）；
    - llm 段只来自新列（旧列无 llm）；
    - 返回合并后的完整配置 dict。
    """
    out = dict(new_cfg or {})
    for section in ("chat", "retrieval"):
        new_sec = out.get(section)
        old_sec = legacy.get(section)
        if not isinstance(new_sec, dict):
            new_sec = {}
        if not isinstance(old_sec, dict):
            old_sec = {}
        if not old_sec:
            continue
        merged = dict(old_sec)
        for k, v in new_sec.items():
            if v is None or v == "":
                merged.pop(k, None)
            else:
                merged[k] = v
        if merged:
            out[section] = merged
    return out


async def get_department_config(db: AsyncSession, dept_id: str) -> dict:
    """读取部门级完整配置（dict；部门不存在/未设置 → 空 dict = 全部用全局）

    读取兼容（数据不强制搬迁）：department_config 列 chat/retrieval 段
    显式设置的字段优先；其余缺失字段回退旧 chat_config 列同名字段
    （存量部门升级后、首次保存前行为与旧版一致）。
    """
    orm = await db.get(DepartmentORM, dept_id)
    if orm is None:
        return {}
    return _merge_legacy_config(
        _parse_config(orm.department_config),
        _parse_chat_config(orm.chat_config))


async def get_department_chat_config(db: AsyncSession,
                                     dept_id: str) -> dict:
    """读取部门级聊天配置（chat/retrieval 段；兼容 department_config 优先）

    dict；部门不存在/未设置 → 空 dict = 全部用全局。内部委托
    get_department_config（含旧 chat_config 列回退）。
    """
    cfg = await get_department_config(db, dept_id)
    out: dict = {}
    for section in ("chat", "retrieval"):
        if isinstance(cfg.get(section), dict) and cfg[section]:
            out[section] = cfg[section]
    return out


def _coerce_field(section: str, key: str, value):
    """部门配置字段类型归一化（与 settings_service._coerce chat 段语义一致）

    - temperature/top_p/similarity_threshold/timeout → float；
      max_tokens/history_rounds/top_k → int；enable_multi_turn → bool；
      system_prompt → str；llm 段 base_url/api_key/model 原样保留；
    - 无法转换的值原样返回（路由层白名单已校验过数字类型，此处兜底）
    """
    if section == "chat" and key == "enable_multi_turn":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if key in ("temperature", "top_p", "similarity_threshold", "timeout"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if key in ("max_tokens", "history_rounds", "top_k"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if key == "system_prompt":
        return str(value)
    return value


async def save_department_chat_config(db: AsyncSession, dept_id: str,
                                      payload: dict) -> Optional[dict]:
    """字段级保存部门聊天配置（白名单字段已在路由层校验）

    - 只覆盖 payload 中出现的字段（chat/retrieval 段内）；
    - 字段值为 None 或空串 → 从部门配置中移除该字段（=该字段不覆盖全局，
      跟随全局配置）；其余值写入（类型归一化）；
    - 全部字段被移除/首次保存空载荷 → chat_config 置 NULL（=纯全局）；
    - 返回保存后的部门配置 dict；部门不存在返回 None。
    """
    orm = await db.get(DepartmentORM, dept_id)
    if orm is None:
        return None
    cur = _parse_chat_config(orm.chat_config)
    for section in ("chat", "retrieval"):
        if not isinstance(payload.get(section), dict):
            continue
        sec = cur.setdefault(section, {})
        for k, v in payload[section].items():
            if v is None or v == "":
                # 清除字段（=不覆盖全局）；"恢复默认"对部门语义为跟随全局
                sec.pop(k, None)
            else:
                sec[k] = _coerce_field(section, k, v)
        if not sec:
            cur.pop(section, None)
    orm.chat_config = json.dumps(cur, ensure_ascii=False) if cur else None
    await db.commit()
    logger.info("部门聊天配置已更新: %s (%s)", orm.name, dept_id)
    return cur


async def save_department_config(db: AsyncSession, dept_id: str,
                                 payload: dict) -> Optional[dict]:
    """字段级保存部门完整配置到 department_config 列（白名单已在路由层校验）

    - payload 段：llm/chat/retrieval（每段内字段级合并到现有配置）；
    - 字段值 None 或空串 → 移除该字段（=不覆盖全局，跟随全局配置；
      api_key 空串同语义）；api_key 传回脱敏值（含 ****）→ 保留部门
      原值不覆盖（与全局档案 update_profile 语义一致）；
    - 段内全部字段移除 → 段移除；全空 → department_config 置 NULL
      （=纯全局）；旧 chat_config 列保持不动（读取时自动回退合并）；
    - 返回保存后的部门配置 dict（含旧列回退结果）；部门不存在返回 None。
    """
    orm = await db.get(DepartmentORM, dept_id)
    if orm is None:
        return None
    cur = _parse_config(orm.department_config)
    for section in ("llm", "chat", "retrieval"):
        if not isinstance(payload.get(section), dict):
            continue
        sec = cur.setdefault(section, {})
        for k, v in payload[section].items():
            if v is None or v == "":
                # 清除字段（=不覆盖全局）；"恢复默认"对部门语义为跟随全局
                sec.pop(k, None)
            elif k == "api_key" and isinstance(v, str) and "****" in v:
                continue  # 脱敏回传不覆盖（保留部门原值）
            else:
                sec[k] = _coerce_field(section, k, v)
        if not sec:
            cur.pop(section, None)
    orm.department_config = (
        json.dumps(cur, ensure_ascii=False) if cur else None)
    await db.commit()
    logger.info("部门完整配置已更新: %s (%s)", orm.name, dept_id)
    # 返回读取视角（department_config + 旧 chat_config 列回退）
    return await get_department_config(db, dept_id)


async def delete(db: AsyncSession, dept_id: str) -> bool:
    """删除部门：不存在返回 False；有用户/知识库引用抛 ValueError → 路由层 409"""
    orm = await db.get(DepartmentORM, dept_id)
    if orm is None:
        return False
    from backend.models.user_models import KBORM, UserORM
    user_count = (await db.execute(
        select(func.count()).select_from(UserORM)
        .where(UserORM.department_id == dept_id))).scalar() or 0
    if user_count > 0:
        raise ValueError(f"部门下存在 {user_count} 个用户，请先转移或删除用户")
    kb_count = (await db.execute(
        select(func.count()).select_from(KBORM)
        .where(KBORM.department_id == dept_id))).scalar() or 0
    if kb_count > 0:
        raise ValueError(f"部门下存在 {kb_count} 个知识库，请先迁移或删除知识库")
    await db.delete(orm)
    await db.commit()
    logger.info("删除部门: %s (%s)", orm.name, orm.id)
    return True
