/**
 * 会话列表（左栏）：SessionList + SessionItem
 *
 * 从 `Chat.tsx` 拆出——同目录 `components/` 下已有 ChatInput / MessageList /
 * SourcePanel 等 5 个组件，只有这两个还留在页面文件里，让 ChatPage 的流式
 * 状态机埋在第 500 行之后。
 *
 * **两个组件都必须 memo**（原因见各自注释）：messages 是页面顶层 state，
 * 流式时每 50ms 变一次；不 memo 的话会话条数一多，每帧要重建上千个 DOM 节点，
 * 主线程被堵 300+ms，50ms 的节流反被拖成 2.4Hz。
 *
 * `DRAFT_SESSION(_ID)` 一并搬来并导出：ChatPage 新建会话时仍要用。
 */
import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { Button, Card, Checkbox, Empty, List, Popconfirm } from 'antd';
import {
  CloseOutlined, DeleteOutlined, DownloadOutlined, EditOutlined,
  MessageOutlined, PlusOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';

import { ChatSession } from '../../../shared/api/client';

// 「新建」后的草稿会话 id：仅前端占位、不落库，发送第一条消息时后端才真正创建会话
// （避免点几次「新建」就往会话列表里堆一串空会话）
export const DRAFT_SESSION_ID = '__draft__';
// 草稿会话在列表顶部的占位项（条数/时间不展示，由渲染分支单独处理）
export const DRAFT_SESSION: ChatSession = {
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
  /** 批量删除选中的会话（二次确认与调接口都在 ChatPage，这里只管选中态与触发） */
  onBatchDelete: (ids: string[]) => void;
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
export const sameSession = (a: ChatSession, b: ChatSession): boolean =>
  a.id === b.id
  && a.title === b.title
  && a.message_count === b.message_count
  && a.updated_at === b.updated_at
  && a.created_at === b.created_at
  && (a.kb_ids ?? []).join(',') === (b.kb_ids ?? []).join(',');

/**
 * 会话条目的相对时间（显示在「n 条消息」前面）。
 *
 * 时间源用 updated_at：后端每次问答都会刷新它，会话列表也按它倒序排列，
 * 所以这里显示的正是"这条会话最近一次说话是多久以前"。
 *
 * 分档：5 分钟内=刚刚 / 5~59 分钟=n分钟前 / 1~23 小时=n小时前 /
 *       1~7 天=n天前 / 超过 7 天=月日（如 9月23日）
 *
 * 文本刻意不带空格（"30分钟前"而非"30 分钟前"）：这一行还要并排放三个操作按钮，
 * 230px 的卡片宽度塞不下，去掉空格并压低字号才放得下（实测裁剪情况见 Git 记录）
 */
const formatSessionTime = (ts?: string): string => {
  if (!ts) return '';
  const t = dayjs(ts);
  if (!t.isValid()) return '';
  const now = dayjs();
  const mins = now.diff(t, 'minute');
  if (mins < 5) return '刚刚';
  if (mins < 60) return `${mins}分钟前`;
  const hours = now.diff(t, 'hour');
  if (hours < 24) return `${hours}小时前`;
  const days = now.diff(t, 'day');
  if (days <= 7) return `${days}天前`;
  return t.format('M月D日');
};

interface SessionItemProps {
  item: ChatSession;
  /** 是否当前打开的会话（高亮选中态） */
  isActive: boolean;
  /** token.colorTextTertiary（条数/「等待提问…」灰字） */
  textTertiary: string;
  /** 多选管理模式：图标位换成复选框、点整条 = 切换勾选（不开会话）、隐藏操作按钮 */
  selectable?: boolean;
  /** 多选模式下本条是否已勾选 */
  selected?: boolean;
  onToggleSelect?: (id: string) => void;
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
  selectable,
  selected,
  onToggleSelect,
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
        // 多选管理模式：整条点击 = 切换勾选（不打开会话）
        if (selectable) {
          onToggleSelect?.(item.id);
          return;
        }
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
      {/* 两行布局：第一行 = 图标 + 会话名（两列）；第二行 = 时间/条数/操作按钮。
          第二行**不**缩进到图标列之后——正是为了用上左侧那 34px：这一行要并排放
          时间文本和三个按钮，缩进的话时间会被挤成省略号（实测） */}
      <div style={{ display: 'flex', flexDirection: 'column', width: '100%' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, width: '100%' }}>
        {/* 多选模式：同一位置换成复选框（不额外占宽、不会把会话名挤短）。
            复选框自己负责切换并阻止冒泡，否则会连同整条的 onClick 切换两次 */}
        {selectable ? (
          <Checkbox
            checked={!!selected}
            onClick={e => e.stopPropagation()}
            onChange={() => onToggleSelect?.(item.id)}
            style={{ marginTop: 3, flexShrink: 0 }}
          />
        ) : (
          <MessageOutlined style={{ color: 'var(--brand-primary, #2563eb)', marginTop: 3 }} />
        )}
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
        </div>
      </div>
      {/* 第二行：整行宽度（不缩进到图标列之后），时间 + 条数 + 操作按钮 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          marginTop: 2,
          width: '100%',
        }}
      >
            {/* 相对时间 + 条数：时间在前（形如「刚刚 · 2 条消息」）。
                时间戳取 updated_at——问答后后端会刷新它，列表也随之置顶。
                本行占满整行宽度（不缩进），所以 12px 字号也放得下；文本仍不带空格
                （"30分钟前"而非"30 分钟前"）留出余量。
                **不要**给它 flexShrink/ellipsis——本行右侧按钮用了 margin-left:auto，
                auto margin 会先吃掉全部剩余空间，再让 flexShrink 判定"空间不足"，
                文本会被压成「31分钟前 · 2 条…」（已实测踩过）。保持 flexShrink:0 */}
            <span style={{ fontSize: 12, color: textTertiary, flexShrink: 0, whiteSpace: 'nowrap' }}>
              {formatSessionTime(item.updated_at)} · {item.message_count} 条消息
            </span>
            {/* 操作按钮组：重命名 → 导出 → 删除（顺序按用户要求）。
                左边距用 auto 而非固定间距——加上时间标签后这一行更挤，靠右对齐让
                「时间+条数」与按钮组自动分配空间。
                多选管理模式整组隐藏——那种模式下由顶部「删除选中」统一删，留着单个删除按钮
                既挤占宽度又容易误触 */}
            {!selectable && (
              <span
                className="session-item-actions"
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 0,
                  marginLeft: 'auto',
                  flexShrink: 0,
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
            )}
      </div>
      </div>
    </List.Item>
  );
}, (prev, next) =>
  // 只比"影响显示"的 prop，**回调 prop 一律忽略**：它们只在点击那一刻用，
  // 而 AntApp.useApp() 的 message 一旦换新对象，依赖它的 useCallback 会全部跟着换，
  // 默认浅比较就判定 "props 变了" → 上百个条目整列重建（实测一次删除渲染数百次、
  // 主线程堵 800ms）。点击时闭包里的旧回调功能等价——需要状态的地方（当前会话 id /
  // 已选知识库）已经改用 ref 读取，不存在过期问题
  prev.item === next.item
  && prev.isActive === next.isActive
  && prev.textTertiary === next.textTertiary
  && prev.selectable === next.selectable
  && prev.selected === next.selected);

