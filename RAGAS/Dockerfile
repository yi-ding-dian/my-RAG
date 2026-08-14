# ============================================================
# Dockerfile — RAGAS 评估系统
# 多阶段构建：阶段1 构建前端，阶段2 构建 Python 运行环境
# ============================================================

# ---------- 阶段1: 构建前端 ----------
FROM node:18 AS node-builder

WORKDIR /app

# 先复制依赖描述文件，利用 Docker 缓存层
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci

# 复制前端源码并构建
COPY frontend/ ./frontend/
RUN cd frontend && npm run build

# ---------- 阶段2: Python 运行环境 ----------
FROM python:3.10-slim AS python-base

# 安装编译依赖和运行时工具
#   git: RAGAS 部分指标需要从 git 仓库拉取资源
#   gcc / python3-dev: 编译 numpy、scipy 等 C 扩展
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制 requirements.txt，利用缓存安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码（排除由 .dockerignore 控制的目录）
COPY . .

# 从构建阶段复制前端产物
COPY --from=node-builder /app/frontend/dist ./frontend/dist

# 创建数据持久化目录（运行时通过 volume 挂载）
RUN mkdir -p data/datasets data/uploads reports prompts

EXPOSE 8090

# 使用 uvicorn 启动 FastAPI 服务
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8090"]
