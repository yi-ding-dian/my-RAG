import axios from 'axios';
import { authHeader, clearAuth, getToken, type User } from '../auth/token';

export type { User };

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
});

// 请求拦截器：自动携带 Authorization（/api/auth/login 与 /api/health 为公开接口，放行）
api.interceptors.request.use(config => {
  const url = config.url ?? '';
  if (!url.includes('/auth/login') && !url.includes('/health')) {
    const header = authHeader();
    if (header.Authorization) config.headers.Authorization = header.Authorization;
  }
  return config;
});

// 响应拦截器：401 统一清除本地认证并跳转登录页（公开接口放行，避免死循环）
api.interceptors.response.use(
  res => res,
  err => {
    const status = err.response?.status;
    const url = err.config?.url ?? '';
    const isPublic = url.includes('/auth/login') || url.includes('/health');
    if (status === 401 && !isPublic) {
      clearAuth();
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
    }
    return Promise.reject(err);
  },
);

// ========== 类型定义（与后端契约一致） ==========

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

export type DocumentStatus = 'uploaded' | 'parsing' | 'parsed' | 'ingested' | 'failed';

export type ParseMethod = 'naive' | 'title' | 'regex' | 'parent_child' | 'qa' | 'agentic';

/** 解析引擎：auto=自动（MinerU 优先，不可用自动降级；layout=DeepDOC 时走 DeepDoc）| mineru=强制 MinerU 高精度（PDF 混排）| deepdoc=强制 DeepDoc（RAGFlow，表格输出可检索 HTML，仅 PDF）| plain=纯文本提取 */
export type ParserEngine = 'auto' | 'mineru' | 'deepdoc' | 'plain';

/** MinerU 解析后端（mineru-api /file_parse backend 参数）：auto/不传=跟随服务端默认（hybrid-auto-engine）| hybrid-auto-engine=混合自动引擎（质量优：表格规范/OCR 准/流程图识别，速度稍慢）| pipeline=管线（快约 20s，表格可能错乱） */
export type MinerUBackend = 'auto' | 'hybrid-auto-engine' | 'pipeline';

/** PDF 版面识别引擎：MinerU=高精度（推荐）| DeepDOC=表格输出为可检索 HTML| PlainText=纯文本直提（pypdf/python-docx，无表格/图片识别） */
export type LayoutRecognize = 'MinerU' | 'DeepDOC' | 'PlainText';

/** 解析方式（合并解析引擎+版面识别，无自动档）：MinerU=高精度（默认）| DeepDOC=表格输出可检索 HTML（仅 PDF）| PlainText=纯文本直提（本地 pypdf/python-docx，恒可用） */
export type ParseMode = 'MinerU' | 'DeepDOC' | 'PlainText';

/** 解析语言：ch=中文 | en=英文 */
export type ParseLang = 'ch' | 'en';

/** 思考模式（DeepSeek thinking 控制，图谱抽取/上下文摘要 LLM 调用共用）：
 * disabled=关闭思考（推荐，简单延迟敏感任务更快更省 token）| enabled_low/
 * enabled_high/enabled_max=开启思考并指定强度（low/high/max） */
export type ThinkingMode = 'disabled' | 'enabled_low' | 'enabled_high' | 'enabled_max';

/**
 * 解析配置：naive=通用切块 | title=按标题切块 | regex=正则切块 | parent_child=父子分块
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
  delimiter?: string;
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
  /** 思考模式（DeepSeek thinking 控制，图谱抽取/上下文摘要调用共用）：disabled=关闭思考（默认，更快更省 token）| enabled_low/high/max=开启思考并指定强度 */
  thinking_mode?: ThinkingMode;
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
  size: number;
  status: DocumentStatus;
  error?: string;
  chunk_count: number;
  parse_method: string; // 'mineru' | 'plain'
  parser_id?: string; // 解析方式：'naive' | 'title' | 'regex'（列表/详情返回）
  parser_config?: Record<string, unknown>; // 解析参数（chunk_size/overlap/split_level/regex_pattern 等）
  created_at: string;
  updated_at: string;
  /** 是否已移入回收站（软删除标记，检索自动排除） */
  deleted?: boolean;
  /** 移入回收站时间（恢复后清空） */
  deleted_at?: string | null;
  /** 知识图谱状态：none=未构建/building=构建中/ready=已构建/failed=构建失败 */
  graph_status?: 'none' | 'building' | 'ready' | 'failed';
  /** 图谱构建失败原因（graph_status=failed 时返回） */
  graph_error?: string;
}

