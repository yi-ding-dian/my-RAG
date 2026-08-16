"""服务配置档案 API：/api/settings（读放开管理员，写仅 super_admin）

- GET/POST/PUT/DELETE /api/settings/profiles    档案 CRUD（GET 管理员可读；
  POST/PUT/DELETE 仅超管）
- POST /api/settings/profiles/{id}/activate     激活档案（仅超管，全链路即时生效）
- POST /api/settings/profiles/{id}/test         连接测试（LLM/Embedding/MinerU/
  DeepDoc/MySQL/MinIO 逐项探测；只测不写，super_admin/dept_admin 可执行）
- GET/POST /api/settings/chat                   聊天设置 + 部门 LLM 配置
  （chat/retrieval 段 + llm 段 6 字段白名单）：GET 登录即可读（返回当前
  用户视角的合并配置：全局活跃档案 + 本部门覆盖字段，llm 段为合并后的
  LLM 配置且 api_key 脱敏，dept 段为部门原始配置或 null）；POST 需
  super_admin 或 dept_admin（白名单字段，禁止触碰 Embedding/MinIO 等
  基础设施段）：super_admin 写全局活跃档案（现状行为），dept_admin
  强制写入本部门 department_config（llm/chat/retrieval，不碰全局
  profile），对本部门所有成员生效
- GET /api/settings/llm/models                  模型列表（解析配置弹窗数据源，
  登录即可读）：当前激活档案 LLM 模型列表仅 {name, model}（不含 api_key 等
  敏感字段）+ 激活索引 active；无激活档案 404
- POST /api/settings/llm/test-model              按模型名测试连接（解析配置
  弹窗切换模型前调用，登录即可，只测不写）：后端按 name 从激活档案查完整
  条目（含 api_key）→ probe_llm；查不到 404；返回 {ok, reason, latency_ms}
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_active_config
from backend.db import get_db
from backend.deps import get_current_user, require_super_admin, require_user_admin
from backend.models.user_models import UserPublic
from backend.services import audit_service, department_service
from backend.services.settings_service import (LLM_TEST_TIMEOUT,
                                               SECTION_SCHEMA,
                                               find_llm_item,
                                               get_settings_service,
                                               mask_api_key,
                                               merge_chat_config,
                                               merge_department_llm)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["系统配置"])

# 聊天设置白名单：仅这些字段可经 /api/settings/chat 读写（防越权改基础设施段）。
# 全部从 settings_service.SECTION_SCHEMA 的 whitelist 标记派生（schema 是唯一来源；
# llm 段白名单 = 部门可覆盖的 LLM 字段，其余基础设施段仍仅超管档案管理可改）
def _whitelist(section: str) -> frozenset:
    return frozenset(f.name for f in SECTION_SCHEMA[section].fields.values()
                     if f.whitelist)


CHAT_SECTION_FIELDS = _whitelist("chat")
CHAT_RETRIEVAL_FIELDS = _whitelist("retrieval")
CHAT_LLM_FIELDS = _whitelist("llm")
CHAT_SECTIONS = frozenset(
    s for s, spec in SECTION_SCHEMA.items()
    if any(f.whitelist for f in spec.fields.values()))


def _global_llm_dict() -> dict:
    """全局活跃 LLM 实际配置 → dict（含 timeout 等生效值，供合并展示）"""
    llm = get_active_config().llm
    try:
        return llm.model_dump()
    except AttributeError:  # pragma: no cover - 旧 pydantic 兜底
        return {k: getattr(llm, k, None) for k in (
            "base_url", "api_key", "model", "temperature",
            "max_tokens", "timeout")}


def _mask_llm(llm: dict) -> dict:
    """llm 段对外脱敏：api_key → **** 形态（仅展示，绝不返回明文）"""
    out = dict(llm)
    if out.get("api_key"):
        out["api_key"] = mask_api_key(out["api_key"])
    return out


def _effective_chat_payload(profile: dict,
                            dept_config: Optional[dict] = None) -> dict:
    """响应结构：合并后的聊天配置 + llm 合并视角 + 部门原始配置段（脱敏）

    - retrieval/chat：全局活跃档案 + 部门覆盖字段（merge_chat_config）
    - llm：全局活跃 LLM 实际配置 + 部门 llm 字段覆盖（api_key 脱敏）
    - dept：部门原始配置（{"llm"/"chat"/"retrieval"}，仅含部门显式设置
      的字段，llm.api_key 脱敏）；None = 当前用户无部门/部门未设置
      （=纯全局）
    """
    merged = merge_chat_config(profile, dept_config or {})
    dept_llm = dept_config.get("llm") if isinstance(dept_config, dict) else None
    if not isinstance(dept_llm, dict):
        dept_llm = {}
    llm = _mask_llm(merge_department_llm(_global_llm_dict(), dept_llm))
    dept = None
    if dept_config:
        dept = {}
        for section in ("llm", "chat", "retrieval"):
            sec = dept_config.get(section)
            if isinstance(sec, dict) and sec:
                sec = dict(sec)
                if section == "llm":
                    sec = _mask_llm(sec)
                dept[section] = sec
    return {**merged, "llm": llm, "dept": dept}


def _validate_numeric_field(k: str, v, cast: str) -> None:
    """白名单数值字段类型校验（cast 由 schema 驱动；消息与历史实现逐字一致）"""
    try:
        if cast == "float":
            float(v)
        elif cast == "int":
            int(v)
    except (TypeError, ValueError):
        if cast == "int":
            raise HTTPException(
                status_code=400, detail=f"字段 {k} 必须是整数")
        raise HTTPException(
            status_code=400, detail=f"字段 {k} 必须是数字")


def _validate_chat_section(section: str, body: dict, payload: dict,
                           field_error: str) -> None:
    """chat/retrieval/llm 段的统一白名单+类型校验（schema whitelist/cast/pass_null 驱动）

    - 段内出现未知字段 → 400（白名单 = schema 该段 whitelist=True 的字段）
    - null：段 pass_null=True（chat/llm）→ 原样传给保存层（由 update_profile 的
      on_null 语义决定清空/忽略）；pass_null=False（retrieval）→ 直接丢弃
    - 数值字段（schema cast=float/int，range 校验字段除外）不可转数字 → 400
    """
    sec = body.get(section)
    if sec is None:
        return
    if not isinstance(sec, dict):
        raise HTTPException(status_code=400, detail=f"{section} 必须是对象")
    whitelist = _whitelist(section)
    unknown = set(sec) - whitelist
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"{field_error}: {', '.join(sorted(unknown))}")
    pass_null = SECTION_SCHEMA[section].pass_null
    for k, v in sec.items():
        fspec = SECTION_SCHEMA[section].fields.get(k)
        if v is None:
            if pass_null:
                payload.setdefault(section, {})[k] = None
            continue  # 该段无 null 语义，忽略
        # 类型校验（str/bool 不校验；range 字段由 update_profile 统一校验，
        # 保证"历史轮数需为 1~20"消息不变）
        if fspec is not None and fspec.cast in ("float", "int") and fspec.range is None:
            _validate_numeric_field(k, v, fspec.cast)
        payload.setdefault(section, {})[k] = v


def _validate_chat_payload(body: dict) -> dict:
    """聊天设置白名单校验：只接受 chat/retrieval/llm 段内的白名单字段

    返回仅含白名单字段的载荷（可直接交给 settings_service.update_profile）：
    - 顶层出现其他段（embedding/minio/...）→ 400（防越权改基础设施配置）
    - 段内出现未知字段 → 400；数值字段不可转数字 → 400
    - chat 段 temperature/top_p/max_tokens 显式传 null 合法（= 用 LLM 配置默认）
    - llm 段字段显式传 null/空串 = 该字段不覆盖全局（api_key 空串同语义，
      即"故意清空用全局"）；"****" 脱敏回传由保存层处理（保留原值）
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    unknown = set(body) - CHAT_SECTIONS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"不允许修改配置段: {', '.join(sorted(unknown))}（仅可修改聊天设置）")
    payload: dict = {}
    _validate_chat_section("chat", body, payload, "不允许修改聊天设置字段")
    _validate_chat_section("retrieval", body, payload, "不允许修改检索字段")
    _validate_chat_section("llm", body, payload, "不允许修改 LLM 配置字段")
    if not payload:
        raise HTTPException(
            status_code=400, detail="请至少提供 chat、retrieval 或 llm 段")
    return payload


