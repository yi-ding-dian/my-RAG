/**
 * API 层类型定义（与后端契约一致）。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 */

import type { User } from '../auth/token';

export type { User };

// ========== 知识库 / 文档 / 图谱 ==========

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  doc_count: number;
  chunk_count: number;
  created_at: string;
  department_id?: string | null; // 新增：所属部门，null=全局
  owner_id?: string | null; // 新增：创建人用户 id
  /** 向量状态摘要（维度冲突检测）：维度不匹配时前端提示重建向量 */
  vector_status?: {
    current_dim: number | null;
    model_dim: number | null;
    compatible: boolean;
  };
  /** 标签列表（≤10 个，每个 ≤20 字符） */
  tags?: string[];
}

/** 向量状态详情（GET /kbs/{id}/vector-status） */
export interface VectorStatus {
  kb_id: string;
  collection_vectors: number;
  current_dim: number | null;
  model_dim: number | null;
  compatible: boolean;
  message: string;
}

/** 重建任务状态（GET /kbs/{id}/rebuild-status） */
export interface RebuildTaskStatus {
  kb_id: string;
  task_id: string | null;
  running: boolean;
  done: number;
  total: number;
  failed: number;
  current_doc: string | null;
  finished_at: string | null;
  errors: { doc_id: string | null; doc_name: string | null; error: string }[];
}

export type DocumentStatus =
  | 'uploaded'
  /** 转换中：ppt/pptx 上传后先经文档转换服务转 PDF 的中间态（完成后 → uploaded） */
  | 'converting'
  | 'parsing'
  | 'parsed'
  | 'ingested'
  | 'failed'
  | 'pending_confirm';

export type ParseMethod = 'naive' | 'title' | 'regex' | 'parent_child' | 'qa' | 'agentic' | 'hierarchical';

/** 解析引擎：auto=自动（MinerU 优先，不可用自动降级；layout=DeepDOC 时走 DeepDoc）| mineru=强制 MinerU 高精度（PDF 混排）| deepdoc=强制 DeepDoc（RAGFlow，表格输出可检索 HTML，仅 PDF）| docx_struct=本地结构化解析（OOXML 直读，标题层级/自动编号保留，仅 docx/doc；doc 经 LibreOffice 转换）| plain=纯文本提取 */
export type ParserEngine = 'auto' | 'mineru' | 'deepdoc' | 'docx_struct' | 'plain';

/** MinerU 解析后端（mineru-api /file_parse backend 参数）：auto/不传=跟随服务端默认（hybrid-auto-engine）| hybrid-auto-engine=混合自动引擎（质量优：表格规范/OCR 准/流程图识别，速度稍慢）| pipeline=管线（快约 20s，表格可能错乱） */
export type MinerUBackend = 'auto' | 'hybrid-auto-engine' | 'pipeline';

/** PDF 版面识别引擎：MinerU=高精度（推荐）| DeepDOC=表格输出为可检索 HTML| PlainText=纯文本直提（pypdf/python-docx，无表格/图片识别） */
export type LayoutRecognize = 'MinerU' | 'DeepDOC' | 'PlainText';

/** 解析方式（合并解析引擎+版面识别，无自动档）：MinerU=高精度（默认）| DeepDOC=表格输出可检索 HTML（仅 PDF）| DocxStruct=结构解析（OOXML 直读，保留标题层级与自动编号，仅 docx/doc）| PlainText=纯文本直提（本地 pypdf/python-docx，恒可用） */
export type ParseMode = 'MinerU' | 'DeepDOC' | 'DocxStruct' | 'PlainText';

/** 解析语言：ch=中文 | en=英文 */
export type ParseLang = 'ch' | 'en';

/** 思考模式（DeepSeek thinking 控制，图谱抽取/上下文摘要 LLM 调用共用）：
 * disabled=关闭思考（推荐，简单延迟敏感任务更快更省 token）| enabled_low/
 * enabled_high/enabled_max=开启思考并指定强度（low/high/max） */
export type ThinkingMode = 'disabled' | 'enabled_low' | 'enabled_high' | 'enabled_max';

/**
 * 解析配置：naive=通用切块 | title=按标题切块 | regex=正则切块 | parent_child=父子分块
 * | hierarchical=层级聚合切块（按章节树自底向上聚合，块边界落在标题之间，块首自带
 * 祖先标题链；仅结构解析 docx_struct 产物可选，参数同 naive：chunk_size/overlap）
 * parent_* 字段仅在 method=parent_child 时使用（其他方式后端忽略，前端也不发送）
 * layout_recognize/pages 等 PDF 解析字段仅 PDF/混排文档场景有意义（简化：始终随表单发送当前值）
 */
export interface IngestConfig {
  method?: ParseMethod;
  /** 解析引擎（默认 auto，不传等价于自动探测降级） */
  parser_engine?: ParserEngine;
  /** MinerU 解析后端（仅 parser_engine=mineru 时发送；auto/不传=跟随服务端默认 hybrid-auto-engine） */
  backend?: MinerUBackend;
  chunk_size?: number;
  overlap?: number;
  delimiter?: string | string[];
  split_level?: number;
  regex_pattern?: string;
  /** 父块大小（字符），范围 200-4000，仅 parent_child */
  parent_chunk_size?: number;
  /** 父块重叠（字符），范围 0-500，仅 parent_child */
  parent_chunk_overlap?: number;
  /** 父块分割标题层级 H1-H6（1-6），仅 parent_child */
  parent_split_level?: number;
  /** 检索模式：parent=返回父块（推荐）| child=返回子块，仅 parent_child */
  retrieval_mode?: 'parent' | 'child';
  // ===== PDF 解析配置（仅 PDF/混排文档场景有意义） =====
  /** 版面识别引擎（默认 MinerU） */
  layout_recognize?: LayoutRecognize;
  /** 页码范围数组，如 [[1, 1000000]]，空值后端按默认全篇处理 */
  pages?: number[][];
  /** 任务页面大小（1-128，默认 12） */
  task_page_size?: number;
  /** 表格识别：识别并还原文档中的表格结构 */
  table_enable?: boolean;
  /** 公式识别：识别数学公式 */
  formula_enable?: boolean;
  /** 图片提取：提取文档图片并保存（存 MinIO） */
  return_images?: boolean;
  /** 包含父标题：切块时在块前补标题路径 */
  enable_heading_in_content?: boolean;
  /** 语言（默认 ch） */
  lang_list?: ParseLang;
  /** 上下文检索增强：开启后切块时对每个块调用 LLM 生成上下文摘要（向量化/检索文本加【上下文】前缀，产生额外 token 费用，失败/超时跳过不阻塞入库，默认关） */
  contextual_retrieval?: boolean;
  /** 知识图谱：开启后入库时用 LLM 对每个切块抽取实体与关系，合并构建知识图谱（存储 data/storage/graphs/{kb_id}.json，产生额外 token 费用，失败/超时跳过不阻塞入库，默认关） */
  knowledge_graph?: boolean;
  /** 图片摘要：解析后用多模态模型读出图内文字（证照/扫描件）写进正文，使其可被检索。
   *  默认关；未配置模型时后端预检会返回 400（不阻塞其他解析方式）。 */
  image_summary?: boolean;
  /** 图片摘要输出格式（仅 image_summary=true 时生效）：不传 = 跟随部门/全局配置的格式。
   *  选了与部门配置**不同**的格式时，提示词改用该格式的内置模板（部门自定义的那份
   *  是照旧格式写的，硬套会产出解析不了的内容）；部门配置不受影响，本选择也不持久化
   *  （重跑需重新选）。 */
  image_summary_format?: ImgSummaryFormat;
  /** 思考模式（DeepSeek thinking 控制，图谱抽取/上下文摘要调用共用）：disabled=关闭思考（默认，更快更省 token）| enabled_low/high/max=开启思考并指定强度 */
  thinking_mode?: ThinkingMode;
  /** Agentic 分块超限确认（仅 method=agentic）：文档 1 万~5 万字时后端要求确认，确认后带 true 重新提交（仅本次生效，不持久化） */
  agentic_confirm?: boolean;
  /** 解析 LLM 模型（上下文摘要/知识图谱抽取专用，值为系统配置 LLM 模型列表的 name；空=默认用当前激活对话模型，对话不受影响） */
  parse_llm_model?: string;
  /** QA 问答切块规范性强制继续（仅 method=qa）：true=跳过问答对占比检测直接入库（入库失败确认"继续入库"时提交） */
  qa_force_continue?: boolean;
}

