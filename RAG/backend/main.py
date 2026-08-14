"""my-RAG 知识库后端入口

启动: uvicorn backend.main:app --host 0.0.0.0 --port 8091
（端口取 config.settings.PORT，.env 可覆盖）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from backend.config import (BASE_DIR, CHAT_DIR, CHROMA_DIR, DATA_DIR,
                            DOCUMENTS_DIR, KBS_DIR, PARSED_DIR, UPLOAD_DIR,
                            get_active_config, settings as config_settings)
from backend.db import init_db
# 注意: routers.settings 模块名与 config.settings 同名，必须用别名避免遮蔽
from backend.routers import (admin_documents, audit, auth, chat, departments,
                             documents, ext_query, files, knowledge_bases,
                             settings, stats, users)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_active_config()
    logger.info("=" * 50)
    logger.info("my-RAG 知识库系统 启动")
    # JWT 密钥安全检查（P0）：默认值/空/过短（<16 字符）→ 拒绝启动
    # （弱密钥可被伪造任意用户令牌，默认密钥不拒启等于形同虚设）
    _jwt = (config_settings.JWT_SECRET or "").strip()
    if not _jwt or len(_jwt) < 16 or _jwt == "my-rag-dev-secret-change-me":
        raise RuntimeError(
            "JWT_SECRET 未配置或过弱，请在 .env 设置 JWT_SECRET（至少 16 字符）")
    logger.info(f"LLM: {cfg.llm.model} @ {cfg.llm.base_url}")
    logger.info(f"Embedding: {cfg.embedding.model} @ {cfg.embedding.base_url}")
    logger.info(f"MinerU: {cfg.mineru.api_url}")
    logger.info(f"数据目录: {DATA_DIR}")
    logger.info(f"  上传: {UPLOAD_DIR}")
    logger.info(f"  解析: {PARSED_DIR}")
    logger.info(f"  知识库元数据: {KBS_DIR}")
    logger.info(f"  文档元数据: {DOCUMENTS_DIR}")
    logger.info(f"  会话: {CHAT_DIR}")
    logger.info(f"  向量库: {CHROMA_DIR}")
    # 数据库初始化：建库（MySQL，失败降级 warn）→ 建表 → 种子（默认部门 + admin）
    db_info = await init_db()
    logger.info(f"数据库初始化完成: 后端={db_info['backend']}, 种子={db_info['seeded'] or '已存在跳过'}")
    # 存储桶 ensure（MinIO 失败仅 warning 并给 mc 命令提示，不阻塞启动）
    from backend.services.storage_service import get_storage_service
    await get_storage_service().ensure_bucket()
    # 解析中断恢复（P0-1）：后台解析任务无持久化，进程重启后 parsing 状态文档
    # 无法重新解析且前端无限轮询；启动时统一拨回 failed（可重新解析）
    from backend.services.document_service import get_document_service
    recovered = get_document_service().recover_stuck_parsing()
    if recovered:
        logger.warning("启动恢复: %d 个解析中断文档已标记失败（可重新解析）",
                       len(recovered))
    logger.info("=" * 50)
    yield
    logger.info("my-RAG 服务关闭")


app = FastAPI(
    title="my-RAG 知识库系统",
    description="RAG 知识库：文档入库 / 向量检索 / 流式问答",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS（生产环境建议在 .env 中限制具体来源）
_cors_origins = config_settings.CORS_ORIGINS.strip() or "*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins.split(",") if _cors_origins != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(knowledge_bases.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(stats.router)
app.include_router(settings.router)
app.include_router(files.router)
app.include_router(auth.router)
app.include_router(users.router)
# 用户头像接口独立 router（登录即可，无 require_user_admin 管理依赖）
app.include_router(users.avatar_router)
app.include_router(departments.router)
app.include_router(audit.router)
# 超管全局文档管理（跨部门文档查询，仅 super_admin）
app.include_router(admin_documents.router)
# 外部查询（知识库对外开放）：管理 API 仅 super_admin，外部 API 公开 token 鉴权
app.include_router(ext_query.admin_router)
app.include_router(ext_query.ext_router)


@app.get("/api/health")
def health():
    """健康检查"""
    cfg = get_active_config()
    return {
        "status": "ok",
        "service": "my-rag-backend",
        "llm_model": cfg.llm.model,
        "embedding_model": cfg.embedding.model,
        "version": "0.1.0",
    }


# 生产环境: 服务前端静态文件（若 frontend/dist 存在）
if FRONTEND_DIST.exists():
    _assets = FRONTEND_DIST / "assets"
    if _assets.exists():
        app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        """兜底路由: 非 API 请求返回前端 index.html"""
        # P2-12: /api 与 /api/xxx 一律 404（之前 "/api" 无斜杠会落入 SPA 返回 index.html）
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index_path = FRONTEND_DIST / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return JSONResponse({"detail": "Not Found"}, status_code=404)
