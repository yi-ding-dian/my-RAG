# RAGAS 评估系统

一键评估 RAG 应用的回答质量，解决"我的 RAG 到底好不好、哪里需要改进"的痛点。

---

## 快速开始

### 一键启动（推荐）

```bash
cd RAGAS
cp .env.example .env    # 编辑 .env 填入 LLM/Embedding/ES 地址，ES 地址可选
bash start.sh
```

自动安装依赖 → 构建前端 → 启动服务。访问 `http://<本机IP>:8090` 即可使用。

### 开发模式（前后端分离）

终端 1 — 后端：
```bash
cd RAGAS
python -m backend.main
```

终端 2 — 前端（热更新）：
```bash
cd RAGAS/frontend
npm install
npm run dev
```

前端运行在 `http://localhost:3001`。

### Docker 部署

```bash
cd RAGAS/docker

# 编辑环境变量（可选，默认连接 127.0.0.1 的 LLM/Embedding 服务）
# export HOST_PORT=8090
# export LLM_BASE_URL=http://your-llm:8000/v1

# 启动（首次自动从本地镜像启动）
docker compose up -d
```

访问 `http://<本机IP>:8090` 即可使用。

数据持久化目录（自动创建）：
- `docker/data/datasets/` — 数据集文件
- `docker/data/uploads/` — 上传文件
- `docker/reports/` — 评估报告
- `docker/prompts/` — 提示词缓存

> **构建镜像**：如需重新构建，在项目根目录执行 `docker build -t ragas-eval:latest .`

---

## 使用流程

```
① 模型配置 → ② 上传数据 → ③ 编辑数据 → ④ 配置提示词 → ⑤ 执行评估 → ⑥ 查看结果
```

### ① 模型配置
在「模型配置」页面设置 LLM、Embedding、Elasticsearch 的连接参数，支持多档案切换和连接测试。

### ② 上传数据集
在「数据集管理」页面上传 CSV/JSON 格式的评估数据，需包含 `question`、`answer`、`contexts` 等列。也支持手动创建空数据集。

### ③ 编辑数据（可选）
在「数据编辑」页面直接增删改样本数据。支持 inline 编辑、添加新样本、删除样本。

### ④ 配置提示词（可选）
在「提示词管理」页面查看各评估指标的提示词内容，支持一键 AI 翻译为中文、手动编辑翻译文本、自由切换评估语言。

### ⑤ 执行评估
在「执行评估」页面选择数据集和评估指标，配置 LLM 参数（temperature、max_tokens、并发数），点击开始评估。评估在后台异步执行，可随时查看实时日志或取消任务。

### ⑥ 查看结果
在「评估结果」页面查看聚合评分（雷达图 + 柱状图）、逐条评分明细、导出 JSON/CSV/HTML 报告。首次评估时会根据第一个指标用时预测剩余时间。


---

## 数据格式
Ragas 的 Dataset 通常需要包含特定列，比如：

question：问题

answer：生成的答案

contexts：检索到的上下文（通常是字符串列表）

ground_truth：真实答案（可选）

---

## 评估指标

| 指标 | 需要 Embedding | 需要标准答案 | 说明 |
|------|:---:|:---:|------|
| 忠实度 | ✗ | ✗ | 答案是否忠于上下文、无虚构 |
| 答案相关性 | ✓ | ✗ | 答案与问题的相关程度 |
| 上下文精确度 | ✗ | ✗ | 检索到的上下文是否精确有用 |
| 上下文召回率 | ✗ | ✓ | 检索是否覆盖了必要信息 |
| 答案正确性 | ✗ | ✓ | 答案与参考答案的匹配度 |
| 答案相似度 | ✓ | ✓ | 答案与参考答案的语义相似度 |

---

## 系统架构

```
React + Ant Design → FastAPI → RAGAS 0.4.3 → Qwen 3.5 LLM
                                        → bge-m3 Embedding
                                        → Elasticsearch（可选检索）
```

详细架构见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

---

## 技术栈

- **前端**: React 18 + TypeScript + Ant Design 5 + ECharts + Vite
- **后端**: Python 3.10 + FastAPI + Uvicorn
- **评估引擎**: RAGAS 0.4.3
- **评分模型**: Qwen3.5-9B-GPTQ-4bit（vLLM 部署）
- **向量模型**: bge-m3
- **检索库**: Elasticsearch 7.x
