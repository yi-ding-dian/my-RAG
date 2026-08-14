import React, { useState } from 'react';
import { Alert, App as AntApp, Button, Checkbox, Form, Input } from 'antd';
import {
  BookOutlined,
  LockOutlined,
  SafetyCertificateOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { APP_VERSION } from '../constants';

interface LoginFormValues {
  username: string;
  password: string;
}

/** 记住账号的 localStorage 键（仅存用户名） */
const REMEMBER_KEY = 'myrag.remembered_username';

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
      {/* 左侧：品牌区 */}
      <div className="login-panel login-panel--brand">
        <div className="login-deco login-deco--1" />
        <div className="login-deco login-deco--2" />
        <div className="login-deco login-deco--3" />
        <div className="login-brand">
          <div className="login-logo">
            <BookOutlined />
          </div>
          <h1 className="login-title">my-RAG</h1>
          <p className="login-slogan">企业知识库 · 智能问答 · 数据安全</p>
          <p className="login-desc">
            让企业文档沉淀为可检索的智能资产，
            <br />
            基于自有大模型私有化部署，回答全程可溯源。
          </p>
          <ul className="login-features">
            <li>
              <SafetyCertificateOutlined className="login-feature-icon" />
              <span>
                <b>数据安全可控</b>
                <em>私有化部署，文档与模型全内网运行</em>
              </span>
            </li>
            <li>
              <ThunderboltOutlined className="login-feature-icon" />
              <span>
                <b>秒级检索问答</b>
                <em>混合检索 + 重排，答案标注引用来源</em>
              </span>
            </li>
            <li>
              <TeamOutlined className="login-feature-icon" />
              <span>
                <b>多角色协作</b>
                <em>部门级知识隔离，权限精细可控</em>
              </span>
            </li>
          </ul>
        </div>
      </div>

      {/* 右侧：登录表单 */}
      <div className="login-panel login-panel--form">
        <div className="login-form-wrap">
          <div className="login-card">
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
              <div
                style={{
                  textAlign: 'center',
                  marginTop: 16,
                  fontSize: 12,
                  color: 'rgba(15, 23, 42, 0.45)',
                }}
              >
                忘记密码？请联系系统管理员重置
              </div>
            </Form>
          </div>

          <div className="login-version">
            my-RAG {APP_VERSION} · 企业知识库智能问答系统
          </div>
        </div>
      </div>

      <div className="login-footer">© 2026 my-RAG 企业知识库智能问答系统 · 保留所有权利</div>
    </div>
  );
};

export default LoginPage;
