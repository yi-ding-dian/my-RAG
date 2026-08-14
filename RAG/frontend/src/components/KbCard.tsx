import React from 'react';
import { Button, Card, Popconfirm, Tag, Tooltip, Typography } from 'antd';
import {
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  RightOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import { useNavigate } from 'react-router-dom';
import type { KnowledgeBase } from '../api/client';

/** 名称 hash → 渐变配色（8 组，与品牌蓝系同风格） */
const KB_GRADIENTS = [
  'linear-gradient(135deg, var(--brand-primary, #2563eb) 0%, var(--brand-primary-deep, #1d4ed8) 100%)',
  'linear-gradient(135deg, #0ea5e9 0%, #0284c7 100%)',
  'linear-gradient(135deg, #10b981 0%, #047857 100%)',
  'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)',
  'linear-gradient(135deg, #ef4444 0%, #b91c1c 100%)',
  'linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%)',
  'linear-gradient(135deg, #ec4899 0%, #be185d 100%)',
  'linear-gradient(135deg, #14b8a6 0%, #0f766e 100%)',
];

/** 字符串 hash（知识库名称 → 稳定取色） */
const hashString = (s: string): number => {
  let h = 0;
  for (let i = 0; i < s.length; i += 1) {
    h = (h * 31 + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
};

interface KbCardProps {
  kb: KnowledgeBase;
  canManage: boolean;
  onEdit: (kb: KnowledgeBase) => void;
  onRebuild: (kb: KnowledgeBase) => void;
  onDelete: (kb: KnowledgeBase) => void;
  onRemoveTag: (kb: KnowledgeBase, tag: string) => void;
}

/**
 * 知识库卡片（表格 → 卡片网格的第 1 波改造）：
 * 渐变图标（名称 hash 取色）+ 名称 1 行省略 + 标签（≤3 个，多余 +n）
 * + 描述 2 行 clamp（无描述占位）+ 底部（文档数/切块数/向量状态/创建时间）。
 * 点击卡片（非按钮区）→ 进入文档管理 /documents?kb_id=xxx；
 * 操作按钮（文档/编辑/重建向量/删除）hover 时显示，stopPropagation 防误跳。
 */
const KbCard: React.FC<KbCardProps> = ({
  kb,
  canManage,
  onEdit,
  onRebuild,
  onDelete,
  onRemoveTag,
}) => {
  const navigate = useNavigate();
  const vs = kb.vector_status;
  const incompatible = vs?.compatible === false;
  const tags = kb.tags ?? [];

  const openDocs = () => navigate(`/documents?kb_id=${kb.id}`);
  const stop = (e: React.MouseEvent) => e.stopPropagation();

  return (
    <Card className="kb-card" onClick={openDocs}>
      <div className="kb-card__head">
        <div
          className="kb-card__icon"
          style={{ background: KB_GRADIENTS[hashString(kb.name) % KB_GRADIENTS.length] }}
        >
          {(kb.name.trim().charAt(0) || '库').toUpperCase()}
        </div>
        <Typography.Text strong ellipsis className="kb-card__name" title={kb.name}>
          {kb.name}
        </Typography.Text>
        <div className="kb-card__actions" onClick={stop}>
          <Tooltip title="文档管理">
            <Button size="small" type="text" icon={<FolderOpenOutlined />} onClick={openDocs} />
          </Tooltip>
          {canManage && (
            <Tooltip title="编辑">
              <Button
                size="small"
                type="text"
                icon={<EditOutlined />}
                onClick={e => {
                  stop(e);
                  onEdit(kb);
                }}
              />
            </Tooltip>
          )}
          {canManage && (
            <Tooltip title="重建向量">
              <Popconfirm
                title={`重建「${kb.name}」的全部向量？`}
                description="将清除该知识库所有向量并重新生成，可能需要较长时间"
                okText="开始重建"
                cancelText="取消"
                onConfirm={() => onRebuild(kb)}
              >
                <Button
                  size="small"
                  type="text"
                  danger={incompatible}
                  icon={<SyncOutlined />}
                  onClick={stop}
                />
              </Popconfirm>
            </Tooltip>
          )}
          {canManage && (
            <Tooltip title="删除">
              <Popconfirm
                title={`删除知识库「${kb.name}」？`}
                description="将级联删除该库下的全部文档与向量数据，不可恢复！"
                okText="删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                onConfirm={() => onDelete(kb)}
              >
                <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={stop} />
              </Popconfirm>
            </Tooltip>
          )}
        </div>
      </div>

      {/* 标签：≤3 个，多余 +n；canManage 时可关闭（关闭后立即保存） */}
      <div className="kb-card__tags">
        {tags.length > 0 ? (
          <>
            {tags.slice(0, 3).map(t => (
              <Tag
                key={t}
                closable={canManage}
                onClose={e => {
                  e.preventDefault();
                  e.stopPropagation();
                  onRemoveTag(kb, t);
                }}
              >
                {t}
              </Tag>
            ))}
            {tags.length > 3 && <Tag>+{tags.length - 3}</Tag>}
          </>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            暂无标签
          </Typography.Text>
        )}
      </div>

      {/* 描述：2 行 clamp；无描述显示灰色占位 */}
      <div className="kb-card__desc">
        {kb.description || <span className="kb-card__desc--empty">暂无描述</span>}
      </div>

      <div className="kb-card__footer">
        <div className="kb-card__footerTop">
          <span className="kb-card__meta">
            <FileTextOutlined /> {kb.doc_count} 文档
          </span>
          <span className="kb-card__meta">
            <DatabaseOutlined /> {kb.chunk_count} 切块
          </span>
          <span style={{ flex: 1 }} />
          {!vs || vs.current_dim == null ? (
            <Tag>暂无向量</Tag>
          ) : vs.compatible === false ? (
            <Tag color="red" title={vs.model_dim != null ? `当前模型 ${vs.model_dim} 维` : ''}>
              维度不符
            </Tag>
          ) : (
            <Tag color="green" title={vs.current_dim != null ? `${vs.current_dim} 维` : ''}>
              正常
            </Tag>
          )}
        </div>
        {/* 常显「进入文档管理」文字入口（H5 可发现性：不依赖 hover/触屏；点击与整卡点击同目标） */}
        <Button
          type="link"
          size="small"
          className="kb-card__enter"
          icon={<FolderOpenOutlined />}
          onClick={e => {
            stop(e);
            openDocs();
          }}
        >
          进入文档管理
          <RightOutlined style={{ fontSize: 10 }} />
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
          {kb.created_at ? dayjs(kb.created_at).format('YYYY-MM-DD') : '-'}
        </Typography.Text>
      </div>
    </Card>
  );
};

export default KbCard;
