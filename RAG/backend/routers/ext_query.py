"""外部查询 API（知识库对外开放查询）

管理（仅 super_admin，prefix /api/ext-queries）：
- GET    /api/ext-queries                列表（附加 kb_names；token 打码回传）
- GET    /api/ext-queries/{id}/token     取完整 token（复制分发链接用，落审计）
- POST   /api/ext-queries                新建 {name, kb_ids, config} → 完整配置（含 token）
- PUT    /api/ext-queries/{id}           编辑 name/kb_ids/config（token 不变）
- POST   /api/ext-queries/{id}/reset-token  重置 token（旧链接立即失效）→ {token}
- POST   /api/ext-queries/{id}/toggle    启用/停用切换
- DELETE /api/ext-queries/{id}           删除

外部（公开，prefix /api/ext，token 鉴权，无需系统账号）：
- GET  /api/ext/{config_id}/info?token=xxx   页面挂载校验 → {name, kb_names}
- GET  /api/ext/{config_id}/images/{doc_id}/{name}?token=xxx
  图片代理：读取本配置暴露知识库内的解析图片（外部无 JWT，内部图片接口必 401）
- POST /api/ext/{config_id}/chat              Bearer token；body {query, session_id?}
  → SSE 流式（meta(sources) → delta → done / error），复用 chat_service 的
    system 组装与 LLM 流式能力（_build_system_content/_build_knowledge/
    _build_refs/_get_client），LLM 模型由 config.llm_model 指定（空 = 全局
    激活模型，指定但已不存在也回退全局）、生成参数由 config 覆盖；
    多库检索（每库 top_k 候选 → 合并按 score 降序取全局 top_k）；
    无命中直接告知不调 LLM；每次查询落审计日志（ext_query_logs.jsonl）；
    每 config 每分钟限流（超限 429）。
- POST /api/ext/{config_id}/query            Bearer token；同步非流式（MCP/Agent 接入）

图片（config.enable_images，默认开）：
- 检索后由 _adapt_images 统一适配：开 → 内部链接 /api/files/images/... 改写为
  本配置的 /api/ext/{id}/images/...?token=xxx（meta 下发与注入 LLM 同一份文本，
  故回答正文里模型输出的也是外部可直接加载的链接）；关 → 整体剥除图片语法。
- 关闭时后端同时不再追加 _IMAGE_OUTPUT_RULE，模型自然不会输出图片。

安全设计（对外统一防探测）：
- 配置不存在 / token 不匹配 / 已停用 → 一律 401「链接无效或已失效」（不区分
  响应，避免暴露配置存在性）；管理端操作不存在配置 → 404（超管场景无探测风险）
- token 即访问凭证：泄露可被外部滥用，超管可重置（旧链接立即失效）/停用
- 限流：每 config 每分钟 20 次（内存滑动窗口），防 token 泄露后刷量
- 审计日志：只记 query 摘要（截 100 字）与命中数，不记回答内容
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from backend.config import get_active_config
from backend.db import get_db
from backend.deps import require_super_admin
from backend.models.user_models import UserPublic
from backend.services import audit_service
from backend.services.chat_service import ChatService, \
    get_chat_service, sse_event
from backend.services.document_service import get_document_service
from backend.services.ext_query_service import (get_ext_query_service,
                                                coerce_config, is_expired,
                                                resolve_llm_config,
                                                resolve_system_prompt,
                                                resolve_top_p)
from backend.services.settings.service import list_prompt_items
from backend.services.kb_service import get_kb_service
from backend.services.retrieval_service import (RetrievalUnavailableError,
                                                get_retrieval_service)
from backend.services.storage_service import get_storage_service

from backend.logger import AppLog

logger = logging.getLogger(__name__)
log = AppLog(__name__)

# ==================== 请求模型 ====================

# 管理端 config 提交（字段全可选：None/缺省 = 跟随全局活跃配置；
# system_prompt 空串 = 内置默认模板，与聊天配置语义一致）
# 注意：这里必须与 ext_query_service.CONFIG_FIELDS 一一对应——pydantic 会
# 静默丢弃未声明的字段，漏加会导致该配置项"改了没反应"（曾踩过 enable_images）
class ExtQueryConfigIn(BaseModel):
    system_prompt: Optional[str] = None
    # 引用的提示词库条目名（配置档案 prompts 段）；与 system_prompt 二选一，自定义优先
    system_prompt_ref: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    top_k: Optional[int] = None
    similarity_threshold: Optional[float] = None
    enable_multi_turn: Optional[bool] = None
    enable_images: Optional[bool] = None
    # 指定 LLM 模型（全局活跃档案模型列表里的名称）；空串/缺省 = 跟随全局激活模型
    llm_model: Optional[str] = None


class ExtQueryCreateRequest(BaseModel):
    name: str = Field(..., description="外部查询名称（1~50 字）")
    kb_ids: List[str] = Field(..., description="暴露的知识库 ID 列表（1~10 个，超管视角全部门）")
    config: Optional[ExtQueryConfigIn] = None
    expires_at: Optional[str] = Field(
        None, description='到期时间 "YYYY-MM-DD HH:MM:SS"；空/缺省 = 永久有效')


class ExtQueryUpdateRequest(BaseModel):
    name: Optional[str] = None
    kb_ids: Optional[List[str]] = None
    config: Optional[ExtQueryConfigIn] = None
    expires_at: Optional[str] = Field(
        None, description='到期时间；空串 = 清除有效期（改永久）；不传 = 保持不变')


class ExtQueryRenewRequest(BaseModel):
    """续期请求（从 max(现在, 原到期时间) 顺延，未过期时不损失剩余天数）"""
    days: int = Field(30, description="续期天数（1~3650）")


class ExtQueryChatRequest(BaseModel):
    query: str = Field(..., description="外部用户问题")
    session_id: Optional[str] = Field(
        None, description="会话 ID（同一链接页面内多轮上下文；为空则不续上下文）")


class ExtQuerySyncRequest(BaseModel):
    """同步查询请求（MCP 接入用：一次请求返回完整回答 + 引用来源，非流式）"""
    query: str = Field(..., description="外部用户问题")
    top_k: Optional[int] = Field(
        None, description="检索条数覆盖（1~20；None=取配置 config.top_k，再取全局）")


# ==================== 共享辅助 ====================

_NOT_FOUND_MSG = "外部查询不存在"


async def _list_kb_map(db: AsyncSession) -> dict:
    """超管视角全部知识库 id → {id, name, department_id}（外部配置可引用任何部门库）"""
    kbs = await get_kb_service().list(db)
    return {kb.id: {"id": kb.id, "name": kb.name,
                    "department_id": kb.department_id} for kb in kbs}


def _attach_kb_names(items: list, kb_map: dict) -> list:
    """列表项附加 kb_names（前端展示库名/部门用）"""
    for it in items:
        it["kb_names"] = [kb_map[k] for k in it.get("kb_ids", [])
                          if k in kb_map]
    return items


async def _validate_kb_ids(db: AsyncSession, kb_ids: List[str]) -> None:
    """暴露的库必须存在（超管视角所有部门；任一不存在 → 400 指明）"""
    kb_map = await _list_kb_map(db)
    missing = [k for k in kb_ids if k not in kb_map]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"知识库不存在或不可用: {', '.join(missing[:3])}")


def _auth_ext(config_id: str, token: str) -> dict:
    """外部鉴权：配置存在 + token 匹配 + 已启用 + 未过期，任一不满足 → 统一 401

    防探测：不区分「配置不存在 / token 错误 / 已停用 / 已过期」的响应差异——
    外部无法据此判断链接是"过期了"还是"压根不存在"。
    """
    ext = get_ext_query_service().get(config_id)
    if (not ext or not token or ext.get("token") != token
            or not ext.get("enabled", True) or is_expired(ext)):
        raise HTTPException(status_code=401, detail="链接无效或已失效")
    return ext


def _bearer_token(request: Request) -> Optional[str]:
    """解析 Authorization: Bearer xxx"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


