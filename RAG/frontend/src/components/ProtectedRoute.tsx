import React from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { Spin } from 'antd';
import { useAuth } from '../auth/AuthContext';
import type { User } from '../auth/token';

interface ProtectedRouteProps {
  /** 允许访问的角色列表（缺省 = 任意登录用户） */
  roles?: User['role'][];
  children: React.ReactNode;
}

/**
 * 路由守卫：
 * 1. 会话恢复中（loading）→ 居中 Spin
 * 2. 未登录 → 重定向 /login（携带 from 回跳）
 * 3. 已登录但角色不匹配 → 重定向 /chat
 */
const ProtectedRoute: React.FC<ProtectedRouteProps> = ({ roles, children }) => {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          minHeight: '60vh',
        }}
      >
        <Spin size="large" tip="加载中..." />
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  if (roles && !roles.includes(user.role)) {
    return <Navigate to="/chat" replace />;
  }

  return <>{children}</>;
};

export default ProtectedRoute;
