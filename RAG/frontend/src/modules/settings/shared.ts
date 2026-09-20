import type {
  LLMModelItem,
  ProfileTestResult,
  ServiceProfile,
  ServiceProfileInput,
  VisionModelItem,
} from '../../shared/api/client';

/** 配置域快捷导航定义（方案 A）：标题 + 当前值摘要 + 对应编辑折叠 key */
export const DOMAIN_CARDS: Array<{
  key: string;
  title: string;
  summary: (p: ServiceProfile) => string;
  /** 本域可探测的后端段（点击测试连接按这些段 toast 结果；无则无测试按钮） */
  sections?: SectionKey[];
}> = [
  { key: 'ar', title: '档案', summary: p => p.name },
  {
    key: 'llm',
    title: '模型服务',
    sections: ['llm', 'embedding'],
    summary: p => {
      const sec = (p.llm as unknown as { models?: LLMModelItem[]; active?: number }) ?? {};
      const models = Array.isArray(sec.models) ? sec.models : [];
      const cur = models[sec.active ?? 0];
      return cur ? `${cur.name}（共 ${models.length} 个模型）` : '未配置模型';
    },
  },
  {
    key: 'mineru',
    title: '解析服务',
    sections: ['mineru', 'deepdoc', 'gotenberg', 'vision'],
    summary: p => {
      const vm = p.vision?.models ?? [];
      const cur = vm[p.vision?.active ?? 0];
      return `${p.mineru?.url || '-'}`
        + `${p.deepdoc?.base_url ? ` / ${p.deepdoc.base_url}` : ''}`
        + `${p.gotenberg?.base_url ? ` / 转换 ${p.gotenberg.base_url}` : ''}`
        + `${cur ? ` / 图片模型 ${cur.name}` : ''}`;
    },
  },
  {
    key: 'retrieval',
    title: '检索与切块',
    sections: ['rerank'],
    summary: p =>
      `top_k ${p.retrieval?.top_k ?? '-'}｜chunk ${p.chunking?.chunk_size ?? '-'}（重叠 ${p.chunking?.overlap ?? '-'}）`,
  },
  {
    key: 'ingest',
    title: '入库与限制',
    // 「输入字数」原在此摘要，随字段挪到「聊天设置」面板后移除
    // （chat.max_query_len 属聊天输入限制，非入库限制）
    summary: p =>
      `并发 ${p.ingestion?.concurrency ?? 3}｜单库上限 ${p.ingestion?.kb_doc_limit ?? 0}｜上传 ${p.ingestion?.max_upload_mb ?? 100}MB`,
  },
  {
    key: 'mysql',
    title: '数据存储',
    sections: ['mysql', 'minio', 'vector_store'],
    summary: p => {
      const db = p.mysql?.url
        ? String(p.mysql.url).slice(0, 40)
        : `${p.mysql?.host ?? ''}:${p.mysql?.port ?? ''}/${p.mysql?.database ?? ''}`;
      const vs =
        p.vector_store?.backend === 'milvus'
          ? `Milvus ${p.vector_store.milvus_uri || ''}`
          : 'Chroma（本地）';
      return `${db}｜${p.minio?.endpoint ?? '-'}/${p.minio?.bucket ?? '-'}｜${vs}`;
    },
  },
];

export interface TestItem {
  status: 'idle' | 'testing' | 'success' | 'failed';
  msg: string;
}

export type SectionKey = 'llm' | 'embedding' | 'mineru' | 'deepdoc' | 'gotenberg' | 'mysql' | 'minio' | 'vector_store' | 'rerank' | 'vision';

export const emptyTest: Record<SectionKey, TestItem> = {
  llm: { status: 'idle', msg: '' },
  embedding: { status: 'idle', msg: '' },
  mineru: { status: 'idle', msg: '' },
  deepdoc: { status: 'idle', msg: '' },
  gotenberg: { status: 'idle', msg: '' },
  rerank: { status: 'idle', msg: '' },
  mysql: { status: 'idle', msg: '' },
  minio: { status: 'idle', msg: '' },
  vector_store: { status: 'idle', msg: '' },
  vision: { status: 'idle', msg: '' },
};

