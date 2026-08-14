import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
});

export interface DatasetListItem {
  id: string;
  name: string;
  description: string;
  file_name: string;
  row_count: number;
  columns: string[];
  created_at: string;
}

export interface DatasetUploadResponse {
  id: string;
  name: string;
  row_count: number;
  columns: string[];
  message: string;
}

export interface DatasetPreview {
  id: string;
  columns: string[];
  rows: Record<string, unknown>[];
  total: number;
}

export interface MetricInfo {
  key: string;
  label: string;
  desc: string;
}

export interface EvalConfig {
  dataset_id: string;
  metrics: string[];
  use_retrieval: boolean;
  retrieval_top_k: number;
  llm_temperature: number;
  llm_max_tokens: number;
  llm_max_workers: number;
  batch_size: number;
  name: string;
}

export interface EvalTask {
  id: string;
  name: string;
  dataset_id: string;
  dataset_name: string;
  status: 'queued' | 'pending' | 'running' | 'completed' | 'failed';
  progress: number;
  message: string;
  config: EvalConfig;
  error?: string;
  eta_seconds?: number | null;
  created_at: string;
  started_at?: string;
  completed_at?: string;
}

export interface EvalTaskListItem {
  id: string;
  name: string;
  dataset_id: string;
  dataset_name: string;
  status: string;
  progress: number;
  metrics: string[];
  created_at: string;
  completed_at?: string;
  error?: string;
}

export interface EvalResult {
  question: string;
  answer: string;
  contexts: string[];
  ground_truth?: string;
  scores: Record<string, number | null>;
  errors: Record<string, string | null>;
}

export interface AggregateScores {
  scores: Record<string, number>;
  count: number;
}

export interface EvalResults {
  task_id: string;
  task_name: string;
  status: string;
  aggregate: AggregateScores;
  results: EvalResult[];
  created_at: string;
  completed_at?: string;
}

// 数据集 API
export const uploadDataset = (file: File, name?: string, description?: string) => {
  const form = new FormData();
  form.append('file', file);
  if (name) form.append('name', name);
  if (description) form.append('description', description);
  return api.post<DatasetUploadResponse>('/datasets/upload', form);
};

export const listDatasets = () => api.get<DatasetListItem[]>('/datasets');

export const getDataset = (id: string, limit = 50, offset = 0) =>
  api.get<DatasetPreview>(`/datasets/${id}`, { params: { limit, offset } });

export const deleteDataset = (id: string) => api.delete(`/datasets/${id}`);

export const createDataset = (name: string, description?: string) =>
  api.post<DatasetUploadResponse>('/datasets/create', { name, description });

export const addSample = (datasetId: string, data: {
  question: string; answer?: string; contexts?: string[]; ground_truth?: string;
}) => api.post(`/datasets/${datasetId}/samples`, data);

export const updateSample = (datasetId: string, idx: number, data: {
  question?: string; answer?: string; contexts?: string[]; ground_truth?: string;
}) => api.put(`/datasets/${datasetId}/samples/${idx}`, data);

export const deleteSample = (datasetId: string, idx: number) =>
  api.delete(`/datasets/${datasetId}/samples/${idx}`);

// 评估 API
export const getMetrics = () => api.get<Record<string, MetricInfo>>('/evaluations/metrics');

export const createEvaluation = (config: EvalConfig) =>
  api.post<EvalTask>('/evaluations', config);

export const listEvaluations = () => api.get<EvalTaskListItem[]>('/evaluations');

export const getEvaluation = (id: string) => api.get<EvalTask>(`/evaluations/${id}`);

export const cancelEvaluation = (id: string) =>
  api.post(`/evaluations/${id}/cancel`);

export const getEvaluationLogs = (id: string, since = 0) =>
  api.get<{task_id: string; logs: Array<{time: string; level: string; message: string}>; total: number}>(`/evaluations/${id}/logs`, { params: { since } });

export const getEvaluationResults = (id: string) =>
  api.get<EvalResults>(`/evaluations/${id}/results`);

export const deleteEvaluation = (id: string) =>
  api.delete(`/evaluations/${id}`);

// 提示词管理 API
export interface PromptItem {
  name: string;
  en: string;
  zh: string;
  edited: boolean;
}

export interface MetricPromptsSummary {
  metric: string;
  name: string;
  desc: string;
  prompt_count: number;
  has_chinese: boolean;
}

export interface MetricPromptsDetail {
  metric: string;
  name: string;
  desc: string;
  prompts: PromptItem[];
  active_language: string;
}

export const getPrompts = () => api.get<MetricPromptsSummary[]>('/prompts');

export const getMetricPrompts = (metric: string) =>
  api.get<MetricPromptsDetail>(`/prompts/${metric}`);

export const translateMetricPrompts = (metric: string) =>
  api.post<MetricPromptsDetail>(`/prompts/${metric}/translate`);

export const updateMetricPrompts = (metric: string, data: { prompts: { name: string; zh: string }[] }) =>
  api.put<MetricPromptsDetail>(`/prompts/${metric}`, data);

export const getActiveLanguage = () =>
  api.get<{ language: string }>('/prompts/active-language');

export const setActiveLanguage = (language: string) =>
  api.put<{ language: string }>('/prompts/active-language', { language });

export const checkLlmStatus = () =>
  api.get<{ available: boolean }>('/prompts/llm-status');

// 导出
export const getExportUrl = (taskId: string, format: 'json' | 'csv' | 'html') =>
  `/api/results/${taskId}/export/${format}`;

// 配置档案 API
export interface Profile {
  id: string;
  name: string;
  llm_base_url: string;
  llm_api_key: string;
  llm_model: string;
  llm_temperature: number;
  llm_max_tokens: number;
  embedding_base_url: string;
  embedding_api_key: string;
  embedding_model: string;
  es_host: string;
  es_port: number;
  es_user: string;
  es_password: string;
}

export const listProfiles = () => api.get<Profile[]>('/settings/profiles');

export const getActiveProfile = () => api.get<Profile>('/settings/profiles/active');

export const createProfile = (data: Partial<Profile> & { name: string }) =>
  api.post<Profile>('/settings/profiles', data);

export const updateProfile = (id: string, data: Partial<Profile>) =>
  api.put<Profile>(`/settings/profiles/${id}`, data);

export const deleteProfile = (id: string) =>
  api.delete(`/settings/profiles/${id}`);

export const activateProfile = (id: string) =>
  api.post<{message: string; profile: Profile}>(`/settings/profiles/${id}/activate`);

export const testLlmConnection = (data: {
  llm_base_url: string; llm_api_key: string; llm_model: string;
}) => api.post<{status: string; message: string}>('/settings/test/llm', data);

export const testEmbeddingConnection = (data: {
  embedding_base_url: string; embedding_api_key: string; embedding_model: string;
}) => api.post<{status: string; message: string}>('/settings/test/embedding', data);

export const testEsConnection = (data: {
  es_host: string; es_port: number; es_user: string; es_password: string;
}) => api.post<{status: string; message: string}>('/settings/test/es', data);

export default api;
