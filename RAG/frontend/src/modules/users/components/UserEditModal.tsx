/**
 * 新建/编辑用户弹窗。
 *
 * 从 Users.tsx 抽出来：表单 + 提交逻辑自成一体，只依赖"部门下拉候选"与权限标志。
 *
 * 权限细节（与后端一致）：
 * - 部门管理员不可调整成员部门（不注册该字段，提交时也不携带）
 * - 编辑自己时不可改角色、不可禁用自己（后端 400，前端先禁用）
 */
import { useEffect, useState } from 'react';
import {
  App as AntApp, Form, Input, Select,
} from 'antd';

import AppModal from '../../../shared/components/common/AppModal';
import {
  asApiError, createUser, updateUser,
  Department, User, UserCreateInput, UserUpdateInput,
} from '../../../shared/api/client';
import { deptOptions, passwordStrengthRules } from '../shared';

export interface UserEditModalProps {
  open: boolean;
  /** null = 新建 */
  editingUser: User | null;
  /** 部门下拉候选 */
  departments: Department[];
  isDeptAdmin: boolean;
  /** 当前登录用户（判"编辑自己"的限制） */
  me: User | null;
  onClose: () => void;
  /** 保存成功：刷新用户列表 */
  onSaved: () => Promise<void>;
  /** 编辑的是自己时刷新会话用户 */
  onRefreshMe: () => Promise<void>;
}

export const UserEditModal = ({
  open, editingUser, departments, isDeptAdmin, me, onClose, onSaved, onRefreshMe,
}: UserEditModalProps) => {
  const { message } = AntApp.useApp();
  const [submitting, setSubmitting] = useState(false);
  const [userForm] = Form.useForm();

  // 打开（或切换编辑对象）时回填表单。
  // 原先这段在页面的 openCreateUser / openEditUser 里，但 form 实例已随本组件，
  // 回填理应由组件自己做——父组件只需给 editingUser。
  useEffect(() => {
    if (!open) return;
    userForm.resetFields();
    if (editingUser) {
      userForm.setFieldsValue({
        display_name: editingUser.display_name,
        role: editingUser.role,
        // 部门管理员不可调整成员部门：不注册该字段（提交时也不携带）
        ...(isDeptAdmin ? {} : { department_id: editingUser.department_id ?? '' }),
        status: editingUser.status,
        password: '',
      });
    } else {
      userForm.setFieldsValue({ role: 'user' });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editingUser]);

  const handleSubmit = async () => {
    let values: {
      username?: string;
      password?: string;
      display_name: string;
      role: User['role'];
      department_id: string;
      status?: 'active' | 'disabled';
    };
    try {
      values = await userForm.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      if (editingUser) {
        // 部分更新：传哪个改哪个；密码留空则不重置
        const data: UserUpdateInput = {
          display_name: values.display_name,
          role: values.role,
          status: values.status,
        };
        // 部门管理员不可调整成员部门（后端亦会拒绝跨部门变更）
        if (!isDeptAdmin) data.department_id = values.department_id || null;
        if (values.password) data.password = values.password;
        await updateUser(editingUser.id, data);
        message.success('用户已更新');
        // 编辑的是自己时同步刷新当前会话用户
        if (editingUser.id === me?.id) await onRefreshMe();
      } else {
        const data: UserCreateInput = {
          username: values.username!,
          password: values.password!,
          display_name: values.display_name,
          role: values.role,
        };
        // 部门管理员创建的用户强制归属本部门（后端同样强制覆盖）
        data.department_id = isDeptAdmin
          ? (me?.department_id ?? null)
          : (values.department_id || null);
        await createUser(data);
        message.success('用户创建成功');
      }
      onClose();
      await onSaved();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || (editingUser ? '更新失败' : '创建失败'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 520, h: 420 }}
      rememberKey="user-1"
      title={editingUser ? `编辑用户 - ${editingUser.username}` : '新建用户'}
      open={open}
      onOk={handleSubmit}
      onCancel={onClose}
      confirmLoading={submitting}
      okText="保存"
      cancelText="取消"
      width={520}
    >
      <Form form={userForm} layout="vertical" size="small">
        {!editingUser && (
          <Form.Item
            name="username"
            label="用户名"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input placeholder="登录名（唯一）" maxLength={64} />
          </Form.Item>
        )}
        <Form.Item
          name="display_name"
          label="显示名"
          rules={[{ required: true, message: '请输入显示名' }]}
        >
          <Input placeholder="例如：张三" maxLength={64} />
        </Form.Item>
        <Form.Item
          name="role"
          label="角色"
          rules={[{ required: true, message: '请选择角色' }]}
        >
          <Select
            // 部门管理员仅可授予 user/dept_admin；编辑自己时不可改动角色（后端 400）
            disabled={isDeptAdmin && editingUser?.id === me?.id}
            options={isDeptAdmin
              ? [
                { value: 'dept_admin', label: '部门管理员' },
                { value: 'user', label: '普通用户' },
              ]
              : [
                { value: 'super_admin', label: '超级管理员' },
                { value: 'dept_admin', label: '部门管理员' },
                { value: 'user', label: '普通用户' },
              ]}
          />
        </Form.Item>
        {!isDeptAdmin && (
          <Form.Item name="department_id" label="所属部门" tooltip="未分配则留空">
            <Select options={deptOptions(departments)} placeholder="请选择部门（可留空）" />
          </Form.Item>
        )}
        {editingUser ? (
          <Form.Item
            name="status"
            label="状态"
            tooltip="禁用后该用户无法登录与访问"
            rules={[{ required: true, message: '请选择状态' }]}
          >
            <Select
              // 部门管理员不可禁用自己（后端 400）
              disabled={isDeptAdmin && editingUser.id === me?.id}
              options={[
                { value: 'active', label: '启用' },
                { value: 'disabled', label: '禁用' },
              ]}
            />
          </Form.Item>
        ) : (
          <Form.Item
            name="password"
            label="初始密码"
            tooltip="新用户首次登录时须使用该密码，建议提醒其登录后修改"
            rules={[
              { required: true, message: '请输入初始密码' },
              ...passwordStrengthRules('初始密码'),
            ]}
          >
            <Input.Password placeholder="至少 8 位，需同时包含字母和数字" maxLength={128} />
          </Form.Item>
        )}
        {editingUser && (
          <Form.Item
            name="password"
            label="重置密码"
            tooltip="留空则不修改密码"
            rules={[...passwordStrengthRules()]}
          >
            <Input.Password placeholder="留空则不修改密码，重置须至少 8 位且含字母和数字" maxLength={128} />
          </Form.Item>
        )}
      </Form>
    </AppModal>
  );
};