export interface DocumentItem {
  id: string;
  kb_id: string;
  name: string;
  original_name: string;
  file_type: string;
  /** 由其他格式转换而来（ppt/pptx 在上传时转成 PDF）：原格式，
   *  用于列表标注"(原为 ppt)"；空/缺省 = 原生上传 */
  converted_from?: string | null;
  size: number;
  status: DocumentStatus;
  error?: string;
  chunk_count: number;
  parse_method: string; // 'mineru' | 'deepdoc' | 'plain' | 'spreadsheet'
  parser_id?: string; // 解析方式：'naive' | 'title' | 'regex'（列表/详情返回）
  parser_config?: Record<string, unknown>; // 解析参数（chunk_size/overlap/split_level/regex_pattern 等）
  created_at: string;
  updated_at: string;
  /** 是否已移入回收站（软删除标记，检索自动排除） */
  deleted?: boolean;
  /** 移入回收站时间（恢复后清空） */
  deleted_at?: string | null;
  /** 是否参与检索（false=已禁用：向量/BM25 召回与图谱增强都排除；缺失视为 true） */
  enabled?: boolean;
  /** 知识图谱状态：none=未构建/building=构建中/ready=已构建/failed=构建失败 */
  graph_status?: 'none' | 'building' | 'ready' | 'failed';
  /** 图谱构建失败原因（graph_status=failed 时返回） */
  graph_error?: string;
  /** 入库流程轨迹（[{stage, ms, status?}]；可追溯展示；旧文档无=空） */
  ingest_trace?: { stage: string; ms: number; status?: string }[];
  /** 入库总耗时（ms；可追溯展示；旧文档无） */
  ingest_total_ms?: number | null;
  /** 入库开始时间（HH:mm:ss；可追溯展示；旧文档无） */
  ingest_started_at?: string | null;
  /** 入库结束时间（HH:mm:ss；完成/失败/取消时刻） */
  ingest_finished_at?: string | null;
}

/** 解析方式友好名（契约：naive→通用切块 / title→按标题切块 / regex→正则切块 / parent_child→父子分块 / qa→QA 问答 / agentic→Agentic 智能分块 / hierarchical→层级聚合切块） */
export const methodLabel = (method: string): string => {
  switch (method) {
    case 'naive':
      return '通用切块';
    case 'title':
      return '按标题切块';
    case 'regex':
      return '正则切块';
    case 'parent_child':
      return '父子分块';
    case 'qa':
      return 'QA 问答';
    case 'agentic':
      return 'Agentic 智能分块';
    case 'hierarchical':
      return '层级聚合切块';
    default:
      return method;
  }
};

/** 解析方式（parse_method）友好名：mineru/deepdoc/normative/plain/spreadsheet */
export const parseMethodLabel = (method: string | null | undefined): string => {
  switch (method) {
    case 'mineru':
      return 'MinerU 版面识别';
    case 'deepdoc':
      return 'DeepDoc 版面识别';
    case 'normative':
      return '规范条文解析';
    case 'plain':
      return '纯文本提取';
    case 'spreadsheet':
      return '表格解析';
    default:
      return method || '-';
  }
};

/** 解析方式对应 Tag 颜色（契约：通用=blue / 按标题=geekblue / 正则=purple / 父子分块=magenta / QA 问答=cyan / Agentic=gold / 层级聚合=green） */
export const methodColor = (method: string): string => {
  switch (method) {
    case 'naive':
      return 'blue';
    case 'title':
      return 'geekblue';
    case 'regex':
      return 'purple';
    case 'parent_child':
      return 'magenta';
    case 'qa':
      return 'cyan';
    case 'agentic':
      return 'gold';
    case 'hierarchical':
      return 'green';
    default:
      return 'default';
  }
};

/**
 * 文档详情：真实后端返回 chunk_preview（string[]），契约约定 chunks（{index,text}[]，含
 * char_start/char_end 偏移）与 full_text（文档全文），前端同时兼容三种形态。
 */
export interface DocumentDetail {
  id: string;
  name: string;
  status: DocumentStatus;
  chunks?: Array<{ index: number; text: string; char_start?: number; char_end?: number; context?: string | null; label?: string | null; [k: string]: unknown }>;
  /** 文档全文（切块对比视图用），可选兼容 */
  full_text?: string;
  chunk_preview?: string[];
  /**
   * 解析产物标题列表（目录树**唯一**数据源）：识别在后端完成，与切块同一口径
   * （含 MinerU 漏标 `#` 的裸编号标题）。前端不得另写一套抽取逻辑。
   */
  headings?: ApiHeading[];
  [k: string]: unknown;
}

/** 后端下发的解析产物标题（对应 models.ParsedHeading） */
export interface ApiHeading {
  /** 标题层级（1~6；编号体系下为文档内相对层级） */
  level: number;
  /** 标题文本（不含 # 前缀） */
  text: string;
  /** 标题行在全文中的起始偏移（目录跳转锚点） */
  pos: number;
  /** 标题行结束偏移（不含换行符） */
  end: number;
  /** 标题行原文（含 # 前缀；预览整行渲染用） */
  raw: string;
}

/** 文档结构树条目（「查看文档结构」弹窗）：结构解析产物里的一个标题 */
export interface DocxOutlineItem {
  /** 标题层级（1=一级标题，最大 6） */
  level: number;
  /** 标题文本（Word 自动编号已还原，如 "1.1 安装说明"） */
  title: string;
}

/** 「查看文档结构」响应（GET /kbs/{kbId}/documents/{docId}/docx-outline）：
 *  仅 docx/doc 文档有效；warning 非空表示提取异常（items 通常为空） */
export interface DocxOutlineResponse {
  items: DocxOutlineItem[];
  /** 标题总数 */
  count: number;
  /** 最大层级（0=未检测到标题） */
  max_level: number;
  /** 提取异常提示（正常为 null） */
  warning: string | null;
}