@router.get("/profiles")
async def list_profiles(user=Depends(require_user_admin)):
    """所有配置档案（密钥字段已脱敏，active 标记活跃档案）；管理员只读"""
    return get_settings_service().list_profiles()


@router.get("/profiles/active")
async def get_active_profile(user=Depends(require_user_admin)):
    """当前活跃档案（管理员只读）"""
    p = get_settings_service().get_active()
    if not p:
        raise HTTPException(status_code=404, detail="没有激活的配置档案")
    return get_settings_service().public_profile(p["id"])


@router.get("/embedding-dim")
async def embedding_dim(user=Depends(require_user_admin)):
    """当前激活 embedding 模型的实际输出维度（实测，全局缓存；管理员只读）

    响应契约: {dimension, model, ok, message}
    - dimension: 实测维度（模型不可用/未配置为 null）；ok=False 时 message 含原因
    - 与配置档案里的 dimension 字段不同：这是真实调用 embed 的结果，
      更换模型后维度冲突检测（/api/kbs/{id}/vector-status）以此为准
    """
    from backend.config import get_active_config
    from backend.services.dim_check import get_model_dimension
    cfg = get_active_config().embedding
    dim = await get_model_dimension()
    if dim is None:
        return {"dimension": None, "model": cfg.model, "ok": False,
                "message": "模型维度检测失败（模型不可用或未配置），入库时将实际校验"}
    return {"dimension": dim, "model": cfg.model, "ok": True,
            "message": f"当前模型实际输出维度 {dim} 维"}