/** 解析方式友好名（契约：naive→通用切块 / title→按标题切块 / regex→正则切块 / parent_child→父子分块 / qa→QA 问答 / agentic→Agentic 智能分块） */
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
    default:
      return method;
  }
};

/** 解析方式对应 Tag 颜色（契约：通用=blue / 按标题=geekblue / 正则=purple / 父子分块=magenta / QA 问答=cyan / Agentic=gold） */
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
  [k: string]: unknown;
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

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  sources?: Source[];
  created_at?: string;
  /** 前端会话状态专用：用户点击「停止」中断生成（仅 UI 标注，不落盘） */
  stopped?: boolean;
}

// ========== 认证 API ==========

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export const login = (username: string, password: string) =>
  api.post<LoginResponse>('/auth/login', { username, password });

export const getMe = () => api.get<User>('/auth/me');

export const changePassword = (oldPassword: string, newPassword: string) =>
  api.post<{ message: string }>('/auth/change-password', {
    old_password: oldPassword,
    new_password: newPassword,
  });

// ========== 用户管理 API（仅 super_admin） ==========

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

export const listUsers = (params?: { department_id?: string }) =>
  api.get<User[]>('/users', { params });

export const createUser = (data: UserCreateInput) => api.post<User>('/users', data);

export const updateUser = (id: string, data: UserUpdateInput) =>
  api.put<User>(`/users/${id}`, data);

export const deleteUser = (id: string) => api.delete(`/users/${id}`);

// ========== 头像 API（登录即可，自己传自己的） ==========

/** 上传当前登录用户头像（jpg/png/webp/gif，≤1MB），返回 {avatar: key} */
export const uploadAvatar = (file: File) => {
  const form = new FormData();
  form.append('file', file);
  return api.post<{ avatar: string }>('/users/me/avatar', form);
};

/** 删除当前登录用户头像（回默认），返回 {avatar: null} */
export const deleteAvatar = () => api.delete<{ avatar: string | null }>('/users/me/avatar');

/** 头像代理 URL（img 无法带 header，追加 query token，与 markdown 图片代理一致） */
export const avatarUrl = (userId: string): string => {
  const token = getToken();
  const base = `/api/files/avatars/${encodeURIComponent(userId)}`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
};

// ========== 部门管理 API（仅 super_admin） ==========

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

export const listDepartments = () => api.get<Department[]>('/departments');

export const createDepartment = (data: DepartmentInput) =>
  api.post<Department>('/departments', data);

export const updateDepartment = (id: string, data: DepartmentInput) =>
  api.put<Department>(`/departments/${id}`, data);

export const deleteDepartment = (id: string) => api.delete(`/departments/${id}`);

// ========== 知识库 API ==========

/** 标签聚合条目（GET /kbs/tags，count 降序） */
export interface TagCount {
  name: string;
  count: number;
}

/**
 * 知识库列表；tags 传多个时后端按"同时包含全部标签"（交集）过滤。
 * query 用手工拼接（axios 数组参数默认序列化为 tag[]=x 形式，FastAPI 不识别）。
 */
export const listKbs = (params?: { tag?: string[] }) => {
  const qs = params?.tag?.length
    ? `?${params.tag.map(t => `tag=${encodeURIComponent(t)}`).join('&')}`
    : '';
  return api.get<KnowledgeBase[]>(`/kbs${qs}`);
};

export const createKb = (data: {
  name: string;
  description?: string;
  department_id?: string | null;
  tags?: string[];
}) => api.post<KnowledgeBase>('/kbs', data);