// 测试结果 -> 展示项
export const toTestItems = (res: ProfileTestResult): Record<SectionKey, TestItem> => ({
  llm: { status: res.llm.ok ? 'success' : 'failed', msg: res.llm.message },
  embedding: { status: res.embedding.ok ? 'success' : 'failed', msg: res.embedding.message },
  mineru: { status: res.mineru.ok ? 'success' : 'failed', msg: res.mineru.message },
  deepdoc: { status: res.deepdoc.ok ? 'success' : 'failed', msg: res.deepdoc.message },
  gotenberg: { status: res.gotenberg?.ok ? 'success' : 'failed', msg: res.gotenberg?.message ?? '未参与探测' },
  rerank: { status: res.rerank.ok ? 'success' : 'failed', msg: res.rerank.message },
  mysql: { status: res.mysql.ok ? 'success' : 'failed', msg: res.mysql.message },
  minio: { status: res.minio.ok ? 'success' : 'failed', msg: res.minio.message },
  vector_store: { status: res.vector_store.ok ? 'success' : 'failed', msg: res.vector_store.message },
  vision: { status: res.vision?.ok ? 'success' : 'failed', msg: res.vision?.message ?? '未参与探测' },
});

/** 编辑弹窗折叠面板 key → 可探测段（点击面板标题右侧"测试"按钮）；
 *  不含 ar/ingest 等无连接探测的纯参数面板（retrieval 含 rerank 连接探测） */
export const PANEL_TEST_SECTIONS: Record<string, SectionKey[]> = {
  llm: ['llm'],
  embedding: ['embedding'],
  // 文档解析相关：MinerU / DeepDoc / Gotenberg 三段合并在一个面板里，
  // 面板头的 ⚡ 一次测这三项
  parse: ['mineru', 'deepdoc', 'gotenberg'],
  retrieval: ['rerank'],
  mysql: ['mysql'],
  minio: ['minio'],
  vector_store: ['vector_store'],
  vision: ['vision'],
};

export const sectionLabel: Record<SectionKey, string> = {
  llm: 'LLM 对话模型',
  embedding: 'Embedding 模型',
  mineru: 'MinerU 文档解析',
  deepdoc: 'DeepDoc 解析（RAGFlow）',
  gotenberg: '文档转换（Gotenberg）',
  rerank: 'Rerank 重排序',
  mysql: '数据库',
  minio: 'MinIO 对象存储',
  vector_store: '向量存储',
  vision: '图片解析模型',
};

// 全部段测试中
export const allTesting = (): Record<SectionKey, TestItem> => ({
  llm: { status: 'testing', msg: '' },
  embedding: { status: 'testing', msg: '' },
  mineru: { status: 'testing', msg: '' },
  deepdoc: { status: 'testing', msg: '' },
  gotenberg: { status: 'testing', msg: '' },
  rerank: { status: 'testing', msg: '' },
  mysql: { status: 'testing', msg: '' },
  minio: { status: 'testing', msg: '' },
  vector_store: { status: 'testing', msg: '' },
  vision: { status: 'testing', msg: '' },
});

// 全部段同一失败信息（接口整体报错时）
export const allFailed = (msg: string): Record<SectionKey, TestItem> => ({
  llm: { status: 'failed', msg },
  embedding: { status: 'failed', msg },
  mineru: { status: 'failed', msg },
  deepdoc: { status: 'failed', msg },
  gotenberg: { status: 'failed', msg },
  rerank: { status: 'failed', msg },
  mysql: { status: 'failed', msg },
  minio: { status: 'failed', msg },
  vector_store: { status: 'failed', msg },
  vision: { status: 'failed', msg },
});

/** 档案编辑表单的扁平字段值（antd Form values；字段名 = 表单项 name，
 *  与 toFormValues 回填键一一对应） */
export interface ProfileFormValues {
  name: string;
  embedding_base_url: string;
  embedding_api_key: string;
  embedding_model: string;
  embedding_dimension: number;
  mineru_url: string;
  mineru_timeout: number;
  deepdoc_base_url: string;
  deepdoc_email: string;
  deepdoc_password: string;
  deepdoc_timeout: number;
  deepdoc_dataset_prefix: string;
  /** 文档转换（Gotenberg，Office → PDF）：地址留空 = 不做转换 */
  gotenberg_base_url: string;
  gotenberg_timeout: number;
  retrieval_top_k: number;
  retrieval_enable_hybrid: boolean;
  rerank_enabled: boolean;
  rerank_base_url: string;
  rerank_model: string;
  rerank_api_key: string;
  rerank_top_n: number;
  chunk_size: number;
  chunk_overlap: number;
  /** 标题分层模型（可选，空串=不做 LLM 分层）：值 = 模型列表里的标识 */
  heading_llm_model: string;
  contextual_retrieval_max_full_doc_chars: number;
  ingestion_concurrency: number;
  ingestion_kb_doc_limit: number;
  ingestion_max_upload_mb: number;
  chat_max_query_len: number;
  /** 引用摘要字数（悬停引用标 [n] 时浮层显示的窗口大小） */
  chat_citation_snippet_chars: number;
  mysql_host: string;
  mysql_port: number;
  mysql_user: string;
  mysql_password: string;
  mysql_database: string;
  mysql_url: string;
  minio_endpoint: string;
  minio_access_key: string;
  minio_secret_key: string;
  minio_bucket: string;
  minio_secure: boolean;
  minio_region: string;
  vector_store_backend: string;
  vector_store_milvus_uri: string;
  // ---- 图片摘要（全局档案默认值；部门可在「部门配置」里覆盖）----
  /** 选中的模型 name（空 = 用列表第一个） */
  image_summary_model: string;
  /** 提示词（空 = 用内置默认模板） */
  image_summary_prompt: string;
  /** fields（固定字段，默认）/ prose（自然段） */
  image_summary_output_format: string;
  /** 「文字」字段长度上限 */
  image_summary_text_max_chars: number;
  /** 单文档摘要张数上限（0 = 不限） */
  image_summary_max_images: number;
  // 结构化选项（勾选后自动生成 prompt；任一改动即重新生成）
  image_summary_opt_label_type: boolean;
  image_summary_opt_read_text: boolean;
  image_summary_opt_describe_scene: boolean;
  image_summary_opt_describe_layout: boolean;
  /** 系统提示词库条目（Form.List 的嵌套路径，供外部查询引用） */
  prompts?: { items?: { name?: string; content?: string }[] };
}

