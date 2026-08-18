/**
 * 认证 / 用户管理 / 部门管理 API。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 */
import api from './http';
import { getToken } from '../auth/token';
import type { User } from '../auth/token';
import type {
  Department,
  DepartmentInput,
  LoginResponse,
  UserCreateInput,
  UserUpdateInput,
} from './types';

// ========== 认证 API ==========

export const login = (username: string, password: string) =>
  api.post<LoginResponse>('/auth/login', { username, password });

export const getMe = () => api.get<User>('/auth/me');

export const changePassword = (oldPassword: string, newPassword: string) =>
  api.post<{ message: string }>('/auth/change-password', {
    old_password: oldPassword,
    new_password: newPassword,
  });

// ========== 用户管理 API（仅 super_admin） ==========

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

export const listDepartments = () => api.get<Department[]>('/departments');

export const createDepartment = (data: DepartmentInput) =>
  api.post<Department>('/departments', data);

export const updateDepartment = (id: string, data: DepartmentInput) =>
  api.put<Department>(`/departments/${id}`, data);

export const deleteDepartment = (id: string) => api.delete(`/departments/${id}`);