export const updateKb = (id: string, data: {
  name?: string;
  description?: string;
  /** 传 [] 清空；省略=不改 */
  tags?: string[];
}) => api.put<KnowledgeBase>(`/kbs/${id}`, data);

export const deleteKb = (id: string) => api.delete(`/kbs/${id}`);

/** 覆盖式设置知识库标签（can_manage_kb；空数组=清空） */
export const updateKbTags = (kbId: string, tags: string[]) =>
  api.put<KnowledgeBase>(`/kbs/${kbId}/tags`, { tags });

/** 标签聚合（当前用户可见范围，count 降序）——标签筛选条数据源 */
export const listKbTags = () => api.get<{ tags: TagCount[] }>('/kbs/tags');

// ========== 向量维度状态 + 一键重建（P0：更换 embedding 模型后维度冲突） ==========

/** 检测知识库向量维度 vs 当前模型维度（空 collection / 维度相同 / 模型不可用 → compatible） */
export const getVectorStatus = (kbId: string) => api.get<VectorStatus>(`/kbs/${kbId}/vector-status`);

/** 一键重建向量（清空旧向量 → 当前模型重新向量化；can_manage_kb 权限） */
export const rebuildVectors = (kbId: string) =>
  api.post<{ task_id: string }>(`/kbs/${kbId}/rebuild-vectors`);

/** 查询重建进度（前端轮询用） */
export const getRebuildStatus = (kbId: string) =>
  api.get<RebuildTaskStatus>(`/kbs/${kbId}/rebuild-status`);

/** 当前激活 embedding 模型的实际输出维度（实测，super_admin） */
export const getEmbeddingDim = () =>
  api.get<{ dimension: number | null; model: string; ok: boolean; message: string }>(
    '/settings/embedding-dim',
  );

// ========== 文档 API ==========

export const uploadDocument = (kbId: string, file: File, force = false) => {
  const form = new FormData();
  form.append('file', file);
  return api.post<DocumentItem>(
    `/kbs/${kbId}/documents/upload?force=${force}`,
    form,
    { timeout: 120000 },
  );
};

/** 触发入库响应：degrade 为解析前探测到的自动降级提示（所选解析器不可用），可选 */
export interface IngestResult {
  message: string;
  status: string;
  doc_id: string;
  degrade?: string;
}

export const ingestDocument = (kbId: string, docId: string, config?: IngestConfig) =>
  api.post<IngestResult>(`/kbs/${kbId}/documents/${docId}/ingest`, config);

// ========== 解析器可用性探测（解析前检测，解析弹窗状态徽标） ==========

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

export const getParserStatus = () => api.get<ParserStatus>('/kbs/parsers/status');

/**
 * 文档列表（P2-10 服务端分页）：
 * - 传 page/page_size → 返回 {total, page, page_size, items}
 * - 不传 → 返回全量数组（旧调用兼容）
 * - status 可选：状态筛选下沉后端（uploaded/parsing/ingested/failed/all，
 *   空=全部），先过滤后分页，避免"筛选只作用于当前页"
 */
export const listDocuments = (
  kbId: string,
  params?: { page?: number; page_size?: number; status?: string },
) =>
  api.get<DocumentItem[] | DocumentPage>(
    `/kbs/${kbId}/documents`, { params });

/** 分页响应结构（后端带 page/page_size 参数时返回） */
export interface DocumentPage {
  total: number;
  page: number;
  page_size: number;
  items: DocumentItem[];
}

export const getDocument = (kbId: string, docId: string) =>
  api.get<DocumentDetail>(`/kbs/${kbId}/documents/${docId}`);

export const deleteDocument = (kbId: string, docId: string) =>
  api.delete(`/kbs/${kbId}/documents/${docId}`);

/** 回收站列表（can_manage_kb；分页语义与 listDocuments 一致，P2-10） */
export const listTrashDocuments = (
  kbId: string,
  params?: { page?: number; page_size?: number },
) =>
  api.get<DocumentItem[] | DocumentPage>(`/kbs/${kbId}/documents/trash`, { params });

