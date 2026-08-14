#!/bin/bash
# my-RAG 知识库系统 — 源码部署一键启动脚本（dev 模式）
# 用法: ./deploy/start.sh    （任意目录执行均可，自动定位项目根）
# 效果: 建/复用 .venv → 装依赖 → 构建前端 → 启动后端 8091 → 轮询就绪 → 启动前端 dev 3002
# 停止: ./deploy/stop.sh
set -e

# 项目根 = 本脚本所在目录的上级（deploy/ -> 项目根）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"
PID_FILE="$SCRIPT_DIR/.my-rag.pid"

cleanup() {
    local code=$?
    [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
    exit $code
}
trap cleanup EXIT

echo "============================================"
echo "  my-RAG 知识库系统 启动（源码部署）"
echo "  项目根: $PROJECT_DIR"
echo "============================================"

# 1. 虚拟环境（优先项目 .venv，其次当前环境）
PYTHON=""
if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python"
    echo "[OK] 虚拟环境: .venv ($("$PYTHON" --version 2>&1))"
elif [ -n "$VIRTUAL_ENV" ]; then
    PYTHON="$VIRTUAL_ENV/bin/python"
    echo "[OK] 虚拟环境: $VIRTUAL_ENV"
else
    PYTHON=$(command -v python3 || command -v python)
    echo "[!] 未检测到项目 .venv，使用: $PYTHON"
fi
if [ -z "$PYTHON" ]; then
    echo "[ERR] 未找到 python3"
    exit 1
fi

# 2. 安装 Python 依赖（幂等标记 .deps_installed）
if [ ! -f "$PROJECT_DIR/.deps_installed" ]; then
    echo "[..] 安装 Python 依赖..."
    if "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt" -q 2>&1; then
        touch "$PROJECT_DIR/.deps_installed"
        echo "[OK] 依赖安装完成"
    else
        echo "[ERR] 依赖安装失败，请检查网络或 requirements.txt"
        exit 1
    fi
fi

# 3. 构建前端（dist 不存在时构建；开发改动后手动 npm run build 或直接起 dev）
FRONTEND_DIR="$PROJECT_DIR/frontend"
if [ ! -d "$FRONTEND_DIR/dist" ]; then
    echo "[..] 构建前端..."
    pushd "$FRONTEND_DIR" > /dev/null
    if [ ! -d node_modules ]; then
        npm install --silent || { echo "[ERR] npm 安装失败"; exit 1; }
    fi
    npm run build || { echo "[ERR] 前端构建失败"; exit 1; }
    popd > /dev/null
    echo "[OK] 前端构建完成"
fi

# 4. 检查是否已在运行（防重复启动）
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[OK] 服务已在运行 (PID: $OLD_PID)"
        echo "访问地址: http://localhost:8091"
        echo "前端: http://localhost:3002"
        exit 0
    fi
    echo "[!] 发现残留 PID 文件，清理..."
    rm -f "$PID_FILE"
fi

# 5. 启动后端（8091）
echo "[..] 启动后端 (port 8091)..."
PYTHONPATH="$PROJECT_DIR" nohup "$PYTHON" -m uvicorn backend.main:app \
    --host 0.0.0.0 --port 8091 \
    > /tmp/my_rag_server.log 2>&1 &
BACKEND_PID=$!
echo "[OK] 后端已启动 (PID: $BACKEND_PID)"

# 6. 启动前端 dev（3002，热更新）
echo "[..] 启动前端 dev (port 3002)..."
FRONTEND_PID=""
if [ -d "$FRONTEND_DIR/node_modules" ]; then
    pushd "$FRONTEND_DIR" > /dev/null
    nohup npm run dev > /tmp/my_rag_frontend.log 2>&1 &
    FRONTEND_PID=$!
    popd > /dev/null
    echo "[OK] 前端已启动 (PID: $FRONTEND_PID)"
else
    echo "[!] 前端 node_modules 不存在，跳过 dev（仅后端托管 dist）"
fi

echo "$BACKEND_PID" > "$PID_FILE"

# 7. 轮询等待后端就绪（最长 30 秒）
echo "[..] 等待后端就绪..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8091/api/health > /dev/null 2>&1; then
        echo "[OK] 后端已就绪: http://localhost:8091"
        break
    fi
    sleep 1
done
if ! curl -sf http://localhost:8091/api/health > /dev/null 2>&1; then
    echo "[!] 后端启动超时，请检查日志: tail -f /tmp/my_rag_server.log"
fi

echo ""
echo "访问地址:"
echo "  后端 API: http://localhost:8091/api/health"
echo "  前端: http://localhost:3002  (若未启动 dev，后端直接托管 dist 于 http://localhost:8091)"
echo "日志文件: /tmp/my_rag_server.log /tmp/my_rag_frontend.log"
echo "停止服务: ./deploy/stop.sh"
