# ============================================================
# docker/frontend.Dockerfile — my-RAG 前端（React + Vite）
# 多阶段构建：
#   阶段1 builder（node:22-alpine）: npm ci 精确安装 + npm run build 产出 dist
#   阶段2 runner（nginx:alpine）: 托管 dist 静态文件 + /api 反向代理到 rag-backend
# 注意: compose 中以项目根目录为 build context（本文件在 docker/ 下），
#       故 COPY 路径均以项目根为基准（frontend/ 前缀），nginx.conf 从 docker/ 复制。
# ============================================================

# ---------- 阶段1: 构建前端静态产物 ----------
FROM node:22-alpine AS builder

WORKDIR /app/frontend

# 先复制依赖描述文件，利用 Docker 层缓存（npm ci 依赖 package-lock.json 精确安装，
# 与 package.json 必须保持同步，勿手动改动 lock 后 npm install）
COPY frontend/package.json frontend/package-lock.json ./

# npm 源（构建期参数，默认官方 registry，不改变默认行为）
# 为什么做成参数：国内网络访问 registry.npmjs.org 常超时，而国内镜像
#   （如 npmmirror）可达；写死在 Dockerfile 里会让文件与仓库分叉，
#   做成参数后按需覆盖：
#     docker compose -f docker/docker-compose.yml build \
#       --build-arg NPM_REGISTRY=https://registry.npmmirror.com
# 注意：仅影响构建期依赖下载，产物 dist 与使用官方源构建完全等价。
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm ci --registry=${NPM_REGISTRY}

# 复制源码并构建（根目录 .dockerignore 已排除 frontend/node_modules 与 dist，
# 不会覆盖 npm ci 产物）
COPY frontend/ .
RUN npm run build

# ---------- 阶段2: nginx 运行环境 ----------
FROM nginx:alpine

# SPA 静态托管 + /api 反向代理（含 SSE 流式透传关键配置，见 docker/nginx.conf）
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

# 从构建阶段复制前端产物到 nginx 站点根目录
COPY --from=builder /app/frontend/dist /usr/share/nginx/html

EXPOSE 80

# nginx 官方镜像默认以前台运行（CMD 已在镜像内定义，无需覆盖）
