# RAGAS 评估系统 — 架构文档

## 目录结构

```
RAGAS/
├── backend/                          # FastAPI 后端
│   ├── main.py                       # 入口：app 初始化、CORS、路由注册、静态文件托管
│   ├── config.py                     # 全局配置（pydantic-settings，支持 .env 文件）
│   │
│   ├── models/                       # Pydantic 数据模型层
│   │   ├── data_models.py            # 数据集相关模型（EvalExample, EvalDataset, DatasetPreview）
│   │   └── eval_models.py            # 评估相关模型（EvalConfig, EvalTask, EvalResult, EvalMetric 枚举）
│   │
│   ├── routers/                      # API 路由层
│   │   ├── data.py                   # POST/GET/DELETE /api/datasets + 样本级 CRUD
│   │   ├── evaluation.py             # POST/GET/DELETE /api/evaluations + 日志/取消/结果
│   │   ├── prompts.py                # GET/PUT/POST /api/prompts（提示词管理 + 翻译）
│   │   ├── results.py                # GET /api/results/{id}/export/{json|csv|html}
│   │   └── settings.py               # GET/POST/PUT/DELETE /api/settings/profiles
│   │
│   ├── services/                     # 业务逻辑层
│   │   ├── data_service.py           # 数据集 CRUD（上传解析、持久化、列表、删除、样本编辑）
│   │   ├── eval_service.py           # 评估任务编排（创建、线程池执行、状态/结果/日志管理）
│   │   ├── prompt_service.py         # 提示词管理（读取/翻译/编辑 RAGAS 指标提示词，语言切换）
│   │   ├── retrieval_service.py      # Elasticsearch 检索服务（全文搜索知识库索引）
│   │   └── settings_service.py       # 配置档案管理（多 profile 切换、连接测试）
│   │
│   └── evaluation/                   # RAGAS 评估核心
│       ├── llm_config.py             # Qwen LLM + bge-m3 Embedding 客户端工厂
│       ├── metrics.py                # 指标注册表（RAGAS metric → 名称映射）
│       └── pipeline.py               # 评估管道（数据集准备 → LLM 连接 → 指标过滤 → 逐指标执行 → 结果整理）
│
├── frontend/                         # React + Vite + TypeScript + Ant Design
│   ├── src/
│   │   ├── api/client.ts             # Axios 封装，所有后端 API 调用
│   │   ├── App.tsx                   # 布局 + 路由（7 个页面，BrowserRouter）
│   │   ├── pages/
│   │   │   ├── Datasets.tsx          # 数据集管理（上传、列表、预览、删除）
│   │   │   ├── DataEditor.tsx        # 数据编辑（表格内直接编辑样本）
│   │   │   ├── Evaluate.tsx          # 评估配置（选数据集/指标/Language/LLM 参数，最近任务列表）
│   │   │   ├── Results.tsx           # 结果看板（雷达图、柱状图、评分明细、导出、日志弹窗）
│   │   │   ├── Flow.tsx              # 工作流程图（6 步评估流程 + 系统架构说明）
│   │   │   ├── Prompts.tsx           # 提示词管理（查看/翻译/编辑各指标的提示词）
│   │   │   └── Settings.tsx          # 配置档案（多 profile CRUD、连接测试）
│   │   └── main.tsx                  # 入口
│   ├── index.html
│   ├── vite.config.ts                # 开发代理 /api → localhost:8090，host: 0.0.0.0
│   └── package.json
│
├── prompts/                          # 提示词缓存目录（运行时生成）
│   └── prompts_db.json               # 活跃语言 + 各指标提示词内容
├── data/
│   ├── datasets/                     # 数据集 JSON 元数据持久化
│   └── uploads/                      # 上传的原始文件（CSV/JSON/Excel）
├── reports/                          # 评估任务 + 结果 + 日志 JSON 持久化
├── .env                              # 环境变量配置（LLM / Embedding / ES）
├── requirements.txt
└── ARCHITECTURE.md
```

---

## 数据流