// 表单扁平字段 <-> 嵌套档案对象互转
export const toProfileInput = (vals: ProfileFormValues, llmSection?: {
  models: LLMModelItem[]; active: number;
}, visionSection?: {
  models: VisionModelItem[]; active: number;
}): ServiceProfileInput => ({
  name: vals.name,
  // llm 段为模型列表结构：未添加模型时省略（后端用 .env 出厂默认单模型）
  llm: llmSection && llmSection.models.length
    ? { models: llmSection.models, active: llmSection.active }
    : undefined,
  embedding: {
    base_url: vals.embedding_base_url,
    api_key: vals.embedding_api_key,
    model: vals.embedding_model,
    dimension: vals.embedding_dimension,
  },
  mineru: { url: vals.mineru_url, timeout: vals.mineru_timeout },
  deepdoc: {
    base_url: vals.deepdoc_base_url,
    email: vals.deepdoc_email,
    password: vals.deepdoc_password,
    timeout: vals.deepdoc_timeout,
    dataset_prefix: vals.deepdoc_dataset_prefix,
  },
  // 文档转换（Gotenberg）：地址留空 = 不做转换，.doc 走原有路径
  gotenberg: {
    base_url: vals.gotenberg_base_url,
    timeout: vals.gotenberg_timeout,
  },
  retrieval: {
    top_k: vals.retrieval_top_k,
    enable_hybrid: vals.retrieval_enable_hybrid,
    rerank: {
      enabled: vals.rerank_enabled,
      base_url: vals.rerank_base_url,
      model: vals.rerank_model,
      api_key: vals.rerank_api_key,
      top_n: vals.rerank_top_n,
    },
  },
  chunking: {
    chunk_size: vals.chunk_size, overlap: vals.chunk_overlap,
    // 空串 = 不做 LLM 分层（后端 on_null=restore 语义允许清空已配置项）
    heading_llm_model: vals.heading_llm_model || '',
  },
  contextual_retrieval: {
    max_full_doc_chars: vals.contextual_retrieval_max_full_doc_chars,
  },
  ingestion: {
    concurrency: vals.ingestion_concurrency,
    kb_doc_limit: vals.ingestion_kb_doc_limit ?? 0,
    max_upload_mb: vals.ingestion_max_upload_mb ?? 100,
  },
  mysql: {
    host: vals.mysql_host,
    port: vals.mysql_port,
    user: vals.mysql_user,
    password: vals.mysql_password,
    database: vals.mysql_database,
    url: vals.mysql_url || undefined,
  },
  minio: {
    endpoint: vals.minio_endpoint,
    access_key: vals.minio_access_key,
    secret_key: vals.minio_secret_key,
    bucket: vals.minio_bucket,
    secure: vals.minio_secure,
    region: vals.minio_region || '',
  },
  vector_store: {
    backend: vals.vector_store_backend,
    milvus_uri: vals.vector_store_milvus_uri || '',
  },
  // 图片解析模型：与 llm 同为模型列表结构，未添加时省略（= 未配置 → 解析时
  // 勾选图片摘要会被预检拦下并提示管理员配置）
  vision: visionSection && visionSection.models.length
    ? { models: visionSection.models, active: visionSection.active }
    : undefined,
  image_summary: {
    model: vals.image_summary_model || '',
    prompt: vals.image_summary_prompt || '',
    output_format: vals.image_summary_output_format || 'fields',
    text_max_chars: vals.image_summary_text_max_chars ?? 200,
    max_images: vals.image_summary_max_images ?? 50,
    options: {
      label_type: vals.image_summary_opt_label_type ?? true,
      read_text: vals.image_summary_opt_read_text ?? true,
      describe_scene: vals.image_summary_opt_describe_scene ?? true,
      describe_layout: vals.image_summary_opt_describe_layout ?? false,
    },
  },
  // 聊天设置段：后端 update_profile 是**字段级合并**（只覆盖传入字段），
  // 故这里只提交本表单承载的字段，system_prompt/thinking_mode 等其他
  // chat 字段不受影响
  chat: {
    max_query_len: vals.chat_max_query_len,
    citation_snippet_chars: vals.chat_citation_snippet_chars,
  },
  // 系统提示词库：始终提交（空数组 = 清空库）；过滤掉没填名称的半成品条目
  prompts: {
    items: (vals.prompts?.items ?? [])
      .filter(it => (it?.name ?? '').trim())
      .map(it => ({
        name: (it?.name ?? '').trim(),
        content: (it?.content ?? '').trim(),
      })),
  },
});