# ==================== 图片适配（外部场景） ====================
#
# 背景：知识块文本里的图片是内部代理链接 /api/files/images/{doc_id}/{name}，
# 该端点按「登录用户 + 知识库权限」鉴权——外部用户与 Agent 都没有系统账号，
# 原链接一律 401，图片必然加载失败。故对外统一改写为本配置的专用图片端点
# （复用 ext token 鉴权，见 ext_image），外链与内部聊天链路均不受影响。

# 内部图片代理链接（知识块文本中的形态）：doc_id 与文件名同 files.py 白名单
_INTERNAL_IMAGE_RE = re.compile(
    r"/api/files/images/([\w.-]+)/([\w.-]+)")
# Markdown 图片语法（关闭图片展示时整体剥除）
_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 外部图片端点链接（同步接口提取 image_urls 用）
_EXT_IMAGE_RE = re.compile(r"/api/ext/[\w-]+/images/[^\s)\"']+")
# 图片文件名白名单（与 files.py 同款，防路径穿越）
_IMAGE_NAME_RE = re.compile(r"^[\w.-]+$")

# 图片输出引导：模型默认倾向用文字"描述"图片而不输出图片语法。实测知识块里
# 图片注入到位（image492.png 等）、sources 的 image_urls 提取正常，但回答仍是
# 纯文字——不显式引导，前端的图片渲染能力等于空转。
_IMAGE_OUTPUT_RULE = (
    "\n\n[图片展示规则]\n"
    "引用的知识片段中若含 Markdown 图片（形如 ![](/api/files/images/xxx.png)），"
    "且该图片与问题相关，请在回答的相应位置原样输出该图片语法，"
    "以便展示原文档中的示意图；图片链接必须原样保留，不要改写、补全或省略。"
)


