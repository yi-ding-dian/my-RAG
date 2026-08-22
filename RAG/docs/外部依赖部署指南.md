# 外部依赖获取方式

my-RAG 本体不含任何模型服务与中间件，以下 7 类外部依赖需自行部署/提供。
全部外部服务地址在**网页「系统配置」页**配置（多配置档案机制，切换即时生效，
逐项支持连接测试），`.env` / `docker/.env.docker` 仅作出厂默认值。

| # | 服务 | 默认端口 | 可降级性 |
|---|------|---------|---------|
| 1 | MySQL 8 | 5455 | **硬前置**，无它无法登录/多租户 |
| 2 | LLM（OpenAI 兼容） | 1234（示例） | 必须自备 |
| 3 | Embedding（bge-m3） | 8300 | 必须自备 |
| 4 | MinIO 对象存储 | 9000 / 9001 | 可降级 `STORAGE_BACKEND=local` |
| 5 | MinerU 文档解析 | 8001 | 可选，不可用自动降级纯文本 |
| 6 | Rerank 重排序 | 无固定 | 可选，未配置自动跳过 |
| 7 | RAGAS 评估系统 | 8090 | 可选，不可用自动降级（仅统计展示受限） |

---

## 1. MySQL 8（硬前置，端口 5455）

**作用**：多租户与团队协作的数据底座（users / departments / kbs 三表 +
审计日志）。**无它连登录都进不去**：登录鉴权即查询 users 表。
注意：后端启动时"建库失败仅打印 warning"，此时服务表面起来但登录必然失败，
务必确认建库成功（可用 `mysqladmin ping` 或登录查询验证）。

**获取方式**（推荐用同仓库编排，见 `../docker/README.md` 与
`docker/docker-compose.infra.yml`）：

```bash
docker run -d --name my-rag-mysql \
  -p 5455:3306 \
  -e MYSQL_ROOT_PASSWORD=<your-password> \
  -e MYSQL_DATABASE=my_rag \
  -e MYSQL_USER=ragflow \
  -e MYSQL_PASSWORD=<your-password> \
  -v my-rag-mysql-data:/var/lib/mysql \
  mysql:8
```

**连接配置**：`MYSQL_HOST=127.0.0.1` / `MYSQL_PORT=5455` / `MYSQL_USER=ragflow`
/ `MYSQL_PASSWORD=<your-password>` / `MYSQL_DATABASE=my_rag`。

**可降级性**：无。测试可注入 `MYSQL_URL=sqlite+aiosqlite://` 离线跑测试，
但生产必须 MySQL。

## 2. LLM 对话模型（OpenAI 兼容，必须自备）

**作用**：流式问答生成（聊天、检索摘要、RAGAS 评估的 judge 模型）。
**必须自备**：本仓库不内置模型。任选其一：

- **在线 API**：DeepSeek 等 OpenAI 兼容 API，填 `LLM_BASE_URL=https://api.deepseek.com/v1` + `LLM_API_KEY=<your-key>`
- **本地 vLLM**：
  ```bash
  docker run -d --name my-rag-llm \
    -p 1234:8000 \
    -v /path/to/model:/model \
    vllm/vllm-openai --model /model --served-model-name my-model
  ```
- **本地 LM Studio**：图形界面加载模型后开启 "Start Server"，默认 `http://127.0.0.1:1234/v1`

**连接配置**：`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（OpenAI 兼容 /v1 协议）。

**可降级性**：无。未配置时问答不可用（其余功能不受影响）。

## 3. Embedding 向量模型（bge-m3，必须自备）

**作用**：文档切块向量化 + 检索（Chroma 内置，模型外部提供）。
**必须自备**，OpenAI 兼容 /v1 embeddings 协议：

```bash
docker run -d --name my-rag-embedding \
  -p 8300:8000 \
  -v /path/to/bge-m3:/model \
  vllm/vllm-openai --model /model --task embedding --served-model-name bge-m3
```

**连接配置**：`EMBEDDING_BASE_URL=http://127.0.0.1:8300/v1` / `EMBEDDING_MODEL=bge-m3`。

**可降级性**：无。文档入库与检索必需（向量维度检测 + 一键重建功能可验证一致性）。

