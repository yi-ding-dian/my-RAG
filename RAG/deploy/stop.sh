#!/bin/bash
# my-RAG 知识库系统 — 源码部署一键停止脚本
# 用法: ./deploy/stop.sh
# 效果: 按 PID 文件精确停止 后端(8091)+前端 dev(3002)；无 PID 文件时按端口兜底
# 注意: 不做 pkill 全机匹配（避免误杀同机器其他 uvicorn/vite，如 RAGAS 8090）
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$SCRIPT_DIR/.my-rag.pid"

echo "============================================"
echo "  my-RAG 知识库系统 停止"
echo "============================================"

# 1. 按 PID 文件精确停止（前端 dev 为 npm 链，先杀 npm 父进程再杀 vite 子进程）
if [ -f "$PID_FILE" ]; then
    read -r BACKEND_PID FRONTEND_PID < "$PID_FILE"
    if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill "$BACKEND_PID" 2>/dev/null || true
        echo "[OK] 已停止后端进程 (PID: $BACKEND_PID)"
    else
        echo "[OK] 后端进程未在运行 (PID: ${BACKEND_PID:-无})"
    fi
    if [ -n "$FRONTEND_PID" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        # npm 子进程树（node -> sh -> vite）；逐步终止，最多 2 层后兜底按端口
        kill "$FRONTEND_PID" 2>/dev/null || true
        sleep 1
        kill -0 "$FRONTEND_PID" 2>/dev/null || true
        echo "[OK] 已停止前端 dev 进程 (PID: $FRONTEND_PID)"
    else
        echo "[OK] 前端 dev 进程未在运行 (PID: ${FRONTEND_PID:-无})"
    fi
    rm -f "$PID_FILE"
else
    echo "[!] 未找到 PID 文件，走端口兜底"
fi

# 2. 兜底：仅按本项目端口精确回收（若 PID 文件缺失或子进程残留；
#    只作用于监听 8091/3002 的进程，绝不触碰同机其他服务）
for PORT in 8091 3002; do
    PIDS=$(fuser -k "$PORT"/tcp 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "[OK] 端口 $PORT 进程已回收"
    else
        echo "[OK] 端口 $PORT 无进程"
    fi
done

echo "完成"