/** 恢复回收站文档（无需重新解析，恢复后立即重新进入检索） */
export const restoreDocument = (kbId: string, docId: string) =>
  api.post<DocumentItem>(`/kbs/${kbId}/documents/${docId}/restore`);

/** 彻底删除（仅回收站内操作，删除后不可恢复） */
export const purgeDocument = (kbId: string, docId: string) =>
  api.post<{ message: string }>(`/kbs/${kbId}/documents/${docId}/purge`);

/** 清空回收站：批量彻底删除 */
export const emptyTrash = (kbId: string) =>
  api.post<{ message: string; count: number }>(`/kbs/${kbId}/documents/trash/empty`);

/**
 * 文档原始内容（预览用）：fetch 带鉴权头取字节流。
 * - pdf → Blob（前端 URL.createObjectURL + iframe 原生渲染）
 * - txt/md/url → 文本（caller 自行 .text()）
 * 非 2xx 时抛出后端 detail 中文错误。
 */
export const getDocumentRaw = async (kbId: string, docId: string): Promise<Blob> => {
  const res = await fetch(`/api/kbs/${kbId}/documents/${docId}/raw`, {
    headers: authHeader(),
  });
  if (res.status === 401) {
    // 与 axios 拦截器一致：登录过期统一跳转
    clearAuth();
    if (!window.location.pathname.startsWith('/login')) {
      window.location.href = '/login';
    }
    throw new Error('登录已过期，请重新登录');
  }
  if (!res.ok) {
    let detail = `加载失败（HTTP ${res.status}）`;
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch {
      // 非 JSON 响应体，保留默认提示
    }
    throw new Error(detail);
  }
  return res.blob();
};

/**
 * 下载文档原始文件（docx 等不支持在线预览的类型，P1-5）：fetch 带鉴权头取
 * 字节流，按 Content-Disposition 文件名触发浏览器下载；非 2xx 抛后端中文错误。
 */
export const downloadDocumentRaw = async (kbId: string, docId: string): Promise<void> => {
  const res = await fetch(`/api/kbs/${kbId}/documents/${docId}/raw`, {
    headers: authHeader(),
  });
  if (res.status === 401) {
    clearAuth();
    if (!window.location.pathname.startsWith('/login')) {
      window.location.href = '/login';
    }
    throw new Error('登录已过期，请重新登录');
  }
  if (!res.ok) {
    let detail = `下载失败（HTTP ${res.status}）`;
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch {
      // 非 JSON 响应体，保留默认提示
    }
    throw new Error(detail);
  }
  const blob = await res.blob();
  // 优先解析 RFC 5987 filename*=UTF-8''，其次普通 filename
  const cd = res.headers.get('Content-Disposition') || '';
  let filename = `document_${docId}`;
  const m1 = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (m1) {
    filename = decodeURIComponent(m1[1]);
  } else {
    const m2 = cd.match(/filename="?([^";]+)"?/i);
    if (m2) filename = m2[1];
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
};

/** 重命名文档（只改展示名 original_name；扩展名须保留，无扩展名自动补全） */
export const renameDocument = (kbId: string, docId: string, name: string) =>
  api.post<DocumentItem>(`/kbs/${kbId}/documents/${docId}/rename`, { name });

// ========== 知识图谱 API ==========

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

/** 查询知识库知识图谱（可选按文档过滤）；图谱不存在 → 404"该知识库暂无知识图谱" */
export const getKnowledgeGraph = (kbId: string, docId?: string) =>
  api.get<KnowledgeGraph>(`/kbs/${kbId}/graph`, {
    params: docId ? { doc_id: docId } : {},
  });

/** 触发文档图谱补建/重建（后台任务，复用现有切块不重新解析入库）
 * - 未入库 → 400"请先入库后再构建图谱"；构建中 → 409"图谱正在构建中"
 * - 已构建文档调用即重建（先清旧引用再抽取合并，实体不翻倍）
 * - body.llm_model（可选）：本次构建专用模型（「本次构建生效」：只在本次
 *   任务内覆盖抽取模型，不写回文档配置；不传用文档原配置/激活模型）
 * 返回后轮询文档 graph_status：building → ready/failed
 */
