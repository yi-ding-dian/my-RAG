import React, { useState } from 'react';
import { App as AntApp, Form, Input, Modal } from 'antd';
import {
  asApiError, importDocumentFromUrl } from '../api/client';

interface UrlImportModalProps {
  open: boolean;
  kbId?: string;
  onCancel: () => void;
  /** 导入成功后的回调（父组件刷新列表） */
  onSuccess: () => void;
}

/**
 * URL 网页导入弹窗：抓取网页标题/正文导入为文档（仅 http/https，
 * 后端约束：超时 30s、响应超 5MB 拒绝）。导入后状态为待解析，
 * 需在列表选择解析方式后点击解析入库。
 */
const UrlImportModal: React.FC<UrlImportModalProps> = ({
  open,
  kbId,
  onCancel,
  onSuccess,
}) => {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<{ url: string }>();
  const [importing, setImporting] = useState(false);

  const handleOk = async () => {
    if (!kbId) return;
    let url: string;
    try {
      url = (await form.validateFields()).url.trim();
    } catch {
      return; // 校验失败提示已由 Form 规则展示
    }
    setImporting(true);
    try {
      await importDocumentFromUrl(kbId, url);
      message.success('URL 导入成功，请在文档列表选择解析方式后点击解析');
      form.resetFields();
      onSuccess();
      onCancel();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || 'URL 导入失败');
    } finally {
      setImporting(false);
    }
  };

  return (
    <Modal
      title="从 URL 导入网页"
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      confirmLoading={importing}
      okText="导入"
      cancelText="取消"
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="url"
          label="网页地址"
          rules={[
            { required: true, whitespace: true, message: '请输入网页地址' },
            {
              pattern: /^https?:\/\/.+/i,
              message: '仅支持 http/https 网址',
            },
          ]}
        >
          <Input placeholder="https://example.com/article" />
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default UrlImportModal;