def _ext_image_url(config_id: str, token: str, doc_id: str, name: str) -> str:
    """知识库图片 → 外部查询专用图片链接（带 ext token，外部页/Agent 直接可用）"""
    return (f"/api/ext/{config_id}/images/{doc_id}/{name}"
            f"?token={quote(token or '')}")


def _adapt_images(text: str, config_id: str, token: str, enabled: bool) -> str:
    """知识块文本中的图片适配外部场景

    - 开启图片：内部图片链接改写为本配置的专用端点（外部可直接加载）
    - 关闭图片：整体剥除图片语法（外部无 JWT，留着只会渲染成裂图或死链）
    """
    if not text:
        return text
    if enabled:
        return _INTERNAL_IMAGE_RE.sub(
            lambda m: _ext_image_url(config_id, token, m.group(1), m.group(2)),
            text)
    return _IMAGE_MD_RE.sub("", text)


def _adapt_sources_images(sources: List, config_id: str, token: str,
                          conf: dict) -> None:
    """就地适配引用片段的图片链接（meta 下发与注入 LLM 用同一份文本）"""
    enabled = bool(conf.get("enable_images", True))
    for s in sources:
        s.text = _adapt_images(s.text, config_id, token, enabled)
        if s.parent_text:
            s.parent_text = _adapt_images(s.parent_text, config_id, token,
                                          enabled)


# ==================== 管理 API（仅 super_admin） ====================

admin_router = APIRouter(prefix="/api/ext-queries", tags=["外部查询管理"])


def _mask_token(token: str) -> str:
    """token 打码：访问即凭证，列表/详情不回传明文（仅保留首尾少量字符）"""
    if not token:
        return ""
    if len(token) <= 10:
        return "********"
    return f"{token[:4]}****{token[-3:]}"


@admin_router.get("")
async def list_ext_queries(db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(require_super_admin)):
    """列表（token 打码回传——完整凭证需单独接口取回，防列表批量泄露）"""
    kb_map = await _list_kb_map(db)
    items = _attach_kb_names(get_ext_query_service().list(), kb_map)
    for it in items:
        if it.get("token"):
            it["token"] = _mask_token(it["token"])
    return items


@admin_router.get("/overview")
async def ext_query_overview(user: UserPublic = Depends(require_super_admin)):
    """总览统计：链接数 / 启用数 / 即将到期数 / 已过期数 + 记录今日与累计数

    供总览页卡片展示。注意路由须定义在字面量路径上——与 /{config_id}/xxx
    段数不同，不会互相抢占。
    """
    return await get_ext_query_service().overview()


@admin_router.get("/defaults")
async def ext_query_defaults(user: UserPublic = Depends(require_super_admin)):
    """表单留空（"跟随全局"）时各项实际会用到的值

    供前端在 placeholder 里显示「跟随全局（0.2）」这类文案——此前只写
    "跟随全局"，超管看不到真正生效的数字，等于盲填。取值口径与 /chat、
    /query 里的 fallback 完全一致（模型相关项取全局激活模型自己的值）。
    """
    cfg = get_active_config()
    llm_cfg = resolve_llm_config({})  # 未指定模型 = 全局激活模型
    # "跟随全局"时实际会用到的提示词：全局聊天设置里配了就用它，没有才回落到
    # 内置模板（{refs} 处运行时注入引用内容）
    from backend.services.chat_service import _SYSTEM_PROMPT_TEMPLATE
    default_prompt = resolve_system_prompt({}, cfg.chat.system_prompt) or \
        _SYSTEM_PROMPT_TEMPLATE.format(
            refs="【此处运行时注入本次检索到的引用内容】")
    return {
        "llm_model": llm_cfg.get("model") or "",
        "temperature": llm_cfg.get("temperature"),
        "max_tokens": llm_cfg.get("max_tokens"),
        # Top P：本配置 → 模型配置（LLMConfig.top_p，默认 0.9）
        "top_p": resolve_top_p({}, resolve_llm_config({})),
        "top_k": cfg.retrieval.top_k,
        "similarity_threshold": cfg.retrieval.similarity_threshold,
        "enable_multi_turn": False,
        "enable_images": True,
        # 提示词库条目（外部链接的表单下拉可选项，来源=当前激活档案）
        "prompt_options": list_prompt_items(),
        # "跟随全局"时实际会用到的提示词全文（全局聊天设置优先，否则内置模板）
        "default_system_prompt": default_prompt,
    }


