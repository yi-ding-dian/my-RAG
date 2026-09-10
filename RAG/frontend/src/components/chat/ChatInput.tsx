import React, { useRef, useState } from 'react';
import { Button, Input, Typography, theme } from 'antd';
import type { TextAreaRef } from 'antd/es/input/TextArea';
import { SendOutlined, StopOutlined } from '@ant-design/icons';

const { Text } = Typography;

interface ChatInputProps {
  /** 发送消息（流式生成中为 false） */
  onSend: (text: string) => void;
  /** 停止当前流式生成 */
  onStop?: () => void;
  streaming?: boolean;
  disabled?: boolean;
}

/**
 * 输入区：圆角浅底容器 + 无边框 TextArea + 主色圆角发送按钮。
 * Enter 发送 / Shift+Enter 换行，发送与停止按钮互斥（沿用原交互）。
 */
const ChatInput: React.FC<ChatInputProps> = ({ onSend, onStop, streaming = false, disabled = false }) => {
  const { token } = theme.useToken();
  const [value, setValue] = useState('');
  const taRef = useRef<TextAreaRef>(null);

  const doSend = () => {
    const text = value.trim();
    if (!text || streaming || disabled) return;
    onSend(text);
    setValue('');
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      doSend();
    }
  };

  return (
    <div
      className="chat-input-box"
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        padding: '10px 12px 8px',
        background: token.colorFillQuaternary,
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: 12,
      }}
    >
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
        <Input.TextArea
          ref={taRef}
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={streaming ? '生成中…' : '输入问题，Enter 发送，Shift+Enter 换行'}
          autoSize={{ minRows: 1, maxRows: 6 }}
          disabled={disabled}
          variant="borderless"
          style={{ flex: 1, background: 'transparent', padding: '6px 4px', fontSize: 14 }}
        />
        {streaming ? (
          <Button
            icon={<StopOutlined />}
            onClick={onStop}
            danger
            shape="round"
            style={{ padding: '4px 16px' }}
          >
            停止
          </Button>
        ) : (
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={doSend}
            disabled={disabled || !value.trim()}
            shape="round"
            style={{ padding: '4px 16px', boxShadow: '0 2px 8px rgba(var(--brand-primary-rgb, 37, 99, 235), 0.25)' }}
          >
            发送
          </Button>
        )}
      </div>
      {streaming && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
          <Text type="secondary" style={{ fontSize: 11 }}>
            正在生成…
          </Text>
        </div>
      )}
    </div>
  );
};

export default ChatInput;
