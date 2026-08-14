import React, { lazy, Suspense, useState } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useNavigate, useLocation } from 'react-router-dom';
import {
  Avatar,
  Dropdown,
  Layout,
  Menu,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
  theme,
} from 'antd';
import {
  BarChartOutlined,
  BookOutlined,
  CheckOutlined,
  DatabaseOutlined,
  DownOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  LinkOutlined,
  LogoutOutlined,
  MessageOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons';

import ProtectedRoute from './components/ProtectedRoute';
import ForcedPasswordModal from './components/ForcedPasswordModal';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { THEME_PRESETS, useTheme } from './theme';
import { APP_VERSION } from './constants';
import { avatarUrl } from './api/client';
import type { User } from './auth/token';

/* 页面按需加载（代码分割）：主 chunk 只含框架与路由，页面与业务依赖各自独立 chunk */
const ChatPage = lazy(() => import('./pages/Chat'));
const RetrievalTestPage = lazy(() => import('./pages/RetrievalTest'));
const DocumentsPage = lazy(() => import('./pages/Documents'));
const GlobalDocumentsPage = lazy(() => import('./pages/GlobalDocuments'));
const KnowledgeBasesPage = lazy(() => import('./pages/KnowledgeBases'));
const AnalyticsPage = lazy(() => import('./pages/Analytics'));
const SettingsPage = lazy(() => import('./pages/Settings'));
const UsersPage = lazy(() => import('./pages/Users'));
const ProfilePage = lazy(() => import('./pages/Profile'));
const LoginPage = lazy(() => import('./pages/Login'));
const ExtQueriesPage = lazy(() => import('./pages/ExtQueries'));
/* 外部查询公开页（无登录，独立于布局） */
const ExtQueryPage = lazy(() => import('./pages/ExtQueryPage'));

const { Sider, Content } = Layout;

/** 路由加载中占位 */
const PageLoading: React.FC = () => (
  <div
    style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      minHeight: '40vh',
    }}
  >
    <Spin tip="加载中..." />
  </div>
);

/**
 * 菜单项：新增页面只需在此数组追加一项并注册对应 Route 即可。
 * /documents 不设顶级菜单：从知识库卡片点击进入（/documents?kb_id=xxx）。
 * /settings 与 /users 对超级管理员或部门管理员可见（见 filterMenu）。
 */
const menuItems = [
  { key: '/chat', icon: <MessageOutlined />, label: '问答' },
  { key: '/retrieval-test', icon: <ExperimentOutlined />, label: '检索测试' },
  { key: '/kbs', icon: <DatabaseOutlined />, label: '知识库' },
  { key: '/analytics', icon: <BarChartOutlined />, label: '统计分析' },
  { key: '/users', icon: <TeamOutlined />, label: '用户管理' },
  { key: '/settings', icon: <SettingOutlined />, label: '系统配置' },
  { key: '/ext-queries', icon: <LinkOutlined />, label: '外部查询' },
  /* 超管专属：跨部门全局文档管理（与部门内 /documents 文档管理区分） */
  { key: '/global-documents', icon: <FileSearchOutlined />, label: '文档管理（全部）' },
];

const roleMeta: Record<User['role'], { color: string; text: string }> = {
  super_admin: { color: 'red', text: '超级管理员' },
  dept_admin: { color: 'blue', text: '部门管理员' },
  user: { color: 'default', text: '普通用户' },
};

/** 菜单过滤：/settings 与 /users 对 super_admin 与 dept_admin 开放（普通用户不可见）；
    外部查询仅 super_admin（暴露知识库的敏感配置） */
const filterMenu = (user: User | null) => {
  const isAdmin = user?.role === 'super_admin' || user?.role === 'dept_admin';
  return menuItems.filter(item => {
    if (item.key === '/settings' || item.key === '/users') return isAdmin;
    if (item.key === '/ext-queries' || item.key === '/global-documents') {
      return user?.role === 'super_admin';
    }
    return true;
  });
};

