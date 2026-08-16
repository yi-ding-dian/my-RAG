"""RAG 知识库数据模型（字段统一驼峰命名，与前端约定一致）"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class KnowledgeBase(BaseModel):
    """知识库"""
    id: str = Field(..., description="知识库 ID")
    name: str = Field(..., description="知识库名称")
    description: str = Field("", description="描述")
    doc_count: int = Field(0, description="文档数")
    chunk_count: int = Field(0, description="切块数")
    created_at: str = Field("", description="创建时间")
    department_id: Optional[str] = Field(None, description="所属部门 ID（null=全局）")
    owner_id: Optional[str] = Field(None, description="创建人用户 ID")
    vector_status: Optional[dict] = Field(
        None, description="向量状态摘要: {current_dim, model_dim, compatible}（维度冲突检测，详情见 GET /vector-status）")
    tags: List[str] = Field(
        default_factory=list, description="标签列表（≤10 个，每个 1-20 字符）")


class CreateKBRequest(BaseModel):
    """创建知识库请求"""
    name: str = Field(..., description="知识库名称")
    description: str = Field("", description="描述")
    department_id: Optional[str] = Field(None, description="所属部门 ID（dept_admin 强制本部门，忽略此字段）")
    tags: Optional[List[str]] = Field(
        None, description="标签列表（≤10 个，每个 1-20 字符，可省略）")


class UpdateKBRequest(BaseModel):
    """更新知识库请求"""
    name: Optional[str] = Field(None, description="知识库名称")
    description: Optional[str] = Field(None, description="描述")
    tags: Optional[List[str]] = Field(
        None, description="标签列表（≤10 个，每个 1-20 字符；传 [] 清空；省略=不改）")


class UpdateKBTagsRequest(BaseModel):
    """覆盖式设置知识库标签"""
    tags: List[str] = Field(
        default_factory=list, description="标签列表（≤10 个，每个 1-20 字符；空数组=清空）")


class TagCount(BaseModel):
    """标签使用计数（标签聚合接口条目）"""
    name: str = Field(..., description="标签名")
    count: int = Field(..., description="使用该标签的知识库数量")


class TagAggregate(BaseModel):
    """标签聚合响应"""
    tags: List[TagCount] = Field(default_factory=list)


class DocumentItem(BaseModel):
    """文档（状态机: uploaded -> parsing -> parsed -> ingested / failed）"""
    id: str = Field(..., description="文档 ID")
    kb_id: str = Field(..., description="所属知识库 ID")
    name: str = Field("", description="内部文件名（UUID）")
    original_name: str = Field("", description="原始文件名")
    file_type: str = Field("", description="文件类型，如 txt/pdf/docx/md")
    size: int = Field(0, description="文件大小（字节）")
    status: str = Field("uploaded", description="状态: uploaded/parsing/parsed/ingested/failed")
    error: Optional[str] = Field(None, description="失败原因")
    chunk_count: int = Field(0, description="切块数")
    parse_method: Optional[str] = Field(None, description="解析方式: mineru/deepdoc/plain")
    parser_id: Optional[str] = Field(None, description="切块方式: naive/title/regex（None=未入库）")
    parser_config: Optional[dict] = Field(None, description="切块参数: chunk_size/overlap/delimiter/split_level/regex_pattern")
    chunk_preview: List[str] = Field(default_factory=list, description="切块预览（限 20 条，每条截 500 字符，兼容保留）")
    chunks_meta: List[dict] = Field(default_factory=list, description="切块元数据完整列表: [{text, char_start, char_end}]（偏移相对解析全文，详情接口用）")
    created_at: str = Field("", description="创建时间")
    updated_at: str = Field("", description="更新时间")
    deleted: bool = Field(False, description="是否已移入回收站（软删除标记，检索自动排除；缺失视为 false）")
    deleted_at: Optional[str] = Field(None, description="移入回收站时间（恢复后清空）")


class RenameDocumentRequest(BaseModel):
    """文档重命名请求（只改展示名 original_name，内部名/向量/chunk 不变）"""
    name: str = Field(..., description="新文件名（1~255 字符，扩展名须与原文件一致，无扩展名自动补）")


class UrlImportRequest(BaseModel):
    """URL 网页导入请求"""
    url: str = Field(..., description="网页 URL（仅支持 http/https）")


class IngestRequest(BaseModel):
    """手动触发入库的切块参数（全部可选，不传用文档已有配置或默认）

    校验规则（同步 400 + 任务内写回 failed 双保险，见 ingestion_service.resolve_parser_config）：
    - method 仅 naive/title/regex/parent_child/qa；regex 必须有 regex_pattern；chunk_size 限 50~20000
    - parent_child 父块参数：parent_chunk_size 限 200~4000、parent_chunk_overlap 0~500、
      parent_split_level 1~6；retrieval_mode 仅 parent/child（默认 parent）
    - qa 问答切块：解析后检测问答对占比（问答对/总段落，≥50% 合格），不合格
      且未带 qa_force_continue → 任务失败（错误信息带检测详情，前端确认后
      带 qa_force_continue=true 重新提交强制入库）
    - 解析配置（新字段，默认见 ingestion_service._DEFAULT_PARSER_CONFIG）：
      layout_recognize 仅 MinerU/DeepDOC/PlainText（均已生效：MinerU=高精度/
      DeepDOC 表格输出为可检索 HTML/PlainText 纯文本直提）；pages 为 [[from,to],...]
      （from/to>=1 且 from<=to）；task_page_size 限 1~128；
      lang_list 仅 ch/en；table_enable/formula_enable/return_images/
      enable_heading_in_content/contextual_retrieval/knowledge_graph 为布尔
      （contextual_retrieval=上下文检索增强，默认关，开启产生额外 token 费用；
      knowledge_graph=知识图谱，默认关，入库时用 LLM 抽取实体关系，开启产生
      额外 token 费用）
    """
    method: Optional[str] = Field(None, description="切块方式: naive=通用切块/title=按标题切块/regex=正则切块/parent_child=父子分块/qa=QA 问答切块（按问/答标记聚合问答对为整块）")
    parser_engine: Optional[str] = Field(None, description="解析引擎: auto=自动（MinerU 优先、不可用降级；layout_recognize=DeepDOC 时走 DeepDoc）/mineru=强制 MinerU（不可用标 failed）/deepdoc=强制 DeepDoc（RAGFlow，表格输出为可检索 HTML，仅 PDF）/plain=纯文本提取（默认 auto）")
    backend: Optional[str] = Field(None, description="MinerU 解析后端: hybrid-auto-engine=混合自动引擎（默认，质量优：表格规范/OCR 准/流程图识别）/pipeline=管线（速度快约 20s，表格可能错乱）/auto=跟随服务端默认（与不传等价，不持久化不透传；仅 MinerU 引擎生效）")
    chunk_size: Optional[int] = Field(None, description="块大小（字符数，默认取活跃配置）")
    overlap: Optional[int] = Field(None, description="重叠字符数（默认取活跃配置）")
    delimiter: Optional[str] = Field(None, description="仅 naive 用，分隔符（如 \\n\\n，可选）")
    split_level: Optional[int] = Field(None, description="仅 title 用，标题层级 1-3")
    regex_pattern: Optional[str] = Field(None, description="仅 regex 用，正则表达式")
    parent_chunk_size: Optional[int] = Field(None, description="仅 parent_child 用，父块大小（字符数，默认 1024）")
    parent_chunk_overlap: Optional[int] = Field(None, description="仅 parent_child 用，父块重叠字符数（默认 100）")
    parent_split_level: Optional[int] = Field(None, description="仅 parent_child 用，父块聚合标题层级 1-6（默认 2，即 #/## 为父块边界）")
    retrieval_mode: Optional[str] = Field(None, description="检索模式: parent=命中返回父块全文作上下文/child=仅返回子块（默认 parent）")
    # ---- 解析配置（解析器参数，随 parser_config 持久化，重跑沿用）----
    layout_recognize: Optional[str] = Field(None, description="版面识别: MinerU=高精度（默认）/DeepDOC=表格输出为可检索 HTML/PlainText=纯文本直提（pypdf/python-docx，无表格/图片识别；选此且 engine=auto 时自动切纯文本提取）")
    pages: Optional[list] = Field(None, description="页码范围 [[from,to],...]（from/to>=1 且 from<=to；多组时当前仅第一组生效，默认 [[1,1000000]] 全量）")
    task_page_size: Optional[int] = Field(None, description="任务页大小 1-128（存配置，当前单任务解析，主要给 MinerU 分页参考，默认 12）")
    table_enable: Optional[bool] = Field(None, description="表格识别开关（MinerU，默认 True）")
    formula_enable: Optional[bool] = Field(None, description="公式识别开关（MinerU，默认 True）")
    return_images: Optional[bool] = Field(None, description="图片提取开关（True 时 MinerU 返回图片→存 MinIO，False 不提取，默认 True）")
    lang_list: Optional[str] = Field(None, description="解析语言 ch=中文/en=英文（MinerU lang_list，默认 ch）")
    enable_heading_in_content: Optional[bool] = Field(None, description="包含父标题（切块后为不含标题的块拼接前缀标题路径，默认 False）")
    contextual_retrieval: Optional[bool] = Field(None, description="上下文检索增强：开启后切块时对每个块调用 LLM 生成上下文摘要（向量化/检索文本加【上下文】前缀，产生额外 token 费用；失败/超时跳过不阻塞入库；默认 False）")
    knowledge_graph: Optional[bool] = Field(None, description="知识图谱：开启后入库时用激活 LLM 对每个切块抽取实体与关系，合并构建知识图谱（存储 data/storage/graphs/{kb_id}.json，产生额外 token 费用；失败/超时跳过不阻塞入库；默认 False）")
    parse_llm_model: Optional[str] = Field(None, description="解析 LLM 模型（上下文摘要/知识图谱抽取专用，值为激活档案 LLM 模型列表的 name；空/缺省=用当前激活对话模型，查不到回退激活模型；对话不受影响；随 parser_config 持久化，重跑沿用）")
    qa_force_continue: Optional[bool] = Field(None, description="QA 问答切块规范性强制继续（仅 method=qa 生效）：False/缺省=解析后检测问答对占比（问答对/总段落），低于 50% 任务失败并带检测详情；True=跳过规范性检测直接入库（前端确认继续入库时提交）")


    """补建/重建文档知识图谱请求（全部可选）

      文档 parser_config（文档持久化配置不变，再次构建不带该字段时仍用
      文档原配置/激活模型）；空/不传 → 沿用文档 parser_config.parse_llm_model
      （文档也未配置 → 激活模型）；模型不在激活档案 → 回退文档配置/激活
      模型（warning 日志，不失败）
    """


class Source(BaseModel):
    """检索命中的引用片段"""
    id: str = Field(..., description="块 ID（doc_id_chunk_index）")
    text: str = Field("", description="片段文本（精准命中子块）")
    score: float = Field(0.0, description="相似度（0~1）")
    document_id: str = Field("", description="来源文档 ID")
    document_name: str = Field("", description="来源文档原始名")
    kb_id: str = Field("", description="来源文档所属知识库 ID（引用溯源用）")
    kb_name: Optional[str] = Field(None, description="来源文档所属知识库名称（多知识库对比检索时由路由层填充）")
    chunk_index: int = Field(0, description="块序号")
    parent_text: Optional[str] = Field(None, description="父块全文（parent_child 模式且 retrieval_mode=parent 时返回，作完整上下文；child 模式或非父子文档为 None）")
    context: Optional[str] = Field(None, description="上下文摘要（上下文检索增强开启时生成；检索返回的 text 已含【上下文】前缀，此字段供前端标签展示与引用拼接）")
    vector_score: Optional[float] = Field(None, description="原始向量检索分数（混合模式下保留供调试；纯向量模式=score；BM25 单独命中为 None）")
    char_start: int = Field(-1, description="块字符起始偏移（相对文档解析全文；-1=历史数据无偏移，检索测试页上下文截取用）")
    char_end: int = Field(-1, description="块字符结束偏移（开区间，相对文档解析全文；-1=历史数据无偏移）")


class ChatMessage(BaseModel):
    """聊天消息（用户/助手）"""
    role: str = Field(..., description="user/assistant")
    content: str = Field("", description="消息内容")
    sources: List[Source] = Field(default_factory=list, description="该消息引用的来源快照")


class ChatSession(BaseModel):
    """聊天会话（落盘 data/chat/{session_id}.json）"""
    id: str = Field(..., description="会话 ID")
    kb_id: str = Field("", description="关联知识库 ID")
    user_id: Optional[str] = Field(None, description="归属用户 ID（旧数据为空=super_admin 归属）")
    title: str = Field("", description="会话标题（问题前 20 字）")
    messages: List[ChatMessage] = Field(default_factory=list, description="消息列表")
    created_at: str = Field("", description="创建时间")
    updated_at: str = Field("", description="更新时间")


class ChatHistoryItem(BaseModel):
    """会话历史列表项"""
    id: str
    kb_id: str
    user_id: Optional[str] = Field(None, description="归属用户 ID")
    title: str
    message_count: int
    created_at: str
    updated_at: str


class ChatRequest(BaseModel):
    """聊天请求（契约字段 query，保留 message 向后兼容）"""
    kb_id: str = Field(..., description="知识库 ID")
    query: str = Field("", description="用户问题（契约字段，优先）")
    message: Optional[str] = Field(None, description="用户问题（向后兼容字段）")
    session_id: Optional[str] = Field(None, description="会话 ID（续聊时传）")
    top_k: Optional[int] = Field(
        None, description="检索条数（1~50；None=取配置 retrieval.top_k，页面选择器透传）")


class RenameSessionRequest(BaseModel):
    """会话重命名请求"""
    title: str = Field(..., description="新标题（1~50 字）")


class RetrieveRequest(BaseModel):
    """检索调试请求（参数全可选，不传与既有行为完全一致，旧调用不破坏）

    - kb_id / kb_ids 二选一（都传时 kb_ids 优先）；kb_ids 支持 1~5 个知识库
      多库对比检索：每个库独立检索（各自 top_k 候选）后合并按 score 降序取
      全局 top_k，Source 附带 kb_id/kb_name
    """
    kb_id: Optional[str] = Field(None, description="知识库 ID（单库检索；与 kb_ids 二选一）")
    kb_ids: Optional[List[str]] = Field(None, description="知识库 ID 数组（1~5 个，多库对比检索；与 kb_id 二选一，都传时优先）")
    query: str = Field(..., description="检索 query")
    top_k: Optional[int] = Field(None, description="返回条数（默认取配置 RETRIEVAL_TOP_K）")
    enable_hybrid: Optional[bool] = Field(None, description="混合检索开关：None=用配置默认；true/false=强制开关（对比实验用；当前版本检索链路为纯向量，参数为预留契约）")
    enable_rerank: Optional[bool] = Field(None, description="重排开关：None=用配置默认；true/false=强制开关（对比实验用；当前版本检索链路无重排，参数为预留契约）")
    similarity_threshold: Optional[float] = Field(None, description="相似度阈值覆盖：None=用配置；给定则覆盖（0~1，低于该分数的命中被过滤），调试阈值影响用")


class RetrieveResponse(BaseModel):
    """检索调试响应（契约: {sources: [...]}）"""
    sources: List[Source]


class ChunkInfo(BaseModel):
    """切块条目（契约: {text, index, char_start, char_end}）"""
    text: str = Field("", description="切块文本")
    index: int = Field(0, description="块序号")
    char_start: int = Field(-1, description="字符起始偏移（相对 full_text；-1=历史数据无偏移）")
    char_end: int = Field(-1, description="字符结束偏移（开区间，相对 full_text；-1=历史数据无偏移）")
    context: Optional[str] = Field(None, description="上下文摘要（上下文检索增强开启时生成；text 保持原文，偏移契约不受影响；无摘要为 None）")


class DocumentDetail(DocumentItem):
    """文档详情（详情接口响应：在 DocumentItem 基础上补充 chunks 对象数组与 full_text）"""
    chunks: List[ChunkInfo] = Field(default_factory=list, description="切块（完整列表，含偏移，来源 chunks_meta）")
    full_text: str = Field("", description="解析后全文（data/parsed/{doc_id}.md，偏移以此为基准；未入库/文件缺失为空）")


class GraphChunkRef(BaseModel):
    """实体/关系在文档中的引用位置（chunk_index 为 chunks_meta 下标，
    char_start/char_end 相对文档解析全文，与 chunks_meta 偏移契约一致）"""
    doc_id: str = Field(..., description="文档 ID")
    chunk_index: int = Field(..., description="块序号（chunks_meta 下标）")
    char_start: int = Field(..., description="字符起始偏移（相对文档解析全文；定位失败回退整块区间）")
    char_end: int = Field(..., description="字符结束偏移（开区间）")


class GraphEntity(BaseModel):
    """知识图谱实体（入库时 LLM 从切块抽取，按 name+type 规范化合并）"""
    id: str = Field(..., description="实体 ID（e1/e2...，图内稳定）")
    name: str = Field(..., description="实体名称（规范化：trim/全半角统一/数字间空格压缩）")
    type: str = Field(..., description="实体类型（人物/机构/技术/概念/事件/成果）")
    description: str = Field("", description="描述（多处出现时合并截断 200 字）")
    count: int = Field(1, description="出现次数（关联块数）")
    chunk_refs: List[GraphChunkRef] = Field(default_factory=list, description="出现位置引用（同块去重）")


class GraphRelation(BaseModel):
    """知识图谱关系（source/target 为实体 ID，按 source+target+type 合并）"""
    id: str = Field(..., description="关系 ID（r1/r2...）")
    source: str = Field(..., description="源实体 ID")
    target: str = Field(..., description="目标实体 ID")
    type: str = Field(..., description="关系类型（提出/开发/发明/启动/导致/影响/属于/相关）")
    description: str = Field("", description="描述")
    weight: float = Field(1.0, description="关系强度（关联块数）")
    chunk_refs: List[GraphChunkRef] = Field(default_factory=list, description="出处引用")


class GraphDocInfo(BaseModel):
    """图谱中文档摘要"""
    name: str = Field("", description="文档原始名")
    chunk_count: int = Field(0, description="构建时的切块数")


class KnowledgeGraph(BaseModel):
    """知识图谱查询响应（GET /api/kbs/{kb_id}/graph）"""
    kb_id: str = Field(..., description="知识库 ID")
    updated_at: str = Field("", description="最近构建时间")
    docs: Dict[str, GraphDocInfo] = Field(default_factory=dict, description="已构建图谱的文档摘要（{doc_id: {name, chunk_count}}）")
    entities: List[GraphEntity] = Field(default_factory=list)
    relations: List[GraphRelation] = Field(default_factory=list)


class Stats(BaseModel):
    """系统统计（阶段2 使用，模型先行预留）"""
    kb_count: int = 0
    doc_count: int = 0
    chunk_count: int = 0
    message_count: int = 0
