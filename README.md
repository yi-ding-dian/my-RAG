# my-RAG 企业知识库套件

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/Python-3.12-blue.svg)
![React](https://img.shields.io/badge/React-18-61dafb.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)

基于本地私有化部署的企业知识库双件套：**知识库问答系统（RAG）** + **评估系统（RAGAS）**。
两个子系统独立开发、独立部署，共用同一套 LLM / Embedding / Elasticsearch 基础设施，
并通过评估闭环打通：RAG 知识库的问答效果可直接发起 RAGAS 评估。

## 目录结构

```
my-RAG/
├── RAG/      # 企业知识库问答系统
├── RAGAS/    # 基于 RAGAS 的评估系统
└── LICENSE   # MIT（各子项目含独立 LICENSE，版权归属 my-RAG Contributors）
```

## 子项目

| 目录 | 简介 | 文档 |
|------|------|------|
| `RAG/`   | **企业知识库问答系统**：文档上传入库 → 向量检索 → 流式问答（强制引用溯源），支持多租户与团队协作（MySQL / MinIO / JWT / 审计） | [RAG/README.md](RAG/README.md) |
| `RAGAS/` | **基于 RAGAS 的评估系统**：一键评估 RAG 应用回答质量（faithfulness / answer_relevancy 等指标），可视化评估界面 | [RAGAS/README.md](RAGAS/README.md) |

## 快速开始

两个子系统独立启动，分别进入对应目录按其 README 指引操作：

```bash
# RAG 知识库系统：按 RAG/README.md 指引启动
cd RAG

# RAGAS 评估系统：一键启动（自动安装依赖 → 构建前端 → 启动服务）
cd RAGAS && bash start.sh
```

默认连接 `127.0.0.1:8000`（LLM）与 `127.0.0.1:8300`（Embedding）的本地模型服务；
Elasticsearch 口令等敏感配置通过各项目 `.env` 注入（不入库），首次使用请参考各项目的 `.env.example` 填写。

## 技术栈与 GitHub Topics

建议在 GitHub 仓库页（About → Topics）为仓库添加以下标签，便于被检索：

```
rag  fastapi  react  chromadb  ragas  llm  minio  mysql
```

| 组件 | 用途 |
|------|------|
| FastAPI + Python 3.12 | 两个子系统后端 |
| React 18 + Ant Design 5 | 两个子系统前端 |
| Chroma | 知识库向量存储（RAG） |
| RAGAS | 评估引擎（RAGAS 子系统） |
| MinIO / MySQL | 对象存储与多租户元数据（RAG） |
| Elasticsearch | 评估侧检索（可选） |

## License

MIT © 2026 my-RAG Contributors