@admin_router.get("/logs")
async def list_ext_query_logs(
        config_id: Optional[str] = Query(None, description="按链接筛选"),
        ip: Optional[str] = Query(None, description="按来源 IP 模糊筛选"),
        start: Optional[str] = Query(
            None, description='起始时间 "YYYY-MM-DD HH:MM:SS"（含）'),
        end: Optional[str] = Query(
            None, description='结束时间 "YYYY-MM-DD HH:MM:SS"（含）'),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=200),
        user: UserPublic = Depends(require_super_admin)):
    """外部查询记录（按链接 / IP / 时间段筛选，时间倒序分页）

    记录含来源 IP 与接入方式——外部查询无需账号，IP 是追溯来源的唯一线索。
    记录在配置被删除后仍保留（config_name 冗余存储，审计价值）。
    """
    return await get_ext_query_service().list_logs(
        config_id=config_id, ip=ip, start=start, end=end,
        page=page, page_size=page_size)


@admin_router.get("/{ext_id}/token")
async def get_ext_query_token(request: Request, ext_id: str,
                              db: AsyncSession = Depends(get_db),
                              user: UserPublic = Depends(require_super_admin)):
    """取完整 token（仅超管）：复制分发链接用——列表不回传明文，
    需要改记录（创建/重置）之外仅在本次响应返回，全程审计"""
    item = get_ext_query_service().get(ext_id)
    if not item:
        raise HTTPException(status_code=404, detail="外部查询配置不存在")
    await audit_service.record_action(
        user, action="ext.token-view", target_type="ext_query",
        target_id=ext_id, target_name=item.get("name", ""), request=request)
    return {"token": item.get("token") or "", "id": ext_id}


@admin_router.post("", status_code=201)
async def create_ext_query(request: Request, body: ExtQueryCreateRequest,
                           db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(require_super_admin)):
    """新建外部查询 → 完整配置（token 仅在本次响应返回明文，用于前端展示复制链接）"""
    await _validate_kb_ids(db, body.kb_ids)
    try:
        item = get_ext_query_service().create(
            body.name, body.kb_ids,
            coerce_config(body.config.model_dump() if body.config else {}),
            user_id=user.id, expires_at=body.expires_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await audit_service.record_action(
        user, action="ext.create", target_type="ext_query", target_id=item["id"],
        target_name=item["name"][:100],
        detail={"kb_count": len(item["kb_ids"])}, request=request)
    kb_map = await _list_kb_map(db)
    return _attach_kb_names([item], kb_map)[0]


@admin_router.put("/{config_id}")
async def update_ext_query(request: Request, config_id: str,
                           body: ExtQueryUpdateRequest,
                           db: AsyncSession = Depends(get_db),
                           user: UserPublic = Depends(require_super_admin)):
    """编辑名称/暴露库/查询参数（token 不变，链接继续有效）"""
    if body.kb_ids is not None:
        await _validate_kb_ids(db, body.kb_ids)
    try:
        item = get_ext_query_service().update(
            config_id,
            name=body.name,
            kb_ids=body.kb_ids,
            config=coerce_config(body.config.model_dump()) if body.config else None,
            expires_at=body.expires_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not item:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_MSG)
    await audit_service.record_action(
        user, action="ext.update", target_type="ext_query", target_id=config_id,
        target_name=(item["name"] or "")[:100],
        detail={"kb_count": len(item["kb_ids"])}, request=request)
    kb_map = await _list_kb_map(db)
    item = _attach_kb_names([item], kb_map)[0]
    if item.get("token"):
        item["token"] = _mask_token(item["token"])
    return item


@admin_router.post("/{config_id}/reset-token")
async def reset_ext_token(request: Request, config_id: str,
                          user: UserPublic = Depends(require_super_admin)):
    """重置访问 token（旧链接立即失效），返回新 token（仅本次响应明文）"""
    ext_svc = get_ext_query_service()
    ext = ext_svc.get(config_id)
    if not ext:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_MSG)
    token = ext_svc.reset_token(config_id)
    await audit_service.record_action(
        user, action="ext.reset-token", target_type="ext_query",
        target_id=config_id, target_name=(ext["name"] or "")[:100],
        request=request)
    return {"token": token, "message": "访问令牌已重置，旧链接已失效"}


@admin_router.post("/{config_id}/renew")
async def renew_ext_query(request: Request, config_id: str,
                          body: ExtQueryRenewRequest,
                          user: UserPublic = Depends(require_super_admin)):
    """续期：从 max(现在, 原到期时间) 顺延 days 天 → 完整配置

    未过期时从原到期时间顺延（不损失剩余天数）；已过期则从当前时间重新起算。
    续期只改到期时间，token 不变（链接继续有效）。
    """
    ext_svc = get_ext_query_service()
    try:
        item = ext_svc.renew(config_id, body.days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not item:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_MSG)
    await audit_service.record_action(
        user, action="ext.renew", target_type="ext_query", target_id=config_id,
        target_name=(item["name"] or "")[:100],
        detail={"days": body.days, "expires_at": item.get("expires_at")},
        request=request)
    if item.get("token"):
        item["token"] = _mask_token(item["token"])
    return item