@router.post("/profiles")
async def create_profile(request: Request, body: dict,
                         user: UserPublic = Depends(require_super_admin)):
    """创建配置档案（缺省字段用 .env 出厂值补齐；成功记审计，detail 不含密钥）"""
    name = (body or {}).get("name", "")
    try:
        result = get_settings_service().create_profile(name, body or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit_service.record_action(
        user, action="settings.create", target_type="config",
        target_id=result.get("id"), target_name=name, request=request)
    return result


@router.put("/profiles/{profile_id}")
async def update_profile(request: Request, profile_id: str, body: dict,
                         user: UserPublic = Depends(require_super_admin)):
    """更新配置档案（密钥字段传回脱敏值时保留原值；成功记审计）"""
    try:
        result = get_settings_service().update_profile(profile_id, body or {})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="配置档案不存在")
    await audit_service.record_action(
        user, action="settings.update", target_type="config",
        target_id=profile_id, target_name=result.get("name"),
        detail={"sections": [s for s in SECTION_SCHEMA
                             if isinstance(body.get(s), dict)]},
        request=request)
    return result


@router.delete("/profiles/{profile_id}")
async def delete_profile(request: Request, profile_id: str,
                         user: UserPublic = Depends(require_super_admin)):
    """删除配置档案（删除活跃档案后自动激活剩余第一个；成功记审计）"""
    target = get_settings_service().get_profile(profile_id)
    ok = get_settings_service().delete_profile(profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="配置档案不存在")
    await audit_service.record_action(
        user, action="settings.delete", target_type="config",
        target_id=profile_id,
        target_name=(target or {}).get("name"),
        request=request)
    return {"message": "配置档案已删除"}


@router.post("/profiles/{profile_id}/activate")
async def activate_profile(request: Request, profile_id: str,
                           user: UserPublic = Depends(require_super_admin)):
    """激活配置档案 → config.set_active_config() 全链路即时生效（记审计）"""
    result = get_settings_service().activate(profile_id)
    if not result:
        raise HTTPException(status_code=404, detail="配置档案不存在")
    await audit_service.record_action(
        user, action="settings.activate", target_type="config",
        target_id=profile_id, target_name=result.get("name"), request=request)
    return {"message": f"已切换配置档案: {result.get('name')}", "profile": result}