/** 侧栏底部主题预设选择器：10 个圆形色块 5×2 网格居中（前 5 浅后 5 深），hover 显示主题名，点击即时切换 */
const ThemePresetPicker: React.FC = () => {
  const { preset, setPreset } = useTheme();
  const { token } = theme.useToken();
  return (
    <div
      style={{
        borderTop: `1px solid ${token.colorBorderSecondary}`,
        marginTop: 12,
        display: 'grid',
        gridTemplateColumns: 'repeat(5, 18px)',
        justifyContent: 'center',
        gap: '8px 14px',
        padding: '12px 0 10px',
      }}
    >
      {THEME_PRESETS.map(p => {
        const active = p.key === preset.key;
        return (
          <Tooltip key={p.key} title={p.label} placement="top">
            <button
              type="button"
              aria-label={`切换主题：${p.label}`}
              onClick={() => setPreset(p.key)}
              style={{
                width: 18,
                height: 18,
                flexShrink: 0,
                borderRadius: '50%',
                padding: 0,
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: p.colorPrimary,
                // 深色主题色块加亮描边，浅色下与浅底区分
                border:
                  p.mode === 'dark'
                    ? '1px solid rgba(255, 255, 255, 0.45)'
                    : '1px solid rgba(15, 23, 42, 0.12)',
                // 当前主题：外圈主色 ring（内衬容器底色间隔）
                boxShadow: active
                  ? `0 0 0 2px ${token.colorBgContainer}, 0 0 0 4px ${p.colorPrimary}`
                  : 'none',
                outline: 'none',
              }}
            >
              {active && <CheckOutlined style={{ fontSize: 10, color: '#fff', lineHeight: 1 }} />}
            </button>
          </Tooltip>
        );
      })}
    </div>
  );
};

