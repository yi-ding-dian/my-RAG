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

## CORS 跨域白名单

- 后端 CORS 白名单由项目根 `.env` 的 `CORS_ORIGINS` 控制（逗号分隔的具体
  前端来源；缺省 `*` 仅限开发调试）。`.env` 已配置 dev 前端来源
  `http://127.0.0.1:3002,http://localhost:3002`。
- **前端域名变化时必须同步更新 `CORS_ORIGINS`**（如部署到
  `https://kb.example.com` 时改为该来源），否则前端跨域请求会被浏览器拒绝
  （后端接口不可用）。
- CORS 白名单在**后端启动时读取**，修改 `.env` 后需重启后端生效。

## 安全合规清单（客户现场部署必读）

1. **HTTPS**：默认 nginx 走 HTTP（`docker/nginx.conf`）。合规要求时按文件内
   「可选：启用 HTTPS」注释启用 443（证书挂载路径/步骤已写清）；建议同时把
   `.env` 的 `CORS_ORIGINS` 改为 `https://` 来源。
2. **敏感文件权限**：`data/settings.json`（含全部 api_key）与 `.env` 会自动/建议
   收紧为 `600`（本机当前用户可读）。若历史文件为 644，执行
   `chmod 600 data/settings.json .env`。
3. **数据盘与备份加密**：生产环境建议数据盘全盘加密；备份文件加密存储，
   审计/会话导出含业务内容，按敏感数据处置。
4. **密钥管理**：`JWT_SECRET` 必须 ≥16 位强随机；模型 api_key 只在创建/重置时
   明文回传一次，列表仅打码展示；外部查询 token 泄露可随时重置/停用。
5. **发布前自检**：`bash scripts/security_check.sh`（JWT 强度/settings 权限/
   CORS/HTTPS 状态四查一提示，失败项阻断退出码 1）。
