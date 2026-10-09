/**
 * 新建/编辑部门弹窗。
 *
 * 从 Users.tsx 抽出来：只有名称与描述两个字段，弹窗状态（表单/提交中）自持。
 */
import { useState } from 'react';
import {
  App as AntApp, Form, Input,
} from 'antd';

import AppModal from '../../../shared/components/common/AppModal';
import {
  asApiError, createDepartment, updateDepartment, Department,
} from '../../../shared/api/client';

export interface DeptEditModalProps {
  open: boolean;
  /** null = 新建 */
  editingDept: Department | null;
  onClose: () => void;
  /** 保存成功：刷新部门列表 */
  onSaved: () => Promise<void>;
}

export const DeptEditModal = ({
  open, editingDept, onClose, onSaved,
}: DeptEditModalProps) => {
  const { message } = AntApp.useApp();
  const [submitting, setSubmitting] = useState(false);
  const [deptForm] = Form.useForm();

  const handleSubmit = async () => {
    let values: { name: string; description?: string };
    try {
      values = await deptForm.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      if (editingDept) {
        await updateDepartment(editingDept.id, values);
        message.success('部门已更新');
      } else {
        await createDepartment(values);
        message.success('部门创建成功');
      }
      onClose();
      await onSaved();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || (editingDept ? '更新失败' : '创建失败'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 520, h: 420 }}
      rememberKey="user-2"
      title={editingDept ? `编辑部门 - ${editingDept.name}` : '新建部门'}
      open={open}
      onOk={handleSubmit}
      onCancel={onClose}
      confirmLoading={submitting}
      okText="保存"
      cancelText="取消"
    >
      <Form form={deptForm} layout="vertical">
        <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入部门名称' }]}>
          <Input placeholder="例如：研发部" maxLength={64} />
        </Form.Item>
        <Form.Item name="description" label="描述">
          <Input.TextArea placeholder="选填，简单描述该部门" maxLength={200} rows={3} />
        </Form.Item>
      </Form>
    </AppModal>
  );
};