export interface Source {
  id: string;
  text: string;
  score: number;
  document_id: string;
  document_name: string;
  /** 来源文档所属知识库 ID（引用溯源用；历史会话快照可能缺失，前端以当前活跃 kb 兜底） */
  kb_id?: string;
  /** 来源文档所属知识库名称（多知识库对比检索时由后端填充） */
  kb_name?: string;
  chunk_index: number;
  /** 父子分块命中时返回的父块全文（供引用展示），可选 */
  parent_text?: string;
  /** 上下文摘要（上下文检索增强开启时生成；引用 text 已含【上下文】前缀，此字段供标签展示与引用拼接），可选 */
  context?: string;
  /** 原始向量检索分数（混合模式调试用；纯向量模式=score；BM25 单独命中为 null） */
  vector_score?: number | null;
  /**
   * score 的口径（后端下发）：rerank=重排相关度 / rrf=混合检索融合分 /
   * vector=向量 cos 相似度。三大量纲不可比，展示层据此选文案。
   * 历史会话快照无此字段（undefined）= 老数据，降级按 vector_score 展示。
   */
  score_type?: 'rerank' | 'rrf' | 'vector' | null;
  /** 块字符起始偏移（相对文档解析全文，检索测试上下文截取用；-1=历史数据无偏移） */
  char_start?: number;
  /** 块字符结束偏移（开区间，相对文档解析全文；-1=历史数据无偏移） */
  char_end?: number;
}

export interface ChatSession {
  id: string;
  kb_id: string;
  title: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}

/** 会话详情（GET /chat/history/{id}）：含完整消息与引用快照，用于回溯问答现场 */
export interface ChatSessionDetail {
  id: string;
  kb_id: string;
  /** 归属用户 ID（旧会话无此字段 = super_admin 归属） */
  user_id?: string | null;
  title: string;
  messages: ChatMessage[];
  created_at: string;
  updated_at: string;
  /** 内容来自归档目录 = 该会话已被用户删除（超管回溯时据此提示） */
  archived?: boolean;
  /** 归档时已裁剪：只保留了被反馈轮次及其上下文，非完整会话（回溯时提示） */
  trimmed?: boolean;
}

/** 本次问答实际生效的生成参数快照（详情追溯用；model 为空 = 本次未调用模型） */
export interface GenParams {
  model?: string | null;
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
  /** 思考模式：disabled=关闭思考 / enabled_low|enabled_high|enabled_max=开启 */
  thinking_mode?: string;
  enable_multi_turn?: boolean;
  history_rounds?: number;
  kg_enhance?: boolean;
  agentic_enabled?: boolean;
  retrieval?: {
    top_k?: number;
    similarity_threshold?: number | null;
    enable_hybrid?: boolean;
    enable_rerank?: boolean;
  };
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  sources?: Source[];
  /** 请求详情：后端 prompt 事件下发的完整 messages 数组（发给 LLM 的提示词） */
  prompt?: unknown;
  /** 请求详情：召回耗时（后端统计，毫秒） */
  retrieval_ms?: number;
  /** 请求详情：图谱构建耗时（后端统计，毫秒） */
  kg_ms?: number;
  /** 请求详情：查询改写耗时（后端统计，毫秒；未触发改写为 0） */
  rewrite_ms?: number;
  /** 请求详情：改写后的检索查询（原问题未改写时为 null） */
  rewritten_query?: string | null;
  /** 请求详情：提问 → AI 生成首字总耗时（前端计算，毫秒） */
  total_ms?: number;
  /** 请求详情：Agentic 检索决策轨迹（改写查询/分档分数/尝试次数；未开启时无） */
  agentic?: AgenticTrace;
  /** 生成参数快照（模型/温度/思考模式/检索参数；旧数据无此字段） */
  gen_params?: GenParams;
  created_at?: string;
  /** 前端会话状态专用：用户点击「停止」中断生成（仅 UI 标注，不落盘） */
  stopped?: boolean;
}

/** Agentic 检索决策轨迹（SSE event:agentic + 会话落盘 agentic 字段同构） */
export interface AgenticTrace {
  /** 用户原始提问 */
  original_query: string;
  /** 最终采用的检索查询（改写后可能与原提问不同） */
  final_query: string;
  /** 每轮检索决策：尝试次数/查询/最高相似度/来源数/是否改写 */
  trace: Array<{
    attempt: number;
    query: string;
    best_score: number | null;
    sources_count: number;
    from_rewrite: boolean;
    rewrite_failed: boolean;
  }>;
}

// ========== 认证 / 用户 / 部门 ==========

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export interface UserCreateInput {
  username: string;
  password: string;
  display_name: string;
  role: 'super_admin' | 'dept_admin' | 'user';
  department_id?: string | null;
}

/** 更新用户：传哪个字段改哪个（含 status 与 password 重置） */
export type UserUpdateInput = Partial<Omit<UserCreateInput, 'username'>> & {
  status?: 'active' | 'disabled';
};

export interface Department {
  id: string;
  name: string;
  description: string;
  created_at: string;
}

export interface DepartmentInput {
  name: string;
  description?: string;
}

// ========== 用户画像 / 偏好记忆 ==========

/** 画像条目类型：profile=身份/背景事实 | preference=偏好 */
export type UserMemoryType = 'profile' | 'preference';

/** 用户画像条目（GET /api/users/{user_id}/memory 条目） */
export interface UserMemoryItem {
  id: string;
  type: UserMemoryType;
  content: string;
  /** 置信度 0-1（LLM 提取条目；手动编辑保留原值） */
  confidence: number;
  created_at: string;
  updated_at: string;
}

/** 用户画像响应（GET /api/users/{user_id}/memory） */
export interface UserMemory {
  user_id: string;
  /** 个性化开关（关=不注入 system prompt；对话结束仍定时提取） */
  memory_enabled: boolean;
  updated_at: string;
  items: UserMemoryItem[];
}

/** 更新载荷（PUT /api/users/{user_id}/memory；enabled 与 items 至少传一个） */
export interface UserMemoryUpdate {
  enabled?: boolean;
  /** 全量替换（带 id=更新既有条目，无 id=新增） */
  items?: Array<{ id?: string; type: UserMemoryType; content: string }>;
}

// ========== 知识库标签 / 向量 / 解析 ==========

/** 标签聚合条目（GET /kbs/tags，count 降序） */
export interface TagCount {
  name: string;
  count: number;
}

/** 触发入库响应：degrade 为解析前探测到的自动降级提示（所选解析器不可用），可选 */
export interface IngestResult {
  message: string;
  status: string;
  doc_id: string;
  degrade?: string;
}

/** 单个解析器可用性（探测失败=不可用+用户可读原因） */
export interface ParserStatusEntry {
  available: boolean;
  reason?: string;
}

/** GET /kbs/parsers/status 响应：mineru/deepdoc 并行探测（≤8s），plain 恒可用 */
export interface ParserStatus {
  mineru: ParserStatusEntry;
  deepdoc: ParserStatusEntry;
  plain: ParserStatusEntry;
}

/** 篇幅画像：总字符数 + 2 万阈值（实时读系统配置，超管可改，每次读取） */
export interface AnalyzeLength {
  doc_chars: number;
  threshold_chars: number;
  over_threshold: boolean;
  doc_label: string;
  threshold_label: string;
  paragraphs: number;
}

