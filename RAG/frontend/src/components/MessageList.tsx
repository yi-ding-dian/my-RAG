import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Empty, Modal, Tooltip, theme } from 'antd';
import { ArrowDownOutlined, FileTextOutlined, PaperClipOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import { avatarUrl, type ChatMessage, type Source } from '../api/client';
import { useAuth } from '../auth/AuthContext';
import MdImages from './MdImages';
import SourcePanel from './SourcePanel';
import { computeHighlightRanges, splitByHighlights } from '../utils/sourceHighlight';
import { cleanAnswerText } from '../utils/cleanMarkdown';

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

/** AI 头像：固定自制 ai-avatar.svg（蓝紫渐变机器人线稿）；
 * 加载失败（如后端未 serve 该文件）时隐藏而非显示 alt 文字 */
const AiAvatar: React.FC = () => {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return <img src={AI_AVATAR} alt="AI" onError={() => setFailed(true)} style={AVATAR_STYLE} />;
};

/**
 * 行内引用标记 [n]：悬浮显示引用摘要（Tooltip），点击打开引用详情弹窗。
 * - 样式：小型上标（品牌色），区别于正文
 * - 摘要：父块全文优先（与后端 _build_refs 一致），压缩空白后 ~200 字截断
 * - 图谱引用（document_name="知识图谱"）：同样显示图谱内容摘要，点击进图谱内容视图
 * - 摘要内"与回答重叠的部分"高亮（.citation-highlight，同引用面板），图谱引用跳过
 */
const CitationMark: React.FC<{
  n: number;
  source: Source;
  answerText: string;
  onClick: (source: Source) => void;
}> = ({ n, source, answerText, onClick }) => {
  const raw = (source.parent_text || source.text || '').replace(/\s+/g, ' ').trim();
  const snippet = raw.length > 200 ? `${raw.slice(0, 200)}…` : raw;
  const isGraph = source.document_name === '知识图谱';
  // 摘要内相关部分高亮区间（坐标相对 snippet；图谱引用/无命中 → 原样显示）
  const snippetHighlights = !isGraph && answerText
    ? computeHighlightRanges(answerText, snippet)
    : [];
  // Tooltip 弹层方向：引用标位于视口上部（顶部导航高度内）时改显示在下方，
  // 防止弹层弹出后遮挡页面顶部导航栏（antd 避让只针对视口、不感知导航层）
  const [placement, setPlacement] = useState<'top' | 'bottom'>('top');
  const markRef = useRef<HTMLSpanElement>(null);
  const handleOpenChange = (open: boolean) => {
    if (open && markRef.current) {
      const rect = markRef.current.getBoundingClientRect();
      setPlacement(rect.top < 140 ? 'bottom' : 'top');
    }
  };
  return (
    <Tooltip
      placement={placement}
      onOpenChange={handleOpenChange}
      mouseEnterDelay={0.15}
      overlayStyle={{ maxWidth: 420 }}
      title={
        <div style={{ fontSize: 12, lineHeight: 1.7 }}>
          <div style={{ fontWeight: 600 }}>
            {source.document_name || source.document_id || '未知来源'}
          </div>
          {snippet && (
            <div
              style={{
                marginTop: 2,
                fontWeight: 400,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {splitByHighlights(snippet, snippetHighlights).map((seg, i) =>
                seg.highlighted ? (
                  <mark key={i} className="citation-highlight">
                    {seg.text}
                  </mark>
                ) : (
                  <React.Fragment key={i}>{seg.text}</React.Fragment>
                ),
              )}
            </div>
          )}
          <div style={{ marginTop: 4, fontWeight: 400, opacity: 0.75 }}>
            点击查看{isGraph ? '图谱内容' : '引用详情'}
          </div>
        </div>
      }
    >
      <span
        ref={markRef}
        className="citation-mark"
        onClick={(e) => {
          e.stopPropagation();
          onClick(source);
        }}
        style={{
          fontSize: 12,
          fontWeight: 700,
          lineHeight: 1,
          verticalAlign: 'super',
          cursor: 'pointer',
          userSelect: 'none',
          margin: '0 1px',
        }}
      >
        [{n}]
      </span>
    </Tooltip>
  );
};

/**
 * 把文本按 [n] 引用拆分为 ReactNode 数组：
 * - n 有效（sources[n-1] 存在）→ 渲染为行内引用标（CitationMark：上标 + Tooltip + 点击），
 *   点击回调对应来源（Chat 页 → CitationTraceModal 引用详情弹窗）
 * - 编号不存在/越界（LLM 乱写）→ 原样渲染为普通文本，不报错
 * - 无 sources 或未注册回调 → 整段原样返回
 *
 * 边界规则（防正文误判，句尾标注语义）：
 * - [n] 前不限——prompt 要求"句末紧贴句尾标注"，实际输出形如"…应用[2]。"，
 *   [ 前是正文汉字，必须匹配（旧正则要求 [ 前空白，句尾紧贴形式全部漏匹配）
 * - [n] 后必须是行尾/空白/标点（句尾特征）："见[3]附录"（后接汉字）不匹配；
 *   "参考文献[3]"后接汉字同样不匹配
 * - markdown 链接 [text](url)：text 非纯数字不匹配；[1](url) 极端情形先匹配 [1]，
 *   剩余 (url) 原样输出（模型受句尾 [n] 指令约束不会生成，可接受）
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
  const re = /\[(\d+)\](?=$|[\s,.;:!?，。；：！？、%．％~～）)\]】」"'’])/g;
  let last = 0;
  let key = 0;
  for (;;) {
    const m = re.exec(content);
    if (!m) break;
    if (m.index > last) parts.push(content.slice(last, m.index));
    const n = parseInt(m[1], 10);
    const source = sources[n - 1];
    if (source) {
      parts.push(
        <CitationMark
          key={key++}
          n={n}
          source={source}
          answerText={content}
          onClick={onCitationClick}
        />,
      );
    } else {
      parts.push(m[0]);
    }
    last = m.index + m[0].length;
  }
  if (last < content.length) parts.push(content.slice(last));
  return parts;
};

/** 回答正文图片最大宽度：气泡内自适应（窄屏 100%），大图不超过 480px */
const ANSWER_IMAGE_MAX_WIDTH = 'min(480px, 100%)';

/** 毫秒可读化：<1s 显示毫秒，≥1s 同时显示秒（请求详情"总耗时"展示用） */
const formatMs = (ms: number): string =>
  ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒（${Math.round(ms)} ms）` : `${Math.round(ms)} ms`;

/**
 * 完整提示词逐条渲染（请求详情 Modal）：
 * - 第一条 system → "System（系统提示）"
 * - 最后一条 user → "User（当前问题）"
 * - 中间条目按 role 标注"历史 · user / assistant"
 * 内容 pre-wrap 小字展示（body 限高滚动由 Modal styles 控制）
 */
const renderPromptEntries = (
  prompt: unknown,
  token: ReturnType<typeof theme.useToken>['token'],
): React.ReactNode => {
  if (!Array.isArray(prompt)) {
    return <div style={{ fontSize: 12, color: token.colorTextTertiary }}>（无提示词数据）</div>;
  }
  return prompt.map((entry, i) => {
    const msg = entry as { role?: string; content?: string };
    const role = msg?.role ?? '';
    const content = msg?.content ?? '';
    let title: string;
    if (i === 0 && role === 'system') {
      title = 'System（系统提示）';
    } else if (i === prompt.length - 1 && role === 'user') {
      title = 'User（当前问题）';
    } else if (role === 'user') {
      title = '历史 · user';
    } else if (role === 'assistant') {
      title = '历史 · assistant';
    } else {
      title = `消息 ${i + 1}`;
    }
    return (
      <div key={i} style={{ marginBottom: 10 }}>
        <div
          style={{
            fontSize: 12,
            fontWeight: 600,
            color: token.colorTextSecondary,
            marginBottom: 2,
          }}
        >
          {title}
        </div>
        <pre
          style={{
            margin: 0,
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            color: token.colorText,
            background: token.colorFillTertiary,
            padding: '8px 10px',
            borderRadius: 6,
          }}
        >
          {content || '（空）'}
        </pre>
      </div>
    );
  });
};

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
  // 先清洗行首 Markdown 结构符号（### 标题 / - 列表等），再拆分 [n] 引用标：
  // 显示文本与高亮基准（answerText）都用清洗后文本，保证所见即所算
  const cleaned = cleanAnswerText(content);
  const parts = renderCitationContent(cleaned, sources, onCitationClick);
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
  // 引用来源 Modal 对应的回答文本（引用面板相关高亮的匹配基准）
  const [modalAnswerText, setModalAnswerText] = useState('');
  // 当前打开"请求详情"弹窗的 assistant 消息（null 关闭；存消息引用，仅本次
  // 流式生成的消息带 prompt/耗时字段，历史会话消息自动无入口）
  const [detailMsg, setDetailMsg] = useState<ChatMessage | null>(null);
  // 请求详情弹窗的检索问题（打开时从 messages 向前取最近的 user 消息内容）
  const [detailQuestion, setDetailQuestion] = useState('');

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

  // 打开"请求详情"弹窗：记录目标消息，并向前找最近的 user 消息作为检索问题
  const openDetail = (m: ChatMessage) => {
    setDetailMsg(m);
    let question = '';
    const idx = messages.indexOf(m);
    for (let i = idx - 1; i >= 0; i--) {
      if (messages[i].role === 'user') {
        question = messages[i].content;
        break;
      }
    }
    setDetailQuestion(question);
  };

  // 流式生成中（waiting）且最后一条助手消息已有内容 → 追加闪烁光标
  const last = messages[messages.length - 1];
  const streamingLive = waiting && !!last && last.role === 'assistant' && !!last.content;
  // 尚未输出任何内容（等待首字）时展示思考动画
  const showThinking = waiting && !streamingLive;
  // 该消息是否仍在生成中（waiting 期间的最后一条消息）：后端在 meta 事件即下发
  // sources（供行内 [n] Tooltip 映射），但"引用来源"面板要等生成完成（done → waiting
  // 结束）才显示，避免"正在思考…"时引用面板提前出现
  const isPendingLast = (idx: number) => waiting && idx === messages.length - 1;

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
          // 等待回复期间最后一条 assistant 消息内容为空 → 跳过整条渲染
          // （"正在思考…"气泡自带 AI 头像，避免同一时刻出现两个 AI 头像；
          //  AI 输出内容后 thinking 消失、空消息变正常气泡，其余消息不受影响）
          if (showThinking && idx === messages.length - 1 && m.role === 'assistant') {
            return null;
          }
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
                {m.sources && m.sources.length > 0 && !isPendingLast(idx) && (
                  <div style={{ marginTop: 8 }}>
                    {/* 默认收起：一行小按钮，点击弹出来源详情 Modal */}
                    <Button
                      type="link"
                      size="small"
                      className="source-trigger"
                      icon={<PaperClipOutlined />}
                      onClick={() => {
                        setModalSources(m.sources ?? null);
                        // 高亮基准用清洗后文本（与气泡渲染一致，所见即所算）
                        setModalAnswerText(cleanAnswerText(m.content));
                      }}
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
                    {/* 请求详情入口：仅本次流式生成且带详情数据的 assistant
                        消息显示（历史会话加载的消息无这些字段，自动不显示） */}
                    {!isUser && !!m.prompt && (m.retrieval_ms !== undefined || m.total_ms !== undefined) && (
                      <Button
                        type="link"
                        size="small"
                        className="source-trigger"
                        style={{ padding: 0, marginLeft: 6, fontSize: 11, height: 'auto', lineHeight: '16px' }}
                        onClick={() => openDetail(m)}
                      >
                        详情
                      </Button>
                    )}
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
          answerText={modalAnswerText}
          onViewOriginal={handleViewOriginal}
        />
      </Modal>
    )}

    {/* 请求详情 Modal：检索问题 / 召回耗时 / 总耗时 / 完整提示词（body 限高滚动） */}
    {detailMsg && (
      <Modal
        open
        width={760}
        title={
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
            <FileTextOutlined style={{ color: 'var(--brand-primary, #2563eb)' }} />
            请求详情
          </span>
        }
        footer={[
          <Button key="close" onClick={() => setDetailMsg(null)}>
            关闭
          </Button>,
        ]}
        onCancel={() => setDetailMsg(null)}
        styles={{ body: { maxHeight: '70vh', overflowY: 'auto', paddingTop: 8 } }}
        destroyOnClose
      >
        {/* 检索问题：该条回答对应的用户原问题（当前链路无查询改写） */}
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>检索问题</div>
          <div
            style={{
              fontSize: 13,
              color: token.colorTextSecondary,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              background: token.colorFillTertiary,
              padding: '8px 12px',
              borderRadius: 6,
            }}
          >
            {detailQuestion || '（无）'}
          </div>
        </div>
        {/* 耗时统计（后端统计召回/图谱构建，前端计算提问→首字总耗时） */}
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>耗时</div>
          <div style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}>
            召回耗时：{detailMsg.retrieval_ms !== undefined ? `${detailMsg.retrieval_ms} ms` : '—'}
            {detailMsg.kg_ms !== undefined && ` ｜ 图谱构建：${detailMsg.kg_ms} ms`}
          </div>
          <div style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}>
            总耗时（提问→首字）：{detailMsg.total_ms !== undefined ? formatMs(detailMsg.total_ms) : '—'}
          </div>
        </div>
        {/* 完整提示词：整块打包发给 AI 的 messages 数组（可读格式逐条展示） */}
        <div style={{ fontWeight: 600, marginBottom: 4 }}>
          完整提示词{Array.isArray(detailMsg.prompt) ? `（${detailMsg.prompt.length} 条消息）` : ''}
        </div>
        {renderPromptEntries(detailMsg.prompt, token)}
      </Modal>
    )}
    </>
  );
};

export default MessageList;
