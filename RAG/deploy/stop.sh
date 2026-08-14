#!/bin/bash
# my-RAG 知识库系统 — 源码部署一键停止脚本
# 用法: ./deploy/stop.sh
# 效果: 停后端 uvicorn(8091) + 前端 dev(3002)，清理 PID 文件
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$SCRIPT_DIR/.my-rag.pid"

echo "============================================"
echo "  my-RAG 知识库系统 停止"
echo "============================================"

# 1. 按 PID 文件停止主进程（后端）
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null || true
        echo "[OK] 已停止后端进程 (PID: $PID)"
    fi
    rm -f "$PID_FILE"
fi

# 2. 兜底：停 uvicorn 8091（含 --reload 子进程）
pkill -f "uvicorn backend.main:app" 2>/dev/null && echo "[OK] 已清理 uvicorn 进程" || echo "[OK] 无 uvicorn 进程"

# 3. 停前端 dev（3002 vite）
pkill -f "vite" 2>/dev/null && echo "[OK] 已停止前端 dev" || echo "[OK] 无前端 dev 进程"

echo "完成"