/** 标题结构画像：# 标题/包裹式/前导符号式 + 编号式补充（第X章/X.X/一、） */
export interface AnalyzeStructure {
  has_headings: boolean;
  heading_count: number;
  numbered_headings: number;
  examples: string[];
  /** 检测到的标题编号体系（按命中数降序；切块层级推断用，自动启用） */
  heading_systems?: {
    system: string;
    label: string;
    hits: number;
    examples: string[];
  }[];
  /** 建议启用的体系（按语义优先级排序，入库自动使用） */
  suggested_systems?: string[];
  /** docx/doc 结构探测（仅 docx/doc 文档返回；规范性判定用）：样式标题
   *  覆盖率（outlineLvl/Heading 样式/numPr）+ 编号体系命中 */
  docx_structure?: {
    /** 样式标题段落数（outlineLvl / Heading 样式 / numPr 之一） */
    style_headings: number;
    /** 非空段落总数 */
    total_paragraphs: number;
    /** 样式标题覆盖率（style_headings / total_paragraphs） */
    style_ratio: number;
    /** 样式标题的编号体系命中（与 heading_systems 同构） */
    style_heading_systems: {
      system: string;
      label: string;
      hits: number;
      examples: string[];
    }[];
    /** 是否规范文档（样式标题 >= 3 且覆盖率 >= 2%）：建议结构化解析 */
    is_normative: boolean;
  };
}

/** QA 格式画像：问答对/总段落 + 占比（>=50% 判定 is_qa，与入库检测同口径） */
export interface AnalyzeQa {
  qa_pairs: number;
  total_paragraphs: number;
  ratio: number;
  is_qa: boolean;
}

/** 指代密集度画像：指代词频率按字数归一化（次/千字）→ low/mid/high */
export interface AnalyzeReferenceDensity {
  count: number;
  per_1000_chars: number;
  level: 'low' | 'mid' | 'high';
  level_label: string;
}

/** 引擎建议：基于文件类型 + 解析器可用性探测（probe 为探测结果，可空） */
export interface EngineSuggestion {
  suggested: string; // mineru | deepdoc | docx_struct | plain | auto
  reason: string;
  probe?: ParserStatus | null;
}

/** 切块方式推荐项（chunk_method 为主推荐，alternatives 为备选/可选） */
export interface MethodRecommendation {
  method: ParseMethod;
  label: string;
  recommended: boolean;
  reason: string;
  /** 徽标文案：推荐 / 可选 / 备选（**后端给出**，前端不再自行判断该标谁） */
  badge?: string;
}

/** 一条决策：入库配置里一个字段的取值、来源与中文理由（后端 ParsePlan.decisions）
 *
 *  source：rule=规则命中 / engine=引擎联动 / profile=画像 / default=缺省 / limit=上限回退
 *  billable=true 表示这是花钱的 LLM 环节，是否开启由用户按预算决定 */
export interface PlanDecision {
  key: string;
  value: unknown;
  reason: string;
  source: string;
  billable: boolean;
}

/** 单个 LLM 环节的成本（**原始单位**：调用次数 + 字符数 + 图片张数）
 *
 *  不做 token 换算——各模型词表不同（同一汉字 0.6~2 token），换算留给展示层按
 *  本模型实测系数来。calls 是主计量（成本 = 次数 × 单价）；未开启的环节也给
 *  "开启后会是多少"，前端做开关预览时零请求。 */
export interface CostItem {
  /** contextual_retrieval / knowledge_graph / image_summary / agentic / heading_levels */
  key: string;
  label: string;
  calls: number;
  input_chars: number;
  output_chars: number;
  images: number;
  /** 估算口径（可直接做 tooltip），如"每块重发全文 ≤2.0 万字 × 26 块" */
  detail: string;
  /** false = 对本文档不可开启（如超阈值的上下文检索会让整文档入库失败） */
  available: boolean;
}

export interface CostEstimate {
  chunks: number;
  enabled: Record<string, boolean>;
  items: CostItem[];
  /** 以下 total_* 只对**已开启**项求和 */
  total_calls: number;
  total_input_chars: number;
  total_output_chars: number;
  total_images: number;
  notes: string[];
}

/** 入库方案（后端唯一真相源）：推荐 → 入库配置的唯一出口
 *
 *  三处曾经各说各话的数据（Step1 面板 / Step2 徽标 / Step2 默认选中）都从它派生，
 *  结构上不可能再打架。config 可直接作为 ingest 请求体提交。 */
export interface ParsePlan {
  config: IngestConfig;
  decisions: PlanDecision[];
  cost: CostEstimate;
  alternatives: MethodRecommendation[];
  /** 增强开关的推荐值（前端 Step3 初值） */
  switches: Record<string, boolean>;
  engine: string;
  warnings: string[];
}

/** GET /kbs/ingest-defaults 响应：前端表单默认值与合法范围的唯一来源
 *
 *  存在的意义：前端曾把 800/100、512/50 这组默认值硬编码在 5 处且与后端活跃配置
 *  漂移。改为接口供给后，前端只做展示与用户覆盖。 */
export interface IngestDefaults {
  methods: Record<string, IngestConfig>;
  ranges: Record<string, [number, number]>;
  retrieval_modes: string[];
  agentic: { confirm_chars: number; hard_chars: number };
  parser_defaults: Record<string, unknown>;
}

/** GET /kbs/{kb_id}/documents/{doc_id}/analyze 响应（画像 + 推荐，任何一步
 *  失败不影响整体：部分画像 + warnings，接口恒 200） */
export interface AnalyzeResult {
  doc_id: string;
  file_type: string;
  extracted: boolean;
  extract_warning: string | null;
  engine_suggestion: EngineSuggestion;
  length: AnalyzeLength;
  structure: AnalyzeStructure;
  qa: AnalyzeQa;
  reference_density: AnalyzeReferenceDensity;
  recommendations: {
    chunk_method: MethodRecommendation;
    alternatives: MethodRecommendation[];
    contextual_retrieval: { recommended: boolean; reason: string };
    enable_heading_in_content: boolean;
  };
  warnings: string[];
  /** 入库方案（唯一真相源）：向导与批量智能模式的配置都从这里取。
   *  后端决策矩阵异常时为 null（部分画像 + warnings，接口仍 200）。 */
  parse_plan?: ParsePlan | null;
  /** 表格文档画像（xlsx/xls/csv，非表格文档无此字段） */
  spreadsheet?: {
    sheet_count: number;
    sheets: { name: string; rows: number; cols: number }[];
    total_rows: number;
    merged_cells: number;
  };
}

/** 分页响应结构（后端带 page/page_size 参数时返回） */
export interface DocumentPage {
  total: number;
  page: number;
  page_size: number;
  items: DocumentItem[];
}

/** 超管全局文档条目：文档字段 + 知识库/部门归属（GET /api/admin/documents） */
export interface GlobalDocumentItem extends DocumentItem {
  /** 所属知识库名称（文档的 kb 已删除时后端回退 kb_id） */
  kb_name: string;
  /** 所属部门 ID（null=未分配部门） */
  department_id: string | null;
  /** 所属部门名称（null=未分配部门） */
  department_name: string | null;
}