export const toFormValues = (p: ServiceProfile) => ({
  name: p.name,
  embedding_base_url: p.embedding?.base_url,
  embedding_api_key: p.embedding?.api_key,
  embedding_model: p.embedding?.model,
  embedding_dimension: p.embedding?.dimension,
  mineru_url: p.mineru?.url,
  mineru_timeout: p.mineru?.timeout,
  deepdoc_base_url: p.deepdoc?.base_url,
  deepdoc_email: p.deepdoc?.email,
  deepdoc_password: p.deepdoc?.password,
  deepdoc_timeout: p.deepdoc?.timeout,
  deepdoc_dataset_prefix: p.deepdoc?.dataset_prefix,
  gotenberg_base_url: p.gotenberg?.base_url,
  gotenberg_timeout: p.gotenberg?.timeout,
  retrieval_top_k: p.retrieval?.top_k,
  retrieval_enable_hybrid: p.retrieval?.enable_hybrid ?? true,
  rerank_enabled: p.retrieval?.rerank?.enabled ?? false,
  rerank_base_url: p.retrieval?.rerank?.base_url ?? '',
  rerank_model: p.retrieval?.rerank?.model ?? '',
  rerank_api_key: p.retrieval?.rerank?.api_key ?? '',
  rerank_top_n: p.retrieval?.rerank?.top_n ?? 10,
  chunk_size: p.chunking?.chunk_size,
  chunk_overlap: p.chunking?.overlap,
  heading_llm_model: p.chunking?.heading_llm_model ?? '',
  contextual_retrieval_max_full_doc_chars:
    p.contextual_retrieval?.max_full_doc_chars ?? 20000,
  ingestion_concurrency: p.ingestion?.concurrency ?? 3,
  ingestion_kb_doc_limit: p.ingestion?.kb_doc_limit ?? 0,
  ingestion_max_upload_mb: p.ingestion?.max_upload_mb ?? 100,
  chat_max_query_len: p.chat?.max_query_len ?? 2000,
  chat_citation_snippet_chars: p.chat?.citation_snippet_chars ?? 600,
  mysql_host: p.mysql?.host,
  mysql_port: p.mysql?.port,
  mysql_user: p.mysql?.user,
  mysql_password: p.mysql?.password,
  mysql_database: p.mysql?.database,
  mysql_url: p.mysql?.url,
  minio_endpoint: p.minio?.endpoint,
  minio_access_key: p.minio?.access_key,
  minio_secret_key: p.minio?.secret_key,
  minio_bucket: p.minio?.bucket,
  minio_secure: p.minio?.secure,
  minio_region: p.minio?.region,
  vector_store_backend: p.vector_store?.backend ?? 'chroma',
  vector_store_milvus_uri: p.vector_store?.milvus_uri ?? '',
  image_summary_model: p.image_summary?.model ?? '',
  image_summary_prompt: p.image_summary?.prompt ?? '',
  image_summary_output_format: p.image_summary?.output_format ?? 'fields',
  image_summary_text_max_chars: p.image_summary?.text_max_chars ?? 200,
  image_summary_max_images: p.image_summary?.max_images ?? 50,
  image_summary_opt_label_type: p.image_summary?.options?.label_type ?? true,
  image_summary_opt_read_text: p.image_summary?.options?.read_text ?? true,
  image_summary_opt_describe_scene: p.image_summary?.options?.describe_scene ?? true,
  image_summary_opt_describe_layout: p.image_summary?.options?.describe_layout ?? false,
  // 系统提示词库（旧档案无该段 → 空列表）
  prompts: { items: p.prompts?.items ?? [] },
});
