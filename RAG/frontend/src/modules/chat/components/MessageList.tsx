import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import AppModal from '../../../shared/components/common/AppModal';
import {
  App as AntApp,
  Button,
  Empty,
  Input,
  Popover,
  Tooltip,
  theme,
} from 'antd';
import {
  ArrowDownOutlined,
  BulbOutlined,
  DislikeOutlined,
  DownOutlined,
  LikeOutlined,
  PaperClipOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { avatarUrl, submitFeedback, type ChatMessage, type Source } from '../../../shared/api/client';
import { useAuth } from '../../../shared/auth/AuthContext';
import MdImages from '../../../shared/components/common/MdImages';
import RequestDetailModal from '../../../shared/components/common/RequestDetailModal';
import SourcePanel from './SourcePanel';
import { buildSnippet, computeHighlightRanges, computeNumberRanges, splitByHighlights } from '../../../shared/utils/sourceHighlight';
import { cleanAnswerText } from '../../../shared/utils/cleanMarkdown';
import { renderTableBlocks } from '../../../shared/components/common/MarkdownTable';

interface MessageListProps {
  messages: ChatMessage[];
  /** 是否正在等待助手回复（显示思考中动画） */
  waiting?: boolean;
  /** 等待阶段的进度提示文案（Agentic 决策进度；空=「正在思考…」） */
  waitingHint?: string;
  /** 点击回答中 [n] 引用标或引用面板"查看原文"时回调（打开溯源弹窗，可选） */
  onCitationClick?: (source: Source) => void;
  /** 会话 ID（反馈关联：定位消息序号） */
  sessionId?: string;
  /** 知识库 ID（反馈关联） */
  kbId?: string;
  /** 引用摘要窗口大小（字）：来自配置档案「聊天设置 → 引用设置」，缺省 600 */
  citationSnippetChars?: number;
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
 * 引用摘要表格噪声清洗（仅展示层，不改动 source 原始数据，后端契约不变）：
 * Excel(xlsx/mineru) 解析后的 chunk 是 markdown 表格形态，摘要开头常是
 * 表头/分隔行（| 桂林 | 桂林 |…、|---|）等噪声。规则：
 * - 先保留换行逐行处理再拼回单行（识别表格结构的前提；行间单空格相连，
 *   与原先整段压缩空白的展示等价）
 * - 丢弃表格结构行：整行仅剩 - : 空格（如 |---|、|:---| 压成 ---）或为空
 * - 内容行把连续的 | 与空白压缩为单个空格（| 桂林 | 桂林 | → 桂林 桂林），
 *   逐行 trim，不丢弃任何内容（信息只是换紧凑形式）
 * - 清洗后仍 ~400 字截断：Excel 块前部常是表头/重复列名噪声，放宽截断
 *   长度让块后部的真实命中单元格（如具体景点/金额）能浮现出来
 * - 非表格形态文本逐行压缩后与原先行为一致，图谱引用内容不受影响
 */
const cleanSourceSummary = (text: string): string => {
  const rows: string[] = [];
  for (const line of text.split('\n')) {
    const row = line.replace(/[|\s]+/g, ' ').trim();
    if (!row || /^[-:\s]*$/.test(row)) continue;
    rows.push(row);
  }
  return rows.join(' ');
};

/**
 * 行内引用标记 [n]：悬浮显示引用摘要（Tooltip），点击打开引用详情弹窗。
 * - 样式：小型上标（品牌色），区别于正文
 * - 摘要：父块全文优先（与后端 _build_refs 一致），清洗表格噪声后压缩空白 ~400 字截断
 * - 图谱引用（document_name="知识图谱"）：同样显示图谱内容摘要，点击进图谱内容视图
 * - 摘要内"与回答重叠的部分"高亮（.citation-highlight，同引用面板），图谱引用跳过
 */
const CitationMark: React.FC<{
  n: number;
  source: Source;
  answerText: string;
  onClick: (source: Source) => void;
  /** 摘要窗口大小（字）：配置档案「聊天设置 → 引用设置」，默认 600 */
  snippetChars: number;
  /** 该消息正在流式生成中：跳过 Tooltip 与高亮计算，只渲染上标 */
  streaming?: boolean;
}> = ({ n, source, answerText, onClick, snippetChars, streaming }) => {
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

  // 摘要高亮计算收进 useMemo：原先直接写在渲染路径上，**每次渲染**都要拿回答
  // 全文重跑一遍匹配（每个引用标各一份），流式时每帧重算是输出"一卡一卡"的主因
  // 之一。streaming=true 时整段跳过——正在生成的消息每帧都在变，算了立刻作废
  const summary = useMemo(() => {
    if (streaming) return null;
    const raw = cleanSourceSummary(source.parent_text || source.text || '');
    const isGraph = source.document_name === '知识图谱';
    // 先在全量文本上算高亮（位置才准），再围绕首个命中开窗——**不能先截断再算**：
    // 回答用到的内容常落在块的中后段（表格块的有效数字都在表格下方），从头硬截
    // 会让命中整段落在窗口外，浮层里一个高亮都标不出来（实测丢图那轮的 5 个引用
    // 全部如此：命中数字都在 400 字之后）。
    const fullHighlights = !isGraph && answerText
      ? computeHighlightRanges(answerText, raw)
      : [];
    // 数字标记：回答里出现过的数字在引用里的位置（渲染成方框，便于核对金额）
    const fullNumbers = !isGraph && answerText
      ? computeNumberRanges(answerText, raw)
      : [];
    const built = buildSnippet(raw, fullHighlights, snippetChars, fullNumbers);
    return {
      isGraph,
      snippet: built.text,
      snippetHighlights: built.highlights,
      snippetNumbers: built.numbers,
    };
  }, [streaming, answerText, source, snippetChars]);

  const mark = (
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
  );

  // 流式中直接返回上标本身（不建 Tooltip、不算高亮）；生成结束后 waiting 归 false，
  // 这条消息重渲染成完整版，悬浮预览照常可用
  if (!summary) return mark;

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
          {summary.snippet && (
            <div
              style={{
                marginTop: 2,
                fontWeight: 400,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {splitByHighlights(summary.snippet, summary.snippetHighlights, summary.snippetNumbers).map((seg, i) => {
                const cls = [
                  seg.highlighted ? 'citation-highlight' : '',
                  seg.isNumber ? 'citation-number' : '',
                ].filter(Boolean).join(' ');
                return cls
                  ? <mark key={i} className={cls}>{seg.text}</mark>
                  : <React.Fragment key={i}>{seg.text}</React.Fragment>;
              })}
            </div>
          )}
          <div style={{ marginTop: 4, fontWeight: 400, opacity: 0.75 }}>
            点击查看{summary.isGraph ? '图谱内容' : '引用详情'}
          </div>
        </div>
      }
    >
      {mark}
    </Tooltip>
  );
};

/**
 * 把文本按 [n] 引用拆分为 ReactNode 数组：
 * - n 有效（sources[n-1] 存在）→ 渲染为行内引用标（CitationMark：上标 + Tooltip + 点击），
 *   点击回调对应来源（Chat 页 → CitationTraceModal 引用详情弹窗）
 * - **多编号合并标注 [1,4]**（prompt 明确要求的形式：连续多句引用同一组编号时合并）→
 *   拆成各自独立的引用标（[1][4]），每个都能单独悬浮看摘要、单独点击；越界的那个编号
 *   原样输出，不吞掉模型输出
 * - 编号全部不存在/越界（LLM 乱写）→ 整段原样渲染为普通文本，不报错
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
  snippetChars: number,
  streaming: boolean,
): React.ReactNode[] => {
  const parts: React.ReactNode[] = [];
  if (!content) return parts;
  if (!sources || sources.length === 0 || !onCitationClick) return [content];
  // 捕获组含逗号分隔的多个编号：[1] / [1,4] / [1, 4] 都收
  const re = /\[(\d+(?:\s*,\s*\d+)*)\](?=$|[\s,.;:!?，。；：！？、%．％~～）)\]】」"'’])/g;
  let last = 0;
  let key = 0;
  for (;;) {
    const m = re.exec(content);
    if (!m) break;
    if (m.index > last) parts.push(content.slice(last, m.index));
    const nums = m[1].split(',').map(s => parseInt(s.trim(), 10));
    if (nums.every(n => !sources[n - 1])) {
      // 编号全部越界：整段原样输出（既有降级行为）
      parts.push(m[0]);
    } else {
      // 逐个编号渲染：有效的是可点引用标，越界的原样保留
      nums.forEach((n) => {
        const source = sources[n - 1];
        if (source) {
          parts.push(
            <CitationMark
              key={key++}
              n={n}
              source={source}
              answerText={content}
              onClick={onCitationClick}
              snippetChars={snippetChars}
              streaming={streaming}
            />,
          );
        } else {
          // 越界编号（模型自造）：原样输出，不吞掉模型写的内容
          parts.push(`[${n}]`);
        }
      });
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
  snippetChars: number,
  streaming: boolean,
): React.ReactNode[] => {
  // 先清洗行首 Markdown 结构符号（### 标题 / - 列表等）：
  // 显示文本与高亮基准（answerText）都用清洗后文本，保证所见即所算。
  // 首尾空白先 trim：模型有时以空行开头（实测 "\n\n知识库中未找到您要的信息！"），
  // 在气泡里会显示成一块空白（历史脏数据也靠这一步兜住）
  const cleaned = cleanAnswerText(content.trim());
  // 再识别表格块（管道表格 / HTML <table> 存量兜底，半截自动回退纯文本），
  // 表格之外的文本继续走 [n] 引用标拆分 + MdImages 图片渲染
  const blocks = renderTableBlocks(cleaned);
  return blocks.map((b, bi) => {
    if (typeof b !== 'string') {
      return <React.Fragment key={`t${bi}`}>{b}</React.Fragment>;
    }
    const parts = renderCitationContent(b, sources, onCitationClick, snippetChars, streaming);
    return parts.map((p, pi) =>
      typeof p === 'string'
        ? <MdImages key={`m${bi}-${pi}`} text={p} maxWidth={ANSWER_IMAGE_MAX_WIDTH} />
        : <React.Fragment key={`c${bi}-${pi}`}>{p}</React.Fragment>,
    );
  });
};

/** 消息列表：用户右侧 / 助手左侧气泡；助手消息 pre-wrap 渲染，引用来源默认收成一行入口按钮 */
/** 回答反馈条：👍/👎（点踩展开纠正说明）——提交到 /chat/feedback */
const FeedbackBar: React.FC<{ idx: number; sessionId?: string; kbId?: string }> = ({
  idx,
  sessionId,
  kbId,
}) => {
  const { message } = AntApp.useApp();
  const [feedbacked, setFeedbacked] = useState<'up' | 'down' | ''>('');
  const [popOpen, setPopOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const handle = async (rating: 'up' | 'down', r: string) => {
    setSubmitting(true);
    try {
      await submitFeedback({
        rating,
        kb_id: kbId,
        session_id: sessionId,
        msg_idx: idx,
        reason: r,
      });
      setFeedbacked(rating);
      setPopOpen(false);
      message.success(rating === 'up' ? '感谢反馈' : '已反馈，我们会优化');
    } catch {
      message.error('反馈提交失败，请重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 2 }}>
      <Tooltip title={feedbacked === 'up' ? '已感谢反馈' : '回答有帮助'}>
        <Button
          type="text"
          size="small"
          icon={<LikeOutlined />}
          loading={submitting}
          style={{ color: feedbacked === 'up' ? '#52c41a' : undefined }}
          aria-label="点赞"
          onClick={() => feedbacked === '' && void handle('up', '')}
        />
      </Tooltip>
      <Popover
        open={popOpen}
        onOpenChange={setPopOpen}
        trigger="click"
        content={
          <div style={{ width: 260 }}>
            <Input.TextArea
              rows={2}
              maxLength={500}
              placeholder="哪里不对？可填写纠正/原因（可选）"
              value={reason}
              onChange={e => setReason(e.target.value)}
            />
            <div style={{ marginTop: 8, textAlign: 'right' }}>
              <Button
                size="small"
                type="primary"
                loading={submitting}
                onClick={() => void handle('down', reason)}
              >
                提交
              </Button>
            </div>
          </div>
        }
        placement="bottomLeft"
      >
        <Tooltip title={feedbacked === 'down' ? '已记录反馈' : '回答有问题'}>
          <Button
            type="text"
            size="small"
            icon={<DislikeOutlined />}
            style={{ color: feedbacked === 'down' ? '#ff4d4f' : undefined }}
            aria-label="点踩"
            onClick={() => feedbacked === '' && setPopOpen(true)}
          />
        </Tooltip>
      </Popover>
    </div>
  );
};

interface MessageItemProps {
  m: ChatMessage;
  /** 消息序号（反馈条定位用） */
  idx: number;
  /** 该条正在流式生成中（内容每帧都在变） */
  streaming: boolean;
  /** waiting 期间的最后一条：引用来源按钮延后到生成完再显示 */
  pending: boolean;
  /** 该条对应的用户提问（详情弹窗「检索问题」；无则为空串） */
  question: string;
  sessionId?: string;
  kbId?: string;
  snippetChars: number;
  /** token.colorTextTertiary（元信息行/已停止文字的灰字色） */
  textTertiary: string;
  userId: string;
  userAvatar?: string | null;
  onCitationClick?: (s: Source) => void;
  /** 打开引用来源弹窗（sources + 高亮基准原文） */
  onOpenSources: (sources: Source[], answerText: string) => void;
  /** 打开请求详情弹窗 */
  onOpenDetail: (m: ChatMessage, question: string) => void;
}

/**
 * 单条消息（用户/助手气泡 + 反馈条 + 元信息行）。
 *
 * **必须 memo 化**：流式输出时每 50ms 就会 setMessages 一次，不隔离的话每次都要把
 * 整段会话的每条消息连同各自的 antd Tooltip/Button/头像全部重建一遍——实测单帧
 * 300ms、主线程被堵死，节流被反噬成 2.4Hz（"输出一卡一卡"）。抽成 memo 组件后，
 * 只有内容真变的那条（正在生成的那条）重渲染，历史消息整棵子树跳过。
 * 前提是回调 prop 全部来自 useCallback（否则每次渲染都是新函数，memo 形同虚设）。
 */
const MessageItem: React.FC<MessageItemProps> = ({
  m,
  idx,
  streaming,
  pending,
  question,
  sessionId,
  kbId,
  snippetChars,
  textTertiary,
  userId,
  userAvatar,
  onCitationClick,
  onOpenSources,
  onOpenDetail,
}) => {
  const isUser = m.role === 'user';
  // 思考区展开状态：null = 用户未手动干预 → 跟随流式（思考阶段自动展开，
  // 让用户看到字在动而不是干等；正文开始/生成结束后自动收起为一行）；
  // 用户点开/收起过就固定为用户的选择，不再被流式状态覆盖
  const [thinkOpenManual, setThinkOpenManual] = useState<boolean | null>(null);
  const thinkingNow = !!(pending && !m.content && m.reasoning);
  const thinkOpen = thinkOpenManual ?? thinkingNow;
  return (
    <div
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
        {/* 思考过程（推理模型 reasoning_content）：**仅流式期间存在于前端内存**
            ——后端不落盘，刷新/切会话即消失，故历史消息不会出现本块。
            限高滚动（见 chat.css .bubble-thinking-body）：长思考不撑爆列表 */}
        {!isUser && !!m.reasoning && (
          <div
            className="bubble-thinking"
            style={{ padding: '8px 12px', marginBottom: m.content ? 6 : 0 }}
          >
            <div
              className="bubble-thinking-head"
              onClick={() => setThinkOpenManual(!thinkOpen)}
            >
              {thinkingNow ? (
                <span className="bubble-thinking-dot" />
              ) : (
                <BulbOutlined style={{ fontSize: 12 }} />
              )}
              <span>
                {thinkingNow
                  ? '正在深度思考…'
                  : `已思考 ${m.reasoning.length} 字`}
              </span>
              <DownOutlined
                style={{
                  fontSize: 10,
                  transition: 'transform 0.2s',
                  transform: thinkOpen ? 'rotate(180deg)' : 'rotate(0deg)',
                }}
              />
            </div>
            {thinkOpen && (
              <div className="bubble-thinking-body">{m.reasoning}</div>
            )}
          </div>
        )}
        {m.content && (
          <div
            className={`${isUser ? 'bubble-user' : 'bubble-assistant'} ${streaming ? 'typing-cursor' : ''}`}
            style={{
              padding: '10px 14px',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {renderContent(m.content, m.sources, onCitationClick, snippetChars, streaming)}
            {/* 用户点击停止后：尾部灰色小字标注（仅前端会话状态，不污染落盘内容） */}
            {!isUser && m.stopped && !streaming && (
              <div style={{ marginTop: 6, fontSize: 12, color: textTertiary }}>
                （已停止生成）
              </div>
            )}
          </div>
        )}
        {/* 回答反馈（👍👎；流式进行中/用户消息不展示） */}
        {!isUser && !streaming && (
          <FeedbackBar idx={idx} sessionId={sessionId} kbId={kbId} />
        )}
        {/* 元信息行：时间戳 + 引用来源 + 详情。三者并列一行——两个入口挨着
            更好点，也省一行高度。各自条件独立保留：时间/详情随 created_at
            与流式状态，引用来源随 sources 与 pending 状态（流式中仍可点开看） */}
        {((m.created_at && !streaming)
          || (m.sources && m.sources.length > 0 && !pending)) && (
          <div
            style={{
              marginTop: 4,
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 11,
              lineHeight: '16px',
              color: textTertiary,
              justifyContent: isUser ? 'flex-end' : 'flex-start',
            }}
          >
            {m.created_at && !streaming && (
              <span>{dayjs(m.created_at).format('HH:mm')}</span>
            )}
            {/* 默认收起：一行小按钮，点击弹出来源详情 Modal */}
            {m.sources && m.sources.length > 0 && !pending && (
              <Button
                type="link"
                size="small"
                className="source-trigger"
                style={{ padding: 0, fontSize: 12, height: 'auto', lineHeight: '16px' }}
                icon={<PaperClipOutlined style={{ fontSize: 12 }} />}
                onClick={() => onOpenSources(m.sources ?? [], m.content)}
              >
                引用来源（{m.sources.length}）
              </Button>
            )}
            {/* 请求详情入口：仅本次流式生成且带详情数据的 assistant
                消息显示（历史会话加载的消息无这些字段，自动不显示） */}
            {m.created_at && !streaming && !isUser
              && ((!!m.prompt && (m.retrieval_ms !== undefined || m.total_ms !== undefined)) || !!m.agentic) && (
              <Button
                type="link"
                size="small"
                className="source-trigger"
                style={{ padding: 0, fontSize: 11, height: 'auto', lineHeight: '16px' }}
                onClick={() => onOpenDetail(m, question)}
              >
                详情
              </Button>
            )}
          </div>
        )}
      </div>
      {/* 用户头像在气泡右侧（与气泡同级 flex 项，顶部对齐） */}
      {isUser && <UserAvatar userId={userId} avatarKey={userAvatar} />}
    </div>
  );
};

const MemoMessageItem = memo(MessageItem);

const MessageList: React.FC<MessageListProps> = ({
  messages,
  waiting,
  waitingHint,
  onCitationClick,
  sessionId,
  kbId,
  citationSnippetChars,
}) => {
  // 引用摘要窗口大小：配置档案「聊天设置 → 引用设置」（默认 600 字）
  const snippetChars = citationSnippetChars ?? 600;
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
  // （useCallback：这个回调要透传到 memo 化的 MessageItem 上，每次新建函数会让 memo 失效）
  const handleViewOriginal = useCallback((s: Source) => {
    setModalSources(null);
    onCitationClick?.(s);
  }, [onCitationClick]);

  // 打开"引用来源"弹窗：高亮基准用清洗后文本（与气泡渲染一致，所见即所算）
  const handleOpenSources = useCallback((sources: Source[], answerText: string) => {
    setModalSources(sources);
    setModalAnswerText(cleanAnswerText(answerText));
  }, []);

  // 打开"请求详情"弹窗：检索问题由 MessageItem 随消息一起传上来（在渲染处向前
  // 找最近的 user 消息算好），这里不碰 messages——否则回调依赖 messages，每次
  // 流式增量都换新函数，memo 就白做了
  const handleOpenDetail = useCallback((m: ChatMessage, question: string) => {
    setDetailMsg(m);
    setDetailQuestion(question);
  }, []);

  // 流式生成中（waiting）且最后一条助手消息已有内容 → 追加闪烁光标
  const last = messages[messages.length - 1];
  const streamingLive = !!(waiting && last && last.role === 'assistant' && last.content);
  // 正文与思考都还没出（真正的首字等待期）才显示"正在思考…"占位：已有思考
  // 内容时由思考区承载等待反馈，否则同一时刻会出现两块等待 UI
  const showThinking = !!(waiting && !streamingLive && !last?.reasoning);

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
          // 等待回复期间最后一条 assistant 消息内容为空 → 跳过整条渲染
          // （"正在思考…"气泡自带 AI 头像，避免同一时刻出现两个 AI 头像；
          //  AI 输出内容后 thinking 消失、空消息变正常气泡，其余消息不受影响）
          if (showThinking && idx === messages.length - 1 && m.role === 'assistant') {
            return null;
          }
          // 该条对应的用户提问（详情弹窗「检索问题」）：向前找最近的一条 user 消息。
          // 在这里算好成字符串再传下去，MessageItem 的 memo 比较才不被新对象破坏
          let question = '';
          for (let i = idx - 1; i >= 0; i--) {
            if (messages[i].role === 'user') { question = messages[i].content; break; }
          }
          return (
            <MemoMessageItem
              key={idx}
              m={m}
              idx={idx}
              // 只有正在生成的那条会重渲染；其余条目 memo 命中，整棵子树跳过
              streaming={streamingLive && idx === messages.length - 1}
              pending={!!(waiting && idx === messages.length - 1)}
              question={question}
              sessionId={sessionId}
              kbId={kbId}
              snippetChars={snippetChars}
              textTertiary={token.colorTextTertiary}
              userId={user?.id ?? ''}
              userAvatar={user?.avatar}
              onCitationClick={onCitationClick}
              onOpenSources={handleOpenSources}
              onOpenDetail={handleOpenDetail}
            />
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
              <span style={{ color: token.colorTextTertiary, fontSize: 13 }}>
                {waitingHint || '正在思考…'}
              </span>
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
      <AppModal
        dimension="auto"
        defaultSize={{ w: 720, h: 420 }}
        rememberKey="sources"
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
      </AppModal>
    )}

    {/* 请求详情 Modal：检索问题 / 召回耗时 / 总耗时 / 完整提示词
        （实现已抽到 shared/components/common/RequestDetailModal——「用户反馈」页的
         会话回溯抽屉复用同一实现，避免两处各自演化） */}
    <RequestDetailModal
      message={detailMsg}
      question={detailQuestion}
      onClose={() => setDetailMsg(null)}
    />
    </>
  );
};

// memo 化：Chat 页其它 state（溯源弹窗、会话设置等）变化时不重渲染整段消息列表。
// 流式更新仍会进来（messages 变了），但内部 MemoMessageItem 会把无变化的消息挡掉
export default memo(MessageList);
