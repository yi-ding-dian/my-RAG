/**
 * 服务配置档案 / LLM 模型 / 聊天设置 API。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 */
import api from './http';
import type {
  ChatSettingsPayload,
  LLMModelItem,
  LlmModelList,
  LlmTestResult,
  ProfileTestResult,
  ServiceProfile,
  ServiceProfileInput,
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

// ========== 解析配置 LLM 模型（GET 模型列表 / POST 按名测连接，登录即可） ==========

/** 解析配置弹窗数据源：当前激活档案的 LLM 模型列表（登录即可读） */
export const getLlmModelList = () =>
  api.get<LlmModelList>('/settings/llm/models');

/** 按模型名测试连接（切换解析模型前调用；后端按 name 查完整配置后探测，只测不写） */
export const testLlmModelByName = (name: string) =>
  api.post<LlmTestResult>('/settings/llm/test-model', { name });

// ========== 聊天设置 + 部门 LLM 配置（GET 登录可读，POST 需 super_admin/dept_admin） ==========

export const getChatSettings = () => api.get<ChatSettingsPayload>('/settings/chat');

export const updateChatSettings = (data: ChatSettingsPayload) =>
  api.post<ChatSettingsPayload>('/settings/chat', data);