@router.post("/profiles/{profile_id}/test")
async def test_profile(request: Request, profile_id: str,
                       body: Optional[dict] = None,
                       user: UserPublic = Depends(require_user_admin)):
    """连接测试：按档案测试，body 可选（传未保存的表单值覆盖对应字段）

    只测不写（不对配置做任何变更），super_admin / dept_admin 可执行；
    返回: {llm, embedding, mineru, deepdoc, mysql, minio} 各 {ok, latency_ms, message}
    LLM/Embedding/MySQL/MinIO 5s 超时，MinerU 3s、DeepDoc 8s 超时（短超时避免页面卡死）
    审计：记各连接成功与否（status=success 仅当全部 ok）。
    """
    svc = get_settings_service()
    profile = svc.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="配置档案不存在")
    if body:
        # 表单未保存值覆盖测试（支持传单 section 或全量；chat 段不入测试
        # ——连接测试只覆盖可探测的服务段，与 schema 段序一致）
        profile = dict(profile)
        for section in (s for s in SECTION_SCHEMA if s != "chat"):
            if isinstance(body.get(section), dict):
                profile[section] = body[section]
    result = await svc.test_connections(profile)
    ok_map = {k: bool(v.get("ok")) for k, v in result.items()}
    await audit_service.record_action(
        user, action="settings.test-connections", target_type="config",
        target_id=profile_id, target_name=profile.get("name"),
        detail=ok_map,
        status="success" if all(ok_map.values()) else "failed",
        request=request)
    return result


@router.post("/llm/test")
async def test_llm_connection(body: dict,
                              user: UserPublic = Depends(require_user_admin)):
    """测试单个 LLM 模型连接：GET {base_url}/models（probes.probe_llm），≤5s

    前端勾选激活模型时先调用：成功才允许激活；失败返回原因由前端提示
    （管理员可确认后强制激活）。只测不写（不对配置做任何变更），
    super_admin / dept_admin 可执行。body 为单个模型条目
    {name, base_url, api_key, model, timeout, ...}。
    """
    from backend.services.probes import probe_llm
    timeout = LLM_TEST_TIMEOUT
    if isinstance(body, dict):
        try:
            raw = body.get("timeout")
            if raw:
                timeout = min(LLM_TEST_TIMEOUT, float(raw))
        except (TypeError, ValueError):
            pass
    result = await probe_llm(body, timeout=timeout)
    return {"ok": result["ok"], "reason": result["reason"],
            "latency_ms": result["latency_ms"]}


@router.get("/llm/models")
async def list_llm_models(user: UserPublic = Depends(get_current_user)):
    """LLM 模型列表（解析配置弹窗数据源，登录即可读）

    当前激活档案的 LLM 模型列表：仅 {name, model}（不含 api_key/base_url 等
    敏感字段）+ 激活索引 active（前端标注"当前使用"）；无激活档案 → 404。
    谁都能打开解析配置弹窗，故权限为登录即可（普通用户解析时也可见/可切换）。
    """
    svc = get_settings_service()
    p = svc.get_active()
    if not p:
        raise HTTPException(status_code=404, detail="没有激活的配置档案")
    llm = p.get("llm") or {}
    models = llm.get("models")
    items = []
    if isinstance(models, list):
        for m in models:
            if isinstance(m, dict):
                items.append({"name": m.get("name"), "model": m.get("model")})
    return {"models": items, "active": int(llm.get("active") or 0)}


@router.post("/llm/test-model")
async def test_llm_model_by_name(body: dict,
                                 user: UserPublic = Depends(get_current_user)):
    """按模型名测试连接（解析配置弹窗切换模型前调用；只测不写，登录即可）

    前端无明文 api_key 且 /llm/test 为管理员专用，故本接口按 name 从激活
    档案查完整条目（含 api_key）→ probes.probe_llm（GET {base_url}/models，
    ≤5s）；查不到模型 → 404；返回 {ok, reason, latency_ms}。
    """
    from backend.services.probes import probe_llm
    name = (body or {}).get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=400, detail="缺少模型名称 name")
    item = find_llm_item(name)
    if not item:
        raise HTTPException(status_code=404, detail=f"模型不存在: {name}")
    timeout = LLM_TEST_TIMEOUT
    try:
        raw = item.get("timeout")
        if raw:
            timeout = min(LLM_TEST_TIMEOUT, float(raw))
    except (TypeError, ValueError):
        pass
    result = await probe_llm(item, timeout=timeout)
    return {"ok": result["ok"], "reason": result["reason"],
            "latency_ms": result["latency_ms"]}