@admin_router.post("/{config_id}/toggle")
async def toggle_ext_query(request: Request, config_id: str,
                           user: UserPublic = Depends(require_super_admin)):
    """启用/停用切换（停用后该链接所有请求立即 401）"""
    ext_svc = get_ext_query_service()
    item = ext_svc.toggle(config_id)
    if not item:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_MSG)
    await audit_service.record_action(
        user, action="ext.toggle", target_type="ext_query", target_id=config_id,
        target_name=(item["name"] or "")[:100],
        detail={"enabled": item["enabled"]}, request=request)
    if item.get("token"):
        item["token"] = _mask_token(item["token"])
    return item


@admin_router.delete("/{config_id}")
async def delete_ext_query(request: Request, config_id: str,
                           user: UserPublic = Depends(require_super_admin)):
    """删除（链接立即失效）"""
    ext_svc = get_ext_query_service()
    ext = ext_svc.get(config_id)
    if not ext:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_MSG)
    ext_svc.delete(config_id)
    await audit_service.record_action(
        user, action="ext.delete", target_type="ext_query", target_id=config_id,
        target_name=(ext["name"] or "")[:100], request=request)
    return {"message": "外部查询已删除"}


# ==================== 外部查询 API（公开，token 鉴权） ====================

ext_router = APIRouter(prefix="/api/ext", tags=["外部查询"])

# 无命中告知文案（与内部聊天一致）
_NO_HIT_TIP = ("未检索到相关内容，我无法回答该问题。"
               "请尝试换一种问法，或先与管理员确认知识库内容。")


@ext_router.get("/{config_id}/info")
async def ext_info(config_id: str, token: str = Query(default=""),
                   db: AsyncSession = Depends(get_db)):
    """页面挂载校验：{name, kb_names}；无效链接统一 401「链接无效或已失效」"""
    ext = _auth_ext(config_id, token)
    kb_map = await _list_kb_map(db)
    return {"id": ext["id"], "name": ext["name"],
            "kb_names": [kb_map[k] for k in ext["kb_ids"] if k in kb_map]}


@ext_router.get("/{config_id}/images/{doc_id}/{name}")
async def ext_image(config_id: str, doc_id: str, name: str,
                    token: str = Query(default="")):
    """外部图片代理：读取本配置暴露知识库内的解析图片（外部用户无 JWT）

    - 鉴权：?token=（与 /info 一致，`<img>` 无法带 header）；
      配置不存在 / 错 token / 已停用 → 统一 401
    - 防探测：未开启图片 / name 非法 / 文档不存在 / 文档已删除 /
      **文档不属于本配置暴露的知识库** → 一律 404「图片不存在」，
      不区分「不存在」与「无权限」（越权读取其他库图片在此拦截）
    - 不校验部门权限：暴露范围由 ext 配置的 kb_ids 定义（超管视角可选任意部门库）
    """
    ext = _auth_ext(config_id, token)
    if not ext["config"].get("enable_images", True):
        raise HTTPException(status_code=404, detail="图片不存在")
    # name 白名单（同 files.py，防路径穿越；与"图片不存在"同款 404 伪装）
    if (not name or ".." in name or not _IMAGE_NAME_RE.fullmatch(name)):
        raise HTTPException(status_code=404, detail="图片不存在")
    doc = get_document_service().get(doc_id)
    if not doc or doc.deleted or doc.kb_id not in ext["kb_ids"]:
        raise HTTPException(status_code=404, detail="图片不存在")

    key = f"images/{doc_id}/{name}"
    # 临时文件（后缀保留便于识别；下载后由 BackgroundTask 清理）
    fd, tmp_path = tempfile.mkstemp(suffix=Path(name).suffix or ".img")
    os.close(fd)
    try:
        await get_storage_service().download_to(key, tmp_path)
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        log.system_error("外部查询图片读取失败 %s: %s", key, str(e)[:150])
        raise HTTPException(status_code=404, detail="图片不存在")

    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

    def _cleanup():
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass

    return FileResponse(tmp_path, media_type=content_type,
                        background=BackgroundTask(_cleanup))