/** 全局文档分页响应（先过滤后分页，total=过滤后数量） */
export interface GlobalDocumentPage {
  total: number;
  page: number;
  page_size: number;
  items: GlobalDocumentItem[];
}

/** 部门汇总条目（/api/admin/documents/summary）：部门 → 知识库 → 文档数 */
export interface DepartmentSummaryEntry {
  department_id: string;
  department_name: string;
  doc_count: number;
  kb_count: number;
  kbs: {
    kb_id: string;
    kb_name: string;
    doc_count: number;
    chunk_count: number;
  }[];
}

/** 部门汇总响应 */
export interface DepartmentSummaryResponse {
  total: number;
  items: DepartmentSummaryEntry[];
}

/** 实体/关系在文档中的引用位置（chunk_index 为 chunks_meta 下标；偏移相对文档解析全文，与 chunks_meta 契约一致） */
export interface GraphChunkRef {
  doc_id: string;
  chunk_index: number;
  char_start: number;
  char_end: number;
}

/** 知识图谱实体（入库时 LLM 从切块抽取，按 name+type 规范化合并） */
export interface GraphEntity {
  id: string;
  name: string;
  /** 人物/机构/技术/概念/事件/成果 */
  type: string;
  description: string;
  /** 出现次数（关联块数） */
  count: number;
  chunk_refs: GraphChunkRef[];
}

/** 知识图谱关系（source/target 为实体 ID） */
export interface GraphRelation {
  id: string;
  source: string;
  target: string;
  /** 提出/开发/发明/启动/导致/影响/属于/相关 */
  type: string;
  description: string;
  /** 关系强度（关联块数） */
  weight: number;
  chunk_refs: GraphChunkRef[];
}

/** 知识图谱查询响应（GET /api/kbs/{kb_id}/graph，doc_id 可选过滤单文档） */
export interface KnowledgeGraph {
  kb_id: string;
  updated_at: string;
  docs: Record<string, { name: string; chunk_count: number }>;
  entities: GraphEntity[];
  relations: GraphRelation[];
}

// ========== 对话 ==========

export interface StreamChatParams {
  kb_id: string;
  query: string;
  session_id?: string;
  top_k?: number;
}

/** Agentic 决策进度阶段（SSE event:agentic_status，前端按阶段换提示文案） */
export type AgenticStatusStage = 'retrieving' | 'rewriting' | 'rechecking';

/** Agentic 决策进度事件 */
export interface AgenticStatus {
  stage: AgenticStatusStage;
  /** 检索轮次（retrieving/rechecking 附带：第几轮检索） */
  attempt?: number;
  /** 改写后的查询（rechecking 附带，展示"已改写为…"） */
  query?: string;
}

export interface StreamCallbacks {
  /** 收到 event:meta，携带检索来源 */
  onMeta?: (sources: Source[]) => void;
  /** 收到 event:prompt，携带完整提示词与检索/图谱/改写耗时（请求详情用） */
  onPrompt?: (info: { prompt: unknown[]; retrieval_ms?: number; kg_ms?: number; rewrite_ms?: number; rewritten_query?: string | null }) => void;
  /** 收到 event:agentic，携带 Agentic 检索决策轨迹（默认关闭时不收到） */
  onAgentic?: (info: AgenticTrace) => void;
  /** 收到 event:agentic_status，携带检索/改写/重检进度（默认关闭时不收到） */
  onAgenticStatus?: (info: AgenticStatus) => void;
  /** 收到 event:delta，增量文本 */
  onDelta?: (text: string) => void;
  /** 收到 event:done（gen_params = 本次实际生效的生成参数，供「详情」追溯） */
  onDone?: (info: {
    session_id: string;
    message_count: number;
    gen_params?: GenParams;
  }) => void;
  /** 收到 event:error 或网络错误（用户主动停止时 message 为 '已停止'） */
  onError?: (message: string) => void;
}

/** 检索调试参数（全可选：不传即跟随配置，与既有行为一致） */
export interface RetrieveChatParams {
  /** 单库检索（与 kb_ids 二选一；都传时后端以 kb_ids 优先） */
  kb_id?: string;
  /** 多知识库对比检索（1-5 个：每库独立检索后合并按分数降序取全局 top_k） */
  kb_ids?: string[];
  query: string;
  /** 返回条数（默认取配置） */
  top_k?: number;
  /** 混合检索开关：不传=跟随配置；true/false=强制开关（对比实验） */
  enable_hybrid?: boolean;
  /** 重排开关：不传=跟随配置；true/false=强制开关（对比实验） */
  enable_rerank?: boolean;
  /** 相似度阈值覆盖：不传=用配置默认；给定则覆盖（0-1），调试阈值影响 */
  similarity_threshold?: number;
}

// ========== 统计 / RAGAS ==========

export interface Stats {
  kb_count: number;
  doc_count: number;
  chunk_count: number;
  session_count: number;
  message_count: number;
}

export interface RagasTask {
  id: string;
  name: string;
  dataset_id?: string;
  dataset_name?: string;
  status: string;
  progress?: number;
  metrics?: string[];
  error?: string;
  created_at?: string;
  completed_at?: string;
  /** 以下为知识库本地发起任务合并的元数据（非本地任务无此字段） */
  kb_name?: string;
  source?: 'logs' | 'chat';
  sample_count?: number;
  /** 发起人 user id（取消按钮按当前用户比对显隐；旧任务无此字段） */
  user_id?: string;
}

export interface RagasSample {
  question: string;
  answer: string;
  scores: Record<string, number | null>;
}

export interface RagasReport {
  task_id: string;
  task_name: string;
  status: string;
  aggregate: {
    scores: Record<string, number>;
    count: number;
  };
  results: RagasSample[];
  created_at?: string;
  completed_at?: string;
}

export interface RagasStatus {
  available: boolean;
  base_url?: string;
  tasks?: RagasTask[];
  message?: string;
}

/** 手动填写的一条测试集样本（question/ground_truth 必填；answer 缺省后端自动=ground_truth） */
export interface RagasSampleInput {
  question: string;
  /** 答案（可选，缺省时后端把 ground_truth 同时写入 answer） */
  answer?: string;
  /** 正确答案/参考答案（用户填的正确答案即 ground_truth） */
  ground_truth?: string;
}

/** 从知识库发起 RAGAS 评估请求体 */
export interface RagasEvaluationRequest {
  kb_id: string;
  /** 评估指标（默认：需 ground_truth 的上下文召回率/答案正确性/答案相似度） */
  metrics?: string[];
  /** 自动采样样本数 1~100，默认 20（传 samples 时忽略） */
  sample_count?: number;
  /** 自动采样来源：logs=检索日志真实问题（无答案）；chat=会话问答（问题+答案） */
  sample_source?: 'logs' | 'chat';
  /** 检索上下文 top_k 1~20，默认 3 */
  top_k?: number;
  /** 手动测试集 1~100 条；传了 samples 时优先于自动采样 */
  samples?: RagasSampleInput[];
  /** 预览模式：仅采样返回样本列表，不发起评估（从聊天历史导入用） */
  preview?: boolean;
}

/** 预览模式返回：样本列表（前端填充测试集表单后可编辑） */
export interface RagasEvaluationPreview {
  samples: RagasSampleInput[];
}

