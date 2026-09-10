import type {
  LLMModelItem,
  ProfileTestResult,
  ServiceProfile,
  ServiceProfileInput,
} from '../../api/client';

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
    sections: ['mineru', 'deepdoc'],
    summary: p =>
      `${p.mineru?.url || '-'}${p.deepdoc?.base_url ? ` / ${p.deepdoc.base_url}` : ''}`,
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
    summary: p =>
      `并发 ${p.ingestion?.concurrency ?? 3}｜单库上限 ${p.ingestion?.kb_doc_limit ?? 0}｜上传 ${p.ingestion?.max_upload_mb ?? 100}MB｜输入 ${p.chat?.max_query_len ?? 2000}字`,
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

export type SectionKey = 'llm' | 'embedding' | 'mineru' | 'deepdoc' | 'mysql' | 'minio' | 'vector_store' | 'rerank';

export const emptyTest: Record<SectionKey, TestItem> = {
  llm: { status: 'idle', msg: '' },
  embedding: { status: 'idle', msg: '' },
  mineru: { status: 'idle', msg: '' },
  deepdoc: { status: 'idle', msg: '' },
  rerank: { status: 'idle', msg: '' },
  mysql: { status: 'idle', msg: '' },
  minio: { status: 'idle', msg: '' },
  vector_store: { status: 'idle', msg: '' },
};

// 测试结果 -> 展示项
export const toTestItems = (res: ProfileTestResult): Record<SectionKey, TestItem> => ({
  llm: { status: res.llm.ok ? 'success' : 'failed', msg: res.llm.message },
  embedding: { status: res.embedding.ok ? 'success' : 'failed', msg: res.embedding.message },
  mineru: { status: res.mineru.ok ? 'success' : 'failed', msg: res.mineru.message },
  deepdoc: { status: res.deepdoc.ok ? 'success' : 'failed', msg: res.deepdoc.message },
  rerank: { status: res.rerank.ok ? 'success' : 'failed', msg: res.rerank.message },
  mysql: { status: res.mysql.ok ? 'success' : 'failed', msg: res.mysql.message },
  minio: { status: res.minio.ok ? 'success' : 'failed', msg: res.minio.message },
  vector_store: { status: res.vector_store.ok ? 'success' : 'failed', msg: res.vector_store.message },
});

/** 编辑弹窗折叠面板 key → 可探测段（点击面板标题右侧"测试"按钮）；
 *  不含 ar/ingest 等无连接探测的纯参数面板（retrieval 含 rerank 连接探测） */
export const PANEL_TEST_SECTIONS: Record<string, SectionKey[]> = {
  llm: ['llm'],
  embedding: ['embedding'],
  mineru: ['mineru'],
  deepdoc: ['deepdoc'],
  retrieval: ['rerank'],
  mysql: ['mysql'],
  minio: ['minio'],
  vector_store: ['vector_store'],
};

export const sectionLabel: Record<SectionKey, string> = {
  llm: 'LLM 对话模型',
  embedding: 'Embedding 模型',
  mineru: 'MinerU 文档解析',
  deepdoc: 'DeepDoc 解析（RAGFlow）',
  rerank: 'Rerank 重排序',
  mysql: '数据库',
  minio: 'MinIO 对象存储',
  vector_store: '向量存储',
};

// 全部段测试中
export const allTesting = (): Record<SectionKey, TestItem> => ({
  llm: { status: 'testing', msg: '' },
  embedding: { status: 'testing', msg: '' },
  mineru: { status: 'testing', msg: '' },
  deepdoc: { status: 'testing', msg: '' },
  rerank: { status: 'testing', msg: '' },
  mysql: { status: 'testing', msg: '' },
  minio: { status: 'testing', msg: '' },
  vector_store: { status: 'testing', msg: '' },
});

// 全部段同一失败信息（接口整体报错时）
export const allFailed = (msg: string): Record<SectionKey, TestItem> => ({
  llm: { status: 'failed', msg },
  embedding: { status: 'failed', msg },
  mineru: { status: 'failed', msg },
  deepdoc: { status: 'failed', msg },
  rerank: { status: 'failed', msg },
  mysql: { status: 'failed', msg },
  minio: { status: 'failed', msg },
  vector_store: { status: 'failed', msg },
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
}

// 表单扁平字段 <-> 嵌套档案对象互转
export const toProfileInput = (vals: ProfileFormValues, llmSection?: {
  models: LLMModelItem[]; active: number;
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
});
