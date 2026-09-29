import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Input, theme } from 'antd';
import type { TextAreaRef } from 'antd/es/input/TextArea';
import { CloseOutlined, LoadingOutlined, PictureOutlined, SendOutlined, StopOutlined } from '@ant-design/icons';
import { asApiError, uploadChatImage } from '../../../shared/api/client';

/** 待发送的图片：本地预览 + 上传结果 */
interface PendingImage {
  /** 本地自增 id（不用 key：上传完成前还没有 key） */
  id: number;
  /** objectURL 本地预览（上传中也先看得见；移除/清空/卸载时 revoke） */
  preview: string;
  /** 上传成功后的对象存储 key（空串 = 仍在上传中） */
  key: string;
}

interface ChatInputProps {
  /** 发送消息（流式生成中不触发）；images = 已上传图片的 key 列表 */
  onSend: (text: string, images: string[]) => void;
  /** 停止当前流式生成 */
  onStop?: () => void;
  streaming?: boolean;
  disabled?: boolean;
  /** 识图总开关（后端 chat.image_enabled；关闭时不渲染图片入口） */
  imageEnabled?: boolean;
  /** 单次最多几张图（后端 chat.image_max_count，前端同款拦截） */
  maxImages?: number;
  /** 单张大小上限 MB（后端 chat.image_max_mb） */
  maxImageMb?: number;
  /** 视觉模型可用性：null=探活中 / true=可用 / false=不可用 */
  visionOk?: boolean | null;
  /** 不可用时展示的原因（空则用内置文案） */
  visionReason?: string;
  /** 校验/上传失败的提示出口（父组件用 message.error 展示） */
  onError?: (msg: string) => void;
}

/** 视觉模型不可用时的兜底文案（与后端 VISION_UNAVAILABLE_MSG 一致） */
const VISION_FAIL_TEXT = '视觉模型当前无法使用，无法识图';

/**
 * 输入区：圆角浅底容器 + 无边框 TextArea + 主色圆角发送按钮。
 * Enter 发送 / Shift+Enter 换行，发送与停止按钮互斥（沿用原交互）。
 *
 * 聊天识图：
 * - 三个入口：点图片按钮选文件 / Ctrl+V 粘贴截图 / 拖拽图片到输入区
 * - **选中即上传**（不等发送）：发送时只带 key，用户点发送就走，不必再等
 *   一次上传往返。代价是"选了图又不发"会留个孤儿对象（量小可接受，
 *   删会话时会清掉该会话的图）
 * - 前端先拦一道（类型/大小/张数），后端仍会再校验一次——前端可绕过
 * - 视觉模型不可用且有图时**拦住发送**并显红字提示；发送时后端还会兜底
 *   下发 vision_error（探活只证明服务活着，不证明模型真能读图）
 */
