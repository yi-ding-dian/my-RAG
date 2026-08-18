/**
 * axios 实例与拦截器（认证头注入 / 401 统一跳转登录）。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 */
import axios from 'axios';
import { authHeader, clearAuth } from '../auth/token';

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

/**
 * axios 错误的最小结构（前端用到的字段；catch 中收窄 unknown 用，
 * 替代 `catch (e: any)` 后的属性访问，行为与原 any 直访一致）。
 */
export interface ApiErrorShape {
  response?: { status?: number; data?: { detail?: string; message?: string } };
  message?: string;
  code?: string;
  name?: string;
}

/** 从任意 catch 值中安全提取 API 错误（非对象值返回空壳，等价原 `e.xxx` 的 undefined 语义） */
export const asApiError = (e: unknown): ApiErrorShape =>
  typeof e === 'object' && e !== null ? (e as ApiErrorShape) : {};

export default api;