export const buildDocumentGraph = (
  kbId: string,
  docId: string,
  body?: { llm_model?: string },
) =>
  api.post<{ message: string; doc_id: string }>(
    `/kbs/${kbId}/documents/${docId}/graph-build`,
    body,
  );

/** 中断进行中的文档图谱构建（仅 building 可中断 → 200；
 * 非构建中 → 409"当前不在图谱构建中，无法中断"）
 * 中断后任务停止、状态恢复构建前值（旧图谱保留），可再次构建
 */
export const cancelDocumentGraphBuild = (kbId: string, docId: string) =>
  api.post<{ message: string; doc_id: string }>(
    `/kbs/${kbId}/documents/${docId}/graph-build/cancel`,
  );

// ========== 超管全局文档管理 API（仅 super_admin） ==========

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

/**
 * 跨部门全部文档查询（仅 super_admin）：可选过滤 department_id / kb_id /
 * status（uploaded/parsing/parsed/ingested/failed/unparsed/all）/
 * keyword（文件名模糊）；page/page_size 默认 50 上限 200。
 * 未分配部门（department_id 为 null 的知识库）传 department_id='__unassigned__'。
 */
export const listGlobalDocuments = (params?: {
  department_id?: string;
  kb_id?: string;
  status?: string;
  keyword?: string;
  page?: number;
  page_size?: number;
}) => api.get<GlobalDocumentPage>('/admin/documents', { params });

/** URL 网页导入为文档（仅 http/https；标题做文件名，正文提取为纯文本） */
export const importDocumentFromUrl = (kbId: string, url: string) =>
  api.post<DocumentItem>(`/kbs/${kbId}/documents/from-url`, { url });

// ========== 对话 API（SSE 流式 + 检索 + 历史） ==========

export interface StreamChatParams {
  kb_id: string;
  query: string;
  session_id?: string;
  top_k?: number;
}

export interface StreamCallbacks {
  /** 收到 event:meta，携带检索来源 */
  onMeta?: (sources: Source[]) => void;
  /** 收到 event:delta，增量文本 */
  onDelta?: (text: string) => void;
  /** 收到 event:done */
  onDone?: (info: { session_id: string; message_count: number }) => void;
  /** 收到 event:error 或网络错误（用户主动停止时 message 为 '已停止'） */
  onError?: (message: string) => void;
}

/**
 * SSE 流式对话：基于 fetch（axios 对 SSE 不友好）。
 * 使用 AbortController 支持"停止"按钮，返回 abort 函数。
 * 逐行解析 `event:` / `data:`，兼容 chunked 传输导致的半行拆分。
 */
