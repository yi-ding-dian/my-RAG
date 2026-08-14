#!/bin/bash
# my-RAG 知识库系统 — 源码部署前端编译脚本
# 用法: ./deploy/build.sh
# 效果: npm run build 编译前端到 frontend/dist（后端启动时可直接托管该产物）
# 注意: 需先安装前端依赖（./deploy/install.sh），前端 dev 模式（热更新）无需编译
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_DIR/frontend"

echo "============================================"
echo "  my-RAG 前端编译"
echo "============================================"

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    echo "[ERR] 前端依赖未安装，请先执行: ./deploy/install.sh"
    exit 1
fi

echo "[..] 编译前端 (输出: frontend/dist) ..."
pushd "$FRONTEND_DIR" > /dev/null
npm run build || { echo "[ERR] 编译失败"; popd > /dev/null; exit 1; }
popd > /dev/null
echo "[OK] 编译完成: $FRONTEND_DIR/dist"
