/**
 * 会话现场回溯抽屉（超管）：从「用户反馈」页点「关联会话」打开，恢复用户点赞/
 * 点踩那一刻的现场——用户问了什么、AI 答了什么、引用了哪些片段。
 *
 * - 请求带 include_deleted=true：会话已被用户删除时后端回退读归档目录
 *   （点踩者常顺手把会话删掉，而那条恰恰最需要回看）
 * - 数据就绪后把 msg_idx 指向的回答滚到视野中央并高亮（反馈针对的就是它）
 * - 会话彻底不可回溯（归档也过保留期被清理）→ 显式提示，不静默空白
 */
import React, { useEffect, useRef, useState } from 'react';
import {
  App as AntApp, Button, Drawer, Empty, Input, Select, Skeleton, Space, Tag, Tooltip, Typography,
} from 'antd';
import {
  addRagasDatasetSamples,
  getSession, listRagasDatasets,
  type ChatMessage, type ChatSessionDetail, type RagasDataset,
} from '../../shared/api/client';
import AppEmpty from '../../shared/components/common/AppEmpty';
import AppModal from '../../shared/components/common/AppModal';
import RequestDetailModal from '../../shared/components/common/RequestDetailModal';
import { useAuth } from '../../shared/auth/AuthContext';
import { isKgSource, scoreBadge } from '../../shared/utils/sourceScore';

const { Text } = Typography;

export interface SessionReplayDrawerProps {
  open: boolean;
  /** 会话 ID（null = 未选定） */
  sessionId: string | null;
  /** 被反馈消息在 messages 数组中的下标（-1 = 无定位信息，不高亮） */
  msgIdx: number;
  /** 该条反馈（谁点的/赞或踩/原因），透传给详情弹窗——超管不用回头对照列表 */
  feedback?: { rating: string; reason?: string } | null;
  onClose: () => void;
}

/** 引用片段悬浮卡：限宽换行写在内容元素上，不依赖 Tooltip 的样式 API（跨版本稳定） */
const SourceTip: React.FC<{ text: string }> = ({ text }) => (
  <div style={{ maxWidth: 520, maxHeight: 320, overflow: 'auto', whiteSpace: 'pre-wrap' }}>
    {text || '（无片段文本）'}
  </div>
);