export function streamChat(params: StreamChatParams, callbacks: StreamCallbacks): () => void {
  const controller = new AbortController();

  const parseData = (eventType: string, raw: string) => {
    let data: unknown = raw;
    try {
      data = JSON.parse(raw);
    } catch {
      // data 非 JSON（如纯文本 delta）时原样使用
    }
    switch (eventType) {
      case 'meta': {
        // 兼容两种形态：裸数组 [Source]（契约/mock）与 {"sources":[Source]}（真实后端）
        const sources = Array.isArray(data)
          ? (data as Source[])
          : ((data as { sources?: Source[] })?.sources ?? []);
        callbacks.onMeta?.(sources);
        break;
      }
      case 'delta':
        // 兼容两种形态：裸字符串（mock）与 {"text":"..."}（真实后端）
        if (typeof data === 'string') {
          callbacks.onDelta?.(data);
        } else {
          const text = (data as { text?: string })?.text;
          if (text) callbacks.onDelta?.(text);
        }
        break;
      case 'done': {
        const info = (typeof data === 'object' && data !== null ? data : {}) as {
          session_id?: string;
          message_count?: number;
        };
        callbacks.onDone?.({ session_id: info.session_id ?? '', message_count: info.message_count ?? 0 });
        break;
      }
      case 'error': {
        const msg =
          typeof data === 'string' ? data : ((data as { message?: string })?.message ?? '生成出错');
        callbacks.onError?.(msg);
        break;
      }
    }
  };

  (async () => {
    let res: Response;
    try {
      // fetch 不走 axios 拦截器，需手动携带认证头
      res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeader() },
        // 兼容：契约字段 query；真实后端要求 message（同时发送，后端自行忽略多余字段）
        body: JSON.stringify({ ...params, message: params.query }),
        signal: controller.signal,
      });
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        callbacks.onError?.('已停止');
      } else {
        callbacks.onError?.('网络请求失败');
      }
      return;
    }

    // 401 统一处理（与 axios 拦截器一致）：清除本地认证并跳转登录
    if (res.status === 401) {
      clearAuth();
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
      callbacks.onError?.('登录已过期，请重新登录');
      return;
    }

    if (!res.ok || !res.body) {
      callbacks.onError?.(`请求失败（HTTP ${res.status}）`);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let eventType = '';

    const handleLine = (line: string) => {
      if (!line) return;
      if (line.startsWith('event:')) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        const raw = line.slice(5).trim();
        if (raw && eventType) parseData(eventType, raw);
      }
    };

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx = buffer.indexOf('\n');
        while (idx >= 0) {
          handleLine(buffer.slice(0, idx).replace(/\r$/, ''));
          buffer = buffer.slice(idx + 1);
          idx = buffer.indexOf('\n');
        }
      }
      // 处理流结束时残留的尾部
      if (buffer.trim()) {
        buffer.split('\n').forEach(l => handleLine(l.replace(/\r$/, '')));
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        callbacks.onError?.('流式响应中断');
      } else {
        callbacks.onError?.('已停止');
      }
    }
  })();

  return () => controller.abort();
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

/**
 * 检索调试：真实后端返回裸数组 Source[]，契约约定 {sources: Source[]}，此处归一化为统一结构。
 * signal 可选：传入 AbortController.signal 支持「停止检索」取消（axios 抛 CanceledError，
 * 调用方以 err.code === 'ERR_CANCELED' 识别静默结束）。
 */
export const retrieveChat = async (data: RetrieveChatParams, signal?: AbortSignal) => {
  const res = await api.post<Source[] | { sources: Source[] }>('/chat/retrieve', {
    ...data,
    message: data.query,
  }, { signal });
  return { sources: Array.isArray(res.data) ? res.data : res.data.sources };
};

// ========== 会话历史 API ==========

export const listSessions = (kbId: string) =>
  api.get<ChatSession[]>('/chat/history', { params: { kb_id: kbId } });

export const getSession = (sessionId: string) =>
  api.get<{ messages: ChatMessage[] }>(`/chat/history/${sessionId}`);

export const deleteSession = (sessionId: string) =>
  api.delete(`/chat/history/${sessionId}`);

export const renameSession = (sessionId: string, title: string) =>
  api.post<{ message: string; title: string }>(`/chat/history/${sessionId}/rename`, { title });

/**
 * 导出会话为 Markdown：fetch 拿 blob 后创建下载链接（不裸传 token，
 * Authorization 头由 authHeader() 携带；文件名取自 Content-Disposition）。
 */
export const exportSession = async (sessionId: string): Promise<void> => {
  const res = await fetch(`/api/chat/history/${sessionId}/export`, {
    headers: authHeader(),
  });
  if (res.status === 401) {
    clearAuth();
    if (!window.location.pathname.startsWith('/login')) {
      window.location.href = '/login';
    }
    throw new Error('登录已过期，请重新登录');
  }
  if (!res.ok) throw new Error(`导出失败（HTTP ${res.status}）`);
  const blob = await res.blob();
  // 优先解析 RFC 5987 filename*=UTF-8''，其次普通 filename
  const cd = res.headers.get('Content-Disposition') || '';
  let filename = `会话_${sessionId}.md`;
  const m1 = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (m1) {
    filename = decodeURIComponent(m1[1]);
  } else {
    const m2 = cd.match(/filename="?([^";]+)"?/i);
    if (m2) filename = m2[1];
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
};