@ext_router.post("/{config_id}/chat")
async def ext_chat(config_id: str, body: ExtQueryChatRequest,
                   request: Request,
                   db: AsyncSession = Depends(get_db)):
    """外部流式问答（SSE：meta → delta → done / error）

    - 鉴权：Authorization: Bearer {token}（配置不存在/错 token/停用 → 401）
    - 限流：每 config 每分钟 RATE_LIMIT_PER_MIN 次（超限 → 429）
    - 流程复用现有能力：多库检索（每库 top_k）→ sources 合并 → system 组装
      （config.system_prompt 优先，{knowledge}/{refs} 占位符支持）→ LLM 流式
      （全局活跃 LLM 配置，temperature/top_p/max_tokens 由 config 覆盖）→
      无命中直接告知；查询审计日志落盘；多轮上下文（内存，仅同 session_id 内）
    """
    ext = _auth_ext(config_id, _bearer_token(request) or "")
    ext_svc = get_ext_query_service()
    if not ext_svc.check_rate_limit(config_id):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    question = (body.query or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="query 不能为空")

    conf = ext["config"]
    cfg = get_active_config()
    top_k = int(conf.get("top_k") or cfg.retrieval.top_k)
    # 阈值同样跟随全局：此前只判 None 就直接不过滤，与表单上"跟随全局"的
    # 文案并不相符（写的是跟随全局，实际是关掉阈值）
    min_score = (conf.get("similarity_threshold")
                 if conf.get("similarity_threshold") is not None
                 else cfg.retrieval.similarity_threshold)
    # kb_name 映射在请求生命周期内查好，闭包给事件生成器使用（StreamingResponse
    # 生成器执行时 db session 已不可用）
    kb_map = await _list_kb_map(db)

    async def event_generator():
        try:
            # 1) 多库检索：每库 top_k 候选 → 合并按 score 降序取全局 top_k
            merged: List = []
            try:
                for kid in ext["kb_ids"]:
                    merged.extend(await get_retrieval_service().retrieve(
                        kid, question, top_k=top_k, min_score=min_score))
            except RetrievalUnavailableError as e:
                log.system_error("外部查询检索服务不可用: %s", e)
                yield sse_event("error", {"message": str(e)})
                return
            except Exception as e:
                log.system_error("外部查询检索失败: %s", e, exc_info=True)
                yield sse_event("error", {"message": f"检索失败: {e}"})
                return
            merged.sort(key=lambda s: s.score, reverse=True)
            sources = merged[:top_k]
            for s in sources:
                if s.kb_id in kb_map:
                    s.kb_name = kb_map[s.kb_id]["name"]
            # 图片适配：内部图片链接 → 本配置专用端点（关闭则剥除）；
            # meta 下发的文本与注入 LLM 的知识文本保持同一份
            _adapt_sources_images(sources, config_id, ext["token"], conf)
            yield sse_event("meta", {
                "sources": [s.model_dump(mode="json") for s in sources],
            })

            # 2) 无命中：直接告知，不调用 LLM（日志 hit_count=0）
            if not sources:
                await ext_svc.log_query(config_id, ext.get("name", ""), question,
                                        0, request=request, source="chat")
                yield sse_event("delta", {"text": _NO_HIT_TIP})
                yield sse_event("done", {"session_id": body.session_id or "",
                                         "message_count": 0})
                return

            # 3) 组装 prompt（复用 chat_service 的 system 组装：config 的
            #    system_prompt 优先（支持 {knowledge}/{refs} 占位符），
            #    空/缺省 → 内置默认模板）
            refs = ChatService._build_refs(sources)
            knowledge = ChatService._build_knowledge(sources)
            system_content = ChatService._build_system_content(
                resolve_system_prompt(conf, cfg.chat.system_prompt),
                refs, knowledge)
            # 图片引导追加在组装结果之后：内置默认模板与自定义模板两条路径
            # 都能覆盖，且不改动 chat_service 的共用组装逻辑（内部聊天零影响）
            if conf.get("enable_images", True):
                system_content += _IMAGE_OUTPUT_RULE
            messages: List[dict] = [{"role": "system", "content": system_content}]
            if conf.get("enable_multi_turn", False):
                # 历史轮数不再单独配置（外部场景没人会去调这个数）：固定跟随
                # 全局聊天设置；且 Agent 接入（/query）本就无会话概念，只有
                # 网页版 /chat 走得到这里
                history = ext_svc.get_context(config_id, body.session_id or "",
                                              cfg.chat.history_rounds)
                messages.extend(history)
            messages.append({"role": "user", "content": question})

            # 4) LLM 流式（模型由 config.llm_model 指定，空=全局激活模型；
            #    生成参数 config 非 None 覆盖）
            llm_cfg = resolve_llm_config(conf)
            client = get_chat_service()._get_client(llm_cfg)
            request_kwargs: dict = {
                "model": llm_cfg["model"],
                "messages": messages,
                "temperature": (conf.get("temperature")
                                if conf.get("temperature") is not None
                                else llm_cfg["temperature"]),
                "max_tokens": (conf.get("max_tokens")
                               if conf.get("max_tokens") is not None
                               else llm_cfg["max_tokens"]),
                "stream": True,
            }
            # Top P 始终下发（本配置 → 模型配置 → 0.9），避免不传时
            # 采样范围随各模型服务端默认漂移
            request_kwargs["top_p"] = resolve_top_p(conf, llm_cfg)
            answer_parts: List[str] = []
            try:
                stream = await client.chat.completions.create(**request_kwargs)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = delta.content if delta else None
                    if content:
                        answer_parts.append(content)
                        yield sse_event("delta", {"text": content})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.system_error("外部查询 LLM 流式调用失败: %s", e, exc_info=True)
                err_msg = f"LLM 调用失败: {e}"
                ext_svc.append_context(config_id, body.session_id or "",
                                       question, err_msg)
                await ext_svc.log_query(config_id, ext.get("name", ""), question,
                                        len(sources), request=request,
                                        source="chat")
                yield sse_event("error", {"message": err_msg})
                return

            # 5) done + 上下文追加 + 记录落库
            ext_svc.append_context(config_id, body.session_id or "",
                                   question, "".join(answer_parts))
            await ext_svc.log_query(config_id, ext.get("name", ""), question,
                                    len(sources), request=request,
                                    source="chat")
            yield sse_event("done", {"session_id": body.session_id or "",
                                     "message_count": len(answer_parts)})
        except Exception as e:
            log.system_error("外部查询 SSE 流异常: %s", e, exc_info=True)
            yield sse_event("error", {"message": f"服务异常: {e}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ==================== 外部同步查询（MCP 接入，非流式） ====================

# 回答/引用文本长度上限（对齐外部接入 6000 字规范；单条引用 2000 字）
ANSWER_MAX_LEN = 6000
SOURCE_MAX_LEN = 2000
# 请求体 top_k 覆盖范围（与 config top_k 同范围）
TOP_K_MAX = 20


def _truncate(text: str, max_len: int) -> str:
    """文本截断：超过 max_len 直接截断（保证长度严格 ≤max_len）"""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len]


def _extract_image_urls(text: str) -> List[str]:
    """从引用文本提取图片链接（外部图片端点，去重保序；无则空数组）

    文本在检索后已经过 _adapt_images 改写：开启图片时链接为本配置的
    /api/ext/{config_id}/images/{doc_id}/{name}?token=xxx（Agent 拼上
    KB_EXT_URL 即可直接加载）；关闭图片时图片语法已被剥除，此处自然为空。
    """
    seen = set()
    urls = []
    for m in _EXT_IMAGE_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            urls.append(m)
    return urls


# 模型"简化"过的图片链接：/api/ext/{doc_id}/{name}
# （negative lookahead 排除标准形式，避免误伤已正确的链接）
_SIMPLIFIED_EXT_IMAGE_RE = re.compile(
    r"/api/ext/(?![^\s/?#]+/images/)([^\s/?#]+)/([^\s/?#]+)")


def _fix_answer_images(answer: str, config_id: str) -> str:
    """兜底还原模型简化的图片链接

    实测：提示词已明确要求"链接必须原样保留"，模型仍会把
    /api/ext/{config_id}/images/{doc_id}/{name} 压缩成 /api/ext/{doc_id}/{name}
    （丢掉 config_id 与 images 段），直接下发在浏览器里必然 404。此处按标准
    形式还原；已是标准形式的链接原样放行。
    """
    if not answer or "/api/ext/" not in answer:
        return answer
    return _SIMPLIFIED_EXT_IMAGE_RE.sub(
        lambda m: f"/api/ext/{config_id}/images/{m.group(1)}/{m.group(2)}",
        answer)


@ext_router.post("/{config_id}/query")
async def ext_query_sync(config_id: str, body: ExtQuerySyncRequest,
                         request: Request,
                         db: AsyncSession = Depends(get_db)):
    """外部同步问答（MCP/脚本接入：一次请求返回完整回答与引用，非 SSE）

    - 鉴权：Authorization: Bearer {token}（配置不存在/错 token/停用 → 401，
      与 /chat 完全一致）
    - 限流：与 /chat 共用同一 config 限流桶（每 config 每分钟
      RATE_LIMIT_PER_MIN 次，超限 → 429）
    - 流程复用现有能力：多库检索（每库 top_k 候选 → 合并按 score 降序取
      全局 top_k）→ system 组装（config.system_prompt 优先，
      {knowledge}/{refs} 占位符支持）→ **非流式** LLM（stream=False 一次
      返回；temperature/top_p/max_tokens 由 config 覆盖）→ 无命中直接
      固定文案不调 LLM；每次查询落审计日志（同一 ext_query_logs.jsonl，
      仅 query 摘要与命中数）
    - 响应：{answer(≤6000 截断), sources: [{document_name, text(≤2000/条),
      image_urls}]}——image_urls 从引用文本提取 /api/files/images/ 链接
      （无则空数组），供 Agent 端展示知识库图片
    - 错误：检索失败 → 400、LLM 失败 → 502（中文提示，与外部接口风格一致）
    """
    ext = _auth_ext(config_id, _bearer_token(request) or "")
    ext_svc = get_ext_query_service()
    if not ext_svc.check_rate_limit(config_id):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    question = (body.query or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="query 不能为空")

    conf = ext["config"]
    cfg = get_active_config()
    if body.top_k is not None:
        if not 1 <= body.top_k <= TOP_K_MAX:
            raise HTTPException(status_code=422, detail=f"top_k 需为 1~{TOP_K_MAX}")
        top_k = body.top_k
    else:
        top_k = int(conf.get("top_k") or cfg.retrieval.top_k)
    # 阈值跟随全局（同 /chat，见其注释）
    min_score = (conf.get("similarity_threshold")
                 if conf.get("similarity_threshold") is not None
                 else cfg.retrieval.similarity_threshold)

    # 1) 多库检索：每库 top_k 候选 → 合并按 score 降序取全局 top_k
    merged: List = []
    try:
        for kid in ext["kb_ids"]:
            merged.extend(await get_retrieval_service().retrieve(
                kid, question, top_k=top_k, min_score=min_score))
    except RetrievalUnavailableError as e:
        log.system_error("外部同步查询检索服务不可用: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.system_error("外部同步查询检索失败: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=f"检索失败: {e}")
    merged.sort(key=lambda s: s.score, reverse=True)
    sources = merged[:top_k]
    # 图片适配：内部图片链接 → 本配置专用端点（关闭则剥除）。Agent 侧同样
    # 没有系统账号，返回内部链接的话 image_urls 拿过去必然 401
    _adapt_sources_images(sources, config_id, ext["token"], conf)

    # 2) 无命中：固定文案直接返回，不调用 LLM（日志 hit_count=0）
    if not sources:
        await ext_svc.log_query(config_id, ext.get("name", ""), question, 0,
                                request=request, source="query")
        return {"answer": _NO_HIT_TIP, "sources": []}

    # 3) 组装 prompt（复用 chat_service 的 system 组装：config.system_prompt
    #    优先（支持 {knowledge}/{refs} 占位符），空/缺省 → 内置默认模板；
    #    同步查询无会话概念，不带多轮历史）
    refs = ChatService._build_refs(sources)
    knowledge = ChatService._build_knowledge(sources)
    system_content = ChatService._build_system_content(
        resolve_system_prompt(conf, cfg.chat.system_prompt), refs, knowledge)
    if conf.get("enable_images", True):
        system_content += _IMAGE_OUTPUT_RULE
    messages: List[dict] = [{"role": "system", "content": system_content},
                            {"role": "user", "content": question}]

    # 4) 非流式 LLM（stream=False 一次返回完整回答；模型由 config.llm_model
    #    指定、空=全局激活模型；生成参数 config 非 None 覆盖）
    llm_cfg = resolve_llm_config(conf)
    client = get_chat_service()._get_client(llm_cfg)
    request_kwargs: dict = {
        "model": llm_cfg["model"],
        "messages": messages,
        "temperature": (conf.get("temperature")
                        if conf.get("temperature") is not None
                        else llm_cfg["temperature"]),
        "max_tokens": (conf.get("max_tokens")
                       if conf.get("max_tokens") is not None
                       else llm_cfg["max_tokens"]),
        "stream": False,
    }
    # Top P 始终下发（同 /chat，见其注释）
    request_kwargs["top_p"] = resolve_top_p(conf, llm_cfg)
    try:
        resp = await client.chat.completions.create(**request_kwargs)
        answer = ""
        if getattr(resp, "choices", None):
            answer = resp.choices[0].message.content or ""
    except Exception as e:
        log.system_error("外部同步查询 LLM 调用失败: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"LLM 调用失败: {e}")

    # 5) 截断与响应组装：answer 先兜底还原模型简化的图片链接、再 ≤6000 截断；
    #    sources 每条 text ≤2000，附图片 URL 列表（无图片 → 空数组）；审计日志落盘
    answer = _truncate(_fix_answer_images(answer, config_id), ANSWER_MAX_LEN)
    out_sources = []
    for s in sources:
        text = s.parent_text or s.text
        out_sources.append({
            "document_name": s.document_name,
            "text": _truncate(text, SOURCE_MAX_LEN),
            "image_urls": _extract_image_urls(text),
        })
    await ext_svc.log_query(config_id, ext.get("name", ""), question,
                            len(sources), request=request, source="query")
    return {"answer": answer, "sources": out_sources}
