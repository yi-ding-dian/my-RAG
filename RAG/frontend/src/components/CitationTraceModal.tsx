import React, { useEffect, useState } from 'react';
import { Alert, Modal, Spin } from 'antd';
import type { DocumentDetail, Source } from '../api/client';
import { getDocument } from '../api/client';
import ChunkCompareView from './ChunkCompareView';

interface CitationTraceModalProps {
  open: boolean;
  /** 知识库 ID（source.kb_id 缺失的历史快照兜底用当前活跃 kb） */
  kbId?: string;
  /** 被点击的引用来源（null 时不加载） */
  source: Source | null;
  onClose: () => void;
}

/**
 * 引用溯源弹窗：点击回答中 [n] 引用标或引用面板"查看原文"时打开，
 * 加载该文档详情（full_text + chunks），复用 ChunkCompareView 定位高亮到对应 chunk。
 */
const CitationTraceModal: React.FC<CitationTraceModalProps> = ({ open, kbId, source, onClose }) => {
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !source) return;
    let cancelled = false;
    setDetail(null);
    setError(null);
    setLoading(true);
    const kb = source.kb_id || kbId;
    if (!kb) {
      setLoading(false);
      setError('无法确定来源知识库，请重新发起提问后重试');
      return;
    }
    getDocument(kb, source.document_id)
      .then(res => {
        if (!cancelled) setDetail(res.data);
      })
      .catch(e => {
        if (cancelled) return;
        // 404=文档已删除/不存在；403/其他=无权限或服务异常
        const status = (e as any)?.response?.status;
        setError(
          status === 404
            ? '文档不存在或已删除，无法定位原文'
            : '加载文档原文失败（可能无访问权限），请稍后重试',
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, source, kbId]);

  return (
    <Modal
      className="chunk-detail-modal"
      title={`引用溯源 - ${source?.document_name || source?.document_id || ''}`}
      open={open}
      onCancel={onClose}
      footer={null}
      width={1150}
      // 固定弹窗高度：头部固定不动，滚动只发生在内容区内部（滚动条在弹窗内）。
      // 滚动结构修复同 Documents 切块详情弹窗（见 index.css .chunk-detail-modal）：
      // rc-dialog 的 sentinel 中间层让 content 的 height:100% 失效，需由 className 打通 flex 链。
      // 高度用 min(80vh, 视口高-120px) 兜底，小视口下全屏 wrap 也不会滚动
      style={{ top: '8vh', height: 'min(80vh, calc(100vh - 120px))' }}
      styles={{
        content: { display: 'flex', flexDirection: 'column', height: '100%' },
        header: { flexShrink: 0 },
        body: {
          padding: '16px 20px',
          flex: 1,
          minHeight: 0,
          overflow: 'auto',
          display: 'flex',
          flexDirection: 'column',
        },
      }}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 60 }}>
          <Spin />
        </div>
      ) : error ? (
        <Alert
          type="warning"
          showIcon
          message="无法加载文档原文"
          description={error}
          style={{ marginTop: 16 }}
        />
      ) : detail ? (
        <ChunkCompareView
          // 弹窗固定高度，内容区撑满剩余高度（头部/工具条固定，仅左右内容区内部滚动）
          fillHeight
          // key 绑定文档 id：切换引用来源时重挂载，重置选中态并重新定位
          key={detail.id}
          chunks={
            detail.chunks?.map(c => ({
              index: c.index,
              text: c.text,
              char_start: c.char_start,
              char_end: c.char_end,
              context: c.context,
            })) ?? []
          }
          fullText={detail.full_text}
          initialIndex={source?.chunk_index}
        />
      ) : null}
    </Modal>
  );
};

export default CitationTraceModal;