// ========== 系统统计 API（阶段2） ==========

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

export const getStats = () => api.get<Stats>('/stats');

export const getRagasStatus = () => api.get<RagasStatus>('/stats/ragas');

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

export const startRagasEvaluation = (body: RagasEvaluationRequest) =>
  api.post<RagasEvaluationResult>('/stats/ragas/evaluations', body);

export const previewRagasSamples = (body: RagasEvaluationRequest) =>
  api.post<RagasEvaluationPreview>('/stats/ragas/evaluations', body);

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

export const ragasPrecheck = () =>
  api.get<RagasPrecheck>('/stats/ragas/precheck');

// ========== 检索质量统计（近 30 天） ==========

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

export const getRetrievalQuality = (kbId: string) =>
  api.get<RetrievalQuality>('/stats/quality', { params: { kb_id: kbId } });

export const getRagasReport = (taskId: string) =>
  api.get<RagasReport>(`/stats/ragas/tasks/${taskId}`);

/** 取消 RAGAS 评估任务（发起人本人 / super_admin / dept_admin 本部门可取消） */
export const cancelRagasEvaluation = (taskId: string) =>
  api.post<{ message: string }>(`/stats/ragas/evaluations/${taskId}/cancel`);

// ========== 服务配置档案 API（阶段2） ==========

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
    top_n?: number;
  };
}

export interface ChunkingConfig {
  chunk_size: number;
  overlap: number;
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
  retrieval: RetrievalConfig;
  chunking: ChunkingConfig;
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
  mysql: ConnectionTestResult;
  minio: ConnectionTestResult;
}

// ========== 审计操作日志 API（仅 super_admin） ==========

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

export const listAuditLogs = (params?: AuditLogQuery) =>
  api.get<AuditLogPage>('/audit/logs', { params });

export interface AuditActionOption {
  action: string;
  label: string;
}

export const listAuditActions = () =>
  api.get<{ actions: AuditActionOption[] }>('/audit/actions');

// ========== 系统运行日志 tail API（仅 super_admin） ==========

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

/**
 * 读系统运行日志（按天 + 字节游标增量）：date 缺省=今天（YYYY-MM-DD）；
 * offset < 0 = 尾部模式取最近 limit 行（首次加载/切换日期用）；
 * offset 超文件大小后端自动归位尾部；文件不存在返回空。
 */
export const tailSystemLogs = (date?: string, offset = 0, limit = 200) =>
  api.get<LogTailResult>('/logs/tail', { params: { date, offset, limit } });

/** 运行日志文件条目（data/logs/kb-YYYY-MM-DD.log，按日期倒序） */
export interface LogFileInfo {
  date: string;
  filename: string;
  size_bytes: number;
  mtime: string;
}

export const listLogFiles = () => api.get<{ files: LogFileInfo[] }>('/logs/files');

/** 删除指定天日志文件（不存在静默成功） */
export const deleteLogFile = (date: string) =>
  api.delete<{ message: string; deleted: number }>('/logs/files', { params: { date } });

/** 清空所有运行日志（今天文件截断继续写入，其余天删除） */
export const deleteAllLogFiles = () =>
  api.delete<{ message: string; deleted: number }>('/logs/files');

/**
 * 下载指定天日志文件（仅 super_admin）：fetch 带鉴权头取字节流返回 Blob，
 * 调用方拼文件名（kb-YYYY-MM-DD.log）触发下载；非 2xx 抛后端中文错误。
 */
