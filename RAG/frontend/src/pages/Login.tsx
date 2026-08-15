import React, { useState } from 'react';
import { Alert, App as AntApp, Button, Checkbox, Form, Input } from 'antd';
import { BookOutlined, LockOutlined, UserOutlined } from '@ant-design/icons';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { APP_VERSION } from '../constants';

interface LoginFormValues {
  username: string;
  password: string;
}

/** 记住账号的 localStorage 键（仅存用户名） */
const REMEMBER_KEY = 'myrag.remembered_username';

/**
 * 登录页书籍装饰数据（纯 CSS 绘制，无图片）。
 * 每本 = 一个完整圆角长条书脊，饱满不透明彩色；h 为书脊高度(px)，
 * fg 为书名前景色（按书脊明度选白/深）。height 总和需 ≤ 书架容器高度（见 index.css）。
 */
const SPINE_BOOKS_LEFT = [
  { name: '员工手册', color: '#4f46e5', fg: '#ffffff', h: 92 },
  { name: '工程文档', color: '#0284c7', fg: '#ffffff', h: 55 },
  { name: '技术规范', color: '#059669', fg: '#ffffff', h: 78 },
  { name: '产品手册', color: '#e11d48', fg: '#ffffff', h: 55 },
  { name: '部门文档', color: '#7c3aed', fg: '#ffffff', h: 66 },
  { name: '行业标准', color: '#0d9488', fg: '#ffffff', h: 88 },
  { name: '操作指南', color: '#f59e0b', fg: '#451a03', h: 56 },
  { name: '研发日志', color: '#9333ea', fg: '#ffffff', h: 72 },
  { name: '安全规程', color: '#dc2626', fg: '#ffffff', h: 60 },
];
const SPINE_BOOKS_RIGHT = [
  { name: '质量手册', color: '#2563eb', fg: '#ffffff', h: 96 },
  { name: '企业制度', color: '#475569', fg: '#ffffff', h: 60 },
  { name: '培训教程', color: '#c026d3', fg: '#ffffff', h: 80 },
  { name: '项目资料', color: '#0891b2', fg: '#ffffff', h: 56 },
  { name: '测试用例', color: '#db2777', fg: '#ffffff', h: 68 },
  { name: '运维手册', color: '#65a30d', fg: '#ffffff', h: 92 },
  { name: '会议纪要', color: '#ea580c', fg: '#ffffff', h: 62 },
  { name: '数据安全手册', color: '#4f46e5', fg: '#ffffff', h: 84 },
];

/** 登录页：左侧品牌区（渐变背景 + 装饰 + 价值主张）+ 右侧登录表单卡片 */
const LoginPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const { login } = useAuth();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 记住账号：加载时回填已记住的用户名
  const [rememberedName] = useState<string>(() => {
    try {
      return localStorage.getItem(REMEMBER_KEY) ?? '';
    } catch {
      return '';
    }
  });
  const [remember, setRemember] = useState(() => {
    try {
      return !!localStorage.getItem(REMEMBER_KEY);
    } catch {
      return false;
    }
  });

  // 受保护路由重定向过来时携带 from（登录后回跳）
  const from =
    (location.state as { from?: { pathname?: string } } | null)?.from?.pathname ?? '/chat';

  const handleSubmit = async (values: LoginFormValues) => {
    setError(null);
    setLoading(true);
    try {
      await login(values.username, values.password);
      // 记住账号：勾选保存用户名，取消则清除
      try {
        if (remember) localStorage.setItem(REMEMBER_KEY, values.username.trim());
        else localStorage.removeItem(REMEMBER_KEY);
      } catch {
        // 忽略存储异常
      }
      message.success('登录成功');
      navigate(from, { replace: true });
    } catch (e: any) {
      setError(e.response?.data?.detail || '登录失败，请检查用户名与密码后重试');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      {/* 氛围装饰：柔和光晕 + 散点（纯视觉，pointer-events: none） */}
      <div className="login-aurora login-aurora--1" />
      <div className="login-aurora login-aurora--2" />
      <div className="login-dots" />

      {/* 书籍/书架装饰（知识库意象，纯 CSS，pointer-events: none；小屏隐藏） */}
      <div className="login-bookshelf login-bookshelf--left" aria-hidden="true">
        {SPINE_BOOKS_LEFT.map((b, i) => (
          <span
            key={i}
            className="login-book login-book--spine login-book--titled"
            style={
              {
                height: b.h,
                background: b.color,
                '--book-fg': b.fg,
                '--book-t': `"${b.name}"`,
              } as React.CSSProperties
            }
          />
        ))}
        <span className="login-shelf-board" />
      </div>
      <div className="login-bookshelf login-bookshelf--right" aria-hidden="true">
        {SPINE_BOOKS_RIGHT.map((b, i) => (
          <span
            key={i}
            className="login-book login-book--spine login-book--titled"
            style={
              {
                height: b.h,
                background: b.color,
                '--book-fg': b.fg,
                '--book-t': `"${b.name}"`,
              } as React.CSSProperties
            }
          />
        ))}
        <span className="login-shelf-board" />
      </div>

      {/* 中央登录卡片：顶部品牌区（logo + 名称 + slogan）+ 表单区 */}
      <div className="login-card">
        <div className="login-brand">
          <div className="login-logo">
            <BookOutlined />
          </div>
          <h1 className="login-title">my-RAG</h1>
          <p className="login-slogan">企业知识库 · 智能问答 · 数据安全</p>
        </div>

        <h2 className="login-card-title">欢迎登录</h2>
        <p className="login-subtitle">请输入账号密码进入系统</p>

        {error && (
          <Alert
            type="error"
            showIcon
            message={error}
            closable
            onClose={() => setError(null)}
            style={{ marginBottom: 18 }}
          />
        )}

        <Form
          layout="vertical"
          onFinish={handleSubmit}
          size="large"
          initialValues={{ username: rememberedName }}
        >
          <Form.Item
            name="username"
            rules={[{ required: true, message: '请输入用户名' }]}
            style={{ marginBottom: 18 }}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名" autoFocus autoComplete="username" />
          </Form.Item>
          <Form.Item
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
            style={{ marginBottom: 10 }}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="密码"
              autoComplete="current-password"
            />
          </Form.Item>
          <div className="login-form-row">
            <Checkbox checked={remember} onChange={e => setRemember(e.target.checked)}>
              记住账号
            </Checkbox>
          </div>
          <Button type="primary" htmlType="submit" block loading={loading}>
            登 录
          </Button>
          {/* 忘记密码指引（L3）：仅引导文案，无实际跳转 */}
          <div className="login-forgot">忘记密码？请联系系统管理员重置</div>
        </Form>
      </div>

      <div className="login-version">my-RAG {APP_VERSION} · 企业知识库智能问答系统</div>
      <div className="login-footer">© 2026 my-RAG 企业知识库智能问答系统 · 保留所有权利</div>
    </div>
  );
};

export default LoginPage;
