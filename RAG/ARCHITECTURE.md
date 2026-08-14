# my-RAG 知识库系统 — 架构文档

本文档描述 RAG 子系统的整体架构、模块职责与关键流程。快速上手与接口契约见
[README.md](./README.md) 与 [docs/api_contract.md](./docs/api_contract.md)。

## 1. 总体架构

### 1.1 系统架构图（mermaid）

```mermaid
flowchart LR
    subgraph 前端["前端 React18 + AntD5"]
        UI[页面层<br/>KnowledgeBases / Documents / Chat /<br/>Analytics / Settings / Users ...]
        APICLIENT[api/client.ts<br/>Axios 封装]
    end

    subgraph 后端["后端 FastAPI (8091)"]
        ROUTERS[路由层<br/>auth users departments knowledge_bases<br/>documents chat stats settings files audit ext_query]
        SERVICES[Services 层<br/>kb / document / vector_store / embedding / parser<br/>ingestion / retrieval / chat / bm25 / rerank /<br/>settings / audit / storage / auth / user / dept /<br/>web_importer / ragas_client / dim_check ...]
        CHUNK[chunking/splitter.py<br/>切块引擎]
    end

    subgraph 数据["数据层"]
        MYSQL[(MySQL<br/>用户/部门/知识库三表)]
        MINIO[(MinIO / local<br/>原始文档与解析图片)]
        CHROMA[(Chroma 嵌入式<br/>data/chroma, kb_{id})]
        JSON[(JSON 元数据<br/>data/kbs documents chat ...)]
    end

    subgraph 外部["外部服务（OpenAI 兼容协议）"]
        LLM[LLM<br/>qwen3 系列 @127.0.0.1:1234/v1]
        EMB[Embedding<br/>bge-m3 dim=1024 @127.0.0.1:8300/v1]
        RERANK[Rerank<br/>本地模型服务 /rerank]
        MINERU[MinerU<br/>文档高质量解析 @127.0.0.1:8001]
    end

    RAGAS[评估系统 RAGAS @8090<br/>只读对接 + 评估发起]

    UI --> APICLIENT
    APICLIENT -->|/api 反向代理| ROUTERS
    ROUTERS --> SERVICES
    SERVICES --> CHUNK
    SERVICES --> MYSQL
    SERVICES --> MINIO
    SERVICES --> CHROMA
    SERVICES --> JSON
    SERVICES --> LLM
    SERVICES --> EMB
    SERVICES --> RERANK
    SERVICES --> MINERU
    SERVICES -->|HTTP 8090| RAGAS
```

> ASCII 版架构图（与 README 一致）见文末附录 A。

### 1.2 RAG 检索链路图（mermaid）

```mermaid
flowchart LR
    A[用户问题] --> B[多知识库选择 kb_ids<br/>top_k / 相似度阈值]
    B --> C1[向量检索<br/>Chroma + bge-m3]
    B --> C2[BM25 混合检索<br/>jieba 中文分词]
    C1 --> D[RRF 融合排序]
    C2 --> D
    D --> E{配置了 Rerank?}
    E -->|是| F[Rerank 重排序<br/>本地模型服务]
    E -->|否| G[直接取 top_k 结果]
    F --> G
    G --> H[上下文组装<br/>父块优先 / 引用标注]
    H --> I[LLM 流式回答<br/>SSE: meta→delta→done]
    I --> J[强制引用溯源<br/>句末 [n] + 来源片段]

    style A fill:#e8f4fd
    style J fill:#e8f4fd
```

### 1.3 文档入库链路

```
上传（txt/md/pdf/docx ≤100MB / URL 网页导入）
    ↓
解析：MinerU 高质量解析（含图片提取）→ 不可用自动降级 pypdf / python-docx
    ↓
切块：通用 / 按标题 / 正则 / 父子分块（chunk_size / chunk_overlap 可配）
    ↓
向量化：bge-m3（批量 32、超长截断 8000 字符）+ 维度检测
    ↓
存储：向量 → Chroma（collection kb_{id}）；原始文档/图片 → MinIO 或 local；
      元数据 → data/*.json + MySQL
    ↓
状态机：uploaded → parsing → parsed → ingested / failed（前端轮询展示）
```

