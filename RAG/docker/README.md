# Docker 部署（生产）

本目录为 my-RAG 生产部署编排（源码部署见 `../deploy/`）。

## 快速开始

```bash
# 1. 编辑 .env.docker（外部服务地址 / MySQL / MinIO / JWT_SECRET）
vi docker/.env.docker

# 2. 构建并后台启动（前端 80 + 后端 8091）
docker compose -f docker/docker-compose.yml up -d --build

# 3. 常用运维
docker compose -f docker/docker-compose.yml logs -f          # 日志
docker compose -f docker/docker-compose.yml down             # 停止（../data 数据保留）
docker compose -f docker/docker-compose.yml build rag-backend  # 单独重建后端
```

- 前端: http://localhost（nginx，SPA + /api 反代 8091）
- 后端: http://localhost:8091/api/health

## 配置说明

### 环境变量（docker/.env.docker）

容器环境变量只作出厂默认值，字段名与 `backend/config.py` 的 Settings 完全一致，
覆盖范围：LLM / Embedding / MinerU / 检索 top_k / 切块 / 会话轮数 / MySQL /
MinIO / STORAGE_BACKEND / DATA_DIR / JWT_SECRET / CORS_ORIGINS。

**运行时配置请优先在网页「系统配置」页修改配置档案**（持久化到
`data/settings.json`，切换即时生效，无需重启）。以下字段仅容器级注入，
不在配置档案 UI 中：

| 字段 | 说明 |
|---|---|
| `JWT_SECRET` | JWT 签名密钥（安全材料，**生产必须改为强随机值**：`openssl rand -hex 32`） |
| `DATA_DIR` | 数据目录（默认 `/app/data`，与 compose 挂载一致） |
| `STORAGE_BACKEND` | `minio` / `local`（local 存本地，离线环境用） |

### Rerank 重排序（通过配置档案设置，无需 env）

Rerank 无独立环境变量，在「系统配置」页检索(retrieval)分组下的 `rerank` 段配置：

```json
"rerank": {
  "enabled": true,
  "base_url": "http://127.0.0.1:xxxx",   // OpenAI 兼容 /rerank 服务
  "model": "bge-reranker-v2-m3",
  "top_n": 10                                // 参与重排的候选条数
}
```

- 启用条件：`enabled=true` 且 `base_url`/`model` 均非空，缺任一即跳过重排（严格降级）
- 需要**本地部署 rerank 模型服务**（如 vLLM 的 rerank 接口），无内置模型
- 检索参数 `enable_hybrid`（BM25+向量混合）与 `similarity_threshold`（相似度阈值）
  同样在配置档案 retrieval 段设置，无需 env

## 数据持久化

- `../data/` bind 挂载（uploads / parsed / kbs / documents / chat / chroma /
  storage / settings.json），`down` 不删数据
- 容器内 rag 用户 uid=1000，宿主机 data/ 属主需同为 1000：
  `sudo chown -R 1000:1000 ./data`
- `host.docker.internal` 已配置 host-gateway 解析，仅监听宿主机 127.0.0.1 的
  外部服务（如 RAGAS 8090）用它访问

## 已知限制

- **MySQL IP 白名单**：MySQL 用户若配置了来源 IP 白名单，需放行 docker 网关网段
  （`docker network inspect` 查看 compose 网络网关），否则后端启动时建库失败
- MinerU 解析图片已通（`/api/files/images/{doc_id}/{name}` 鉴权代理）；图片提取
  需 MinerU 侧开启 return_images
- Rerank 需要本地模型服务（见上节），未配置时检索自动跳过重排