export interface RagasEvaluationResult {
  task_id: string;
  kb_id: string;
  kb_name: string;
  sample_count: number;
  dataset_id: string;
  name: string;
}

/** RAGAS 发起前可用性探测结果（LLM / Embedding 各自） */
export interface RagasProbeResult {
  available: boolean;
  /** 不可用时的中文原因（探测失败也返回，不抛异常） */
  reason: string;
}

/** GET /api/stats/ragas/precheck：发起评估前检测 LLM/Embedding 可用性 */
export interface RagasPrecheck {
  llm: RagasProbeResult;
  embedding: RagasProbeResult;
}

/** 检索质量统计（近 30 天） */

export interface RetrievalHitDoc {
  doc_id: string;
  doc_name: string;
  hits: number;
}

export interface RetrievalZeroHitDoc {
  doc_id: string;
  doc_name: string;
  chunks: number;
}

export interface RetrievalDaily {
  date: string;
  retrievals: number;
  /** 当日命中率：有命中的检索数 / 当日检索总数（0~1） */
  hit_rate: number;
}

export interface RetrievalQuality {
  kb_id: string;
  window_days: number;
  total_retrievals: number;
  avg_hits_per_retrieval: number;
  hit_docs: RetrievalHitDoc[];
  zero_hit_docs: RetrievalZeroHitDoc[];
  daily: RetrievalDaily[];
}

// ========== 服务配置档案 / 模型 ==========

/** LLM 模型条目（多模型列表中的单个模型；timeout 为条目完整配置的一部分） */
export interface LLMModelItem {
  /** 显示名（唯一） */
  name: string;
  base_url: string;
  api_key: string; // 服务端返回脱敏值（sk-****abcd）
  model: string;
  temperature: number;
  max_tokens: number;
  timeout: number;
}

/**
 * llm 段：模型列表 + 激活索引。
 * 激活模型（models[active]）用于问答/上下文摘要/评估 Judge 等全部 LLM 场景。
 */
export interface LLMConfig {
  models: LLMModelItem[];
  active: number;
}

/**
 * 图片解析模型条目（多模态，用于解析时生成图片摘要）。
 *
 * 与 LLMModelItem 的区别：不含 temperature/max_tokens——摘要用固定生成参数
 * （低温、限长），不需要按模型调。
 */
export interface VisionModelItem {
  /** 显示名（唯一）；部门管理员按它选择用哪个模型 */
  name: string;
  base_url: string;
  api_key: string; // 服务端返回脱敏值（sk-****abcd）
  model: string;
  timeout: number;
}

/** vision 段：模型列表 + 激活索引（结构同 llm 段） */
export interface VisionConfig {
  models: VisionModelItem[];
  active: number;
}

/**
 * 图片摘要配置。
 * - 全局档案里是默认值；部门可在「部门配置」里覆盖 model/prompt/选项/上限
 * - options 键：label_type / read_text / describe_scene / describe_layout
 */
/** 图片摘要输出格式：fields=固定字段（检索命中率最高）/ prose=段落描述 / brief=一句话简介 */
export type ImgSummaryFormat = 'fields' | 'prose' | 'brief';

/** 图片摘要提示词预览（GET /settings/image-summary/prompt）
 *  prompt 是**本次实际会发给模型的那份**（后端算好，前端只展示） */
export interface ImageSummaryPromptResult {
  prompt: string;
  /** custom=部门自定义提示词 / default=内置模板 */
  source: 'custom' | 'default';
  /** 本次实际生效的格式（未指定时为部门配置的格式） */
  output_format: ImgSummaryFormat;
}

export interface ImageSummaryConfig {
  /** 选中的模型 name（空 = 用列表第一个） */
  model?: string;
  /** 提示词（空 = 用内置默认模板） */
  prompt?: string;
  /** fields（固定字段，默认）/ prose（自然段）/ brief（一句话简介） */
  output_format?: string;
  /** 「文字」字段长度上限 */
  text_max_chars?: number;
  /** 单文档摘要张数上限（0 = 不限） */
  max_images?: number;
  options?: Record<string, boolean>;
}

export interface EmbeddingConfig {
  base_url: string;
  api_key: string; // 服务端返回脱敏值
  model: string;
  dimension: number;
}

export interface MinerUConfig {
  url: string;
  timeout: number;
}

/** DeepDoc 解析服务配置（RAGFlow API；表格输出为可检索 HTML） */
export interface DeepDocConfigProfile {
  base_url: string;
  email: string;
  password: string; // 服务端返回脱敏值
  timeout: number;
  dataset_prefix: string;
}

/** Gotenberg 文档转换服务配置（Office → PDF）
 *
 *  用途：老版 .doc/.docx 先转 PDF 再交 MinerU——MinerU 的主场是 PDF（按版面
 *  还原标题层级），直接给 docx 时标题会全丢。地址留空 = 不做转换。 */
export interface GotenbergConfigProfile {
  base_url: string;
  timeout: number;
}

export interface RetrievalConfig {
  top_k: number;
  /** 相似度阈值（0-1，默认 0=不过滤） */
  similarity_threshold?: number;
  /** 混合检索开关：BM25 关键词 + 向量 RRF 融合（默认开启；关闭=纯向量） */
  enable_hybrid?: boolean;
  /** Rerank 重排序配置（enabled 且 base_url/model 非空才生效） */
  rerank?: {
    enabled?: boolean;
    base_url?: string;
    model?: string;
    api_key?: string;
    top_n?: number;
  };
}

export interface ChunkingConfig {
  chunk_size: number;
  overlap: number;
  /** 标题分层模型（可选）：层级聚合切块给解析产物标题重新分层用的 LLM 模型
   *  （值为模型列表里的标识 name/model；空=不做 LLM 分层，只用规则）；
   *  旧后端可能缺失，前端做可选兼容 */
  heading_llm_model?: string;
}

/** 会话参数（聊天设置弹窗可编辑段，temperature/top_p/max_tokens 为 null=跟随模型默认） */
export interface ChatConfig {
  /** 温度（0-2，null=跟随模型默认） */
  temperature?: number | null;
  /** Top P（0-1，null=跟随模型默认） */
  top_p?: number | null;
  /** 最大输出 Token（null=跟随模型默认） */
  max_tokens?: number | null;
  /** 多轮对话（默认 true） */
  enable_multi_turn?: boolean;
  /** 历史轮数 */
  history_rounds?: number;
  /** 自定义系统提示词（空串=使用内置默认模板；可含 {refs} 占位符替换为检索引用内容） */
  system_prompt?: string;
  /** 思考模式（聊天问答）：disabled=关闭思考（默认，更快更省 token）| enabled_low/high/max=开启思考并指定强度（在线 DeepSeek 生效；本地 Qwen 模型开启时保持模型默认思考） */
  thinking_mode?: ThinkingMode;
  /** 单条输入（问题/检索 query）最大长度（字，默认 2000） */
  max_query_len?: number;
}

export interface MySQLConfigProfile {
  host: string;
  port: number;
  user: string;
  password: string; // 服务端返回脱敏值
  database: string;
  url?: string; // 覆盖连接串（契约未列出，后端存在该字段，做可选兼容）
}