## 2. 模块目录说明

### 2.1 后端（backend/）

| 目录/文件 | 职责 |
|-----------|------|
| `main.py` | 入口：app 初始化、lifespan（init_db + 存储桶 ensure）、路由注册 |
| `config.py` | 全局配置（pydantic-settings，环境变量优先，.env 为出厂默认） |
| `db.py` / `deps.py` | 数据库引擎与会话管理 / 认证依赖（JWT、角色校验） |
| `models/` | Pydantic 模型：rag_models（知识库/文档/聊天）、user_models（用户/部门） |
| `routers/auth.py` | 登录、当前用户、改密（JWT 签发/校验） |
| `routers/users.py` `departments.py` | 用户/部门管理（仅超管） |
| `routers/knowledge_bases.py` | 知识库 CRUD、标签、向量重建、文档列表/上传/解析/重命名/回收站 |
| `routers/admin_documents.py` | 超管跨库文档管理（全库文档视图） |
| `routers/documents.py` | 文档明细、原始内容预览、状态查询 |
| `routers/chat.py` | SSE 流式问答、检索接口、会话历史 CRUD/重命名/导出 |
| `routers/stats.py` | 检索质量统计、RAGAS 只读对接与评估发起 |
| `routers/settings.py` | 配置档案 CRUD/激活/连接测试、embedding 维度查询 |
| `routers/files.py` | 图片/文件鉴权代理输出（?token= 或 Bearer） |
| `routers/audit.py` | 审计日志查询（仅超管） |
| `routers/ext_query.py` | 外部查询配置（供 MCP / Agent 对接的只读问答入口） |
| `services/kb_service.py` `document_service.py` | 知识库/文档元数据（JSON + MySQL） |
| `services/vector_store.py` | Chroma 向量存储（collection kb_{id}，软删除过滤） |
| `services/embedding_service.py` | bge-m3 向量化（批量/截断/失败降级） |
| `services/parser_client.py` `parser_probe.py` `parser_images.py` | 解析器（MinerU/纯文本）探测、调用与图片链路 |
| `services/ingestion_service.py` | 入库状态机编排（上传→解析→切块→向量化，并发信号量） |
| `services/retrieval_service.py` `contextual_retriever.py` | 混合检索、RRF 融合、父子块回填、多库合并 |
| `services/bm25.py` `rerank_client.py` | BM25 中文分词索引 / Rerank 客户端（未配置自动跳过） |
| `services/chat_service.py` | SSE 流式问答、强制引用溯源、多轮上下文 |
| `services/dim_check.py` | 向量维度检测 + 后台重建任务（轮询进度） |
| `services/retrieval_log.py` | 检索质量日志（近 30 天命中率/零命中） |
| `services/ragas_client.py` `ragas_sampling.py` | RAGAS(8090) 只读/评估发起对接、真实问答采样 |
| `services/settings_service.py` | 配置档案（data/settings.json，多档案 + 连接测试） |
| `services/audit_service.py` | 审计日志落库（MySQL） |
| `services/storage_service.py` | 对象存储抽象（MinIO / local 可切换） |
| `services/auth_service.py` `user_service.py` `department_service.py` | 多租户认证与账号体系 |
| `services/web_importer.py` | URL 网页导入 |
| `services/deepdoc_client.py` `probes.py` | DeepDoc/探测客户端（可选解析链路） |
| `chunking/splitter.py` | 切块引擎（通用/按标题/正则/父子分块） |

### 2.2 前端（frontend/src/pages/）

