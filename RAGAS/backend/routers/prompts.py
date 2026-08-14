"""提示词管理 API"""
from __future__ import annotations
import logging
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.prompt_service import get_prompt_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/prompts", tags=["提示词"])


# ── 请求 / 响应模型 ────────────────────────────────────────

class PromptItem(BaseModel):
    name: str
    zh: str


class UpdatePromptsRequest(BaseModel):
    prompts: List[PromptItem]


class LanguageRequest(BaseModel):
    language: str


# ── 端点 ────────────────────────────────────────────────────

@router.get("")
def list_prompts():
    """获取所有指标的提示词概览"""
    return get_prompt_service().list_metrics()


@router.get("/llm-status")
def llm_status():
    """检查 LLM 是否已配置并可连接"""
    from backend.services.settings_service import get_settings_service
    from openai import OpenAI

    svc = get_settings_service()
    active = svc.get_active()
    if not active or not active.get("llm_base_url"):
        return {"available": False}

    try:
        client = OpenAI(
            api_key=active.get("llm_api_key", "not-needed"),
            base_url=active["llm_base_url"],
        )
        client.models.list()
        return {"available": True}
    except Exception:
        return {"available": False}


# 注意: /active-language 必须在 /{metric} 前定义，避免路由冲突
@router.get("/active-language")
def get_active_language():
    """获取当前评估语言"""
    svc = get_prompt_service()
    return {"language": svc.get_active_language()}


@router.put("/active-language")
def set_active_language(body: LanguageRequest):
    """设置评估语言"""
    svc = get_prompt_service()
    try:
        svc.set_active_language(body.language)
        return {"language": svc.get_active_language()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{metric}")
def get_metric_prompts(metric: str):
    """获取单个指标的完整提示词（中英文）"""
    result = get_prompt_service().get_metric(metric)
    if not result:
        raise HTTPException(status_code=404, detail="指标不存在")
    return result


@router.post("/{metric}/translate")
async def translate_metric(metric: str):
    """AI 翻译指标提示词为中文"""
    svc = get_prompt_service()
    if not svc.get_metric(metric):
        raise HTTPException(status_code=404, detail="指标不存在")
    try:
        return await svc.translate_metric(metric)
    except Exception as e:
        logger.exception("翻译失败: metric=%s", metric)
        raise HTTPException(status_code=500, detail="翻译失败，请检查 LLM 连接或查看后端日志")


@router.put("/{metric}")
def update_metric(metric: str, body: UpdatePromptsRequest):
    """保存手动编辑的中文提示词"""
    svc = get_prompt_service()
    if not svc.get_metric(metric):
        raise HTTPException(status_code=404, detail="指标不存在")
    try:
        return svc.update_metric(metric, [p.model_dump() for p in body.prompts])
    except Exception as e:
        logger.exception("保存失败: metric=%s", metric)
        raise HTTPException(status_code=500, detail="保存失败，请查看后端日志")
