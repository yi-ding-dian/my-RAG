/**
 * 认证令牌与用户信息的 localStorage 管理（独立小模块）。
 * 设计原因：AuthContext 需要持久化用户、api/client 拦截器需要读 token，
 * 若放在 Context 中会形成 AuthContext ↔ client 循环依赖，故独立成模块。
 */

const TOKEN_KEY = 'myrag.token';
const USER_KEY = 'myrag.user';

/** 登录用户信息（与后端 UserPublic 契约一致） */
export interface User {
  id: string;
  username: string;
  display_name: string;
  role: 'super_admin' | 'dept_admin' | 'user';
  department_id: string | null;
  department_name?: string | null;
  status: 'active' | 'disabled';
  created_at: string;
  /** 首次登录须强制修改密码（新建用户为 true；改密成功后清标志；旧后端可能缺失） */
  must_change_password?: boolean;
  /** 头像存储标识（如 avatars/{user_id}.png；null/缺失=默认头像，前端用默认 SVG） */
  avatar?: string | null;
}

/** 读取 token，未登录返回 null */
export const getToken = (): string | null => localStorage.getItem(TOKEN_KEY);

/** 写入 token */
export const setToken = (token: string) => localStorage.setItem(TOKEN_KEY, token);

/** 读取用户信息（JSON 损坏时容错返回 null） */
export const getUser = (): User | null => {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as User;
  } catch {
    return null;
  }
};

/** 写入/清除用户信息 */
export const setUser = (user: User | null) => {
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
  else localStorage.removeItem(USER_KEY);
};

/** 仅清除用户信息（token 保留） */
export const clearUser = () => localStorage.removeItem(USER_KEY);

/** 清除全部认证信息（token + user） */
export const clearAuth = () => {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
};

/**
 * 认证请求头：返回 {Authorization: "Bearer xxx"} 或空对象。
 * 供 axios 请求拦截器与 streamChat 的 fetch 共用（fetch 不经过 axios 拦截器）。
 */
export const authHeader = (): Record<string, string> => {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
};
