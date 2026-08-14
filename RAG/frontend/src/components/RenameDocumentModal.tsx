import React, { useEffect, useState } from 'react';
import { App as AntApp, Form, Input, Modal } from 'antd';
import type { DocumentItem } from '../api/client';
import { renameDocument } from '../api/client';

interface RenameDocumentModalProps {
  open: boolean;
  /** 当前待重命名文档（null 时弹窗不展示表单内容） */
  doc: DocumentItem | null;
  kbId?: string;
  onCancel: () => void;
  /** 重命名成功后的回调（父组件刷新列表） */
  onSuccess: () => void;
}

/**
 * 文档重命名弹窗：只改展示名 original_name（内部存储名/向量/切块不变）。
 * 校验：1~255 字符；扩展名不可修改（无扩展名时后端自动补全）。
 */
const RenameDocumentModal: React.FC<RenameDocumentModalProps> = ({
  open,
  doc,
  kbId,
  onCancel,
  onSuccess,
}) => {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<{ name: string }>();
  const [saving, setSaving] = useState(false);

  // 打开时预填当前文件名
  useEffect(() => {
    if (open && doc) {
      form.setFieldsValue({ name: doc.original_name });
    }
  }, [open, doc, form]);

  const originalName = doc?.original_name ?? '';
  // 原文件扩展名（含点，如 .txt；无扩展名为空字符串）
  const fileExt = originalName.includes('.')
    ? originalName.slice(originalName.lastIndexOf('.'))
    : '';

  const handleOk = async () => {
    if (!doc || !kbId) return;
    let name: string;
    try {
      name = (await form.validateFields()).name.trim();
    } catch {
      return; // 校验失败提示已由 Form 规则展示
    }
    setSaving(true);
    try {
      await renameDocument(kbId, doc.id, name);
      message.success('文档已重命名');
      onSuccess();
      onCancel();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '重命名失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={`重命名 - ${originalName}`}
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="name"
          label="文件名"
          rules={[
            { required: true, whitespace: true, message: '请输入文件名' },
            { max: 255, message: '文件名不能超过 255 个字符' },
            {
              validator: (_rule, value: string) => {
                if (!value || !fileExt) return Promise.resolve();
                const v = value.trim();
                const newExt = v.includes('.') ? v.slice(v.lastIndexOf('.')) : '';
                if (newExt.toLowerCase() === fileExt.toLowerCase()) {
                  return Promise.resolve();
                }
                return Promise.reject(
                  new Error(`扩展名不能修改（将自动保留 ${fileExt}），请省略或保持原扩展名`),
                );
              },
            },
          ]}
        >
          <Input
            placeholder={`请输入新文件名（${fileExt ? `扩展名 ${fileExt} 将自动保留` : '1~255 个字符'}）`}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default RenameDocumentModal;
