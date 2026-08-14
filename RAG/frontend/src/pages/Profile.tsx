/**
 * 个人设置页（/profile）：个人入口，不出现在左侧菜单。
 * - 基本信息卡片：用户名/显示名/角色/部门/状态/注册时间（只读）
 * - 修改密码：调现有 changePassword API，成功后退出并重新登录
 * - 首选项：暗色模式开关（与全局 ThemeContext 联动）
 */
import React, { useState } from 'react';
import {
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Radio,
  Space,
  Tag,
  Typography,
  Upload,
} from 'antd';
import { LockOutlined, SafetyOutlined, UploadOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { avatarUrl, changePassword, deleteAvatar, uploadAvatar } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import type { User } from '../auth/token';
import { THEME_PRESETS, useTheme } from '../theme';
import type { PresetKey } from '../theme';
import PageHeader from '../components/PageHeader';

const { Text } = Typography;

const roleMeta: Record<User['role'], { color: string; text: string }> = {
  super_admin: { color: 'red', text: '超级管理员' },
  dept_admin: { color: 'blue', text: '部门管理员' },
  user: { color: 'default', text: '普通用户' },
};

/** 头像白名单（与后端校验一致）：jpg/jpeg/png/webp/gif，≤1MB */
const AVATAR_EXT_RE = /\.(jpe?g|png|webp|gif)$/i;
const AVATAR_MAX_BYTES = 1024 * 1024;

interface PasswordFormValues {
  old_password: string;
  new_password: string;
  confirm_password: string;
}

const ProfilePage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { user, logout, refreshUser } = useAuth();
  const { preset, setPreset } = useTheme();
  const [submitting, setSubmitting] = useState(false);
  // 头像上传/删除 loading + 代理加载失败回退默认（头像更换后自动重置重试）
  const [uploading, setUploading] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [avatarFailed, setAvatarFailed] = useState(false);

  const handleUploadAvatar = async (file: File) => {
    if (!AVATAR_EXT_RE.test(file.name)) {
      message.error('仅支持 jpg/png/webp/gif 格式的图片');
      return false;
    }
    if (file.size > AVATAR_MAX_BYTES) {
      message.error('头像大小不能超过 1MB');
      return false;
    }
    setUploading(true);
    try {
      await uploadAvatar(file);
      setAvatarFailed(false);
      message.success('头像已更新，聊天中将同步显示');
      await refreshUser(); // 刷新全局 user（localStorage 同步持久化），聊天页即时生效
    } catch (e: any) {
      message.error(e.response?.data?.detail || '上传头像失败，请重试');
    } finally {
      setUploading(false);
    }
    return false; // 阻止 antd 自动上传（已手动调用接口）
  };

  const handleRemoveAvatar = async () => {
    setRemoving(true);
    try {
      await deleteAvatar();
      setAvatarFailed(false);
      message.success('已恢复默认头像');
      await refreshUser();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '恢复默认头像失败，请重试');
    } finally {
      setRemoving(false);
    }
  };

  // 有头像走鉴权代理 URL；无头像/代理加载失败回退默认 SVG
  const avatarSrc =
    user?.avatar && !avatarFailed ? avatarUrl(user.id) : '/default-avatar.svg';

  const handleChangePassword = async (values: PasswordFormValues) => {
    setSubmitting(true);
    try {
      await changePassword(values.old_password, values.new_password);
      message.success('密码修改成功，请重新登录');
      // 现有后端 changePassword 不返回新 token，统一退出引导重新登录
      window.setTimeout(() => logout(), 800);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '修改密码失败，请重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div>
      <PageHeader title="个人设置" description="查看个人信息、修改登录密码与界面偏好" />

      {/* 基本信息 */}
      <Card title="基本信息" style={{ marginTop: 16 }}>
        {/* 头像区：当前头像 + 更换/恢复默认（上传成功后 refreshUser 全局生效） */}
        {user && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 20, marginBottom: 20 }}>
            <img
              src={avatarSrc}
              onError={() => setAvatarFailed(true)}
              alt="我的头像"
              style={{ width: 64, height: 64, borderRadius: '50%', objectFit: 'cover', flexShrink: 0 }}
            />
            <Space direction="vertical" size={4}>
              <Space>
                <Upload
                  accept="image/*"
                  showUploadList={false}
                  beforeUpload={handleUploadAvatar}
                >
                  <Button icon={<UploadOutlined />} loading={uploading}>
                    更换头像
                  </Button>
                </Upload>
                {user.avatar && (
                  <Button loading={removing} onClick={handleRemoveAvatar}>
                    恢复默认
                  </Button>
                )}
              </Space>
              <Text type="secondary" style={{ fontSize: 12 }}>
                支持 jpg/png/webp/gif，不超过 1MB
              </Text>
            </Space>
          </div>
        )}
        {user && (
          <Descriptions
            column={{ xs: 1, sm: 2 }}
            items={[
              { key: 'username', label: '用户名', children: user.username },
              { key: 'display_name', label: '显示名', children: user.display_name || '-' },
              {
                key: 'role',
                label: '角色',
                children: <Tag color={roleMeta[user.role].color}>{roleMeta[user.role].text}</Tag>,
              },
              { key: 'department', label: '所属部门', children: user.department_name || '未分配' },
              {
                key: 'status',
                label: '账号状态',
                children:
                  user.status === 'active' ? (
                    <Tag color="success">正常</Tag>
                  ) : (
                    <Tag color="error">已禁用</Tag>
                  ),
              },
              {
                key: 'created_at',
                label: '注册时间',
                children: user.created_at
                  ? dayjs(user.created_at).format('YYYY-MM-DD HH:mm:ss')
                  : '-',
              },
            ]}
          />
        )}
      </Card>

      {/* 修改密码 */}
      <Card title="修改密码" style={{ marginTop: 16 }}>
        <Form<PasswordFormValues>
          layout="vertical"
          style={{ maxWidth: 460 }}
          onFinish={handleChangePassword}
        >
          <Form.Item
            name="old_password"
            label="旧密码"
            rules={[{ required: true, message: '请输入旧密码' }]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="当前使用的密码" />
          </Form.Item>
          <Form.Item
            name="new_password"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 6, message: '新密码至少 6 位' },
            ]}
          >
            <Input.Password prefix={<SafetyOutlined />} placeholder="至少 6 位" autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="confirm_password"
            label="确认新密码"
            dependencies={['new_password']}
            rules={[
              { required: true, message: '请再次输入新密码' },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('new_password') === value) {
                    return Promise.resolve();
                  }
                  return Promise.reject(new Error('两次输入的密码不一致'));
                },
              }),
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="再次输入新密码" autoComplete="new-password" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Space>
              <Button type="primary" htmlType="submit" loading={submitting}>
                确认修改
              </Button>
              <Text type="secondary" style={{ fontSize: 12 }}>
                修改成功后需使用新密码重新登录
              </Text>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {/* 首选项 */}
      <Card title="首选项" style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 12 }}>
          <Text strong>主题预设</Text>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              10 种明暗配色一键切换（侧边栏底部也可快速切换），偏好会自动保存
            </Text>
          </div>
        </div>
        <Radio.Group
          value={preset.key}
          onChange={e => setPreset(e.target.value as PresetKey)}
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(5, max-content)',
            columnGap: 24,
            rowGap: 10,
          }}
        >
          {THEME_PRESETS.map(p => (
            <Radio key={p.key} value={p.key}>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <span
                  style={{
                    width: 10,
                    height: 10,
                    borderRadius: '50%',
                    display: 'inline-block',
                    background: p.colorPrimary,
                    border:
                      p.mode === 'dark'
                        ? '1px solid rgba(255, 255, 255, 0.45)'
                        : '1px solid rgba(15, 23, 42, 0.12)',
                  }}
                />
                {p.label}
              </span>
            </Radio>
          ))}
        </Radio.Group>
        {/* 版本号已挪至侧边栏底部（App.tsx），此处不再重复展示 */}
      </Card>
    </div>
  );
};

export default ProfilePage;