const AppLayout: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { token } = theme.useToken();
  const { user, logout, refreshUser } = useAuth();

  // 姓名首字（头像用）
  const initial = (user?.display_name || user?.username || 'U').trim().charAt(0).toUpperCase();
  // 侧栏头像：有头像显示代理图片，加载失败回退首字（无头像直接首字）
  const [avatarFailed, setAvatarFailed] = useState(false);

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        theme="light"
        width={220}
        style={{
          borderRight: `1px solid ${token.colorBorderSecondary}`,
          display: 'flex',
          flexDirection: 'column',
          height: '100vh',
        }}
      >
        {/* Logo 区 */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            padding: '18px 20px',
            borderBottom: `1px solid ${token.colorBorderSecondary}`,
          }}
        >
          <div
            style={{
              width: 38,
              height: 38,
              flexShrink: 0,
              borderRadius: 10,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 19,
              color: '#fff',
              background:
                'linear-gradient(135deg, var(--brand-primary, #2563eb) 0%, var(--brand-primary-deep, #1d4ed8) 100%)',
              boxShadow: '0 4px 10px rgba(var(--brand-primary-rgb, 37, 99, 235), 0.3)',
            }}
          >
            <BookOutlined />
          </div>
          <div style={{ minWidth: 0 }}>
            <Typography.Text strong style={{ fontSize: 15, display: 'block', lineHeight: 1.2 }}>
              my-RAG
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 11, display: 'block' }}>
              企业知识库问答
            </Typography.Text>
          </div>
        </div>

        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={filterMenu(user)}
          onClick={({ key }) => navigate(key)}
          style={{ borderRight: 0, marginTop: 8, flex: 1, minHeight: 0, overflowY: 'auto', padding: '0 8px' }}
        />

        {/* 底部自上而下：主题选择器 → 用户信息 → 版本号（版本号最底） */}
        {/* 主题选择器：10 个预设色块两行×5 网格居中（原"深色/浅色模式"按钮升级而来） */}
        <ThemePresetPicker />
        {/* 用户区：用户下拉（个人设置/退出登录） */}
        {user && (
          <div
            style={{
              padding: '6px 12px 8px',
              borderTop: `1px solid ${token.colorBorderSecondary}`,
              display: 'flex',
              flexDirection: 'column',
              gap: 6,
            }}
          >
            <Dropdown
              placement="top"
              trigger={['click']}
              menu={{
                items: [
                  { key: 'profile', icon: <UserOutlined />, label: '个人设置' },
                  { type: 'divider' as const },
                  { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', danger: true },
                ],
                onClick: ({ key }) => {
                  if (key === 'profile') navigate('/profile');
                  else if (key === 'logout') logout();
                },
              }}
            >
              <div className="sider-user" style={{ cursor: 'pointer' }}>
                <Space size={10} style={{ width: '100%' }}>
                  {user.avatar && !avatarFailed ? (
                    <img
                      src={avatarUrl(user.id)}
                      onError={() => setAvatarFailed(true)}
                      alt="我的头像"
                      style={{
                        width: 34,
                        height: 34,
                        flexShrink: 0,
                        borderRadius: '50%',
                        objectFit: 'cover',
                      }}
                    />
                  ) : (
                    <Avatar
                      size={34}
                      style={{
                        flexShrink: 0,
                        background:
                          'linear-gradient(135deg, var(--brand-primary, #2563eb) 0%, var(--brand-primary-deep, #1d4ed8) 100%)',
                        fontWeight: 600,
                      }}
                    >
                      {initial}
                    </Avatar>
                  )}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Typography.Text strong ellipsis style={{ fontSize: 13, display: 'block' }}>
                      {user.display_name || user.username}
                    </Typography.Text>
                    <Tag
                      color={roleMeta[user.role].color}
                      style={{ marginInlineEnd: 0, marginTop: 4, fontSize: 11, lineHeight: '18px' }}
                    >
                      {roleMeta[user.role].text}
                    </Tag>
                  </div>
                  <DownOutlined style={{ fontSize: 10, color: token.colorTextTertiary }} />
                </Space>
              </div>
            </Dropdown>
          </div>
        )}
        {/* 底部版本号（居中灰字小号） */}
        <div style={{ textAlign: 'center', padding: '0 0 14px', marginTop: 8 }}>
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>
            {APP_VERSION}
          </Typography.Text>
        </div>
      </Sider>
      {/* 首次登录强制改密：must_change_password=true 时不可关闭，改密成功后 refreshUser 自动收起 */}
      <ForcedPasswordModal
        open={user?.must_change_password === true}
        onSuccess={() => { void refreshUser(); }}
      />
      <Layout>
        <Content
          style={{
            padding: 24,
            background: token.colorBgLayout,
            overflow: 'auto',
            minHeight: '100vh',
          }}
        >
          <div key={location.pathname} className="page-fade" style={{ minHeight: '100%' }}>
            <Suspense fallback={<PageLoading />}>
              <Routes>
                <Route path="/" element={<Navigate to="/chat" replace />} />
                <Route
                  path="/chat"
                  element={
                    <ProtectedRoute>
                      <ChatPage />
                    </ProtectedRoute>
                  }
                />
                <Route
                  path="/documents"
                  element={
                    <ProtectedRoute>
                      <DocumentsPage />
                    </ProtectedRoute>
                  }
                />
                <Route
                  path="/retrieval-test"
                  element={
                    <ProtectedRoute>
                      <RetrievalTestPage />
                    </ProtectedRoute>
                  }
                />
                <Route
                  path="/kbs"
                  element={
                    <ProtectedRoute>
                      <KnowledgeBasesPage />
                    </ProtectedRoute>
                  }
                />
                <Route
                  path="/analytics"
                  element={
                    <ProtectedRoute>
                      <AnalyticsPage />
                    </ProtectedRoute>
                  }
                />
                <Route
                  path="/users"
                  element={
                    <ProtectedRoute roles={['super_admin', 'dept_admin']}>
                      <UsersPage />
                    </ProtectedRoute>
                  }
                />
                <Route
                  path="/settings"
                  element={
                    <ProtectedRoute roles={['super_admin', 'dept_admin']}>
                      <SettingsPage />
                    </ProtectedRoute>
                  }
                />
                {/* 个人设置：个人入口，不出现在菜单 */}
                <Route
                  path="/profile"
                  element={
                    <ProtectedRoute>
                      <ProfilePage />
                    </ProtectedRoute>
                  }
                />
                {/* 外部查询管理（仅 super_admin）：知识库对外开放配置 */}
                <Route
                  path="/ext-queries"
                  element={
                    <ProtectedRoute roles={['super_admin']}>
                      <ExtQueriesPage />
                    </ProtectedRoute>
                  }
                />
                {/* 全局文档管理（仅 super_admin）：跨部门查看/重命名/软删所有文档 */}
                <Route
                  path="/global-documents"
                  element={
                    <ProtectedRoute roles={['super_admin']}>
                      <GlobalDocumentsPage />
                    </ProtectedRoute>
                  }
                />
                <Route path="*" element={<Navigate to="/chat" replace />} />
              </Routes>
            </Suspense>
          </div>
        </Content>
      </Layout>
    </Layout>
  );
};

const App: React.FC = () => (
  // AuthProvider 放在 BrowserRouter 内层：内部需使用 useNavigate 做登录跳转
  <BrowserRouter>
    <AuthProvider>
      <Suspense fallback={<PageLoading />}>
        <Routes>
          {/* 登录页独立于布局之外，无守卫 */}
          <Route path="/login" element={<LoginPage />} />
          {/* 外部查询公开页（无登录、无布局）：token 走 URL query，无需系统账号 */}
          <Route path="/ext-query/:id" element={<ExtQueryPage />} />
          {/* 其余页面均受 ProtectedRoute 守卫（未登录 → /login） */}
          <Route path="/*" element={<AppLayout />} />
        </Routes>
      </Suspense>
    </AuthProvider>
  </BrowserRouter>
);

export default App;