# ==================== 聊天设置（chat 段；dept_admin 可读写） ====================

@router.get("/chat")
async def get_chat_settings(user: UserPublic = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    """当前用户视角的聊天配置 + LLM 合并配置（全局活跃档案 + 本部门覆盖）

    登录即可读：聊天设置弹窗与系统配置页 LLM 表单数据源（user 角色弹窗
    入口隐藏，但读取接口开放）；返回 merged 值（dept_admin 打开弹窗即见
    本部门生效配置）。llm 段：全局 LLM + 部门 llm 覆盖后的合并值
    （api_key 脱敏，绝不返回明文）。dept 段：本部门原始配置（仅含部门
    显式设置的字段），无部门/未设置=null。无活跃档案 → 404 明确错误。
    """
    p = get_settings_service().get_active()
    if not p:
        raise HTTPException(status_code=404, detail="没有激活的配置档案")
    dept_cfg = None
    if user.department_id:
        dept_cfg = await department_service.get_department_config(
            db, user.department_id) or None
    return _effective_chat_payload(p, dept_cfg)


@router.post("/chat")
async def update_chat_settings(request: Request, body: dict,
                               user: UserPublic = Depends(require_user_admin),
                               db: AsyncSession = Depends(get_db)):
    """更新聊天设置/部门 LLM 配置（按角色分流，白名单字段）

    - super_admin：写全局活跃档案（现状行为，不涉及部门配置）；
    - dept_admin：强制写入本部门 department_config（llm/chat/retrieval
      三段字段级合并，忽略 body 的 department_id，白名单校验本身拒绝该
      字段），对本部门所有成员生效，**不碰全局 profile**；无部门归属 →
      403。响应返回本部门视角的合并配置（llm 合并值 + dept 段 = 部门
      原始配置，api_key 脱敏）。
    - 白名单校验（_validate_chat_payload）：chat 段 6 字段 +
      retrieval.top_k/similarity_threshold + llm 段 6 字段（base_url/
      api_key/model/temperature/max_tokens/timeout），禁止携带其他段
      （防越权改 Embedding/MinIO 等）。成功记审计。
    """
    svc = get_settings_service()
    active = svc.get_active()
    if not active:
        raise HTTPException(status_code=404, detail="没有激活的配置档案")
    payload = _validate_chat_payload(body)
    if user.role == "dept_admin":
        # 部门管理员：强制写本部门（不碰全局 profile）
        if not user.department_id:
            raise HTTPException(
                status_code=403, detail="当前账号未分配部门，无法配置聊天设置")
        saved = await department_service.save_department_config(
            db, user.department_id, payload)
        if saved is None:
            raise HTTPException(status_code=404, detail="部门不存在")
        dept = await department_service.get(db, user.department_id)
        await audit_service.record_action(
            user, action="settings.chat-update", target_type="dept",
            target_id=user.department_id,
            target_name=dept.name if dept else user.department_id,
            detail={"department_id": user.department_id,
                    "sections": list(payload)}, request=request)
        return _effective_chat_payload(active, saved or None)
    # super_admin：写全局活跃档案（现状行为，llm 段同步支持）
    try:
        updated = svc.update_profile(active["id"], payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        # P2 竞态：并发删除活跃档案后更新失败 → 明确 400 而非 500
        raise HTTPException(status_code=400, detail="配置更新失败")
    await audit_service.record_action(
        user, action="settings.chat-update", target_type="config",
        target_id=active["id"], target_name=active.get("name"),
        detail={"sections": list(payload)}, request=request)
    return _effective_chat_payload(updated, None)