export interface MinIOConfigProfile {
  endpoint: string;
  access_key: string;
  secret_key: string; // 服务端返回脱敏值
  bucket: string;
  secure: boolean;
  region: string;
}

export interface ServiceProfile {
  id: string;
  name: string;
  active: boolean;
  llm: LLMConfig;
  embedding: EmbeddingConfig;
  mineru: MinerUConfig;
  /** DeepDoc 段（旧档案可能缺失，前端做可选兼容） */
  deepdoc?: DeepDocConfigProfile;
  /** 文档转换段（Gotenberg，Office → PDF；旧档案可能缺失，前端做可选兼容） */
  gotenberg?: GotenbergConfigProfile;
  retrieval: RetrievalConfig;
  chunking: ChunkingConfig;
  /** 上下文检索增强配置（完整文档视角阈值，字；旧后端可能缺失，前端做可选兼容） */
  contextual_retrieval?: { max_full_doc_chars?: number };
  /** 入库配置：同时解析入库的并发（1~10）、单库文档上限（0=不限）与单文件上传上限（MB，默认 100）；旧后端可能缺失，前端做可选兼容 */
  ingestion?: { concurrency?: number; kb_doc_limit?: number; max_upload_mb?: number };
  /** 节点向量存储段（旧档案可能缺失，前端做可选兼容；backend=chroma|milvus） */
  vector_store?: { backend?: string; milvus_uri?: string };
  /** 图片解析模型列表段（多模态；超管配置，部门从中选一个用） */
  vision?: VisionConfig;
  /** 图片摘要配置（全局默认值；部门可在部门配置里覆盖） */
  image_summary?: ImageSummaryConfig;
  /** 会话参数段（旧后端可能缺失，前端做可选兼容） */
  chat?: ChatConfig;
  mysql: MySQLConfigProfile;
  minio: MinIOConfigProfile;
}

/** 创建/更新/测试连接时提交的档案（部分字段可选） */
export type ServiceProfileInput = Partial<ServiceProfile> & { name?: string };

export interface ConnectionTestResult {
  ok: boolean;
  latency_ms: number;
  message: string;
}

export interface ProfileTestResult {
  llm: ConnectionTestResult;
  embedding: ConnectionTestResult;
  mineru: ConnectionTestResult;
  deepdoc: ConnectionTestResult;
  /** 文档转换（Gotenberg，Office → PDF）：GET {base_url}/health 探活 */
  gotenberg?: ConnectionTestResult;
  rerank: ConnectionTestResult;
  mysql: ConnectionTestResult;
  minio: ConnectionTestResult;
  vector_store: ConnectionTestResult;
  /** 图片解析模型（多模态）：GET {base_url}/models 探活 */
  vision?: ConnectionTestResult;
}

/** 单个 LLM 模型连接测试（GET {base_url}/models，≤5s；勾选激活时先调用） */
export interface LlmTestResult {
  ok: boolean;
  reason: string;
  latency_ms: number;
}

/** 解析配置弹窗模型列表条目（仅名称+model，不含 api_key 等敏感字段） */
export interface ParserLlmModelItem {
  name: string;
  model?: string;
}

/** GET /api/settings/llm/models 响应：模型列表 + 激活索引（前端标注"当前使用"） */
export interface LlmModelList {
  models: ParserLlmModelItem[];
  active: number;
}

/** 部门级 LLM 配置（白名单 6 字段；api_key 对外恒为脱敏值） */
export interface DeptLlmConfig {
  base_url?: string;
  /** 脱敏值（如 sk-d****l-key / ****）：POST 传 "****" = 保留原值；空串 = 跟随全局 */
  api_key?: string;
  model?: string;
  temperature?: number | null;
  max_tokens?: number | null;
  timeout?: number | null;
}

/**
 * 聊天设置 + LLM 配置载荷（后端白名单：chat 段 7 字段 +
 * retrieval.top_k/similarity_threshold + llm 段 6 字段）
 */
export interface ChatSettingsPayload {
  /** 图片摘要（部门可覆盖段）：选模型 + 提示词 + 输出格式 + 两个上限。
   *  提交时可选；GET 返回「全局默认 + 本部门覆盖」的合并值，
   *  并额外返回 vision_options（超管配的模型名列表，不含连接信息）。 */
  image_summary?: ImageSummaryConfig;
  /** 提交时可选：只提交需要修改的段（如仅 llm）；GET 恒返回全量合并值 */
  retrieval?: {
    top_k: number;
    similarity_threshold: number;
  };
  /** 提交时可选：只提交需要修改的段（如仅 llm）；GET 恒返回全量合并值 */
  chat?: {
    /** null = 用 LLM 配置默认 */
    temperature: number | null;
    top_p: number;
    /** null = 用 LLM 配置默认 */
    max_tokens: number | null;
    enable_multi_turn: boolean;
    history_rounds: number;
    /** 空串 = 使用内置默认模板 */
    system_prompt: string;
    /** 知识图谱增强（默认 true；查询时图谱上下文作为「知识图谱」来源引用注入） */
    kg_enhance?: boolean;
    /** 查询改写（默认 true；多轮对话时 LLM 结合历史改写检索查询，消除指代） */
    query_rewrite?: boolean;
    /** 查询改写用的历史轮数（默认 3，1~10；与 history_rounds 解耦，只影响改写） */
    query_rewrite_rounds?: number;
    /** 思考模式：disabled=关闭思考（默认）| enabled_low/high/max=开启并指定强度 */
    thinking_mode?: ThinkingMode;
  };
  /** Agentic 检索增强（默认关闭；分档：分数 ≥ recheck 直接答，< abstain 拒答） */
  agentic?: {
    /* 总开关：开启后在"检索→生成"之间插入改写/分档/拒答决策 */
    enabled?: boolean;
    /** 查询改写重试上限（0~5；改写后重新检索再分档） */
    max_retries?: number;
    /** 直接回答阈值（0~1：相似度 ≥ 该值不进入改写循环） */
    recheck_threshold?: number;
    /** 拒答阈值（0~1：相似度 < 该值直接拒答，不改写不重试） */
    abstain_threshold?: number;
  };
  /** 合并后的 LLM 配置（全局活跃 + 本部门覆盖；api_key 已脱敏） */
  llm?: DeptLlmConfig;
  /**
   * 本部门原始配置（仅含部门显式设置的字段；GET 返回）。
   * null = 无部门/部门未设置（当前值为纯全局活跃档案）。
   * 部门管理员保存时 POST 强制写入本部门，对本部门所有成员生效。
   */
  dept?: {
    llm?: DeptLlmConfig;
    retrieval?: { top_k?: number; similarity_threshold?: number };
    chat?: {
      temperature?: number | null;
      top_p?: number;
      max_tokens?: number | null;
      enable_multi_turn?: boolean;
      history_rounds?: number;
      system_prompt?: string;
      kg_enhance?: boolean;
      query_rewrite?: boolean;
      query_rewrite_rounds?: number;
      thinking_mode?: ThinkingMode;
    };
    agentic?: {
      enabled?: boolean;
      max_retries?: number;
      recheck_threshold?: number;
      abstain_threshold?: number;
    };
  } | null;
}

// ========== 审计 / 运行日志 ==========