### 评估主流程
```
┌──────────────┐     CSV/JSON/Excel      ┌──────────────────┐
│   用户上传    │ ──────────────────────→ │  /api/datasets    │
│  测试数据集   │                        │  DataService      │
└──────────────┘                         │  - 解析校验        │
                                         │  - 列映射          │
                                         │  - 持久化          │
                                         └────────┬─────────┘
                                                  │ List[EvalExample]
                                                  ▼
┌──────────────┐                         ┌──────────────────────┐
│  配置评估参数  │ ──────────────────────→ │  /api/evaluations    │
│  - 数据集     │   EvalConfig            │  EvalService          │
│  - 指标       │                         │  - 创建任务 (progress=0) │
│  - 检索开关   │                         │  - ThreadPoolExecutor  │
│  - 评估语言   │                         │  - 进度/ETA 回调       │
│  - LLM 参数   │                         │  - 取消标志            │
└──────────────┘                          └──────────┬──────────────┘
                                                     │ _run_eval()
                                                     ▼
                                   ┌───────────────────────────────────┐
                                   │      EvalPipeline.run()            │
                                   │                                   │
                                   │  2% 准备数据集                     │
                                   │  4% 检查 Embedding 可用性           │
                                   │  6% 构建 HF Dataset                │
                                   │  8% 连接 Qwen LLM (llm_factory)    │
                                   │ 10% LLM 就绪                       │
                                   │ 10→90% 逐指标 ragas.evaluate()     │
                                   │  (每完成一个指标 +80%/N)            │
                                   │  (首指标完成后预测 ETA)             │
                                   │ 92% 整理结果 → EvalResults         │
                                   │100% 完成                            │
                                   └──────────────────┬────────────────┘
                                                      │
                          ┌───────────────────────────┤
                          │                           │
                          ▼                           ▼
                ┌──────────────────┐       ┌──────────────────┐
                │  GET /evaluations │       │  GET /evaluations│
                │  /{id} (轮询进度)  │       │  /{id}/results   │
                │  含 progress/eta  │       │  (获取完整结果)   │
                └──────────────────┘       └────────┬─────────┘
                                                    │
                                                    ▼
                                         ┌─────────────────────────┐
                                         │     前端结果看板          │
                                         │  - 雷达图（ECharts）      │
                                         │  - 柱状图（ECharts）      │
                                         │  - 评分明细表             │
                                         │  - JSON/CSV/HTML 导出     │
                                         └─────────────────────────┘
```

### 提示词管理流程
```
┌──────────────┐     GET /api/prompts      ┌──────────────────┐
│   提示词管理   │ ←────────────────────── │  PromptService    │
│   Prompts.tsx │ ──────────────────────→  │                   │
│              │  POST /{metric}/translate  │  - 读取 RAGAS    │
│  - 查看指标   │                          │    metric 原始英文 │
│  - AI 翻译   │     PUT /api/prompts/      │  - adapt_prompts()│
│  - 手动编辑   │     active-language       │  - set_prompts()  │
│  - 语言切换   │                          │  - save_prompts()  │
└──────────────┘                           │    缓存到磁盘      │
                                           └──────────────────┘
                                                    │
                                                    ▼
                                           ┌─────────────────────┐
                                           │  prompts/ 目录       │
                                           │  prompts_db.json    │
                                           │  {metric}_chinese   │
                                           │  .json 缓存文件      │
                                           └─────────────────────┘
评估时：eval_service → prompt_service.apply_prompts_for_eval() →
       load_prompts() + set_prompts() → pipeline.run() 使用正确语言
```

---

## API 接口总览

### 数据集
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/datasets/upload` | 上传数据集文件 |
| POST | `/api/datasets/create` | 手动创建空数据集 |
| GET | `/api/datasets` | 数据集列表 |
| GET | `/api/datasets/{id}` | 数据集详情/预览 |
| DELETE | `/api/datasets/{id}` | 删除数据集 |
| POST | `/api/datasets/{id}/samples` | 添加样本 |
| PUT | `/api/datasets/{id}/samples/{idx}` | 更新样本 |
| DELETE | `/api/datasets/{id}/samples/{idx}` | 删除样本 |

### 评估
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/evaluations/metrics` | 可用评估指标列表 |
| POST | `/api/evaluations` | 创建评估任务（异步） |
| GET | `/api/evaluations` | 评估任务列表 |
| GET | `/api/evaluations/{id}` | 任务状态/进度（含 eta_seconds） |
| GET | `/api/evaluations/{id}/logs` | 实时日志（支持 since 增量拉取）|
| POST | `/api/evaluations/{id}/cancel` | 取消运行中的任务 |
| DELETE | `/api/evaluations/{id}` | 删除任务（含文件） |
| GET | `/api/evaluations/{id}/results` | 获取评估结果 |