/** 会话项行高（px）：实测普通项 64、草稿项 60（草稿项第二行是纯文字，比带按钮的普通项矮 4px） */
const SESSION_ROW_H = 64;
const DRAFT_ROW_H = 60;
/** 可视区上下各多渲染的行数：快速滚动时新行已在 DOM 里，避免出现瞬时空白 */
const SESSION_OVERSCAN = 4;

export const SessionList = memo(function SessionList({
  sessions,
  activeSessionId,
  streaming,
  textTertiary,
  onNew,
  onOpen,
  onRename,
  onExport,
  onDelete,
  onBatchDelete,
}: SessionListProps) {
  // 虚拟滚动：只把「可视区 + 上下缓冲」的条目交给 antd List 渲染。
  // 该账号 139 条会话时全量渲染光列表就阻塞主线程 881ms、首屏近 2s 才出得来；
  // 而滚动容器高 813px、每条 64px —— 真正看得见的只有 12 条左右，其余白渲染。
  // 做法：外层 div 撑出总高度（滚动条长度/位置与全量渲染完全一致），
  // 内层按起始项偏移整体 translateY，条目仍由 antd List + SessionItem 渲染（样式不变）。
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportH, setViewportH] = useState(0);

  // 多选管理模式：选中集合按会话 id 存，与虚拟滚动正交（只渲染可视项不影响选中状态）
  const [managing, setManaging] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // 可勾选的会话 id：草稿会话尚未落库，不参与多选（选它没有意义）
  const selectableIds = sessions.map(s => s.id);
  const allSelected = selectableIds.length > 0 && selected.size === selectableIds.length;
  const someSelected = selected.size > 0 && !allSelected;

  const toggleOne = useCallback((id: string) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(selectableIds));
  const exitManaging = useCallback(() => {
    setManaging(false);
    setSelected(new Set());
  }, []);

  // 列表刷新后剔除已不存在的选中项：刚被删掉的会话若还留在集合里，
  // "已选 N 条"就会与实际能删的数量对不上（还会把无效 id 发给后端）
  useEffect(() => {
    setSelected(prev => {
      if (prev.size === 0) return prev;
      const alive = new Set(sessions.map(s => s.id));
      const next = new Set([...prev].filter(id => alive.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [sessions]);

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
        // 管理模式隐藏头部按钮：此时列表上方有「全选 / 已选 N 条 / 删除 / 退出」整行工具条，
        // 230px 宽的卡片头塞不下两组按钮
        managing ? null : (
          <>
            <Button size="small" onClick={() => setManaging(true)} disabled={streaming}>
              管理
            </Button>
            <Button size="small" type="primary" icon={<PlusOutlined />} onClick={onNew} disabled={streaming}>
              新建
            </Button>
          </>
        )
      }
      style={{ width: 230, display: 'flex', flexDirection: 'column' }}
      styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' } }}
    >
      {/* 多选工具条：压在列表上方（不放 Card 头部——230px 宽度塞不下两组按钮） */}
      {managing && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, paddingBottom: 6, flexShrink: 0 }}>
          <Checkbox
            checked={allSelected}
            indeterminate={someSelected}
            onChange={toggleAll}
            disabled={!selectableIds.length}
          >
            <span style={{ fontSize: 12 }}>全选</span>
          </Checkbox>
          <span style={{ marginLeft: 'auto', fontSize: 12, color: textTertiary }}>
            已选 {selected.size} 条
          </span>
          <Button
            size="small"
            type="text"
            danger
            icon={<DeleteOutlined />}
            disabled={!selected.size}
            title={selected.size ? `删除选中的 ${selected.size} 条会话` : '请先勾选会话'}
            onClick={() => onBatchDelete([...selected])}
          />
          <Button
            size="small"
            type="text"
            icon={<CloseOutlined />}
            title="退出管理"
            onClick={exitManaging}
          />
        </div>
      )}
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
                    selectable={managing}
                    selected={selected.has(item.id)}
                    onToggleSelect={toggleOne}
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

