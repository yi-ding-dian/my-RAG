import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Empty, Modal, theme } from 'antd';
import { ArrowDownOutlined, PaperClipOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { avatarUrl, type ChatMessage, type Source } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import MdImages from './MdImages';
import SourcePanel from './SourcePanel';

interface MessageListProps {
  messages: ChatMessage[];
  /** 是否正在等待助手回复（显示思考中动画） */
  waiting?: boolean;
  /** 点击回答中 [n] 引用标或引用面板"查看原文"时回调（打开溯源弹窗，可选） */
  onCitationClick?: (source: Source) => void;
}

/** 消息头像尺寸：32px 圆形，与气泡间距 8px，垂直顶部对齐（多行文本时在首行） */
const AVATAR_STYLE: React.CSSProperties = {
  width: 32,
  height: 32,
  borderRadius: '50%',
  objectFit: 'cover',
  flexShrink: 0,
  marginTop: 2,
};
/** 默认头像 / AI 头像（自制 SVG 资源，frontend/public/） */
const DEFAULT_AVATAR = '/default-avatar.svg';
const AI_AVATAR = '/ai-avatar.svg';

/**
 * 用户消息头像：有头像走鉴权代理 URL（<img> 无法带 header，URL 内嵌
 * query token，与 markdown 图片代理一致）；无头像或加载失败（代理 404/
 * 网络异常）回退默认 SVG。头像更换后 avatarKey 变化 → 重置 failed 自动
 * 重试新头像。
 */
/** 用户头像组件（导出供用户列表等复用：有头像走鉴权代理 URL，失败回退默认 SVG） */
export const UserAvatar: React.FC<{ userId: string; avatarKey?: string | null }> = ({
  userId,
  avatarKey,
}) => {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [avatarKey]);
  const src = avatarKey && !failed ? avatarUrl(userId) : DEFAULT_AVATAR;
  return <img src={src} alt="我的头像" onError={() => setFailed(true)} style={AVATAR_STYLE} />;
};

/** AI 头像：固定自制 ai-avatar.svg（蓝紫渐变机器人线稿） */
const AiAvatar: React.FC = () => (
  <img src={AI_AVATAR} alt="AI" style={AVATAR_STYLE} />
);

/**
 * 把文本按 [n] 引用拆分为 ReactNode 数组：
 * - n 有效（sources[n-1] 存在）→ 渲染为可点击的蓝色引用标，点击回调对应来源
 * - 编号不存在/越界（LLM 乱写）→ 原样渲染为普通文本，不报错
 * - 无 sources 或未注册回调 → 整段原样返回
 *
 * 边界要求（防正文误判）：
 * - [n] 前必须是行首或空白字符（"参考文献[3]"不匹配）
 * - [n] 后必须是行尾/空白/标点（"见[3]附录"不匹配）
 * - 流式增量中未闭合的 "[3"（无 ]）不匹配，输出过程中原样展示
 */
const renderCitationContent = (
  content: string,
  sources: Source[] | undefined,
  onCitationClick: ((source: Source) => void) | undefined,
): React.ReactNode[] => {
  const parts: React.ReactNode[] = [];
  if (!content) return parts;
  if (!sources || sources.length === 0 || !onCitationClick) return [content];
  const re = /(^|\s)\[(\d+)\](?=$|[\s,。；、])/g;
  let last = 0;
  let key = 0;
  for (;;) {
    const m = re.exec(content);
    if (!m) break;
    if (m.index > last) parts.push(content.slice(last, m.index));
    const n = parseInt(m[2], 10);
    const source = sources[n - 1];
    if (m[1]) parts.push(m[1]); // 保留引用标前置空白
    if (source) {
      parts.push(
        <a
          key={key++}
          onClick={() => onCitationClick(source)}
          title={`查看「${source.document_name || source.document_id}」原文`}
          style={{
            fontSize: 12,
            color: 'var(--brand-primary, #2563eb)',
            textDecoration: 'underline',
            textUnderlineOffset: 2,
            cursor: 'pointer',
            margin: '0 1px',
          }}
        >
          [{n}]
        </a>,
      );
    } else {
      parts.push(m[0].slice(m[1].length));
    }
    last = m.index + m[0].length;
  }
  if (last < content.length) parts.push(content.slice(last));
  return parts;
};

