/**
 * 服务配置档案 / LLM 模型 / 聊天设置 API。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 */
import api from './http';
import type {
  ChatSettingsPayload,
  ImageSummaryPromptResult,
  LLMModelItem,
  LlmModelList,
  LlmTestResult,
  ProfileTestResult,
  ServiceProfile,
  ServiceProfileInput,
  VisionModelItem,
} from './types';

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

export const testLlmConnection = (item: Partial<LLMModelItem>) =>
  api.post<LlmTestResult>('/settings/llm/test', item);

/** 测试图片解析模型（多模态）连接：GET {base_url}/models 探活，≤5s。
 *  与 LLM 测试的差异：只探活不试推图（推图代价大），也不做脱敏密钥回查。 */
export const testVisionConnection = (item: Partial<VisionModelItem>) =>
  api.post<LlmTestResult>('/settings/vision/test', item);

// ========== 解析配置 LLM 模型（GET 模型列表 / POST 按名测连接，登录即可） ==========

/** 解析配置弹窗数据源：当前激活档案的 LLM 模型列表（登录即可读） */
export const getLlmModelList = () =>
  api.get<LlmModelList>('/settings/llm/models');

/** 按模型名测试连接（切换解析模型前调用；后端按 name 查完整配置后探测，只测不写） */
export const testLlmModelByName = (name: string) =>
  api.post<LlmTestResult>('/settings/llm/test-model', { name });

/** 图片摘要「本次实际会用的提示词」（解析入口悬浮展示，登录即可读）
 *
 *  规则只在后端一处：选了与部门配置不同的格式 → 返回该格式内置模板；
 *  相同/不传 → 部门自定义提示词优先。前端只展示，不自己拼模板，
 *  否则部门自定义过提示词时"看到的"和"实际用的"会对不上。
 *  source：custom=部门自定义 / default=内置模板。 */
export const getImageSummaryPrompt = (kbId?: string, outputFormat?: string) => {
  const qs = new URLSearchParams();
  if (kbId) qs.set('kb_id', kbId);
  if (outputFormat) qs.set('output_format', outputFormat);
  const s = qs.toString();
  return api.get<ImageSummaryPromptResult>(
    `/settings/image-summary/prompt${s ? `?${s}` : ''}`);
};

// ========== 聊天设置 + 部门 LLM 配置（GET 登录可读，POST 需 super_admin/dept_admin） ==========

export const getChatSettings = () => api.get<ChatSettingsPayload>('/settings/chat');

export const updateChatSettings = (data: ChatSettingsPayload) =>
  api.post<ChatSettingsPayload>('/settings/chat', data);

/**
 * 查看指定部门的配置（仅 super_admin；「部门配置查询」用，只读）
 *
 * 返回该部门**当前生效**的配置（全局 + 部门覆盖的合并值，llm 段密钥脱敏），
 * 其中 `dept` 段是**部门显式覆盖**的原始字段（供前端标出改过哪些）。
 */
export const getDeptConfigView = (deptId: string) =>
  api.get<ChatSettingsPayload & {
    dept_id: string;
    name: string;
    description?: string | null;
  }>(`/settings/depts/${deptId}/config`);
