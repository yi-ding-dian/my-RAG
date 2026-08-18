/**
 * 首次登录强制改密弹窗（H4：新建用户 must_change_password=true 时触发）
 *
 * 行为：不可关闭、无取消按钮，必须完成改密后才能继续使用系统；
 * 提交走现有 /auth/change-password（当前密码 + 新密码），成功后由父组件
 * refreshUser() 拉取新用户信息（must_change_password=false）自动关闭弹窗。
 */
import React, { useEffect, useState } from 'react';
import { App as AntApp, Form, Input, Modal, Typography } from 'antd';
import { LockOutlined, SafetyOutlined } from '@ant-design/icons';
import {
  asApiError, changePassword } from '../api/client';

const { Text } = Typography;

interface Props {
  open: boolean;
  /** 改密成功后回调（父组件 refreshUser 清标志并关闭弹窗） */
  onSuccess: () => void;
}

interface FormValues {
  old_password: string;
  new_password: string;
  confirm_password: string;
}

const ForcedPasswordModal: React.FC<Props> = ({ open, onSuccess }) => {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<FormValues>();
  const [submitting, setSubmitting] = useState(false);

  // 每次打开重置表单（含输入值，防止残留上次内容）
  useEffect(() => {
    if (open) form.resetFields();
  }, [open, form]);

  const handleOk = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      await changePassword(values.old_password, values.new_password);
      message.success('密码修改成功');
      onSuccess();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '修改密码失败，请重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="首次登录须修改密码"
      open={open}
      onOk={handleOk}
      okText="确认修改"
      cancelButtonProps={{ style: { display: 'none' } }}
      closable={false}
      keyboard={false}
      maskClosable={false}
      confirmLoading={submitting}
      width={440}
    >
      <Text type="secondary" style={{ display: 'block', marginBottom: 16, fontSize: 13 }}>
        当前账号为初始密码，为保障账号安全，请先设置新密码后再使用系统。
      </Text>
      <Form form={form} layout="vertical" size="small">
        <Form.Item
          name="old_password"
          label="当前密码"
          tooltip="即登录时使用的初始密码"
          rules={[{ required: true, message: '请输入当前密码' }]}
        >
          <Input.Password prefix={<LockOutlined />} placeholder="初始密码" autoComplete="current-password" />
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
      </Form>
    </Modal>
  );
};

export default ForcedPasswordModal;
