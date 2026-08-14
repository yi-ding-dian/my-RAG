#!/bin/bash
# RAGAS 评估系统 — 一键启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================"
echo "  RAGAS 评估系统 启动"
echo "============================================"

# 1. 检测虚拟环境
if [ -n "$VIRTUAL_ENV" ] || [ -n "$CONDA_PREFIX" ]; then
    echo "[OK] 虚拟环境: ${VIRTUAL_ENV:-$CONDA_PREFIX}"
else
    echo "[!] 未检测到虚拟环境，尝试使用当前 python"
fi

# 2. 安装依赖
if [ ! -f ".deps_installed" ]; then
    echo "[..] 安装 Python 依赖..."
    pip install -r requirements.txt -q 2>&1 | tail -1
    touch .deps_installed
    echo "[OK] 依赖安装完成"
fi

# 3. 构建前端（如果 dist 不存在）
FRONTEND_DIR="$SCRIPT_DIR/frontend"
if [ ! -d "$FRONTEND_DIR/dist" ]; then
    echo "[..] 构建前端..."
    cd "$FRONTEND_DIR"
    npm install --silent 2>&1 | tail -1
    npx vite build 2>&1 | tail -3
    cd "$SCRIPT_DIR"
    echo "[OK] 前端构建完成"
fi

# 4. 启动后端
echo "[..] 启动后端 (port 8090)..."
PYTHONPATH="$SCRIPT_DIR" nohup python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 --port 8090 \
    > /tmp/ragas_server.log 2>&1 &
SERVER_PID=$!
echo "[OK] 后端已启动 (PID: $SERVER_PID)"

# 等待就绪
sleep 3
if curl -sf http://localhost:8090/api/health > /dev/null 2>&1; then
    echo "[OK] 服务已就绪: http://localhost:8090"
else
    echo "[!] 服务启动中，请稍后检查: tail -f /tmp/ragas_server.log"
fi

echo ""
echo "访问地址: http://localhost:8090"
echo "日志文件: /tmp/ragas_server.log"
echo "停止服务: kill $SERVER_PID"