/** 回答正文图片最大宽度：气泡内自适应（窄屏 100%），大图不超过 480px */
const ANSWER_IMAGE_MAX_WIDTH = 'min(480px, 100%)';

/**
 * 组合渲染管道：先按 [n] 引用标拆分文本（renderCitationContent），每个
 * 文本片段再过 MdImages（![]() → <img>，自动带鉴权 token）。
 *
 * [n] 与 ![]() 互不干扰：两个正则各自处理普通文本中的模式，引用标拆分出的
 * 文本片段交给 MdImages 渲染图片，图片内容里出现的 [1] 类字样由外层引用
 * 解析先于 MdImages 拆走；MdImages 不认识的普通文本原样返回。
 * 流式增量未闭合（![ 缺 ] 或 [1 缺 ]）时两者都按普通文本原样输出，不渲染
 * 半截内容（现有行为保持）。
 */
const renderContent = (
  content: string,
  sources: Source[] | undefined,
  onCitationClick: ((source: Source) => void) | undefined,
): React.ReactNode[] => {
  const parts = renderCitationContent(content, sources, onCitationClick);
  return parts.map((p, i) =>
    typeof p === 'string'
      ? <MdImages key={`m${i}`} text={p} maxWidth={ANSWER_IMAGE_MAX_WIDTH} />
      : p,
  );
};

