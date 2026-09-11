/**
 * 认证上下文：AuthProvider + useAuth()
 * - 挂载时若有 token 调 /api/auth/me 恢复会话（失败清 token）
 * - login() 调 /api/auth/login 并持久化 token + user
 * - logout() 清空本地认证并跳转 /login
 * - refreshUser() 重新拉取当前用户信息（如密码/角色变更后刷新）
 * 项目无状态管理库，使用 React Context（挂在 BrowserRouter 内层，可安全使用路由跳转）。
 */
import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { getMe, login as apiLogin } from '../api/client';
import {
  User, clearAuth, getToken, getUser, setToken, setUser as persistUser,
} from './token';

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<User>;
  logout: () => void;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const navigate = useNavigate();

  // 初始值取 localStorage（登录页刷新不闪 loading）
  const [user, setUser] = useState<User | null>(() => getUser());
  const [loading, setLoading] = useState(true);

  // 挂载时：有 token 则调 /me 恢复会话；失败（失效/禁用/网络）清 token
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!getToken()) {
        setLoading(false);
        return;
      }
      try {
        const res = await getMe();
        if (!cancelled) {
          setUser(res.data);
          persistUser(res.data);
        }
      } catch {
        if (!cancelled) {
          clearAuth();
          setUser(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await apiLogin(username, password);
    setToken(res.data.access_token);
    persistUser(res.data.user);
    setUser(res.data.user);
    return res.data.user;
  }, []);

  const logout = useCallback(() => {
    clearAuth();
    setUser(null);
    navigate('/login', { replace: true });
  }, [navigate]);

  const refreshUser = useCallback(async () => {
    const res = await getMe();
    setUser(res.data);
    persistUser(res.data);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ user, loading, login, logout, refreshUser }),
    [user, loading, login, logout, refreshUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

/** 读取认证上下文（必须在 AuthProvider 内使用） */
export const useAuth = (): AuthContextValue => {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return ctx;
};
