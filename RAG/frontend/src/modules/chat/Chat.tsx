import React, { memo, useCallback, useEffect, useRef, useState } from 'react';
import AppModal from '../../shared/components/common/AppModal';
import { App as AntApp,  Button,  Card,  Empty,  Input,  List,  Popconfirm,  Select,  Tooltip,  Typography,  theme } from 'antd';
import { DeleteOutlined, DownloadOutlined, EditOutlined, FolderOpenOutlined, MessageOutlined, PlusOutlined, SettingOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import {
  asApiError,
  AgenticStatus,
  AgenticTrace,
  ChatMessage,
  ChatSession,
  GenParams,
  KnowledgeBase,
  Source,
  deleteSession,
  exportSession,
  getSession,
  getChatSettings,
  listKbs,
  listSessions,
  renameSession,
  streamChat,
} from '../../shared/api/client';
import MessageList from './components/MessageList';
import { cleanAnswerText } from '../../shared/utils/cleanMarkdown';
import ChatInput from './components/ChatInput';
import ChatSettingsModal from './components/ChatSettingsModal';
import CitationTraceModal from './components/CitationTraceModal';
import { useAuth } from '../../shared/auth/AuthContext';

const KB_ID_KEY = 'myrag.kb_id';
// 「新建」后的草稿会话 id：仅前端占位、不落库，发送第一条消息时后端才真正创建会话
// （避免点几次「新建」就往会话列表里堆一串空会话）
const DRAFT_SESSION_ID = '__draft__';
// 草稿会话在列表顶部的占位项（条数/时间不展示，由渲染分支单独处理）
const DRAFT_SESSION: ChatSession = {
  id: DRAFT_SESSION_ID, kb_ids: [], title: '新会话',
  message_count: 0, created_at: '', updated_at: '',
};

interface SessionListProps {
  sessions: ChatSession[];
  activeSessionId?: string;
  /** 生成中：新建会话按钮置灰 */
  streaming: boolean;
  /** token.colorTextTertiary（条数/「等待提问…」灰字） */
  textTertiary: string;
  onNew: () => void;
  onOpen: (id: string) => void;
  onRename: (item: ChatSession) => void;
  onExport: (id: string) => void;
  onDelete: (id: string) => void;
}

/**
 * 左栏「会话列表」。
 *
 * **必须 memo 化**：messages 是本页的顶层 state，流式输出时每 50ms 变一次 → ChatPage
 * 整棵树重渲染。会话条数一多（实测某账号 154 条，每条 24 个 DOM 节点、内含
 * Popconfirm + 3 个 Button），每帧就得把上千个节点连同 antd 组件重新
 * 创建一遍，主线程被堵 300+ms，50ms 的流式节流被反噬成 2.4Hz —— 肉眼就是"输出一卡
 * 一卡"。而会话列表在流式期间根本不变，没有任何理由跟着重建。
 * 生效前提：回调 prop 全部稳定（见 ChatPage 里的 useCallback）。
 */
/** 会话列表刷新时判断条目内容是否等价（用于复用旧对象引用，见 loadSessions） */
const sameSession = (a: ChatSession, b: ChatSession): boolean =>
  a.id === b.id
  && a.title === b.title
  && a.message_count === b.message_count
  && a.updated_at === b.updated_at
  && a.created_at === b.created_at
  && (a.kb_ids ?? []).join(',') === (b.kb_ids ?? []).join(',');

interface SessionItemProps {
  item: ChatSession;
  /** 是否当前打开的会话（高亮选中态） */
  isActive: boolean;
  /** token.colorTextTertiary（条数/「等待提问…」灰字） */
  textTertiary: string;
  onOpen: (id: string) => void;
  onRename: (item: ChatSession) => void;
  onExport: (id: string) => void;
  onDelete: (id: string) => void;
}

/**
 * 单个会话条目（草稿占位项 / 普通会话项两种形态）。**必须 memo**。
 *
 * SessionList 有三条重渲染路径——切会话（activeSessionId 变）、提问开始与生成
 * 结束（streaming 变）、生成结束后刷新列表（sessions 变），每条都会把所有条目
 * 重建一遍。实测 159 条时这一遍就是单个 368ms 的长任务，表现是"切会话明显卡一下"、
 * "一问完又卡一下"。memo 之后只有**选中态真正变化的那两条**会重渲染
 * （失去高亮的旧项 + 获得高亮的新项），其余直接跳过。
 */
const SessionItem = memo(function SessionItem({
  item,
  isActive,
  textTertiary,
  onOpen,
  onRename,
  onExport,
  onDelete,
}: SessionItemProps) {
  // 草稿会话：顶上那条占位项，尚未落库故不给重命名/导出/删除
  if (item.id === DRAFT_SESSION_ID) {
    return (
      <List.Item
        className="session-item session-item--active"
        style={{ cursor: 'default' }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, width: '100%' }}>
          <MessageOutlined style={{ color: 'var(--brand-primary, #2563eb)', marginTop: 3 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <span style={{ fontSize: 13, display: 'block', lineHeight: '18px' }}>新会话</span>
            <span style={{ fontSize: 12, color: textTertiary }}>等待提问…</span>
          </div>
        </div>
      </List.Item>
    );
  }
  // 会话名默认最多显示 8 个字符，超出用 ... 省略（悬停看完整名：span 的原生 title 属性）
  const name = item.title || '未命名会话';
  const shortName = name.length > 8 ? `${name.slice(0, 8)}...` : name;
  return (
    <List.Item
      onClick={e => {
        // 操作按钮区/浮层点击不触发展开会话：
        // Popconfirm「确定」按钮渲染在 portal 浮层（React 事件沿组件
        // 树冒泡到本项），不拦截会在删除同时 getSession → 竞态：
        // 删除当前会话时 GET 404「加载会话失败」，删除非当前会话时
        // GET 200 把已删会话误加载进消息区。
        const t = e.target as HTMLElement;
        if (t.closest('.ant-popover, .ant-tooltip, .session-item-actions')) return;
        onOpen(item.id);
      }}
      className={`session-item${isActive ? ' session-item--active' : ''}`}
      style={{ cursor: 'pointer' }}
    >
      {/* 两列布局：左气泡图标，右（第一行会话名 / 第二行条数+操作按钮） */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, width: '100%' }}>
        <MessageOutlined style={{ color: 'var(--brand-primary, #2563eb)', marginTop: 3 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          {/* 悬停看全名：用 DOM 原生 title，不用 antd Tooltip——每条会话挂 4 个 Tooltip
              时，139 条就是 500+ 个 Tooltip 实例（每个含 Trigger/Context/state/事件监听），
              首屏光渲染列表实测阻塞 881ms。原生 title 提示效果等价（浮层样式改用浏览器
              默认，项目内知识库选择器同款取舍） */}
          <span
            title={name}
            style={{ fontSize: 13, display: 'block', lineHeight: '18px' }}
          >
            {shortName}
          </span>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              marginTop: 2,
            }}
          >
            <span style={{ fontSize: 12, color: textTertiary, flexShrink: 0 }}>
              {item.message_count} 条消息
            </span>
            {/* 操作按钮组：重命名 → 导出 → 删除（顺序按用户要求）；与条数标签留 3 个汉字间距 */}
            <span
              className="session-item-actions"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 2,
                marginLeft: '3em',
              }}
            >
              {/* 三个图标按钮的悬停提示同样用原生 title：与上面会话名同因（省掉
                  每条 3 个 Tooltip 实例）。Popconfirm 是删除确认功能，保留不动 */}
              <Button
                title="重命名"
                type="text"
                size="small"
                className="session-rename-btn"
                icon={<EditOutlined />}
                onClick={e => {
                  e.stopPropagation();
                  onRename(item);
                }}
              />
              <Button
                title="导出会话"
                type="text"
                size="small"
                className="session-export-btn"
                icon={<DownloadOutlined />}
                onClick={e => {
                  e.stopPropagation();
                  onExport(item.id);
                }}
              />
              <Popconfirm
                title="删除该会话？"
                onConfirm={() => onDelete(item.id)}
              >
                <Button
                  title="删除会话"
                  type="text"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={e => e.stopPropagation()}
                />
              </Popconfirm>
            </span>
          </div>
        </div>
      </div>
    </List.Item>
  );
}, (prev, next) =>
  // 只比"影响显示"的三个 prop，**回调 prop 一律忽略**：它们只在点击那一刻用，
  // 而 AntApp.useApp() 的 message 一旦换新对象，依赖它的 useCallback 会全部跟着换，
  // 默认浅比较就判定 "props 变了" → 上百个条目整列重建（实测一次删除渲染数百次、
  // 主线程堵 800ms）。点击时闭包里的旧回调功能等价——需要状态的地方（当前会话 id /
  // 已选知识库）已经改用 ref 读取，不存在过期问题
  prev.item === next.item
  && prev.isActive === next.isActive
  && prev.textTertiary === next.textTertiary);

