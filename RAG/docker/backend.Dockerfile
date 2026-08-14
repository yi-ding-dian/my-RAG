# ============================================================
# docker/backend.Dockerfile — my-RAG 后端（FastAPI + Chroma 向量库）
# 构建方式: docker compose -f docker/docker-compose.yml build rag-backend
# 注意: compose 中以项目根目录为 build context（requirements.txt 在根目录），
#       本文件位于 docker/ 下，通过 dockerfile: docker/backend.Dockerfile 指定。
# ============================================================

FROM python:3.12-slim

# Python 运行优化：不写 __pycache__、输出不缓冲（容器日志实时可见）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# curl: 供 docker compose healthcheck 使用
# 说明: python:3.12-slim（Debian bookworm）自带新版 sqlite3（>=3.35），chromadb 可直接使用；
#       requirements.txt 中 pysqlite3-binary 是本机旧 sqlite 的兼容 hack，
#       vector_store.py 已用 try/except 包裹，此处保留无副作用。
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖描述文件，利用 Docker 层缓存（仅依赖变更时才重新 pip install）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制后端代码（根目录 .dockerignore 已排除 data/.venv/前端产物等）
COPY backend/ backend/

# 非 root 运行：
#   - 创建 rag 用户，uid=1000 与宿主机普通用户对齐（宿主 data/ 目录属主 uid=1000，
#     bind 挂载 ./data:/app/data 后容器内 rag 用户可直接读写，无需再 chown）
#   - 数据目录 /app/data 提前创建并授权（docker run 单独跑后端时也保证可写；
#     compose 挂载卷后镜像内属主被宿主目录覆盖，宿主侧属主需同为 uid=1000）
RUN useradd --create-home --uid 1000 --user-group rag \
    && mkdir -p /app/data \
    && chown -R rag:rag /app/data

USER rag

EXPOSE 8091

# 启动后端（config.py 的 PORT 字段可由 .env.docker 环境变量覆盖，此处 8091 为默认值）
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8091"]
