/**
 * 个人设置页（/profile）：个人入口，不出现在左侧菜单。
 * - 基本信息卡片：用户名/显示名/角色/部门/状态/注册时间（只读）
 * - 修改密码：调现有 changePassword API，成功后退出并重新登录
 * - 用户画像与偏好：查看/编辑/删除自己的画像条目 + 个性化开关 + 一键清空
 *   （对话结束后系统异步提取，仅本人可编辑；管理员在用户管理页只读查看）
 * - 首选项：暗色模式开关（与全局 ThemeContext 联动）
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
  Upload,
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  LockOutlined,
  SafetyOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  asApiError, avatarUrl, changePassword, deleteAvatar, deleteUserMemory,
  getUserMemory, updateUserMemory, uploadAvatar } from '../api/client';
import type {
  UserMemory, UserMemoryItem, UserMemoryType, UserMemoryUpdate } from '../api/client';
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

  // ---------- 用户画像与偏好 ----------
  const [memory, setMemory] = useState<UserMemory | null>(null);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memorySaving, setMemorySaving] = useState(false);
  const [memoryModalOpen, setMemoryModalOpen] = useState(false);
  const [editingMemoryItem, setEditingMemoryItem] =
    useState<UserMemoryItem | null>(null);
  const [memoryForm] = Form.useForm();

  const loadMemory = useCallback(async () => {
    if (!user) return;
    setMemoryLoading(true);
    try {
      const res = await getUserMemory(user.id);
      setMemory(res.data);
    } catch {
      message.error('加载用户画像失败');
    } finally {
      setMemoryLoading(false);
    }
  }, [message, user]);

  useEffect(() => {
    loadMemory();
  }, [loadMemory]);

  const saveMemory = async (data: UserMemoryUpdate): Promise<boolean> => {
    if (!user) return false;
    setMemorySaving(true);
    try {
      const res = await updateUserMemory(user.id, data);
      setMemory(res.data);
      return true;
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '保存失败，请重试');
      return false;
    } finally {
      setMemorySaving(false);
    }
  };

  const handleToggleMemory = async (checked: boolean) => {
    if (await saveMemory({ enabled: checked })) {
      message.success(checked ? '已开启个性化' : '已关闭个性化');
    }
  };

  const openEditMemoryItem = (item: UserMemoryItem) => {
    setEditingMemoryItem(item);
    memoryForm.setFieldsValue({ type: item.type, content: item.content });
    setMemoryModalOpen(true);
  };

  const handleMemoryItemSubmit = async () => {
    let values: { type: UserMemoryType; content: string };
    try {
      values = await memoryForm.validateFields();
    } catch {
      return;
    }
    if (!memory || !editingMemoryItem) return;
    const items = memory.items.map(i =>
      i.id === editingMemoryItem.id
        ? { id: i.id, type: values.type, content: values.content }
        : { id: i.id, type: i.type, content: i.content });
    if (await saveMemory({ items })) {
      message.success('画像条目已更新');
      setMemoryModalOpen(false);
    }
  };

  const handleDeleteMemoryItem = async (itemId: string) => {
    if (!user) return;
    try {
      await deleteUserMemory(user.id, itemId);
      message.success('条目已删除');
      await loadMemory();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  const handleClearMemory = async () => {
    if (!user) return;
    setMemorySaving(true);
    try {
      await deleteUserMemory(user.id);
      message.success('用户画像已清空');
      await loadMemory();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '清空失败');
    } finally {
      setMemorySaving(false);
    }
  };

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
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '上传头像失败，请重试');
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
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '恢复默认头像失败，请重试');
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
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '修改密码失败，请重试');
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

      {/* 用户画像与偏好（个性化记忆） */}
      <Card
        title="用户画像与偏好"
        style={{ marginTop: 16 }}
        extra={
          memory && memory.items.length > 0 ? (
            <Popconfirm
              title="确定清空全部画像条目？"
              description="清空后聊天不再使用个性化信息，可通过对话重新生成"
              okText="清空"
              okButtonProps={{ danger: true }}
              onConfirm={handleClearMemory}
            >
              <Button danger size="small" loading={memorySaving}>
                清空全部
              </Button>
            </Popconfirm>
          ) : undefined
        }
      >
        <div style={{ marginBottom: 12 }}>
          <Space>
            <Switch
              checked={memory?.memory_enabled ?? true}
              loading={memorySaving}
              onChange={handleToggleMemory}
            />
            <Text strong>开启个性化记忆</Text>
          </Space>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              对话结束后系统会自动从聊天中提取你的稳定事实与偏好（职位/行业/沟通风格等），
              并在后续问答中作为背景参考；可随时编辑或删除，关闭开关后不再使用。
            </Text>
          </div>
        </div>
        {memory && memory.items.length > 0 ? (
          <List
            size="small"
            loading={memoryLoading}
            dataSource={memory.items}
            rowKey="id"
            renderItem={item => (
              <List.Item
                actions={[
                  <Button
                    key="edit"
                    size="small"
                    icon={<EditOutlined />}
                    onClick={() => openEditMemoryItem(item)}
                  />,
                  <Popconfirm
                    key="del"
                    title="删除该条目？"
                    onConfirm={() => handleDeleteMemoryItem(item.id)}
                  >
                    <Button size="small" danger icon={<DeleteOutlined />} />
                  </Popconfirm>,
                ]}
              >
                <Space wrap>
                  <Tag color={item.type === 'profile' ? 'blue' : 'purple'}>
                    {item.type === 'profile' ? '画像' : '偏好'}
                  </Tag>
                  <Text>{item.content}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    把握度 {Math.round(item.confidence * 100)}%
                  </Text>
                </Space>
              </List.Item>
            )}
          />
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={memoryLoading
              ? '加载中…'
              : '暂无画像条目，多聊几轮后系统会自动生成'}
          />
        )}
      </Card>

      {/* 编辑画像条目弹窗 */}
      <Modal
        title="编辑画像条目"
        open={memoryModalOpen}
        onOk={handleMemoryItemSubmit}
        onCancel={() => setMemoryModalOpen(false)}
        confirmLoading={memorySaving}
        okText="保存"
        cancelText="取消"
        width={480}
      >
        <Form form={memoryForm} layout="vertical">
          <Form.Item
            name="type"
            label="类型"
            rules={[{ required: true, message: '请选择类型' }]}
          >
            <Select
              options={[
                { value: 'profile', label: '画像（身份/背景事实）' },
                { value: 'preference', label: '偏好（沟通/格式偏好）' },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="content"
            label="内容"
            rules={[
              { required: true, message: '请输入内容' },
              { max: 500, message: '最多 500 字' },
            ]}
          >
            <Input.TextArea
              rows={3}
              maxLength={500}
              showCount
              placeholder="例如：从事电力行业，SCA 系统调试"
            />
          </Form.Item>
        </Form>
      </Modal>

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
