import React from 'react';
import { Descriptions, Tag, Typography } from 'antd';
import dayjs from 'dayjs';
import AppModal from '../common/AppModal';
import type { DocumentItem } from '../../api/types';
import { methodLabel } from '../../api/types';

const { Text } = Typography;

const statusMeta: Record<string, { color: string; text: string }> = {
  uploaded: { color: 'default', text: '待解析' },
  parsing: { color: 'processing', text: '解析中' },
  parsed: { color: 'warning', text: '已解析' },
  ingested: { color: 'success', text: '已入库' },
  failed: { color: 'error', text: '失败' },
  pending_confirm: { color: 'orange', text: '待确认' },
};

const formatSize = (bytes: number) => {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
};

interface DocumentProfileModalProps {
  open: boolean;
  doc: DocumentItem | null;
  onCancel: () => void;
  /** 操作按钮（由页面主组件注入：重命名/删除/切块详情等） */
  extra?: React.ReactNode;
}

/**
 * 文档档案弹窗：点击文档卡片打开，列出该文档的完整档案信息
 * （文件名/类型/大小/状态/切块数/解析方式/解析方式描述/时间），
 * 底部由页面注入操作按钮（重命名/删除/切块详情等）。
 */
const DocumentProfileModal: React.FC<DocumentProfileModalProps> = ({
  open,
  doc,
  onCancel,
  extra,
}) => {
  if (!doc) return null;
  const st = statusMeta[doc.status] ?? { color: 'default', text: doc.status };

  return (
    <AppModal
      title={doc.original_name}
      open={open}
      onCancel={onCancel}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          {extra}
        </div>
      }
      width={640}
    >
      <Descriptions column={2} size="small" bordered>
        <Descriptions.Item label="文件名" span={2}>
          <Text copyable>{doc.original_name}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="类型">
          <Tag>{doc.file_type.toUpperCase()}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="大小">{formatSize(doc.size)}</Descriptions.Item>
        <Descriptions.Item label="状态">
          <Tag color={st.color}>{st.text}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="切块数">{doc.chunk_count}</Descriptions.Item>
        <Descriptions.Item label="解析方式">
          <Tag color="cyan">
            {doc.parser_id ? methodLabel(doc.parser_id) : '—'}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="上传时间">
          {doc.created_at ? dayjs(doc.created_at).format('YYYY-MM-DD HH:mm:ss') : '-'}
        </Descriptions.Item>
        <Descriptions.Item label="最近更新">
          {doc.updated_at ? dayjs(doc.updated_at).format('YYYY-MM-DD HH:mm:ss') : '-'}
        </Descriptions.Item>
        {doc.graph_status === 'building' && (
          <Descriptions.Item label="知识图谱" span={2}>
            <Tag color="processing">构建中</Tag>
          </Descriptions.Item>
        )}
        {doc.graph_status === 'failed' && (
          <Descriptions.Item label="知识图谱" span={2}>
            <Tag color="error">构建失败</Tag>
            {doc.graph_error && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {doc.graph_error}
              </Text>
            )}
          </Descriptions.Item>
        )}
        {doc.error && (
          <Descriptions.Item label="解析失败原因" span={2}>
            <Text type="danger">{doc.error}</Text>
          </Descriptions.Item>
        )}
      </Descriptions>
    </AppModal>
  );
};

export default DocumentProfileModal;
