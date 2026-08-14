"""模型配置档案 API"""
from __future__ import annotations
import logging
from fastapi import APIRouter, HTTPException

from backend.services.settings_service import get_settings_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["模型配置"])


@router.get("/profiles")
def list_profiles():
    """获取所有配置档案"""
    return get_settings_service().list_profiles()


@router.get("/profiles/active")
def get_active():
    """获取当前激活的配置"""
    p = get_settings_service().get_active()
    if not p:
        raise HTTPException(status_code=404, detail="没有激活的配置")
    return p


@router.post("/profiles")
def create_profile(body: dict):
    """创建配置档案"""
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    try:
        return get_settings_service().create_profile(name, body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: str, body: dict):
    """更新配置档案"""
    result = get_settings_service().update_profile(profile_id, body)
    if not result:
        raise HTTPException(status_code=404, detail="配置不存在")
    return result


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str):
    """删除配置档案"""
    ok = get_settings_service().delete_profile(profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="配置不存在")
    return {"message": "已删除"}


@router.post("/profiles/{profile_id}/activate")
def activate_profile(profile_id: str):
    """激活配置档案"""
    result = get_settings_service().activate(profile_id)
    if not result:
        raise HTTPException(status_code=404, detail="配置不存在")
    return {"message": f"已切换到: {result.get('name')}", "profile": result}


@router.post("/test/llm")
def test_llm(body: dict):
    """测试 LLM 连接"""
    svc = get_settings_service()
    try:
        msg = svc.test_llm(
            base_url=body.get("llm_base_url", ""),
            api_key=body.get("llm_api_key", ""),
            model=body.get("llm_model", ""),
        )
        return {"status": "ok", "message": msg}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/test/embedding")
def test_embedding(body: dict):
    """测试 Embedding 连接"""
    svc = get_settings_service()
    try:
        msg = svc.test_embedding(
            base_url=body.get("embedding_base_url", ""),
            api_key=body.get("embedding_api_key", ""),
            model=body.get("embedding_model", ""),
        )
        return {"status": "ok", "message": msg}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/test/es")
def test_es(body: dict):
    """测试 Elasticsearch 连接"""
    svc = get_settings_service()
    try:
        msg = svc.test_es(
            host=body.get("es_host", ""),
            port=body.get("es_port", 9200),
            user=body.get("es_user", ""),
            password=body.get("es_password", ""),
        )
        return {"status": "ok", "message": msg}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
