# my-RAG 部署说明（Docker 镜像版）

适合不想装 Python/Node、直接拉取发布镜像部署的场景。镜像由 GitHub Actions 构建，
发布在 GitHub Container Registry（GHCR）。

## 1. 准备

需要：

- Docker 20.10+ 和 Docker Compose v2（`docker compose version` 能执行）；
- 至少 4 GB 内存（RAGAS 评估建议 8 GB）；
- 外部服务：
  - **MySQL**（必填，知识库的元数据/多租户）；
  - MinIO（`STORAGE_BACKEND=minio` 时必填；离线可用 `STORAGE_BACKEND=local`）；
  - LLM / Embedding 服务（默认地址 `127.0.0.1:8000`、`127.0.0.1:8300`，可在网页「系统配置」里改）；
  - 可选：Milvus（向量库，默认用 Chroma）、MinerU / Gotenberg（文档解析）、Elasticsearch（RAGAS 检索）。

MySQL / MinIO 可以用仓库里的基础设施编排单机启动：

```bash
docker compose -f RAG/docker/docker-compose.infra.yml up -d
```

Milvus / Gotenberg / MinerU 分别见：

- `RAG/docker/Milvus/docker-compose.yml`
- `RAG/docker/Gotenberg/docker-compose.yml`
- `RAG/docker/MinerU/`

## 2. 配置

```bash
# 从模板复制一份环境变量
cp RAG/docker/.env.docker.example .env

# 必填项（详见模板注释）
#   JWT_SECRET=至少16位，建议 openssl rand -hex 32
#   MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE
#   STORAGE_BACKEND=minio 或 local
# 可选：镜像版本
#   MY_RAG_VERSION=v0.1.0
```

`.env` 同时用于 compose 的变量替换和容器环境变量注入。

数据目录权限（后端容器以 uid 1000 运行）：

```bash
mkdir -p data
sudo chown -R 1000:1000 data
```

## 3. 启动

```bash
# 只启动知识库（前端 + 后端）
docker compose -f docker-compose.release.yml up -d

# 需要 RAGAS 评估系统时，加上 profile
docker compose -f docker-compose.release.yml --profile ragas up -d
```

首次启动会拉取镜像：

```
ghcr.io/yi-ding-dian/my-rag-frontend:v0.1.0   约 26 MB
ghcr.io/yi-ding-dian/my-rag-backend:v0.1.0    约 306 MB
ghcr.io/yi-ding-dian/my-rag-ragas:v0.1.0      约 402 MB
```

公开仓库匿名可拉；私有仓库先登录：

```bash
docker login ghcr.io -u <GitHub 用户名> -p <PAT>
```

## 4. 访问

| 地址 | 说明 |
|---|---|
| `http://服务器IP/` | 知识库前端 |
| `http://服务器IP:8091/api/health` | 后端健康检查 |
| `http://服务器IP:8090/` | RAGAS 评估（启用 ragas profile 时） |

端口被占用时，在 `.env` 里改：

```bash
MY_RAG_HTTP_PORT=8080
MY_RAG_BACKEND_PORT=8091
MY_RAGAS_PORT=8090
```

## 5. 升级 / 回滚

```bash
# 升级：改 .env 里的 MY_RAG_VERSION，例如 v0.1.1
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml up -d

# 回滚：改回旧版本号，重复上面两条
```

生产环境请固定 `vX.Y.Z`，不要用 `latest`。

## 6. 数据目录

| 目录 | 内容 |
|---|---|
| `./data` | 上传文件、解析结果、知识库、对话、Chroma、settings.json |
| `./ragas-data/datasets` | RAGAS 数据集 |
| `./ragas-data/uploads` | RAGAS 上传文件 |
| `./ragas-reports` | RAGAS 评估报告 |

删除容器不会删除这些目录；`docker compose down` 默认保留 bind mount 数据。

## 7. 常见问题

**前端 502 / 打不开**
后端还没通过健康检查。看日志：

```bash
docker compose -f docker-compose.release.yml logs -f rag-backend
```

**启动报 MySQL 连接失败**
`.env` 里的 `MYSQL_HOST` 在容器内不能用 `127.0.0.1`：
- MySQL 在宿主机：填 `host.docker.internal`；
- MySQL 在另一台机器：填局域网 IP。

**后端写数据报权限错误**
`data/` 属主不是 uid 1000：

```bash
sudo chown -R 1000:1000 data
```

**拉取镜像 403 / denied**
私有仓库需要先 `docker login ghcr.io`；公开仓库不需要。

**RAGAS 界面打不开**
默认没有启动 RAGAS，需要加 `--profile ragas`；并在 RAG 的「系统配置 → RAGAS」里把地址填成 `http://ragas-eval:8090`（同一 compose 网络内）。

**想用本地目录存文件，不用 MinIO**
`.env` 里设置 `STORAGE_BACKEND=local`，文件会存到 `./data/storage`。

## 8. 版本对应关系

`.env` 的 `MY_RAG_VERSION` 同时作用于三个镜像，保证前后端版本一致。
镜像名规则：

```
ghcr.io/<owner>/<repo>-backend:<version>
ghcr.io/<owner>/<repo>-frontend:<version>
ghcr.io/<owner>/<repo>-ragas:<version>
```

发布历史页面可以直接复制每个镜像的 `docker pull` 命令。
