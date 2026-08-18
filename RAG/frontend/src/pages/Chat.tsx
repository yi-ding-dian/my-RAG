import React, { useCallback, useEffect, useRef, useState } from 'react';
import { App as AntApp, Button, Card, Empty, Input, List, Modal, Popconfirm, Select, Tooltip, Typography, theme } from 'antd';
import { DeleteOutlined, DownloadOutlined, EditOutlined, FolderOpenOutlined, MessageOutlined, PlusOutlined, SettingOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import {
  asApiError,
  ChatMessage,
  ChatSession,
  KnowledgeBase,
  Source,
  deleteSession,
  exportSession,
  getSession,
  listKbs,
  listSessions,
  renameSession,
  streamChat,
} from '../api/client';
import MessageList from '../components/MessageList';
import { cleanAnswerText } from '../utils/cleanMarkdown';
import ChatInput from '../components/ChatInput';
import ChatSettingsModal from '../components/ChatSettingsModal';
import CitationTraceModal from '../components/CitationTraceModal';
import { useAuth } from '../auth/AuthContext';

const KB_ID_KEY = 'myrag.kb_id';

const ChatPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const { user } = useAuth();
  const navigate = useNavigate();
  // 检索参数（top_k）与聊天设置仅管理员/超管可见，普通用户由系统配置统一管理
  const isAdmin = !!user && user.role !== 'user';

  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [kbId, setKbId] = useState<string | undefined>(() => {
    const saved = localStorage.getItem(KB_ID_KEY);
    return saved || undefined;
  });

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | undefined>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);

  const [topK, setTopK] = useState(5);
  const [streaming, setStreaming] = useState(false);
  const [chatSettingsOpen, setChatSettingsOpen] = useState(false);
  // 引用溯源：点击 [n] 引用标或引用面板"查看原文"时打开弹窗
  const [traceSource, setTraceSource] = useState<Source | null>(null);
  // 引用溯源弹窗的回答文本（该引用所属回答消息的 content；原文回答-对齐高亮匹配基准）
  const [traceAnswerText, setTraceAnswerText] = useState('');
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
        setKbId(undefined);
      } else if (!kbId || !res.data.some(k => k.id === kbId)) {
        setKbId(res.data[0].id);
      }
    } catch {
      message.error('加载知识库列表失败');
    }
  }, [kbId, message]);

  useEffect(() => {
    loadKbs();
  }, [loadKbs]);

  // 记忆知识库选择
  useEffect(() => {
    if (kbId) localStorage.setItem(KB_ID_KEY, kbId);
  }, [kbId]);

  // ---------- 会话列表 ----------
  const loadSessions = useCallback(
    async (id: string) => {
      try {
        const res = await listSessions(id);
        setSessions(res.data);
      } catch {
        message.error('加载会话列表失败');
      }
    },
    [message],
  );

  useEffect(() => {
    if (!kbId) {
      setSessions([]);
      return;
    }
    loadSessions(kbId);
  }, [kbId, loadSessions]);

  const handleNewSession = () => {
    if (streamingRef.current) return;
    setActiveSessionId(undefined);
    setMessages([]);
  };

  const handleOpenSession = async (id: string) => {
    if (streamingRef.current) {
      message.warning('生成中，请先停止');
      return;
    }
    try {
      const res = await getSession(id);
      setMessages(res.data.messages);
      setActiveSessionId(id);
    } catch {
      message.error('加载会话失败');
    }
  };

  const handleDeleteSession = async (id: string) => {
    if (!kbId) return;
    try {
      await deleteSession(id);
      if (id === activeSessionId) {
        setActiveSessionId(undefined);
        setMessages([]);
      }
      await loadSessions(kbId);
      message.success('会话已删除');
    } catch {
      message.error('删除会话失败');
    }
  };

  // 导出会话为 Markdown：fetch 拿 blob 走浏览器下载（与 PDF 预览同模式，不裸传 token）
  const handleExportSession = async (id: string) => {
    try {
      await exportSession(id);
      message.success('会话已导出');
    } catch (e: unknown) {
      message.error(asApiError(e).message || '导出会话失败');
    }
  };

  const openRenameModal = (item: ChatSession) => {
    setRenameTarget(item);
    setRenameTitle(item.title || '');
  };

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
      if (kbId) await loadSessions(kbId);
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
    (text: string) => {
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

  const finishStreaming = useCallback(() => {
    flushDelta();
    streamingRef.current = false;
    setStreaming(false);
    abortRef.current = null;
    if (kbId) loadSessions(kbId); // 刷新会话列表（含新建会话）
  }, [flushDelta, kbId, loadSessions]);

  const handleDone = useCallback(
    (info: { session_id: string; message_count: number }) => {
      if (info.session_id) setActiveSessionId(info.session_id);
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
      if (!kbId) {
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

      abortRef.current = streamChat(
        { kb_id: kbId, query: text, session_id: activeSessionId, top_k: topK },
        { onMeta: handleMeta, onDelta: handleDelta, onDone: handleDone, onError: handleStreamError },
      );
    },
    [kbId, activeSessionId, topK, handleMeta, handleDelta, handleDone, handleStreamError, message],
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
      <Card
        title="会话列表"
        size="small"
        extra={
          <Button size="small" type="primary" icon={<PlusOutlined />} onClick={handleNewSession} disabled={streaming}>
            新建
          </Button>
        }
        style={{ width: 280, display: 'flex', flexDirection: 'column' }}
        styles={{ body: { flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' } }}
      >
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <List
            dataSource={sessions}
            locale={{ emptyText: <Empty description="暂无会话" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            renderItem={item => (
              <List.Item
                onClick={() => handleOpenSession(item.id)}
                className={`session-item${item.id === activeSessionId ? ' session-item--active' : ''}`}
                style={{ cursor: 'pointer' }}
                actions={[
                  <Tooltip key="export" title="导出会话">
                    <Button
                      type="text"
                      size="small"
                      className="session-export-btn"
                      icon={<DownloadOutlined />}
                      onClick={e => {
                        e.stopPropagation();
                        handleExportSession(item.id);
                      }}
                    />
                  </Tooltip>,
                  <Tooltip key="rename" title="重命名">
                    <Button
                      type="text"
                      size="small"
                      className="session-rename-btn"
                      icon={<EditOutlined />}
                      onClick={e => {
                        e.stopPropagation();
                        openRenameModal(item);
                      }}
                    />
                  </Tooltip>,
                  <Tooltip key="del" title="删除会话">
                    <Popconfirm
                      title="删除该会话？"
                      onConfirm={() => handleDeleteSession(item.id)}
                    >
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={e => e.stopPropagation()}
                      />
                    </Popconfirm>
                  </Tooltip>,
                ]}
              >
                <List.Item.Meta
                  avatar={<MessageOutlined style={{ color: 'var(--brand-primary, #2563eb)' }} />}
                  title={
                    <Tooltip title={item.title || '未命名会话'} placement="topLeft">
                      <span
                        style={{
                          fontSize: 13,
                          display: 'block',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {item.title || '未命名会话'}
                      </span>
                    </Tooltip>
                  }
                  description={<span style={{ fontSize: 12 }}>{item.message_count} 条消息</span>}
                />
              </List.Item>
            )}
          />
        </div>
      </Card>

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
            value={kbId}
            onChange={setKbId}
            style={{ width: 240 }}
            placeholder="选择知识库"
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
              disabled={!kbId}
              onClick={() => navigate(`/documents?kb_id=${kbId}`)}
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
                options={[
                  { value: 3, label: '3' },
                  { value: 5, label: '5' },
                  { value: 10, label: '10' },
                ]}
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
          {kbId ? (
            // key 随会话切换重置消息列表滚动状态（新会话默认贴底查看最新消息）
            <MessageList
              key={activeSessionId}
              messages={messages}
              waiting={streaming}
              onCitationClick={(s) => {
                setTraceSource(s);
                // 记录该引用所属的回答文本（按 source.id 匹配消息，供溯源弹窗原文回答-对齐高亮）
                const msg = messages.find(m => (m.sources ?? []).some(sr => sr.id === s.id));
                // 高亮基准用清洗后文本（与气泡渲染一致，所见即所算）
                setTraceAnswerText(cleanAnswerText(msg?.content ?? ''));
              }}
            />
          ) : (
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <Empty description="请先创建知识库并上传文档，再进行问答" />
            </div>
          )}
          <div style={{ paddingTop: 12, borderTop: `1px solid ${token.colorBorderSecondary}`, marginTop: 12 }}>
            <ChatInput onSend={handleSend} onStop={handleStop} streaming={streaming} disabled={!kbId} />
          </div>
        </Card>
      </div>

      {/* 聊天设置弹窗（保存活跃 profile 的 retrieval/chat 段） */}
      <ChatSettingsModal open={chatSettingsOpen} onCancel={() => setChatSettingsOpen(false)} />

      {/* 会话重命名弹窗：Enter 或点击"保存"提交 */}
      <Modal
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
      </Modal>

      {/* 引用溯源弹窗：定位高亮到被点击引用的 chunk 原文 */}
      <CitationTraceModal
        open={!!traceSource}
        kbId={kbId}
        source={traceSource}
        onClose={() => setTraceSource(null)}
        answerText={traceAnswerText}
      />
    </div>
  );
};

export default ChatPage;
