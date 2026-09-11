import React, { useEffect, useState } from 'react';
import AppModal from '../../../shared/components/common/AppModal';
import { Alert,  Spin } from 'antd';
import type { DocumentDetail, Source } from '../../../shared/api/client';
import { asApiError, getDocument } from '../../../shared/api/client';
import ChunkCompareView from '../../knowledge/components/ChunkCompareView';

interface CitationTraceModalProps {
  open: boolean;
  /** 知识库 ID（source.kb_id 缺失的历史快照兜底用当前活跃 kb） */
  kbId?: string;
  /** 被点击的引用来源（null 时不加载） */
  source: Source | null;
  onClose: () => void;
  /**
   * 回答文本（引用溯源原文回答-对齐高亮的匹配基准，可选）：透传给 ChunkCompareView，
   * 对文档 chunks/原文文本叠加与引用面板同源的 .citation-highlight 高亮
   */
  answerText?: string;
}

/**
 * 引用溯源弹窗：点击回答中 [n] 引用标或引用面板"查看原文"时打开。
 * - 普通文档引用：加载文档详情（full_text + chunks），复用 ChunkCompareView
 *   定位高亮到对应 chunk（跳原文入口），并按回答文本叠加回答-对齐高亮
 * - 图谱引用（document_name="知识图谱"）：无对应文档（document_id 为空），
 *   直接展示图谱上下文内容，不提供跳转原文
 */
const CitationTraceModal: React.FC<CitationTraceModalProps> = ({
  open,
  kbId,
  source,
  onClose,
  answerText,
}) => {
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !source) return;
    // 图谱引用：无文档实体，直接展示图谱内容（不请求 getDocument）
    if (source.document_name === '知识图谱') {
      setDetail(null);
      setError(null);
      setLoading(false);
      return;
    }
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
        const status = asApiError(e).response?.status;
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
    <AppModal
      dimension="resizable"
      defaultSize={{ w: 1150, h: 560 }}
      rememberKey="citation-trace"
      className="chunk-detail-modal"
      title={`引用溯源 - ${source?.document_name || source?.document_id || ''}`}
      open={open}
      onCancel={onClose}
      footer={null}
      width={1150}
      // 高度/位置交给 AppModal 统一管理（defaultSize 作为手动尺寸起始值，
      // AppModal 内部按视口钳制 bodyH 且顶部分布自适应居中，避免硬编码
      // top:8vh + height 超出视口导致标题被截/底部被挡）。
      // 滚动结构同 Documents 切块详情弹窗：头部固定，仅内容区内部滚动。
      destroyOnHidden
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
      ) : source?.document_name === '知识图谱' ? (
        /* 图谱引用：直接展示图谱上下文内容（无跳转原文入口） */
        <div
          style={{
            fontSize: 13,
            lineHeight: 1.8,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {source.text || '（无图谱内容）'}
        </div>
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
          answerText={answerText}
        />
      ) : null}
    </AppModal>
  );
};

export default CitationTraceModal;
