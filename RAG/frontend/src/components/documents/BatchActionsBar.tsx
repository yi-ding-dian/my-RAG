import React from 'react';
import { Alert, Button, Card, Input, Popconfirm, Segmented, Space, Steps, Typography } from 'antd';
import { DeleteOutlined, ThunderboltOutlined } from '@ant-design/icons';

const { Text } = Typography;

/**
 * 文档状态筛选（M3 语义统一）：「未入库」= uploaded（待解析）+ parsed（已解析）
 * 两态，两者均可触发入库解析。筛选 value 用 unparsed（与 B 批后端契约对齐：
 * 后端接受 status=unparsed 映射两态，先过滤后分页）。
 */
export type StatusFilter = 'all' | 'unparsed' | 'parsing' | 'ingested' | 'failed';
export const statusFilterOptions: { label: string; value: StatusFilter }[] = [
  { label: '全部', value: 'all' },
  { label: '未入库', value: 'unparsed' },
  { label: '解析中', value: 'parsing' },
  { label: '已入库', value: 'ingested' },
  { label: '失败', value: 'failed' },
];

/** 前端筛选 value → 后端 status 参数：all 不传（=全部），其余原样透传 */
export const toBackendStatus = (filter: StatusFilter): string | undefined =>
  filter === 'all' ? undefined : filter;

interface BatchActionsBarProps {
  kbId?: string;
  canManage: boolean;
  /** 状态筛选（Segmented，标题栏） */
  statusFilter: StatusFilter;
  onStatusFilterChange: (v: StatusFilter) => void;
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
  /** 提示条：未入库文档数 / 解析中自动刷新提示 */
  unparsedCount: number;
  showParsingHint: boolean;
  /** 空状态引导：知识库尚无文档时展示构建知识的三步流程 */
  showGuide: boolean;
  children: React.ReactNode;
}

/**
 * 文档列表 Card 外壳：状态筛选（标题栏）+ 搜索/已选/批量解析/批量删除（extra）
 * + 未入库提示 + 解析中提示 + 空库引导；children 为上传条与表格。
 * （原 Documents.tsx 内联 JSX 整体移入，DOM 结构与行为不变）
 */
const BatchActionsBar: React.FC<BatchActionsBarProps> = ({
  kbId,
  canManage,
  statusFilter,
  onStatusFilterChange,
  keywordInput,
  onKeywordInputChange,
  onKeywordSearch,
  selectedCount,
  batchParsing,
  parseProgress,
  onBatchParse,
  onBatchDelete,
  unparsedCount,
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
      title={
        <Space size={12}>
          <span>文档列表</span>
          <Segmented
            size="small"
            value={statusFilter}
            onChange={v => onStatusFilterChange(v as StatusFilter)}
            options={statusFilterOptions}
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
      {unparsedCount > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={`有 ${unparsedCount} 个文档未入库，可勾选后批量解析`}
        />
      )}
      {showParsingHint && (
        <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
          解析中状态每 2 秒自动刷新，完成后自动更新
        </Text>
      )}
    </Card>
  </>
);

export default BatchActionsBar;
