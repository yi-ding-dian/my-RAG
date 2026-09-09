import React from 'react';
import { Badge, Button, Card, Input, Popconfirm, Segmented, Space, Steps, Typography } from 'antd';
import { DeleteOutlined, ThunderboltOutlined } from '@ant-design/icons';
import type { DocumentStatusCounts } from '../../api/kb';

const { Text } = Typography;

/**
 * 文档状态筛选（M3 语义统一）：「未入库」= uploaded（待解析）+ parsed（已解析）
 * 两态，两者均可触发入库解析。筛选 value 用 unparsed（与 B 批后端契约对齐：
 * 后端接受 status=unparsed 映射两态，先过滤后分页）。
 */
export type StatusFilter = 'all' | 'unparsed' | 'parsing' | 'ingested' | 'failed';

/** 徽标色（antd 语义色，与状态 Tag 配色一致）：全部=主题蓝 / 未入库=橙 /
 * 解析中=青 / 已入库=绿 / 失败=红 */
const STATUS_BADGE_COLORS: Record<StatusFilter, string> = {
  all: '#1677ff',
  unparsed: '#fa8c16',
  parsing: '#13c2c2',
  ingested: '#52c41a',
  failed: '#ff4d4f',
};
const STATUS_LABELS: Record<StatusFilter, string> = {
  all: '全部',
  unparsed: '未入库',
  parsing: '解析中',
  ingested: '已入库',
  failed: '失败',
};
/** 筛选项 → 计数响应字段（total/unparsed/parsing/ingested/failed 一一对应） */
const STATUS_COUNT_KEYS: Record<StatusFilter, keyof DocumentStatusCounts> = {
  all: 'total',
  unparsed: 'unparsed',
  parsing: 'parsing',
  ingested: 'ingested',
  failed: 'failed',
};

/**
 * 由状态计数生成 Segmented 选项：label = 「文案 + 圆形数字徽标」（antd
 * Badge count 实心圆标，showZero 保证数量 0 也显示 0；counts 未加载为
 * null 时按 0 占位，避免加载完成时布局跳动）。Documents / GlobalDocuments
 * 两页共用，保证徽标结构/颜色一致。
 */
export const buildStatusOptions = (
  counts: DocumentStatusCounts | null,
): { label: React.ReactNode; value: StatusFilter }[] =>
  (Object.keys(STATUS_LABELS) as StatusFilter[]).map(value => {
    const count = counts?.[STATUS_COUNT_KEYS[value]] ?? 0;
    return {
      value,
      label: (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          {STATUS_LABELS[value]}
          <Badge count={count} showZero color={STATUS_BADGE_COLORS[value]} />
        </span>
      ),
    };
  });

/** 前端筛选 value → 后端 status 参数：all 不传（=全部），其余原样透传 */
export const toBackendStatus = (filter: StatusFilter): string | undefined =>
  filter === 'all' ? undefined : filter;

interface BatchActionsBarProps {
  kbId?: string;
  canManage: boolean;
  /** 状态筛选（Segmented，标题栏） */
  statusFilter: StatusFilter;
  onStatusFilterChange: (v: StatusFilter) => void;
  /** 状态计数（徽标数据源；null=尚未加载完成，按 0 占位） */
  statusCounts: DocumentStatusCounts | null;
  /** 文件名/关键词搜索（extra 栏） */
  keywordInput: string;
  onKeywordInputChange: (v: string) => void;
  onKeywordSearch: (v: string) => void;
  /** 批量解析 / 批量删除 */
  selectedCount: number;
  batchParsing: boolean;
  parseProgress: { done: number; total: number } | null;
  onBatchParse: () => void;
  onBatchDelete: () => void;
  /** 提示条：解析中自动刷新提示 */
  showParsingHint: boolean;
  /** 空状态引导：知识库尚无文档时展示构建知识的三步流程 */
  showGuide: boolean;
  children: React.ReactNode;
}

/**
 * 文档列表 Card 外壳：状态筛选（标题栏，带计数徽标）+ 搜索/已选/批量解析/
 * 批量删除（extra）+ 解析中提示 + 空库引导；children 为上传条与表格。
 * （原 Documents.tsx 内联 JSX 整体移入，DOM 结构与行为不变）
 */
const BatchActionsBar: React.FC<BatchActionsBarProps> = ({
  kbId,
  canManage,
  statusFilter,
  onStatusFilterChange,
  statusCounts,
  keywordInput,
  onKeywordInputChange,
  onKeywordSearch,
  selectedCount,
  batchParsing,
  parseProgress,
  onBatchParse,
  onBatchDelete,
  showParsingHint,
  showGuide,
  children,
}) => (
  <>
    {/* 空状态引导：知识库尚无文档时展示构建知识的三步流程 */}
    {showGuide && (
      <Card
        size="small"
        style={{ marginBottom: 16 }}
        title="开始构建您的知识库"
      >
        <Steps
          size="small"
          current={-1}
          responsive={false}
          items={[
            {
              title: '上传文件',
              description: '点击上方区域或拖拽文件上传，支持多选批量',
            },
            {
              title: '解析入库',
              description: '在上方列表勾选文档，点击「批量解析」',
            },
            {
              title: '开始问答',
              description: '等待状态变为「已入库」后，即可在问答页提问',
            },
          ]}
        />
      </Card>
    )}

    <Card
      style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
      styles={{ body: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' } }}
      title={
        <Space size={12}>
          <span>文档列表</span>
          <Segmented
            size="small"
            value={statusFilter}
            onChange={v => onStatusFilterChange(v as StatusFilter)}
            options={buildStatusOptions(statusCounts)}
          />
        </Space>
      }
      extra={
        <Space>
          {/* 文件名/关键词过滤：输入防抖 300ms 后生效（与状态筛选叠加）；
              allowClear 清空即恢复全部；批量解析按钮左侧 */}
          <Input.Search
            allowClear
            placeholder="搜索文档名称"
            style={{ width: 220 }}
            value={keywordInput}
            onChange={e => onKeywordInputChange(e.target.value)}
            onSearch={v => onKeywordSearch(v.trim())}
          />
          {canManage && (
            <>
              {selectedCount > 0 && (
                <Text type="secondary">已选 {selectedCount} 项</Text>
              )}
              <Button
                type="primary"
                ghost
                icon={<ThunderboltOutlined />}
                onClick={onBatchParse}
                disabled={!kbId || selectedCount === 0 || batchParsing}
                loading={batchParsing}
              >
                {batchParsing && parseProgress
                  ? `解析中 ${parseProgress.done}/${parseProgress.total}`
                  : '批量解析'}
              </Button>
              {selectedCount > 0 && (
                <Popconfirm
                  title={`将删除选中的 ${selectedCount} 个文档？`}
                  description="文档将移入回收站（向量保留，恢复后无需重新解析），可在回收站彻底删除"
                  onConfirm={onBatchDelete}
                  okText="删除"
                  cancelText="取消"
                  okButtonProps={{ danger: true }}
                >
                  <Button danger icon={<DeleteOutlined />}>
                    批量删除
                  </Button>
                </Popconfirm>
              )}
            </>
          )}
        </Space>
      }
    >
      {children}
      {showParsingHint && (
        <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
          解析中状态每 2 秒自动刷新，完成后自动更新
        </Text>
      )}
    </Card>
  </>
);

export default BatchActionsBar;