const ChatInput: React.FC<ChatInputProps> = ({
  onSend, onStop, streaming = false, disabled = false,
  imageEnabled = false, maxImages = 3, maxImageMb = 5,
  visionOk = null, visionReason = '', onError,
}) => {
  const { token } = theme.useToken();
  const [value, setValue] = useState('');
  const [images, setImages] = useState<PendingImage[]>([]);
  const [dragging, setDragging] = useState(false);
  const taRef = useRef<TextAreaRef>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const seqRef = useRef(0);
  // 渲染期同步"最新值 ref"：卸载清理与 addFiles 计数都要读当前列表，而
  // setState 后的 effect 尚未执行时读 state 会拿到旧值
  const imagesRef = useRef<PendingImage[]>([]);
  imagesRef.current = images;

  useEffect(() => () => {
    // 卸载时释放本地预览 URL（避免 objectURL 泄漏，图片多时很可观）
    imagesRef.current.forEach(it => URL.revokeObjectURL(it.preview));
  }, []);

  const dropImage = useCallback((id: number) => {
    setImages(prev => {
      const hit = prev.find(it => it.id === id);
      if (hit) URL.revokeObjectURL(hit.preview);
      return prev.filter(it => it.id !== id);
    });
  }, []);

  const addFiles = useCallback(async (files: File[]) => {
    if (!imageEnabled || !files.length) return;
    const room = maxImages - imagesRef.current.length;
    if (room <= 0) {
      onError?.(`最多发送 ${maxImages} 张图片`);
      return;
    }
    if (files.length > room) onError?.(`最多发送 ${maxImages} 张图片，多余的已忽略`);
    for (const file of files.slice(0, room)) {
      // 前端先拦：这三类错误不必费一次上传往返，提示也更即时
      if (!file.type.startsWith('image/')) { onError?.('只能发送图片文件'); continue; }
      if (!file.size) { onError?.('图片内容为空'); continue; }
      if (file.size > maxImageMb * 1024 * 1024) {
        onError?.(`图片过大（超过 ${maxImageMb}MB）`);
        continue;
      }
      const id = ++seqRef.current;
      const preview = URL.createObjectURL(file);
      setImages(prev => [...prev, { id, preview, key: '' }]);
      try {
        const { key } = await uploadChatImage(file);
        setImages(prev => prev.map(it => (it.id === id ? { ...it, key } : it)));
      } catch (e) {
        // 上传失败：撤掉占位并提示（后端 detail 已是面向用户的中文）
        dropImage(id);
        onError?.(asApiError(e).response?.data?.detail || '图片上传失败');
      }
    }
  }, [imageEnabled, maxImages, maxImageMb, onError, dropImage]);

  const uploading = images.some(it => !it.key);
  const readyKeys = images.filter(it => it.key).map(it => it.key);
  // 视觉模型不可用且已选图 → 拦住发送（后端还会再挡一次）
  const visionBlocked = images.length > 0 && visionOk === false;
  const canSend = !disabled && !streaming && !uploading && !visionBlocked
    && (!!value.trim() || readyKeys.length > 0);

  const doSend = () => {
    if (!canSend) return;
    onSend(value.trim(), readyKeys);
    setValue('');
    setImages(prev => {
      prev.forEach(it => URL.revokeObjectURL(it.preview));
      return [];
    });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      doSend();
    }
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData?.files ?? []);
    if (!files.length) return;
    // 有图就吞掉默认粘贴：否则会同时把文件名/路径文本粘进输入框
    e.preventDefault();
    void addFiles(files);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    void addFiles(Array.from(e.dataTransfer?.files ?? []));
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
        border: `1px solid ${dragging ? token.colorPrimary : token.colorBorderSecondary}`,
        borderRadius: 12,
      }}
      onDragOver={imageEnabled ? (e) => { e.preventDefault(); setDragging(true); } : undefined}
      onDragLeave={imageEnabled ? () => setDragging(false) : undefined}
      onDrop={imageEnabled ? handleDrop : undefined}
    >
      {images.length > 0 && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {images.map(it => (
            <div key={it.id} style={{ position: 'relative', width: 64, height: 64 }}>
              <img
                src={it.preview}
                alt="待发送图片"
                style={{
                  width: '100%', height: '100%', objectFit: 'cover', borderRadius: 6,
                  border: `1px solid ${token.colorBorderSecondary}`,
                  opacity: it.key ? 1 : 0.55,
                }}
              />
              {!it.key && (
                <LoadingOutlined
                  style={{
                    position: 'absolute', top: '50%', left: '50%',
                    transform: 'translate(-50%, -50%)', fontSize: 18,
                  }}
                />
              )}
              <CloseOutlined
                onClick={() => dropImage(it.id)}
                title="移除这张图片"
                style={{
                  position: 'absolute', top: -6, right: -6, fontSize: 10,
                  background: token.colorTextSecondary, color: '#fff',
                  borderRadius: '50%', padding: 3, cursor: 'pointer',
                }}
              />
            </div>
          ))}
        </div>
      )}
      {visionBlocked && (
        <div style={{ fontSize: 12, color: token.colorError }}>
          {visionReason || VISION_FAIL_TEXT}
        </div>
      )}
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
        {imageEnabled && (
          <>
            <Button
              type="text"
              icon={<PictureOutlined />}
              disabled={disabled || streaming || images.length >= maxImages}
              onClick={() => fileRef.current?.click()}
              title={`发送图片（最多 ${maxImages} 张，单张不超过 ${maxImageMb}MB）`}
            />
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={e => {
                void addFiles(Array.from(e.target.files ?? []));
                e.target.value = '';  // 清空才能重复选同一个文件
              }}
            />
          </>
        )}
        <Input.TextArea
          ref={taRef}
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={imageEnabled ? handlePaste : undefined}
          placeholder={
            streaming ? '生成中…'
              : imageEnabled ? '输入问题，Enter 发送，Shift+Enter 换行；可粘贴或拖入图片'
                : '输入问题，Enter 发送，Shift+Enter 换行'
          }
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
            disabled={!canSend}
            shape="round"
            style={{ padding: '4px 16px', boxShadow: '0 2px 8px rgba(var(--brand-primary-rgb, 37, 99, 235), 0.25)' }}
          >
            发送
          </Button>
        )}
      </div>
    </div>
  );
};

export default ChatInput;
