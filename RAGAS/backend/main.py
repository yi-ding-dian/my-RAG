"""FastAPI 后端入口"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from backend.config import settings
from backend.routers import data, evaluation, results
from backend.routers.settings import router as settings_router
from backend.routers.prompts import router as prompts_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIST = settings.BASE_DIR / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 50)
    logger.info("RAGAS 评估系统 启动")
    logger.info(f"LLM: {settings.LLM_MODEL} @ {settings.LLM_BASE_URL}")
    logger.info(f"ES: {settings.ES_HOST}:{settings.ES_PORT}")
    logger.info(f"数据集目录: {settings.DATASETS_DIR}")
    logger.info("=" * 50)
    yield
    logger.info("服务关闭")


app = FastAPI(
    title="RAGAS 评估系统",
    description="基于 RAGAS 框架的 RAG 知识库评估系统",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — 生产环境建议在 .env 中限制具体来源
CORS_ORIGINS = getattr(settings, "CORS_ORIGINS", "").strip() or "*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS.split(",") if CORS_ORIGINS != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(data.router)
app.include_router(evaluation.router)
app.include_router(results.router)
app.include_router(settings_router)
app.include_router(prompts_router)


@app.get("/api/health")
def health():
    """健康检查"""
    return {"status": "ok", "model": settings.LLM_MODEL}


# 生产环境: 服务前端静态文件
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        """兜底路由: 所有非 API 请求返回前端 index.html"""
        if full_path.startswith("api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index_path = FRONTEND_DIST / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return JSONResponse({"detail": "Not Found"}, status_code=404)
