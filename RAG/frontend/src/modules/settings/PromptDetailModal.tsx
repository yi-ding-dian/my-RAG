import React from 'react';
import { App as AntApp, Button } from 'antd';
import AppModal from '../../shared/components/common/AppModal';
import { PRE_STYLE } from './shared';

/** 要展示的提示词（null = 不弹） */
export interface PromptPreview {
  name: string;
  content: string;
}

/**
 * 提示词详情弹窗
 *
 * 条目里的正文框只有 4 行且很窄，看全文与复制都靠这个弹窗。内容由调用方
 * 传入**表单当前值**（不是保存后的旧值），改完还没存也能看到最新内容。
 */
const PromptDetailModal: React.FC<{
  data: PromptPreview | null;
  onClose: () => void;
}> = ({ data, onClose }) => {
  const { message } = AntApp.useApp();
  if (!data) return null;

  const copy = (text: string, label: string) => {
    void navigator.clipboard.writeText(text).then(
      () => message.success(`${label}已复制到剪贴板`),
      () => message.error('复制失败（浏览器未授权剪贴板）'),
    );
  };

  return (
    <AppModal
      dimension="resizable"
      defaultSize={{ w: 720, h: 520 }}
      rememberKey="prompt-detail"
      open
      width={720}
      title={`提示词详情：${data.name || '（未命名）'}`}
      footer={[
        <Button key="close" onClick={onClose}>关闭</Button>,
      ]}
      onCancel={onClose}
      destroyOnHidden
    >
      <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4 }}>
        名称
        <Button size="small" style={{ marginLeft: 8 }}
          onClick={() => copy(data.name, '名称')}>
          复制
        </Button>
      </div>
      <div style={{ marginBottom: 12 }}>{data.name || '-'}</div>
      <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 4 }}>
        提示词正文（{data.content.length} 字）
        <Button size="small" style={{ marginLeft: 8 }}
          onClick={() => copy(data.content, '提示词正文')}>
          复制
        </Button>
      </div>
      <pre style={PRE_STYLE}>{data.content || '（空）'}</pre>
    </AppModal>
  );
};

export default PromptDetailModal;