## 4. MinIO 对象存储（可降级 local，端口 9000 / 9001）

**作用**：原始文档 + MinerU 解析图片的对象存储（图片经鉴权代理输出）。

**获取方式**：

```bash
docker run -d --name my-rag-minio \
  -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=<your-access-key> \
  -e MINIO_ROOT_PASSWORD=<your-secret-key> \
  -v my-rag-minio-data:/data \
  minio/minio server /data --console-address ":9001"
```

（推荐用同仓库 `docker compose -f docker/docker-compose.infra.yml up -d minio`）

**连接配置**：`MINIO_ENDPOINT=127.0.0.1:9000` / `MINIO_ACCESS_KEY` /
`MINIO_SECRET_KEY` / `MINIO_BUCKET=my-rag` / `MINIO_SECURE=false`。

**可降级性**：可用 **local 后端**替代 —— 把 `STORAGE_BACKEND` 改为 `local`，
对象改存本地 `data/storage`（目录结构与 MinIO 桶 key 同构），
**无需再配置 MinIO**，离线环境适用。需要 MinIO 时改回 `minio` 即可。

## 5. MinerU 文档解析（可选，端口 8001）

**作用**：pdf/docx 高质量解析（版面识别 + 图片提取），提升文档入库质量。

**获取方式**：按 MinerU 官方 Docker 部署文档启动其 API 服务，服务名
`mineru-api`，映射端口 8001（示例）：

```bash
docker run -d --name mineru-api -p 8001:8001 <mineru-api-image>
```

**连接配置**：`MINERU_API_URL=http://127.0.0.1:8001`（超时 `MINERU_TIMEOUT=300`）。

**可降级性**：可选。未启动/不可用时**自动降级** pypdf/python-docx 纯文本提取
（文档详情 `parse_method`：`mineru` / `plain`）；扫描版 PDF 纯文本为空时
文档状态置 `failed` 并提示"请启动 MinerU"。

## 6. Rerank 重排序（可选）

**作用**：检索结果重排序，提升精度。

**获取方式**：本地部署 OpenAI 兼容 `/rerank` 协议服务（如 vLLM 的 rerank
接口部署 bge-reranker-v2-m3 等模型）。

**连接配置**：无独立环境变量，在「系统配置」页检索(retrieval)分组 `rerank`
段配置：`enabled=true` + `base_url` + `model`（+ `top_n`）。

**可降级性**：可选。未配置自动跳过重排（严格降级），检索走
向量+BM25 混合即可。

## 7. RAGAS 评估系统（配套组件，端口 8090）

**作用**：问答效果评估闭环（faithfulness / answer_relevancy 等指标），
「效果展示」页只读对接其任务列表与报告。

**获取方式**：同仓库的 `RAGAS/` 子项目，独立部署：

```bash
cd RAGAS && bash start.sh                      # 源码一键启动
# 或 Docker：
cd RAGAS && docker compose -f docker/docker-compose.yml up -d --build
```

**连接配置**：`RAGAS_BASE_URL=http://localhost:8090`（源码同机）；
Docker 部署时容器内经 host-gateway 访问宿主机：
`RAGAS_BASE_URL=http://host.docker.internal:8090`。

**可降级性**：可选。未运行时页面 Alert 提示，自身统计不受影响。

---

## 配置档案机制说明

以上服务地址**不在代码中硬编码**：网页「系统配置」页维护多套配置档案
（持久化 `data/settings.json`），每档案含 LLM / Embedding / MinerU / 检索
（含 rerank）/ 切块 / 会话 / MySQL / MinIO 分组，支持：

- 多档案切换（如"本地模型" / "DeepSeek 在线"），切换即时生效，无需重启
- 逐项连接测试（LLM / Embedding / MySQL / MinIO 5s 超时，MinerU 3s）
- api_key 脱敏显示（前4\*\*\*\*后4），编辑时未修改则保存保留原值

`.env` / `docker/.env.docker` 中的值仅作首次启动的出厂默认，运行时以
配置档案为准。Docker 部署时容器级字段（`JWT_SECRET` / `DATA_DIR` /
`STORAGE_BACKEND` 等）不进入配置档案 UI，仅由 `docker/.env.docker` 注入。