export interface AuditLog {
  id: string;
  user_id: string;
  username: string;
  role: 'super_admin' | 'dept_admin' | 'user';
  action: string;
  target_type?: string | null;
  target_id?: string | null;
  target_name?: string | null;
  /** JSON 字符串（请求体关键字段摘要，前端格式化展示） */
  detail?: string | null;
  ip?: string | null;
  status: 'success' | 'failed';
  created_at: string;
}

export interface AuditLogQuery {
  page?: number;
  page_size?: number;
  /** 操作类型精确过滤（如 kb.create） */
  action?: string;
  target_type?: string;
  /** 用户名模糊过滤 */
  username?: string;
  /** 开始时间（含，YYYY-MM-DD HH:mm:ss） */
  start_time?: string;
  /** 结束时间（含，YYYY-MM-DD HH:mm:ss） */
  end_time?: string;
}

export interface AuditLogPage {
  total: number;
  page: number;
  page_size: number;
  items: AuditLog[];
}

export interface AuditActionOption {
  action: string;
  label: string;
}

/** 单行运行日志（后端解析自 data/logs/kb.log；非标准行 level/ts 为 null） */
export interface LogLine {
  /** 原始整行 */
  line: string;
  /** 日志级别（INFO/WARNING/ERROR/DEBUG，解析失败为 null） */
  level: string | null;
  /** 时间戳（YYYY-MM-DD HH:mm:ss,SSS，解析失败为 null） */
  ts: string | null;
  /** 可读消息（module: message） */
  message: string;
}

export interface LogTailResult {
  lines: LogLine[];
  /** 新字节位置（下次轮询传入） */
  offset: number;
  /** 是否已读到文件尾 */
  eof: boolean;
}

/** 运行日志文件条目（data/logs/kb-YYYY-MM-DD.log，按日期倒序） */
export interface LogFileInfo {
  date: string;
  filename: string;
  size_bytes: number;
  mtime: string;
  /** 文件行数（小文件精确计数；大文件采样估算，见 line_count_estimated） */
  line_count: number;
  /** 行数是否为采样估算值（true 时前端提示「约 N 行」） */
  line_count_estimated: boolean;
}

/** 运行日志「输出段」（按相邻两行时间空档切分，供时间段导航） */
export interface LogSegment {
  /** 段起（YYYY-MM-DD HH:mm:ss；整段无标准行时为 null） */
  start_ts: string | null;
  /** 段止（同格式；活跃段随新日志增长） */
  end_ts: string | null;
  /** 段内条数 */
  count: number;
  /** 段内分级别条数（取行首 [LEVEL]，非标准行不计入） */
  levels: Record<string, number>;
}

export interface LogSegmentsResult {
  /** 输出段（时间正序） */
  segments: LogSegment[];
  /** 实际扫描字节数（只扫尾部，见 truncated） */
  scanned_bytes: number;
  /** 是否只覆盖文件尾部（更早内容未统计） */
  truncated: boolean;
}

export interface LogRangeResult {
  lines: LogLine[];
  /** 区间内实际命中行数（lines 可能因 limit 截断） */
  total: number;
  /** 是否因 limit 截断（只返回区间最后 limit 行） */
  truncated: boolean;
}

/**
 * 红绿灯状态（看**系统级故障**，与日志级别正交）。
 *
 * 判定只看「域」不看级别：`[WARNING] ... Embedding 服务不可用` 照样是系统级故障
 * （全站问答瘫痪），而 `[ERROR] ... 文档超阈值入库失败` 是用户级、不点灯。
 */
export interface LogHealth {
  /** green = 正常；red = 窗口内有**未确认**的系统级故障 */
  level: 'green' | 'red';
  /** 回看窗口（分钟） */
  window_minutes: number;
  /** 窗口内**未确认**的系统级故障条数（决定灯色） */
  fault_count: number;
  /** 窗口内已确认（ACK 消警）的条数 */
  acked_count: number;
  /** 未确认故障里 ERROR 级条数 */
  error_count: number;
  /** 未确认故障里 WARNING 级条数 */
  warning_count: number;
  /** 中文摘要（可直接展示） */
  summary: string;
}

/** 总览里的系统级故障条目：在 LogLine 基础上带 ACK 信息 */
export interface LogFaultEntry extends LogLine {
  /** 故障唯一标识（调 POST /logs/ack 逐条确认用） */
  id: string;
  /** 是否已确认（已解决） */
  acked: boolean;
  /** 确认时填的处理备注（未确认或没填为空串） */
  ack_note: string;
}

/** 某天的日志统计：分级别计数与系统级故障数**正交**（用户级 ERROR 计入 error 不计入 faults） */
export interface LogDayStat {
  date: string;
  lines: number;
  info: number;
  warning: number;
  error: number;
  /** 系统级故障条数（含 ERROR 与 WARNING 级） */
  faults: number;
  /** 系统级故障里 ERROR 级的条数；**用户级 ERROR = error - fault_errors** */
  fault_errors: number;
}

/** 系统日志总览（「日志查看」页总览 Tab） */
export interface LogOverview {
  /** 近 N 天（时间正序，只含实际有日志的天） */
  days: LogDayStat[];
  totals: Omit<LogDayStat, 'date'>;
  today: LogDayStat;
  health: LogHealth;
  /** 最近系统级故障（最新在前，带 id/acked 供逐条确认） */
  recent_faults: LogFaultEntry[];
}

// ========== 外部查询 ==========

/** 外部查询的检索/对话参数（复用聊天配置语义；null/缺省 = 跟随全局活跃配置） */
export interface ExtQueryConfig {
  /** 自定义系统提示词（空串 = 内置默认模板；支持 {knowledge}/{refs} 占位符） */
  system_prompt?: string;
  /** 温度 0-2（null=跟随模型默认） */
  temperature?: number | null;
  /** Top P 0-1（null=跟随模型默认） */
  top_p?: number | null;
  /** 最大输出 Token（null=跟随模型默认） */
  max_tokens?: number | null;
  /** 检索条数 1-20（null=跟随全局配置） */
  top_k?: number | null;
  /** 相似度阈值 0-1（null=跟随全局配置，0=不过滤） */
  similarity_threshold?: number | null;
  /** 多轮对话（默认 true） */
  enable_multi_turn?: boolean;
  /** 历史轮数 1-20（默认跟随全局配置） */
  history_rounds?: number | null;
}

/** 暴露的知识库摘要（列表接口附加，前端展示库名/部门） */
export interface ExtQueryKbInfo {
  id: string;
  name: string;
  department_id?: string | null;
}

export interface ExtQuery {
  id: string;
  name: string;
  kb_ids: string[];
  config: ExtQueryConfig;
  /** 访问凭证（即链接密钥）：仅内网管理端返回明文，外部请求用 Bearer 携带 */
  token: string;
  enabled: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
  /** 列表/详情附加的暴露库摘要（含部门 id，前端映射部门名） */
  kb_names?: ExtQueryKbInfo[];
}

export interface ExtQueryCreateInput {
  name: string;
  kb_ids: string[];
  config?: ExtQueryConfig;
}

export type ExtQueryUpdateInput = Partial<Omit<ExtQueryCreateInput, 'token'>>;
