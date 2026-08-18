/**
 * 知识库 / 文档 / 上传 / 解析 / 图谱 API。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 */
import api from './http';
import { authHeader, clearAuth } from '../auth/token';
import type {
  AnalyzeResult,
  DocumentDetail,
  DocumentItem,
  DocumentPage,
  GlobalDocumentPage,
  IngestConfig,
  IngestResult,
  KnowledgeBase,
  KnowledgeGraph,
  ParserStatus,
  RebuildTaskStatus,
  TagCount,
  VectorStatus,
} from './types';

// ========== 知识库 API ==========

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

export const ingestDocument = (kbId: string, docId: string, config?: IngestConfig) =>
  api.post<IngestResult>(`/kbs/${kbId}/documents/${docId}/ingest`, config);

// ========== 解析器可用性探测（解析前检测，解析弹窗状态徽标） ==========

export const getParserStatus = () => api.get<ParserStatus>('/kbs/parsers/status');

// ========== 智能解析引导（文档画像分析，独立模块；向导生成配置后复用 ingest） ==========

export const analyzeDocument = (kbId: string, docId: string) =>
  api.get<AnalyzeResult>(`/kbs/${kbId}/documents/${docId}/analyze`);

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

/**
 * 下载文档原始文件（GET /kbs/{kb_id}/documents/{doc_id}/download，
 * can_access_kb）：fetch 带鉴权头取字节流，按 Content-Disposition 文件名
 * 触发浏览器下载（attachment，原始文件字节，任何非回收站状态均可下载）；
 * 非 2xx 抛后端中文错误。
 */
export const downloadDocument = async (kbId: string, docId: string): Promise<void> => {
  const res = await fetch(`/api/kbs/${kbId}/documents/${docId}/download`, {
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

// ========== 知识图谱 API ==========

/** 查询知识库知识图谱（可选按文档过滤）；图谱不存在 → 404"该知识库暂无知识图谱" */
export const getKnowledgeGraph = (kbId: string, docId?: string) =>
  api.get<KnowledgeGraph>(`/kbs/${kbId}/graph`, {
    params: docId ? { doc_id: docId } : {},
  });

/**
 * 触发文档图谱补建/重建（后台任务，复用现有切块不重新解析入库）
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

/** 取消解析中的文档（仅 parsing 可取消 → 200"取消解析请求已发送"；
 * 非解析中 → 409"当前不在解析中，无法取消"。取消后文档回 failed（"用户
 * 取消解析"），可重新发起解析）
 */
export const cancelDocumentIngestion = (kbId: string, docId: string) =>
  api.post<{ message: string; doc_id: string }>(
    `/kbs/${kbId}/documents/${docId}/ingest/cancel`,
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