export const downloadLogFile = async (date: string): Promise<Blob> => {
  const res = await fetch(`/api/logs/files/download?date=${encodeURIComponent(date)}`, {
    headers: authHeader(),
  });
  if (res.status === 401) {
    // 与 axios 拦截器一致：登录过期统一跳转
    clearAuth();
    if (!window.location.pathname.startsWith('/login')) {
      window.location.href = '/login';
    }
    throw new Error('登录已过期，请重新登录');
  }
  if (!res.ok) {
    let detail = `下载失败（HTTP ${res.status}）`;
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch {
      // 非 JSON 响应体，保留默认提示
    }
    throw new Error(detail);
  }
  return res.blob();
};

/** 按天删除审计记录（created_at 前缀匹配，删除前二次确认） */
export const deleteAuditLogsByDate = (date: string) =>
  api.delete<{ message: string; deleted: number }>('/audit/logs', { params: { date } });

export const listProfiles = () => api.get<ServiceProfile[]>('/settings/profiles');

export const getActiveProfile = () => api.get<ServiceProfile>('/settings/profiles/active');

export const createProfile = (data: ServiceProfileInput) =>
  api.post<ServiceProfile>('/settings/profiles', data);

export const updateProfile = (id: string, data: ServiceProfileInput) =>
  api.put<ServiceProfile>(`/settings/profiles/${id}`, data);

export const deleteProfile = (id: string) =>
  api.delete(`/settings/profiles/${id}`);

export const activateProfile = (id: string) =>
  api.post<{ message: string; profile: ServiceProfile }>(`/settings/profiles/${id}/activate`);

export const testProfileConnection = (id: string, data?: ServiceProfileInput) =>
  api.post<ProfileTestResult>(`/settings/profiles/${id}/test`, data || {});

/** 单个 LLM 模型连接测试（GET {base_url}/models，≤5s；勾选激活时先调用） */
export interface LlmTestResult {
  ok: boolean;
  reason: string;
  latency_ms: number;
}

export const testLlmConnection = (item: Partial<LLMModelItem>) =>
  api.post<LlmTestResult>('/settings/llm/test', item);

// ========== 解析配置 LLM 模型（GET 模型列表 / POST 按名测连接，登录即可） ==========

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

/** 解析配置弹窗数据源：当前激活档案的 LLM 模型列表（登录即可读） */
export const getLlmModelList = () =>
  api.get<LlmModelList>('/settings/llm/models');

/** 按模型名测试连接（切换解析模型前调用；后端按 name 查完整配置后探测，只测不写） */
export const testLlmModelByName = (name: string) =>
  api.post<LlmTestResult>('/settings/llm/test-model', { name });

// ========== 聊天设置 + 部门 LLM 配置（GET 登录可读，POST 需 super_admin/dept_admin） ==========

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
 * 聊天设置 + LLM 配置载荷（后端白名单：chat 段 6 字段 +
 * retrieval.top_k/similarity_threshold + llm 段 6 字段）
 */
export interface ChatSettingsPayload {
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
    };
  } | null;
}

export const getChatSettings = () => api.get<ChatSettingsPayload>('/settings/chat');

export const updateChatSettings = (data: ChatSettingsPayload) =>
  api.post<ChatSettingsPayload>('/settings/chat', data);

// ========== 外部查询 API（仅 super_admin；token 为访问凭证，内网管理端返回明文） ==========

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

export const listExtQueries = () => api.get<ExtQuery[]>('/ext-queries');

export const createExtQuery = (data: ExtQueryCreateInput) =>
  api.post<ExtQuery>('/ext-queries', data);

export const updateExtQuery = (id: string, data: ExtQueryUpdateInput) =>
  api.put<ExtQuery>(`/ext-queries/${id}`, data);

export const resetExtQueryToken = (id: string) =>
  api.post<{ token: string; message: string }>(`/ext-queries/${id}/reset-token`);

export const toggleExtQuery = (id: string) =>
  api.post<ExtQuery>(`/ext-queries/${id}/toggle`);

export const deleteExtQuery = (id: string) =>
  api.delete<{ message: string }>(`/ext-queries/${id}`);

/** 外部查询分享链接（token 即访问凭证，仅超管可见/复制） */
export const extQueryLink = (id: string, token: string): string =>
  `${window.location.origin}/ext-query/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}`;

export default api;
