# Changelog

本仓库两个子系统（`RAG/` 知识库问答系统、`RAGAS/` 评估系统）的变更记录统一维护于此。
版本格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
语义化版本见 [SemVer](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-08-08

首个开源版本。基于本地私有化部署的企业知识库双件套：知识库问答系统（RAG）+
评估系统（RAGAS），两者共用同一套 LLM / Embedding 基础设施。

### 核心功能（RAG 知识库问答系统）

- 知识库管理：CRUD、级联删除、多租户归属（部门 + 创建人）、标签体系
- 文档入库：txt/md/pdf/docx 上传（≤100MB）、URL 网页导入、中文文件名安全处理
- 检索问答：Chroma + bge-m3 向量检索、BM25 混合检索（jieba 中文分词 + RRF 融合）、
  Rerank 重排序（可配置，未配置自动降级）、SSE 流式问答（强制引用溯源 [n] + 来源片段）
- QA 切块：通用 / 按标题 / 正则 / 父子分块（含父子块检索回填与语义检索）
- PlainText 降级解析：MinerU 高质量解析不可用时自动降级 pypdf / python-docx
- 上下文检索（Contextual Retrieval）：检索上下文增强
- 会话与导出：历史列表/重命名/Markdown 导出
- 回收站：软删除 → 恢复 → 彻底删除（purge）

### 企业级能力（RAG）

- 多租户 + 团队协作：用户/部门/知识库三表（MySQL）、角色权限（super_admin / dept_admin / user）、越权 404 伪装
- 审计日志（仅超管可查）、对象存储（MinIO / local 可切换，图片鉴权代理输出）
- 配置档案（LLM/Embedding/MinerU/检索/切块/会话/MySQL/MinIO 多档案，即时生效 + 连接测试）
- 向量维度检测 + 一键重建、检索质量统计（近 30 天命中率/文档排行/零命中文档）
- 外部查询（Ext Query）：面向 MCP / Agent 的只读问答接口，凭 token 鉴权

### MCP 服务（RAG/mcp-server）

- `kb-ext-server`：标准 MCP server，将知识库问答能力封装为 `kb_query` 工具（stdio）
- HTTP 直连方式 `kbQuery.js` 与接入说明

### RAGAS 评估系统

- 一键评估 RAG 应用回答质量：忠实度 / 答案相关性 / 上下文精确度 / 上下文召回率 / 答案正确性 / 答案相似度
- 数据集管理（CSV/JSON 上传、手动创建、样本级 inline 编辑）、提示词管理（AI 翻译 + 手动编辑 + 语言切换）
- 评估任务后台异步执行（实时日志 / 取消）、结果看板（雷达图 + 柱状图 + 逐条明细 + JSON/CSV/HTML 导出）
- 多模型配置档案（LLM/Embedding/ES 连接参数，连接测试）、评估任务级 LLM 覆盖（在线 DeepSeek 等）
- 与主项目闭环：知识库「统计分析」页可一键发起 RAGAS 评估（只读对接端口 8090）

### 基础设施

- 一键部署：源码路径（deploy/ 脚本）与 Docker 路径（docker/ compose）
- 测试：RAG 侧 961 例 pytest（数据完全隔离 + 离线 mock，CI 可离线运行）；RAGAS 侧 25 例
- CI（`.github/workflows/ci.yml`）：RAG 后端 pytest / RAG 前端构建 / RAGAS pytest，纯离线