### 结果导出
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/results/{id}/export/json` | 导出 JSON |
| GET | `/api/results/{id}/export/csv` | 导出 CSV |
| GET | `/api/results/{id}/export/html` | 导出 HTML 报告 |

### 提示词管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/prompts` | 所有指标提示词摘要 |
| GET | `/api/prompts/{metric}` | 单个指标提示词详情（中/英）|
| POST | `/api/prompts/{metric}/translate` | AI 翻译为中文 |
| PUT | `/api/prompts/{metric}` | 保存手动编辑的提示词 |
| GET | `/api/prompts/active-language` | 获取当前评估语言 |
| PUT | `/api/prompts/active-language` | 设置评估语言 |

### 配置档案
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/settings/profiles` | 配置档案列表 |
| POST | `/api/settings/profiles` | 创建档案 |
| PUT | `/api/settings/profiles/{id}` | 更新档案 |
| DELETE | `/api/settings/profiles/{id}` | 删除档案 |
| POST | `/api/settings/profiles/{id}/activate` | 激活档案 |
| GET | `/api/settings/profiles/active` | 获取当前生效配置 |
| POST | `/api/settings/test/llm` | 测试 LLM 连接 |
| POST | `/api/settings/test/embedding` | 测试 Embedding 连接 |
| POST | `/api/settings/test/es` | 测试 ES 连接 |

### 其他
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |

---

## 外部依赖

| 组件 | 用途 | 地址 |
|------|------|------|
| **Qwen3.5-9B** | LLM Judge（评估裁判） | `http://127.0.0.1:8000/v1` (vLLM) |
| **bge-m3** | Embedding 模型（生成向量） | `http://127.0.0.1:8300/v1` |
| **Elasticsearch 7.x** | 知识库存储/检索 | `localhost:1200` auth: elastic/<your-password> |

---

## 评估指标说明

| 指标 | 需要 Embedding | 需要 Ground Truth | 说明 |
|------|:---:|:---:|------|
| faithfulness | ✗ | ✗ | 答案是否忠于上下文，无虚构 |
| answer_relevancy | ✓ | ✗ | 答案与问题的相关程度 |
| context_precision | ✗ | ✗ | 检索到的上下文是否精确有用 |
| context_recall | ✗ | ✓ | 检索是否覆盖了必要信息 |
| answer_correctness | ✗ | ✓ | 答案与参考答案的匹配度 |
| answer_similarity | ✓ | ✓ | 答案与参考答案的语义相似度 |

---

## 关键设计决策

1. **线程池异步评估**：评估任务通过 `ThreadPoolExecutor(max_workers=1)` 在独立线程执行，自带独立事件循环，避免阻塞 FastAPI asyncio 事件循环。前端轮询 `GET /evaluations/{id}` 获取进度和 ETA。
2. **进度映射**：0-10% 准备阶段（数据集/指标/LLM），10-90% 逐指标评估（每完成一个指标 +80%/N），90-100% 整理结果。首指标完成后根据用时预测剩余时间（ETA）。
3. **资源探测**：启动评估时自动探测 Embedding 模型可用性，不可用时自动跳过需要 Embedding 的指标（如 answer_relevancy、answer_similarity）。
4. **结果持久化**：所有任务、结果、日志以 JSON 文件分别存储在 `reports/` 目录（`task_*.json` / `result_*.json` / `log_*.json`），重启后自动恢复。running/pending 任务标记为 failed。
5. **提示词管理**：通过 RAGAS 0.4.3 的 `get_prompts()` / `set_prompts()` / `adapt_prompts()` / `save_prompts()` API 管理各指标提示词。支持中英文切换、AI 翻译、手动编辑。翻译缓存到 `prompts/` 目录。`answer_similarity` 不包含提示词管理方法，需 `hasattr` 防御。
6. **配置档案**：多 profile 管理 LLM/Embedding/ES 连接参数。通过 `pydantic-settings` 加载 `.env`，运行时用 `setattr` 动态覆盖。
7. **生产部署**：后端在构建时自动托管前端 `dist/` 目录，单进程即可服务完整应用。Vite 开发模式下通过 proxy 转发 API 请求。
8. **ES 版本兼容**：服务器使用 ES 7.x，Python 客户端使用 `elasticsearch7` 包确保兼容。
