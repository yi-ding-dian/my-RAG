#!/bin/bash
# my-RAG 知识库系统 — 源码部署依赖安装脚本
# 用法: ./deploy/install.sh
# 效果: 创建/复用 .venv（Python3.12）→ 安装 Python 依赖 → 安装前端 npm 依赖（幂等）
# 提示: 系统默认 python3 可能是旧版本（如 3.7），如遇依赖安装失败，
#       可指定: PYTHON_BIN=/usr/local/bin/python3.12 ./deploy/install.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# 优先环境变量指定的 python，其次 /usr/local/bin/python3.12，再退到 python3
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    if [ -x /usr/local/bin/python3.12 ]; then
        PYTHON_BIN=/usr/local/bin/python3.12
    else
        PYTHON_BIN=$(command -v python3 || command -v python)
    fi
fi

echo "============================================"
echo "  my-RAG 依赖安装"
echo "  Python: $("$PYTHON_BIN" --version 2>&1)"
echo "============================================"

# 1. 创建/复用虚拟环境
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "[..] 创建虚拟环境 .venv ..."
    "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
    echo "[OK] 虚拟环境创建完成"
else
    echo "[OK] 复用已有 .venv"
fi

# 2. 安装 Python 依赖（幂等：已装过则跳过，--force 强制重装）
PYTHON="$PROJECT_DIR/.venv/bin/python"
if [ -f "$PROJECT_DIR/.deps_installed" ] && [ "$1" != "--force" ]; then
    echo "[OK] Python 依赖已安装（如需重装: ./deploy/install.sh --force）"
else
    echo "[..] 安装 Python 依赖..."
    "$PYTHON" -m pip install --upgrade pip -q
    "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt"
    touch "$PROJECT_DIR/.deps_installed"
    echo "[OK] Python 依赖安装完成"
fi

# 3. 安装前端依赖
if [ ! -d "$PROJECT_DIR/frontend/node_modules" ]; then
    echo "[..] 安装前端依赖 (npm install)..."
    pushd "$PROJECT_DIR/frontend" > /dev/null
    npm install || { echo "[ERR] npm 安装失败"; popd > /dev/null; exit 1; }
    popd > /dev/null
    echo "[OK] 前端依赖安装完成"
else
    echo "[OK] 前端依赖已安装"
fi

echo ""
echo "安装完成。下一步:"
echo "  ./deploy/build.sh     # 编译前端"
echo "  ./deploy/start.sh     # 启动服务"