| 页面 | 职责 |
|------|------|
| `Login.tsx` | 登录 |
| `KnowledgeBases.tsx` | 知识库列表/创建/标签/文档管理入口 |
| `Documents.tsx` | 文档上传、解析、回收站、重命名 |
| `GlobalDocuments.tsx` | 超管全库文档视图 |
| `Chat.tsx` | 流式问答（SSE）、会话历史/重命名/导出 |
| `RetrievalTest.tsx` | 检索测试（多库/混合/重排参数调试） |
| `Analytics.tsx` | 检索质量统计 + RAGAS 评估发起与报告 |
| `Settings.tsx` | 配置档案（LLM/Embedding/MinerU/检索/切块/会话/MySQL/MinIO） |
| `Users.tsx` | 用户/部门管理（超管） |
| `Profile.tsx` | 个人资料与改密 |
| `ExtQueries.tsx` / `ExtQueryPage.tsx` | 外部查询配置与演示页 |

## 3. 关键流程

### 3.1 配置即时生效

外部服务地址/模型/参数全部在「系统配置」页可改、可测、可多档案切换，保存后无需
重启；运行时各 service 每次调用动态读取活跃档案（`.env` 仅作出厂默认值）。

### 3.2 权限模型简表

| 角色 | 范围 | 能力 |
|------|------|------|
| `super_admin` | 全局 | 用户/部门管理、审计日志、全部知识库、系统配置 |
| `dept_admin` | 本部门 | 本部门知识库管理、本部门成员管理、发起 RAGAS 评估 |
| `user` | 个人 | 自己创建的知识库 + 被授权的库（部门/共享） |

- 越权访问统一返回 404 伪装（防探测）；文档/图片/检索均做归属校验。
- 认证：JWT（access_token），bcrypt 口令哈希，会话/配置变更入审计日志。

### 3.3 软删除

DELETE 文档 = 移入回收站（向量/存储保留，检索排除）→ 恢复（无需重新解析）→
purge 才物理删除；知识库级联删除同样先入回收站。

## 附录 A：ASCII 架构图（与 README 一致）

```
前端 React18 + AntD5 (dev 3002 / 生产 nginx 托管 dist)
        │  /api 反向代理
后端 FastAPI (8091)   ←── 路由层: auth/users/departments/kbs/documents/chat/stats/settings/files/audit
        │
        ├── services 层（模块化，可单独替换）
        │   ├── kb_service / document_service   元数据（data/kbs、data/documents）+ MySQL 三表
        │   ├── vector_store                    Chroma 嵌入式（data/chroma，collection kb_{id}）
        │   ├── embedding_service               bge-m3（批量 32 / 截断 8000 字符）
        │   ├── parser_client                   MinerU（不可用自动降级 pypdf/python-docx）
        │   ├── ingestion_service               上传→解析→切块→向量化 状态机
        │   ├── retrieval_service / chat_service 向量+BM25 混合 / rerank / SSE 流式（引用标注）
        │   ├── bm25 / rerank_client            混合检索与重排序
        │   ├── dim_check                       向量维度检测 + 后台重建任务
        │   ├── retrieval_log                   检索质量日志（近 30 天）
        │   ├── stats_service / ragas_client    自身统计 + RAGAS(8090) 只读对接
        │   ├── settings_service                配置档案（data/settings.json，多档案+连接测试）
        │   ├── audit_service                   审计日志（MySQL）
        │   ├── storage_service                 对象存储（MinIO / local）
        │   ├── auth_service / user_service / department_service / kb_service（多租户）
        │   └── web_importer                    URL 网页导入
        │
        ├── 外部服务（OpenAI 兼容协议）
        │   ├── LLM:    qwen3.6-35b-a3b-apex-quality @ 127.0.0.1:1234/v1 (LM Studio)
        │   ├── Embedding: bge-m3 (dim=1024) @ 127.0.0.1:8300/v1 (vLLM)
        │   ├── Rerank: 需本地模型服务（配置档案开启，见 docs/部署说明）
        │   ├── MinerU: http://127.0.0.1:8001（Docker 部署）
        │   ├── MySQL:  127.0.0.1:5455（多租户三表）
        │   └── MinIO:  127.0.0.1:9000（对象存储）
        └── RAGAS 评估系统 @ http://localhost:8090（只读对接：任务列表 + 报告）
```