/** 会话项行高（px）：实测普通项 64、草稿项 60（草稿项第二行是纯文字，比带按钮的普通项矮 4px） */
const SESSION_ROW_H = 64;
const DRAFT_ROW_H = 60;
/** 可视区上下各多渲染的行数：快速滚动时新行已在 DOM 里，避免出现瞬时空白 */
const SESSION_OVERSCAN = 4;

const SessionList = memo(function SessionList({
  sessions,
  activeSessionId,
  streaming,
  textTertiary,
  onNew,
  onOpen,
  onRename,
  onExport,
  onDelete,
}: SessionListProps) {
  // 虚拟滚动：只把「可视区 + 上下缓冲」的条目交给 antd List 渲染。
  // 该账号 139 条会话时全量渲染光列表就阻塞主线程 881ms、首屏近 2s 才出得来；
  // 而滚动容器高 813px、每条 64px —— 真正看得见的只有 12 条左右，其余白渲染。
  // 做法：外层 div 撑出总高度（滚动条长度/位置与全量渲染完全一致），
  // 内层按起始项偏移整体 translateY，条目仍由 antd List + SessionItem 渲染（样式不变）。
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportH, setViewportH] = useState(0);

  // items 与原 dataSource 同源：草稿会话（新建后未落库）固定插在第 0 位
  const items = activeSessionId === DRAFT_SESSION_ID ? [DRAFT_SESSION, ...sessions] : sessions;
  const hasDraft = items.length > 0 && items[0].id === DRAFT_SESSION_ID;

  /** 第 i 项的顶部偏移：草稿项矮 4px，其后的普通项整体上移 4px */
  const offsetOf = (i: number) =>
    i * SESSION_ROW_H + (hasDraft && i > 0 ? DRAFT_ROW_H - SESSION_ROW_H : 0);
  const totalH = hasDraft
    ? DRAFT_ROW_H + Math.max(0, items.length - 1) * SESSION_ROW_H
    : items.length * SESSION_ROW_H;

  // 首帧 viewportH 还是 0，只渲染缓冲的几行；测量后立刻补齐，用户看不到这一瞬
  const start = Math.max(0, Math.floor(scrollTop / SESSION_ROW_H) - SESSION_OVERSCAN);
  const end = Math.min(
    items.length,
    Math.ceil((scrollTop + viewportH) / SESSION_ROW_H) + SESSION_OVERSCAN);
  const visible = items.slice(start, end);

  // rAF 节流：滚动事件每帧最多触发一次重渲染
  const tickingRef = useRef(false);
  const handleScroll = useCallback(() => {
    if (tickingRef.current) return;
    tickingRef.current = true;
    requestAnimationFrame(() => {
      tickingRef.current = false;
      const el = scrollRef.current;
      if (el) setScrollTop(el.scrollTop);
    });
  }, []);

  // 可视区高度：挂载时测量，容器尺寸变化（窗口缩放/侧栏伸缩）时重测
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setViewportH(el.clientHeight);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <Card
      title="会话列表"
      size="small"
      extra={
        <Button size="small" type="primary" icon={<PlusOutlined />} onClick={onNew} disabled={streaming}>
          新建
        </Button>
      }
      style={{ width: 230, display: 'flex', flexDirection: 'column' }}
      styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' } }}
    >
      <div ref={scrollRef} onScroll={handleScroll} style={{ flex: 1, overflowY: 'auto' }}>
        {items.length === 0 ? (
          <List
            dataSource={[]}
            locale={{ emptyText: <Empty description="暂无会话" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          />
        ) : (
          // 外层撑出总高度（滚动条长度/位置与全量渲染一致），内层按起始项偏移整体位移
          <div style={{ height: totalH, position: 'relative' }}>
            <div style={{ transform: `translateY(${offsetOf(start)}px)` }}>
              <List
                // rowKey 必须显式指定：antd 的 List 在无 rowKey 且 item 无 key 字段时，
                // 回退用**数组下标**给每个条目包 Fragment（antd/es/list/index.js: renderInternalItem）。
                // 下标做 key 的后果——只要数组长度变化，其后所有条目下标整体偏移、key 全变，
                // React 就卸载整列再挂载整列（实测：新建会话 150/150 重建卡 998ms、
                // 删除会话 148/149 重建卡 973ms）。此时 SessionItem 上的 memo **完全不参与**
                // （重挂载不走比较函数），上一轮"切会话已修好、删除新建仍卡"正是这个原因。
                // 用会话 id 做 key 后：切会话/新建/删除都只重渲染真正变化的那一两条。
                rowKey="id"
                dataSource={visible}
                renderItem={item => (
                  <SessionItem
                    key={item.id}
                    item={item}
                    isActive={item.id === activeSessionId}
                    textTertiary={textTertiary}
                    onOpen={onOpen}
                    onRename={onRename}
                    onExport={onExport}
                    onDelete={onDelete}
                  />
                )}
              />
            </div>
          </div>
        )}
      </div>
    </Card>
  );
});

const ChatPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const { user } = useAuth();
  const navigate = useNavigate();
  // 检索参数（top_k）与聊天设置仅管理员/超管可见，普通用户由系统配置统一管理
  const isAdmin = !!user && user.role !== 'user';

  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  // 知识库多选：存 id 数组（旧版本 localStorage 存的是单个字符串，按单元素兼容）
  const [kbIds, setKbIds] = useState<string[]>(() => {
    const saved = localStorage.getItem(KB_ID_KEY);
    if (!saved) return [];
    try {
      const parsed = JSON.parse(saved);
      if (Array.isArray(parsed)) {
        return parsed.filter(x => typeof x === 'string');
      }
    } catch {
      // 旧格式是裸 id 字符串（非 JSON），落到下面按单值处理
    }
    return [saved];
  });
  /** kbIds 的稳定键：数组直接进 deps 每次渲染都是新引用，会触发死循环 */
  const kbIdsKey = kbIds.join(',');

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | undefined>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // messages 的最新值，供**稳定回调**读取（溯源点击要按 source.id 反查所属消息）。
  // 不用 useCallback 依赖 messages：流式时每 50ms 变一次，回调跟着换新函数，
  // 会把 MessageList / MessageItem 的 memo 全部打穿
  const messagesRef = useRef<ChatMessage[]>([]);
  useEffect(() => { messagesRef.current = messages; }, [messages]);

  // 同理：会话删除回调只需要"读一眼当前值"（删的是不是当前打开的会话、有没有选库），
  // 不是"跟随它们变化"。放进 useCallback 依赖的代价是——切会话 / 换知识库时回调换新，
  // 透传到 memo 化的 SessionItem 后 159 个条目的 memo 全部失效、全量重渲染
  const activeSessionIdRef = useRef<string | undefined>(undefined);
  useEffect(() => { activeSessionIdRef.current = activeSessionId; }, [activeSessionId]);
  const kbIdsRef = useRef<string[]>([]);
  useEffect(() => { kbIdsRef.current = kbIds; }, [kbIds]);

  const [topK, setTopK] = useState(5);
  const [streaming, setStreaming] = useState(false);
  /** 引用摘要窗口大小（字）：配置档案「聊天设置 → 引用设置」，取不到时用默认 600 */
  const [citationSnippetChars, setCitationSnippetChars] = useState<number | undefined>();

  // 拉一次生效的聊天配置（接口返回「全局档案 + 部门覆盖」合并值）：
  // 只取引用摘要字数，用于回答里引用标 [n] 的悬浮浮层窗口大小。
  // 失败静默降级为前端默认 600 —— 展示偏好，不值得打扰用户或阻塞聊天
  useEffect(() => {
    getChatSettings()
      .then(res => {
        const n = res.data?.chat?.citation_snippet_chars;
        if (typeof n === 'number' && n > 0) setCitationSnippetChars(n);
      })
      .catch(() => { /* 静默：沿用默认值 */ });
  }, []);
  // Agentic 决策阶段的进度提示（检索中/改写中/重新检索中；默认空=「正在思考…」）
  const [statusHint, setStatusHint] = useState('');
  const [chatSettingsOpen, setChatSettingsOpen] = useState(false);
  // 引用溯源：点击 [n] 引用标或引用面板"查看原文"时打开弹窗
  const [traceSource, setTraceSource] = useState<Source | null>(null);
  // 引用溯源弹窗的回答文本（该引用所属回答消息的 content；原文回答-对齐高亮匹配基准）
  const [traceAnswerText, setTraceAnswerText] = useState('');

  // 点击回答中 [n] 引用标 / 引用面板"查看原文"：打开溯源弹窗，并按 source.id
  // 反查所属回答（原文-对齐高亮的匹配基准）。空依赖 + messagesRef —— 回调引用稳定，
  // 下游 MessageItem 的 memo 才拦得住重渲染（源注释见 messagesRef 定义处）
  const handleCitationClick = useCallback((s: Source) => {
    setTraceSource(s);
    const msg = messagesRef.current.find(m => (m.sources ?? []).some(sr => sr.id === s.id));
    // 高亮基准用清洗后文本（与气泡渲染一致，所见即所算）
    setTraceAnswerText(cleanAnswerText(msg?.content ?? ''));
  }, []);
  // 会话重命名：弹窗编辑标题（默认值当前标题）
  const [renameTarget, setRenameTarget] = useState<ChatSession | null>(null);
  const [renameTitle, setRenameTitle] = useState('');
  const [renaming, setRenaming] = useState(false);

  const abortRef = useRef<(() => void) | null>(null);
  const streamingRef = useRef(false);
  // 流式增量节流（50ms 合并一次 DOM 更新）
  const deltaBufRef = useRef('');
  const flushTimerRef = useRef<number | null>(null);

  // ---------- 知识库 ----------
  const loadKbs = useCallback(async () => {
    try {
      const res = await listKbs();
      setKbs(res.data);
      if (res.data.length === 0) {
        setKbIds([]);
      } else {
        // 选中的库可能已被删除：只保留仍存在的；一个都不剩 → 回落到第一个
        const valid = kbIds.filter(id => res.data.some(k => k.id === id));
        if (valid.length !== kbIds.length) {
          setKbIds(valid.length ? valid : [res.data[0].id]);
        }
      }
    } catch {
      message.error('加载知识库列表失败');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbIdsKey, message]);

  useEffect(() => {
    loadKbs();
  }, [loadKbs]);

  // 记忆知识库选择
  useEffect(() => {
    if (kbIds.length) localStorage.setItem(KB_ID_KEY, JSON.stringify(kbIds));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kbIdsKey]);

  // ---------- 会话列表 ----------
  // 打开指定会话：加载消息并高亮（失败只提示，不改动当前选中）
  const openSessionById = useCallback(
    async (id: string) => {
      try {
        const res = await getSession(id);
        setMessages(res.data.messages);
        setActiveSessionId(id);
        // 会话记着自己用的库：点开时把选择器切回那几个（多库会话一并恢复）
        if (res.data.kb_ids?.length) setKbIds(res.data.kb_ids);
      } catch {
        message.error('加载会话失败');
      }
    },
    [message],
  );

  // autoOpenFirst：首次进入 / 切换知识库时自动打开第一个（最新）会话。
  // 发完消息刷新列表必须传 false —— 否则会把用户手动选中的/新建的会话拽回第一个
  // autoOpenFirst：进入页面时自动打开最新一个会话（会话自带 kb_ids，打开时会
  // 切换知识库选择器）。发完消息刷新列表必须传 false —— 否则会把用户手动选中的/
  // 新建的会话拽回第一个
  const loadSessions = useCallback(
    async (autoOpenFirst = false) => {
      try {
        // 全局一份列表（不按当前选中的库过滤）：会话自己记着它用的库
        const res = await listSessions();
        // 内容没变的条目**复用旧对象引用**：接口每次返回全新对象，直接 setSessions(res.data)
        // 会让 159 个条目的 item prop 全是新引用 → SessionItem 的 memo 全部失效 → 整列重建
        // （实测每轮问答结束时一次 878ms 的长任务）。复用引用后只有真正变化的条目重渲染
        setSessions(prev => {
          const byId = new Map(prev.map(s => [s.id, s]));
          return res.data.map(s => {
            const old = byId.get(s.id);
            return old && sameSession(old, s) ? old : s;
          });
        });
        if (autoOpenFirst && res.data.length > 0) {
          await openSessionById(res.data[0].id);
        }
      } catch {
        message.error('加载会话列表失败');
      }
    },
    [message, openSessionById],
  );

  // 进入页面：拉全局会话列表并自动打开最新一个（打开时会切回它自己的库）。
  // 会话列表是**全局一份**，不随选择器变化重建——想看别的会话直接点即可
  useEffect(() => {
    loadSessions(true);
  }, [loadSessions]);

  // 以下会话操作回调全部 useCallback 包住：它们要传给 memo 化的 SessionList，
  // 每次渲染新建函数会让 memo 失效，会话列表又回到"每帧重建上千个节点"
  const handleNewSession = useCallback(() => {
    if (streamingRef.current) return;
    // 草稿态：仅前端占位，列表顶部出现「新会话」项并高亮，发第一条消息时才落库
    setActiveSessionId(DRAFT_SESSION_ID);
    setMessages([]);
  }, []);

  const handleOpenSession = useCallback(async (id: string) => {
    if (streamingRef.current) {
      message.warning('生成中，请先停止');
      return;
    }
    await openSessionById(id);
  }, [message, openSessionById]);

  const handleDeleteSession = useCallback(async (id: string) => {
    if (!kbIdsRef.current.length) return;
    try {
      await deleteSession(id);
      // 删的是当前打开的会话 → 清空消息区（其余情况只刷新列表）
      if (id === activeSessionIdRef.current) {
        setActiveSessionId(undefined);
        setMessages([]);
      }
      await loadSessions();
      message.success('会话已删除');
    } catch {
      message.error('删除会话失败');
    }
    // 依赖里**不能**再出现 activeSessionId/kbIds：它们一变就换新函数，而本回调要
    // 透传到 memo 化的 SessionItem——prop 一变，159 个条目全量重渲染（实测 368ms）。
    // 两个都是"读取当前值"而非"跟随变化"，用 ref 取最新即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadSessions, message]);

  // 导出会话为 Markdown：fetch 拿 blob 走浏览器下载（与 PDF 预览同模式，不裸传 token）
  const handleExportSession = useCallback(async (id: string) => {
    try {
      await exportSession(id);
      message.success('会话已导出');
    } catch (e: unknown) {
      message.error(asApiError(e).message || '导出会话失败');
    }
  }, [message]);

  const openRenameModal = useCallback((item: ChatSession) => {
    setRenameTarget(item);
    setRenameTitle(item.title || '');
  }, []);

  const handleRenameSubmit = async () => {
    if (!renameTarget) return;
    const title = renameTitle.trim();
    if (!title) {
      message.warning('标题不能为空');
      return;
    }
    if (title.length > 50) {
      message.warning('标题不能超过 50 字');
      return;
    }
    setRenaming(true);
    try {
      await renameSession(renameTarget.id, title);
      message.success('会话已重命名');
      setRenameTarget(null);
      if (kbIds.length) await loadSessions();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '重命名失败');
    } finally {
      setRenaming(false);
    }
  };

  // ---------- 流式处理 ----------
  const flushDelta = useCallback(() => {
    const text = deltaBufRef.current;
    deltaBufRef.current = '';
    if (flushTimerRef.current) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    if (!text) return;
    setMessages(prev => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.role === 'assistant') {
        next[next.length - 1] = { ...last, content: last.content + text };
      }
      return next;
    });
  }, []);

  const handleDelta = useCallback(
    (text: string, total_ms?: number) => {
      // 只有首条 delta 带 total_ms（后端首个 token 埋点的「提问→首字」总耗时）：
      // 写进最后一条 assistant 消息，"刚问完立刻点详情"即可看到。
      // 早先是前端 performance.now() 自己算，只活在内存里——切会话/刷新就丢
      if (total_ms !== undefined) {
        setMessages(prev => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant') {
            next[next.length - 1] = { ...last, total_ms };
          }
          return next;
        });
      }
      deltaBufRef.current += text;
      if (flushTimerRef.current) return;
      flushTimerRef.current = window.setTimeout(flushDelta, 50);
    },
    [flushDelta],
  );

  const handleMeta = useCallback((sources: Source[]) => {
    setMessages(prev => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.role === 'assistant') {
        next[next.length - 1] = { ...last, sources };
      }
      return next;
    });
  }, []);

  // prompt 事件：完整提示词 + 检索/图谱/改写耗时 + 改写后检索词写入最后一条
  // assistant 消息（setMessages prev 形式：此时最后一条必为刚 push 的 assistant）
  // rewritten_query/rewrite_ms 必须一并回写：少写这两个字段会让弹窗的
  // 「改写后检索词」「查询改写耗时」两处渲染恒不执行——明明改写了却看着像没改写
  const handlePrompt = useCallback(
    (info: { prompt: unknown[]; retrieval_ms?: number; kg_ms?: number;
             rewrite_ms?: number; rewritten_query?: string | null }) => {
      setMessages(prev => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === 'assistant') {
          next[next.length - 1] = {
            ...last,
            prompt: info.prompt,
            retrieval_ms: info.retrieval_ms,
            kg_ms: info.kg_ms,
            rewrite_ms: info.rewrite_ms,
            rewritten_query: info.rewritten_query,
          };
        }
        return next;
      });
    },
    [],
  );

  // agentic 事件：Agentic 检索决策轨迹（改写查询/分档分数/尝试次数）写入
  // 最后一条 assistant 消息（请求详情展示；默认关闭时无该事件）
  const handleAgentic = useCallback((info: AgenticTrace) => {
    setMessages(prev => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.role === 'assistant') {
        next[next.length - 1] = { ...last, agentic: info };
      }
      return next;
    });
  }, []);

  // agentic_status 事件：决策阶段进度（检索中/改写中/重新检索中），
  // 让用户知道 AI 在干嘛而不是干等"思考中"
  const handleAgenticStatus = useCallback((info: AgenticStatus) => {
    const hints: Record<AgenticStatus['stage'], string> = {
      'retrieving': '正在检索知识库…',
      'rewriting': '正在改写查询，提高召回…',
      'rechecking': `已改写为「${info.query ?? ''}」，正在重新检索…`,
    };
    setStatusHint(hints[info.stage] ?? '正在检索知识库…');
  }, []);

  const finishStreaming = useCallback(() => {
    flushDelta();
    streamingRef.current = false;
    setStreaming(false);
    abortRef.current = null;
    loadSessions(); // 刷新会话列表（含新建会话）
  }, [flushDelta, loadSessions]);

  const handleDone = useCallback(
    (info: { session_id: string; message_count: number; gen_params?: GenParams }) => {
      if (info.session_id) setActiveSessionId(info.session_id);
      // 生成参数随 done 下发（prompt 事件下发时它还没算好）：写进最后一条
      // assistant 消息，这样刚问完立刻点「详情」也能看到本次实际生效的参数，
      // 不必等刷新从会话文件重新加载
      if (info.gen_params && Object.keys(info.gen_params).length > 0) {
        const gp = info.gen_params;
        setMessages(prev => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant') {
            next[next.length - 1] = { ...last, gen_params: gp };
          }
          return next;
        });
      }
      finishStreaming();
    },
    [finishStreaming],
  );

  const handleStreamError = useCallback(
    (errMsg: string) => {
      flushDelta();
      if (errMsg !== '已停止') {
        // 未产生任何内容时把错误写进气泡，否则仅提示
        setMessages(prev => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant' && !last.content) {
            next[next.length - 1] = { ...last, content: `⚠️ ${errMsg}` };
          }
          return next;
        });
        message.warning(errMsg);
      } else {
        // 用户主动停止：给最后一条 assistant 消息打停止标记（仅前端会话状态，
        // 不写入落盘内容），MessageList 渲染尾部灰色「已停止生成」小字
        setMessages(prev => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === 'assistant' && !last.stopped) {
            next[next.length - 1] = { ...last, stopped: true };
          }
          return next;
        });
      }
      finishStreaming();
    },
    [flushDelta, finishStreaming, message],
  );

  const handleStop = useCallback(() => {
    abortRef.current?.(); // 触发 AbortError → onError('已停止') → finishStreaming
  }, []);

  const handleSend = useCallback(
    (text: string) => {
      if (!kbIds.length) {
        message.warning('请先创建并选择一个知识库');
        return;
      }
      if (streamingRef.current) return;

      setMessages(prev => [
        ...prev,
        // created_at 供消息列表渲染 HH:mm 时间戳（当前会话内即时可用）
        { role: 'user', content: text, created_at: dayjs().format('YYYY-MM-DD HH:mm:ss') },
        { role: 'assistant', content: '', created_at: dayjs().format('YYYY-MM-DD HH:mm:ss') },
      ]);

      streamingRef.current = true;
      setStreaming(true);
      setStatusHint(''); // 新提问：清除上一条进度提示（收到 agentic_status 再更新）

      abortRef.current = streamChat(
        {
          kb_ids: kbIds,
          query: text,
          // 草稿会话不传 session_id：后端据此新建会话，done 事件回来的真实 id 再写回
          session_id: activeSessionId === DRAFT_SESSION_ID ? undefined : activeSessionId,
          top_k: topK,
        },
        {
          onMeta: handleMeta,
          onAgentic: handleAgentic,
          onAgenticStatus: handleAgenticStatus,
          onPrompt: handlePrompt,
          onDelta: handleDelta,
          onDone: handleDone,
          onError: handleStreamError,
        },
      );
    },
    [kbIdsKey, activeSessionId, topK, handleMeta, handlePrompt, handleDelta, handleDone, handleStreamError, message],
  );

  // 组件卸载时中止未完成的流
  useEffect(() => {
    return () => {
      abortRef.current?.();
    };
  }, []);

  return (
    // 页面高度固定为视口减 Content 上下 padding（24×2），overflow hidden 兜底防溢出：
    // 左栏（会话列表）与右栏（工具条/输入区）固定，仅消息列表内部独立滚动
    <div style={{ display: 'flex', height: 'calc(100vh - 48px)', gap: 16, overflow: 'hidden' }}>
      {/* 左栏：会话列表 */}
      <SessionList
        sessions={sessions}
        activeSessionId={activeSessionId}
        streaming={streaming}
        textTertiary={token.colorTextTertiary}
        onNew={handleNewSession}
        onOpen={handleOpenSession}
        onRename={openRenameModal}
        onExport={handleExportSession}
        onDelete={handleDeleteSession}
      />

      {/* 右栏：对话区（minHeight: 0 允许内部消息列表收缩滚动，防止撑高导致整页滚动） */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            marginBottom: 12,
            height: 40, // 与左栏"会话列表" Card 头部视觉对齐
            padding: '0 14px',
            background: token.colorBgContainer,
            border: `1px solid ${token.colorBorderSecondary}`,
            borderRadius: 10,
            boxShadow: '0 1px 3px rgba(16,24,40,0.04)',
          }}
        >
          <Typography.Text strong>知识库</Typography.Text>
          <Select
            mode="multiple"
            value={kbIds}
            onChange={(v: string[]) => {
              if (v.length > 5) {
                message.warning('最多同时选 5 个知识库');
                return;
              }
              setKbIds(v);
            }}
            maxTagCount="responsive"
            style={{ width: 240 }}
            placeholder="选择知识库（可多选）"
            options={kbs.map(k => ({
              value: k.id,
              // 库名旁悬停显示文档数（下拉内嵌 AntD Tooltip 会遮挡其他选项，用原生 title 最稳）
              label: <span title={`文档数：${k.doc_count} 篇`}>{k.name}</span>,
            }))}
            notFoundContent={<Empty description="暂无知识库，请先到知识库管理创建" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          />
          {/* 管理文档：直达当前知识库的文档管理页（仅可管理角色可见，普通用户不显示） */}
          {isAdmin && (
            <Button
              icon={<FolderOpenOutlined />}
              disabled={!kbIds.length}
              onClick={() => navigate(`/documents?kb_id=${kbIds[0]}`)}
            >
              管理文档
            </Button>
          )}
          {/* 检索参数仅管理员/超管可见：普通用户不显示参数细节，由系统配置统一管理 */}
          {isAdmin && (
            <>
              <Typography.Text strong>top_k</Typography.Text>
              <Select
                value={topK}
                onChange={setTopK}
                style={{ width: 90 }}
                options={[5, 6, 7, 8, 9, 10].map(v => ({
                  value: v, label: String(v),
                }))}
              />
            </>
          )}
          {/* 聊天设置（保存到活跃 profile 的 retrieval/chat 段）；页面 top_k Select 仍优先覆盖聊天设置默认值；仅管理员/超管可见 */}
          {isAdmin && (
            <Tooltip title="聊天设置">
              <Button
                type="text"
                icon={<SettingOutlined />}
                onClick={() => setChatSettingsOpen(true)}
                aria-label="聊天设置"
              />
            </Tooltip>
          )}
        </div>
        <Card size="small" style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }} styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' } }}>
          {kbIds.length ? (
            // key 随会话切换重置消息列表滚动状态（新会话默认贴底查看最新消息）
            <MessageList
              key={activeSessionId}
              messages={messages}
              waiting={streaming}
              waitingHint={statusHint}
              // 草稿态不传 id：反馈条要的是真实 session_id，哨兵值后端不认
              sessionId={activeSessionId === DRAFT_SESSION_ID ? undefined : activeSessionId}
              kbId={kbIds[0]}
              citationSnippetChars={citationSnippetChars}
              onCitationClick={handleCitationClick}
            />
          ) : (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <Empty description="请先创建知识库并上传文档，再进行问答" />
            </div>
          )}
          <div style={{ paddingTop: 12, borderTop: `1px solid ${token.colorBorderSecondary}`, marginTop: 12 }}>
            <ChatInput onSend={handleSend} onStop={handleStop} streaming={streaming} disabled={!kbIds.length} />
          </div>
        </Card>
      </div>

      {/* 聊天设置弹窗（保存活跃 profile 的 retrieval/chat 段） */}
      <ChatSettingsModal open={chatSettingsOpen} onCancel={() => setChatSettingsOpen(false)} />

      {/* 会话重命名弹窗：Enter 或点击"保存"提交 */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 420, h: 360 }}
        rememberKey="chat-settings-save"
        title="重命名会话"
        open={!!renameTarget}
        onCancel={() => setRenameTarget(null)}
        onOk={handleRenameSubmit}
        okText="保存"
        cancelText="取消"
        confirmLoading={renaming}
        width={420}
        destroyOnClose
      >
        <Input
          value={renameTitle}
          onChange={e => setRenameTitle(e.target.value)}
          onPressEnter={handleRenameSubmit}
          maxLength={50}
          placeholder="请输入新标题（1-50 字）"
          autoFocus
        />
      </AppModal>

      {/* 引用溯源弹窗：定位高亮到被点击引用的 chunk 原文 */}
      <CitationTraceModal
        open={!!traceSource}
        kbId={kbIds[0]}
        source={traceSource}
        onClose={() => setTraceSource(null)}
        answerText={traceAnswerText}
      />
    </div>
  );
};

export default ChatPage;
