import React from 'react';
import { Checkbox, Dropdown, Popconfirm, Spin, Tag, Tooltip, Button } from 'antd';
import {
  ApartmentOutlined,
  DeleteOutlined,
  DownOutlined,
  DownloadOutlined,
  EditOutlined,
  EyeOutlined,
  FileTextOutlined,
  ProfileOutlined,
  StopOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import type { MenuProps } from 'antd';
import dayjs from 'dayjs';
import type { DocumentItem, DocumentStatus } from '../../api/types';
import { methodLabel } from '../../api/types';
import CardShell from './CardShell';

/** 文档状态 → Tag 色与文案（与表格列语义一致） */
const statusMeta: Record<DocumentStatus, { color: string; text: string }> = {
  uploaded: { color: 'default', text: '待解析' },
  parsing: { color: 'processing', text: '解析中' },
  parsed: { color: 'warning', text: '已解析' },
  ingested: { color: 'success', text: '已入库' },
  failed: { color: 'error', text: '失败' },
  pending_confirm: { color: 'orange', text: '待确认' },
};

/** 触发解析的状态（与表格 parseableStatuses 一致；页面主组件批量解析用） */
export const parseableStatuses: DocumentStatus[] = [
  'uploaded',
  'parsed',
  'failed',
  'ingested',
  'pending_confirm',
];

const formatSize = (bytes: number) => {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
};

const methodColor = (m: string): string => {
  switch (m) {
    case 'naive':
      return 'blue';
    case 'agentic':
      return 'gold';
    case 'qa':
      return 'purple';
    case 'parent_child':
      return 'magenta';
    default:
      return 'cyan';
  }
};

const typeIcon = (t: string): string => t.toUpperCase();

export interface DocumentCardProps {
  doc: DocumentItem;
  canManage: boolean;
  /** 批量勾选（批量解析/删除）；非管理员不渲染勾选 */
  selected?: boolean;
  onSelectChange?: (doc: DocumentItem, checked: boolean) => void;
  /** 点击卡片（非按钮区）→ 打开文档档案弹窗 */
  onOpen: (doc: DocumentItem) => void;
  /** 解析/重新解析（pending_confirm 状态由下方确认继续按钮承载）；
   *  不传时不渲染解析按钮（如全局文档页只读视图） */
  onStartParse?: (doc: DocumentItem) => void;
  /** Agentic 超限待确认 → 直接带确认标记重提入库 */
  onConfirmAgentic?: (doc: DocumentItem) => void;
  /** 取消解析（parsing 状态） */
  onCancelIngestion?: (doc: DocumentItem) => void;
  /** 切块详情弹窗；不传时不渲染 */
  onDetail?: (doc: DocumentItem) => void;
  /** 移入回收站 */
  onDelete?: (doc: DocumentItem) => void;
  /** 更多菜单：graph / graph-cancel / rename / download / portrait；
   *  不传时不渲染更多菜单 */
  onMoreAction?: (key: string, doc: DocumentItem) => void;
  /** 重命名（直接操作按钮） */
  onRename?: (doc: DocumentItem) => void;
  /** 更多菜单按状态动态组装（仅 onMoreAction 传入时生效；
   *  覆盖默认的图谱/重命名/下载/画像组装） */
  buildMoreItems?: (doc: DocumentItem) => MenuProps['items'];
}

/**
 * 文档卡片（基于 CardShell 底座）：类型图标 + 文件名 + 状态/解析方式 Tag
 * + 大小/切块数 meta + 时间；点击卡片 → 档案弹窗；操作按钮 hover 显示。
 */
const DocumentCard: React.FC<DocumentCardProps> = ({
  doc,
  canManage,
  selected,
  onSelectChange,
  onOpen,
  onStartParse,
  onConfirmAgentic,
  onCancelIngestion,
  onDetail,
  onDelete,
  onMoreAction,
  onRename,
  buildMoreItems,
}) => {
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  const status = statusMeta[doc.status] ?? { color: 'default', text: doc.status };

  /** 更多下拉菜单项（与表格行为一致：图谱/重命名/下载/画像） */
  const defaultMoreItems = (): MenuProps['items'] => {
    const items: NonNullable<MenuProps['items']> = [];
    if (canManage && (onMoreAction || onRename) && doc.status === 'ingested') {
      const building = doc.graph_status === 'building';
      items.push({
        key: 'graph',
        icon: building ? <Spin size="small" /> : <ApartmentOutlined />,
        label: (
          <Tooltip
            title={
              doc.graph_status === 'failed'
                ? `上次构建失败：${doc.graph_error || '未知原因'}，点击重新构建`
                : doc.graph_status === 'ready'
                  ? '重新抽取实体-关系，覆盖旧图谱'
                  : building
                    ? '图谱构建中，请稍候'
                    : '用现有切块抽取实体-关系构建图谱'
            }
          >
            {building
              ? '图谱构建中…'
              : doc.graph_status === 'ready' || doc.graph_status === 'failed'
                ? '重建图谱'
                : '补建图谱'}
          </Tooltip>
        ),
        disabled: building,
      });
      if (building) {
        items.push({
          key: 'graph-cancel',
          icon: <StopOutlined />,
          label: '中断构建',
          danger: true,
        });
      }
    }
    if (canManage && (onMoreAction || onRename)) {
      items.push({ key: 'rename', icon: <EditOutlined />, label: '重命名' });
    }
    items.push({ key: 'download', icon: <DownloadOutlined />, label: '下载' });
    if (canManage && onMoreAction) {
      items.push({
        key: 'portrait',
        icon: <ProfileOutlined />,
        label: '查看文档画像',
      });
    }
    return items;
  };

  /** 最终菜单：外部注入优先（全局页自定义菜单），否则默认组装 */
  const moreItems = buildMoreItems ? buildMoreItems(doc) : defaultMoreItems();

  const tags = (
    <>
      <Tag color={status.color}>{status.text}</Tag>
      <Tag color={methodColor(doc.parser_id ?? '')}>
        {doc.parser_id ? methodLabel(doc.parser_id) : '未切块'}
      </Tag>
      {doc.graph_status === 'building' && <Tag icon={<Spin size="small" />}>图谱构建中</Tag>}
    </>
  );

  const meta = (
    <>
      <span className="gen-card__meta">
        <FileTextOutlined /> {doc.chunk_count} 切块
      </span>
      <span className="gen-card__meta" style={{ marginRight: 0 }}>
        {formatSize(doc.size)}
      </span>
    </>
  );

  /** 动作按钮区：按回调 prop 存在性条件渲染（全局页等只传部分回调） */
  const actionButtons: React.ReactNode[] = [];
  if (canManage && (onCancelIngestion || onStartParse || onConfirmAgentic)) {
    if (doc.status === 'parsing' && onCancelIngestion) {
      actionButtons.push(
        <Tooltip title="取消解析" key="cancel">
          <Button
            size="small"
            type="text"
            icon={<StopOutlined />}
            onClick={e => {
              stop(e);
              onCancelIngestion(doc);
            }}
          />
        </Tooltip>,
      );
    } else if (onStartParse && parseableStatuses.includes(doc.status)) {
      actionButtons.push(
        <Tooltip title={doc.status === 'pending_confirm' ? '确认继续（Agentic 分块超限）' : '解析/重新解析'} key="parse">
          <Button
            size="small"
            type="text"
            icon={<SyncOutlined />}
            onClick={e => {
              stop(e);
              if (doc.status === 'pending_confirm') {
                onConfirmAgentic?.(doc);
              } else {
                onStartParse(doc);
              }
            }}
          />
        </Tooltip>,
      );
    }
  }
  if (onDetail) {
    actionButtons.push(
      <Tooltip title="切块详情" key="detail">
        <Button
          size="small"
          type="text"
          icon={<EyeOutlined />}
          onClick={e => {
            stop(e);
            onDetail(doc);
          }}
        />
      </Tooltip>,
    );
  }
  if (canManage && onRename) {
    actionButtons.push(
      <Tooltip title="重命名" key="rename">
        <Button
          size="small"
          type="text"
          icon={<EditOutlined />}
          onClick={e => {
            stop(e);
            onRename(doc);
          }}
        />
      </Tooltip>,
    );
  }
  if (canManage && onDelete) {
    actionButtons.push(
      <Tooltip title="移入回收站" key="delete">
        <Popconfirm
          title={`移入回收站「${doc.original_name}」？`}
          description="可在回收站恢复"
          okText="移入"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={() => onDelete(doc)}
        >
          <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={stop} />
        </Popconfirm>
      </Tooltip>,
    );
  }
  if (onMoreAction) {
    actionButtons.push(
      <Dropdown
        key="more"
        menu={{
          items: moreItems,
          onClick: ({ key }) => onMoreAction(key, doc),
        }}
        trigger={['click']}
      >
        <Button size="small" type="text" icon={<DownOutlined />} onClick={stop} />
      </Dropdown>,
    );
  }
  const actions = actionButtons.length > 0 ? <>{actionButtons}</> : undefined;

  return (
    <CardShell
      iconText={typeIcon(doc.file_type)}
      title={doc.original_name}
      actions={actions}
      tags={tags}
      meta={meta}
      time={doc.created_at ? dayjs(doc.created_at).format('YYYY-MM-DD HH:mm') : '-'}
      onClick={() => onOpen(doc)}
      footerExtra={
        canManage && onSelectChange && (
          <div className="gen-card__enter" onClick={stop} style={{ marginTop: 4 }}>
            <Checkbox
              checked={selected}
              onChange={e => onSelectChange(doc, e.target.checked)}
            >
              <span style={{ fontSize: 12 }}>勾选批量操作</span>
            </Checkbox>
          </div>
        )
      }
    />
  );
};

export default DocumentCard;
