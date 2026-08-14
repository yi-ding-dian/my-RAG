# 源码部署（deploy/）

本项目提供两种部署方式：

- **源码部署**（本目录）：直接在宿主机运行，适合开发调试
- **Docker 部署**：见 `../docker/`，适合生产环境

## 目录说明

| 文件 | 用途 |
|---|---|
| `install.sh` | 一键安装依赖：创建 .venv（Python3.12）→ pip 装后端依赖 → npm 装前端依赖（幂等） |
| `build.sh` | 编译前端（npm run build → `frontend/dist`） |
| `start.sh` | 一键启动：依赖缺失自动安装 → 构建前端 → 启动后端 8091 → 启动前端 dev 3002 |
| `stop.sh` | 一键停止：停后端 uvicorn + 前端 dev，清理 PID 文件 |

## 快速开始

```bash
# 1. 安装依赖（首次或依赖变更后执行）
./deploy/install.sh

# 2. 启动服务
./deploy/start.sh

# 3. 停止服务
./deploy/stop.sh
```

## 单独操作

```bash
# 只编译前端（后端可直接托管 dist，无需 dev server）
./deploy/build.sh

# 强制重装 Python 依赖
./deploy/install.sh --force
```

## 依赖变更后如何更新

Python 依赖统一在根目录 `requirements.txt`（含 jieba 等检索/认证/存储全部依赖），
`install.sh` 以 `.deps_installed` 标记幂等跳过。**requirements.txt 有增删后必须
强制重装**（否则新增依赖不生效）：

```bash
./deploy/install.sh --force
```

## 注意事项

- 系统默认 `python3` 可能是旧版本（如 3.7），`install.sh` 会自动优先使用
  `/usr/local/bin/python3.12`；也可手动指定：
  `PYTHON_BIN=/usr/local/bin/python3.12 ./deploy/install.sh`
- 服务日志：`/tmp/my_rag_server.log`（后端）、`/tmp/my_rag_frontend.log`（前端）
- 数据持久化于项目根 `data/` 目录（与 Docker 部署共用同一目录规范）
- 外部服务地址（LLM/Embedding/MinerU/RAGAS）可在前端"系统配置"页修改并持久化，
  也可通过项目根 `.env` 配置默认值