/** 消息列表：用户右侧 / 助手左侧气泡；助手消息 pre-wrap 渲染，引用来源默认收成一行入口按钮 */
const MessageList: React.FC<MessageListProps> = ({ messages, waiting, onCitationClick }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const { token } = theme.useToken();
  // 当前登录用户：聊天中自己的头像从此读取（无头像 → 默认 SVG 兜底）
  const { user } = useAuth();
  // 当前打开"引用来源"弹窗的消息来源（null 关闭）；直接存数组引用，会话切换/消息变动后自动失效
  const [modalSources, setModalSources] = useState<Source[] | null>(null);

  // 是否位于消息列表底部附近（60px 容差）：在底部时新消息/流式增量自动跟随滚动；离开底部则显示"最新消息"按钮
  // ref 供 effect 读取即时值（避免闭包过期），state 驱动按钮显隐
  const [atBottom, setAtBottom] = useState(true);
  const atBottomRef = useRef(true);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 60;
    atBottomRef.current = nearBottom;
    setAtBottom(nearBottom);
  }, []);

  // 自动滚动：仅在用户位于底部附近时跟随新消息/流式增量（原行为），离开底部不强制滚动
  useEffect(() => {
    const el = containerRef.current;
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, waiting]);

  // 点击"最新消息"：平滑滚动回底部（滚动到位后 onScroll 判定回到底部，按钮自动淡出）
  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, []);

  // 来源弹窗内"查看原文"：先关来源弹窗，再走原溯源链路（打开 CitationTraceModal），层级清晰
  const handleViewOriginal = (s: Source) => {
    setModalSources(null);
    onCitationClick?.(s);
  };

  // 流式生成中（waiting）且最后一条助手消息已有内容 → 追加闪烁光标
  const last = messages[messages.length - 1];
  const streamingLive = waiting && !!last && last.role === 'assistant' && !!last.content;
  // 尚未输出任何内容（等待首字）时展示思考动画
  const showThinking = waiting && !streamingLive;

  if (messages.length === 0 && !waiting) {
    return (
      <div ref={containerRef} onScroll={handleScroll} style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '8px 12px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Empty
          description="开始你的第一个问题吧"
          style={{ color: token.colorTextTertiary }}
        />
      </div>
    );
  }

  return (
    <>
    {/* 外层根容器：永不滚动的定位上下文（overflow hidden + flex 收缩约束），"最新消息"按钮挂在这里，
        与滚动容器彻底解耦，滚动内容时按钮固定在可视区底部不动 */}
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', position: 'relative', overflow: 'hidden' }}>
    <div
      ref={containerRef}
      onScroll={handleScroll}
      style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '8px 12px' }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 1200, margin: '0 auto' }}>
        {messages.map((m, idx) => {
          const isUser = m.role === 'user';
          const isStreamingLast = streamingLive && idx === messages.length - 1;
          return (
            <div
              key={idx}
              style={{
                display: 'flex',
                gap: 8,
                alignItems: 'flex-start',
                justifyContent: isUser ? 'flex-end' : 'flex-start',
              }}
            >
              {/* 头像列：AI 消息左侧显示 AI 头像；用户消息右侧显示自己头像（DOM 顺序保证 flex 下最右） */}
              {!isUser && <AiAvatar />}
              <div style={{ maxWidth: '85%', minWidth: 0 }}>
                {m.content && (
                  <div
                    className={`${isUser ? 'bubble-user' : 'bubble-assistant'} ${isStreamingLast ? 'typing-cursor' : ''}`}
                    style={{
                      padding: '10px 14px',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                    }}
                  >
                    {renderContent(m.content, m.sources, onCitationClick)}
                    {/* 用户点击停止后：尾部灰色小字标注（仅前端会话状态，不污染落盘内容） */}
                    {!isUser && m.stopped && !isStreamingLast && (
                      <div style={{ marginTop: 6, fontSize: 12, color: token.colorTextTertiary }}>
                        （已停止生成）
                      </div>
                    )}
                  </div>
                )}
                {m.sources && m.sources.length > 0 && (
                  <div style={{ marginTop: 8 }}>
                    {/* 默认收起：一行小按钮，点击弹出来源详情 Modal */}
                    <Button
                      type="link"
                      size="small"
                      className="source-trigger"
                      icon={<PaperClipOutlined />}
                      onClick={() => setModalSources(m.sources ?? null)}
                    >
                      引用来源（{m.sources.length}）
                    </Button>
                  </div>
                )}
                {/* 消息时间戳（HH:mm 小灰字）：每条消息都显示，流式生成中的最后一条暂不显示 */}
                {m.created_at && !isStreamingLast && (
                  <div
                    style={{
                      marginTop: 4,
                      fontSize: 11,
                      lineHeight: '16px',
                      color: token.colorTextTertiary,
                      textAlign: isUser ? 'right' : 'left',
                    }}
                  >
                    {dayjs(m.created_at).format('HH:mm')}
                  </div>
                )}
              </div>
              {/* 用户头像在气泡右侧（与气泡同级 flex 项，顶部对齐） */}
              {isUser && <UserAvatar userId={user?.id ?? ''} avatarKey={user?.avatar} />}
            </div>
          );
        })}
        {showThinking && (
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-start', alignItems: 'flex-start' }}>
            <AiAvatar />
            <div
              className="bubble-assistant"
              style={{
                padding: '12px 16px',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 10,
              }}
            >
              <span className="thinking-dots">
                <span />
                <span />
                <span />
              </span>
              <span style={{ color: token.colorTextTertiary, fontSize: 13 }}>正在思考…</span>
            </div>
          </div>
        )}
        </div>
      </div>

      {/* 不在底部时：输入框上方居中"最新消息"悬浮按钮（常驻 DOM，class 切换 opacity 过渡，避免闪烁）
          定位上下文 = 外层根容器（overflow hidden 永不滚动），滚动消息时按钮固定在可视区底部不动 */}
      <button
        type="button"
        aria-label="回到最新消息"
        className={`chat-scroll-bottom${atBottom ? '' : ' chat-scroll-bottom--visible'}`}
        onClick={scrollToBottom}
      >
        <ArrowDownOutlined /> 最新消息
      </button>
    </div>

    {/* 引用来源详情 Modal：复用 SourcePanel 渲染（编号角标 + 查看原文），body 限高滚动 */}
    {modalSources && (
      <Modal
        open
        width={720}
        title={
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
            <PaperClipOutlined style={{ color: 'var(--brand-primary, #2563eb)' }} />
            引用来源（{modalSources.length}）
          </span>
        }
        footer={[
          <Button key="close" onClick={() => setModalSources(null)}>
            关闭
          </Button>,
        ]}
        onCancel={() => setModalSources(null)}
        styles={{ body: { maxHeight: '70vh', overflowY: 'auto', paddingTop: 8 } }}
        destroyOnClose
      >
        <SourcePanel
          sources={modalSources}
          variant="modal"
          numbered
          onViewOriginal={handleViewOriginal}
        />
      </Modal>
    )}
    </>
  );
};

export default MessageList;
