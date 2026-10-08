"""知识图谱服务（入库阶段构建 + 检索阶段增强）

模块结构：
- service.py: 图谱构建/加载/保存（build_graph_for_doc / load_graph / save_graph /
  graph_path）+ 检索侧的图谱上下文组装（build_kg_source）

**为什么独立成目录**：图谱是本项目里**唯一真正跨流程的领域能力**——它既是入库的
一个阶段（切块后抽实体建图），又是检索的一路（问答时按实体召回）。不像
contextual_retriever / agentic_chunker 那样只服务单个流程（那些收进 ingestion/），
也不像 llm_client / vector_store 那样是与外部系统打交道的通用基础设施。
"""