const SessionReplayDrawer: React.FC<SessionReplayDrawerProps> = ({
  open,
  sessionId,
  msgIdx,
  feedback,
  onClose,
}) => {
  const { message } = AntApp.useApp();
  const { user } = useAuth();
  // 「加入评估集」仅对管理员开放（与后端 /ragas/datasets 的权限口径一致）
  const isAdmin = user?.role === 'super_admin' || user?.role === 'dept_admin';
  const [session, setSession] = useState<ChatSessionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  // 「加入评估集」弹窗：把被点踩的问答沉淀成回归用例，让踩过的坑不再复发
  const [dsOpen, setDsOpen] = useState(false);
  const [dsList, setDsList] = useState<RagasDataset[]>([]);
  const [dsId, setDsId] = useState<string | undefined>(undefined);
  const [dsQuestion, setDsQuestion] = useState('');
  const [dsTruth, setDsTruth] = useState('');
  const [dsSubmitting, setDsSubmitting] = useState(false);
  /** notfound = 会话不存在，或已删除但没有反馈关联（无反馈的会话不保留归档） */
  const [error, setError] = useState<'notfound' | 'failed' | null>(null);
  /** 请求详情弹窗目标（消息下标 + 该回答对应的用户提问） */
  const [detail, setDetail] = useState<{ idx: number; question: string } | null>(null);
  const highlightRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open || !sessionId) {
      return;
    }
    // cancelled 防切换会话时旧请求后到覆盖新数据
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSession(null);
    setDetail(null);
    getSession(sessionId, true)
      .then(res => {
        if (!cancelled) setSession(res.data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const status = (err as { response?: { status?: number } })?.response?.status;
        setError(status === 404 ? 'notfound' : 'failed');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, sessionId]);

  // 数据就绪后把被反馈的那条滚到视野中央（抽屉内容区是独立滚动容器）
  useEffect(() => {
    if (session && highlightRef.current) {
      highlightRef.current.scrollIntoView({ block: 'center' });
    }
  }, [session]);

  /** 打开请求详情：检索问题取该回答往前最近的用户消息（与聊天页同一口径） */
  const openDetail = (messages: ChatMessage[], idx: number) => {
    let question = '';
    for (let i = idx - 1; i >= 0; i--) {
      if (messages[i].role === 'user') {
        question = messages[i].content;
        break;
      }
    }
    setDetail({ idx, question });
  };

  /** 打开「加入评估集」弹窗：提问取该回答前最近的用户消息，参考答案预填反馈里的原因 */
  const openAddToDataset = (idx: number) => {
    const msgs = session?.messages || [];
    let q = '';
    for (let i = idx - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') {
        q = (msgs[i].content || '').trim();
        break;
      }
    }
    if (!q) {
      message.warning('这条回答前面没有对应的用户提问，无法加入评估集');
      return;
    }
    setDsQuestion(q);
    // 点踩时填的「哪里不对」预填为参考答案的起点——它未必就是正确答案，
    // 所以只作预填、留给使用者改成真正的期望答案
    setDsTruth(feedback?.reason || '');
    setDsId(undefined);
    setDsOpen(true);
    listRagasDatasets()
      .then(r => setDsList(r.data.datasets || []))
      .catch(() => setDsList([]));
  };

  const handleAddToDataset = async () => {
    if (!dsId) {
      message.error('请选择要加入的评估集');
      return;
    }
    setDsSubmitting(true);
    try {
      const res = await addRagasDatasetSamples(dsId, [
        { question: dsQuestion, ground_truth: dsTruth.trim() },
      ]);
      const { added, skipped } = res.data;
      if (added) {
        message.success('已加入评估集——以后用该评估集重跑评估会覆盖这条');
      } else if (skipped) {
        message.warning('该问题已在评估集里（按问题去重）');
      }
      setDsOpen(false);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '加入评估集失败');
    } finally {
      setDsSubmitting(false);
    }
  };

  const renderBody = () => {
    if (loading) {
      return <Skeleton active paragraph={{ rows: 8 }} />;
    }
    if (error === 'notfound') {
      return (
        <AppEmpty
          title="会话已不可回溯"
          description="该会话已被删除且没有反馈关联（无反馈的会话不保留归档）；或会话 ID 不存在"
        />
      );
    }
    if (error === 'failed' || !session) {
      return <AppEmpty title="加载失败" description="请稍后重试" />;
    }
    if (!session.messages || session.messages.length === 0) {
      return <AppEmpty title="该会话没有消息" />;
    }
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {session.messages.map((m, idx) => {
          const isUser = m.role === 'user';
          // msg_idx 即 messages 数组下标（前端提交反馈时传的就是它）
          const hit = idx === msgIdx;
          return (
            <div
              key={idx}
              ref={hit ? highlightRef : undefined}
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: isUser ? 'flex-end' : 'flex-start',
                padding: '6px 8px',
                borderRadius: 10,
                background: hit ? 'rgba(250, 173, 20, 0.10)' : undefined,
                border: hit ? '1px dashed #faad14' : '1px solid transparent',
              }}
            >
              <Space size={6} style={{ marginBottom: 4 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {isUser ? '用户提问' : 'AI 回答'}
                </Text>
                {hit && (
                  <Tag color="orange" style={{ marginRight: 0 }}>
                    被反馈的那条
                  </Tag>
                )}
              </Space>
              <div
                className={isUser ? 'bubble-user' : 'bubble-assistant'}
                style={{
                  maxWidth: '92%',
                  padding: '10px 14px',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {m.content || '（空消息）'}
              </div>
              {/* 请求详情：与聊天页同一组件、同一显示条件（消息带 prompt+耗时
                  或 agentic 轨迹才有入口）。归档裁剪保留整条消息，详情不受影响 */}
              {!isUser && (
                <Space size={12} style={{ marginTop: 4 }}>
                  {((!!m.prompt
                    && (m.retrieval_ms !== undefined || m.total_ms !== undefined))
                    || !!m.agentic)
                    && (
                      <Button
                        type="link"
                        size="small"
                        style={{ padding: 0 }}
                        onClick={() => openDetail(session.messages, idx)}
                      >
                        详情
                      </Button>
                    )}
                  {/* 把这条问答沉淀成评估集样本：点踩现场 → 永久回归用例 */}
                  {isAdmin && (
                    <Button
                      type="link"
                      size="small"
                      style={{ padding: 0 }}
                      onClick={() => openAddToDataset(idx)}
                    >
                      加入评估集
                    </Button>
                  )}
                </Space>
              )}
              {m.sources && m.sources.length > 0 && (
                <div style={{ marginTop: 6, maxWidth: '92%' }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    引用来源（{m.sources.length}）
                  </Text>
                  <div
                    style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 2 }}
                  >
                    {m.sources.map((s, si) => (
                      <Tooltip
                        key={si}
                        title={<SourceTip text={s.text} />}
                        placement="topLeft"
                      >
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {isKgSource(s)
                            ? `[${si + 1}] 知识图谱`
                            : `[${si + 1}] ${s.document_name || s.document_id} · 块 ${s.chunk_index} · ${scoreBadge(s)}`}
                        </Text>
                      </Tooltip>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <>
    <Drawer
      title="会话现场回溯"
      width={760}
      open={open}
      onClose={() => { setDetail(null); onClose(); }}
      styles={{ body: { padding: 16 } }}
    >
      {session && (
        <div style={{ marginBottom: 12 }}>
          <Space direction="vertical" size={2}>
            <Space size={8} wrap>
              <Text strong>{session.title || '（无标题会话）'}</Text>
              {session.archived && (
                <Tag color="red" style={{ marginRight: 0 }}>
                  该会话已被用户删除（内容来自归档）
                </Tag>
              )}
              {session.trimmed && (
                <Tooltip title="归档时只保留了被反馈的轮次及其前 2 轮上下文，不是完整会话">
                  <Tag color="orange" style={{ marginRight: 0 }}>
                    归档已裁剪
                  </Tag>
                </Tooltip>
              )}
            </Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              会话 {session.id} · 共 {session.messages.length} 条消息 · 创建于{' '}
              {session.created_at || '—'}
            </Text>
          </Space>
        </div>
      )}
      {renderBody()}
    </Drawer>
    {/* 详情弹窗作为 Drawer 的兄弟节点：嵌在 Drawer 内会被其容器层级约束 */}
    <RequestDetailModal
      message={detail && session ? session.messages[detail.idx] ?? null : null}
      question={detail?.question ?? ''}
      sources={detail && session ? session.messages[detail.idx]?.sources : undefined}
      feedback={feedback}
      onClose={() => setDetail(null)}
    />
    {/* 加入评估集弹窗（同样放在 Drawer 外，避免层级约束） */}
    <AppModal
      dimension="auto"
      defaultSize={{ w: 520, h: 440 }}
      rememberKey="replay-add-dataset"
      title="加入评估集"
      open={dsOpen}
      onCancel={() => setDsOpen(false)}
      onOk={handleAddToDataset}
      okText="加入"
      cancelText="取消"
      confirmLoading={dsSubmitting}
      width={520}
    >
      <Text type="secondary" style={{ fontSize: 12 }}>
        加进固定题集后，以后每次用该评估集重跑都会覆盖这条——让踩过的坑变成永久回归用例。
      </Text>
      <div style={{ marginTop: 12 }}>
        <Text strong>问题</Text>
        <div style={{
          marginTop: 4, padding: '6px 8px', borderRadius: 6, fontSize: 13,
          background: 'rgba(0,0,0,0.03)', wordBreak: 'break-word',
        }}
        >
          {dsQuestion}
        </div>
      </div>
      <div style={{ marginTop: 12 }}>
        <Text strong>参考答案</Text>
        <Input.TextArea
          style={{ marginTop: 4 }}
          rows={3}
          maxLength={2000}
          placeholder="期望的正确回答（留空则该样本评不了需要参考答案的指标）"
          value={dsTruth}
          onChange={e => setDsTruth(e.target.value)}
        />
      </div>
      <div style={{ marginTop: 12 }}>
        <Text strong>加入哪个评估集</Text>
        <Select
          style={{ width: '100%', marginTop: 4 }}
          placeholder="选择评估集"
          value={dsId}
          onChange={setDsId}
          options={dsList.map(d => ({
            value: d.id,
            label: `${d.name}（${d.samples?.length ?? 0} 条 · ${d.kb_name}）`,
          }))}
          notFoundContent={
            <Empty description="还没有评估集，请先到 RAGAS 评测页创建"
                   image={Empty.PRESENTED_IMAGE_SIMPLE} />
          }
        />
      </div>
    </AppModal>
    </>
  );
};

export default SessionReplayDrawer;
