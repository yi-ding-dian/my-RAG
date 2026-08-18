import React, { useCallback, useEffect, useState } from 'react';
import {
  App as AntApp,
  Button,
  Card,
  Dropdown,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type { MenuProps } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ApartmentOutlined,
  CheckOutlined,
  DeleteOutlined,
  DownloadOutlined,
  DownOutlined,
  EditOutlined,
  EyeOutlined,
  ProfileOutlined,
  RollbackOutlined,
  RobotOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  asApiError,
  DocumentItem,
  DocumentStatus,
  emptyTrash,
  listTrashDocuments,
  methodColor,
  methodLabel,
  purgeDocument,
  restoreDocument,
} from '../../api/client';
import AppEmpty from '../AppEmpty';

const { Text } = Typography;

const statusMeta: Record<DocumentStatus, { color: string; text: string }> = {
  uploaded: { color: 'default', text: '待解析' },
  parsing: { color: 'processing', text: '解析中' },
  parsed: { color: 'warning', text: '已解析' },
  ingested: { color: 'success', text: '已入库' },
  failed: { color: 'error', text: '失败' },
  // Agentic 分块超限待确认：不算失败，橙色 Tag + 操作列"确认继续"按钮
  pending_confirm: { color: 'orange', text: '待确认' },
};

// 可触发解析的状态：待解析/已解析/失败/已入库（已入库=重新解析）/
// 待确认（Agentic 超限，可"确认继续"或换方式重新解析）
// 页面主组件批量解析同样使用（判定勾选行是否可解析）
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

/** 解析方式语义说明（与后端 splitter.py / ingestion_service.py 实际行为对齐） */
const METHOD_DESC: Record<string, string> = {
  naive: '按分隔符递归字符切块，块大小与重叠可配',
  title: '按标题切块',
  regex: '按正则匹配位置切块，匹配片段与其余文本都成块',
  parent_child: '父块按标题聚合章节，子块细粒度切分；命中子块返回父块上下文',
  qa: '按问/答标记聚合问答对为整块，入库检测问答对占比 ≥50%',
  agentic: 'LLM 语义切分逻辑段落并打标签',
};

/** 版面识别说明（仅 pdf/docx 文档有意义，与解析配置弹窗文案一致） */
const LAYOUT_RECOGNIZE_DESC: Record<string, string> = {
  MinerU: 'MinerU 高精度版面识别（PDF 混排图文表格）',
  DeepDOC: 'DeepDoc（RAGFlow）解析，表格输出为可检索 HTML（仅 PDF）',
  PlainText: '纯文本直提（pypdf/python-docx，无表格/图片识别）',
};

/** 思考模式显示名（disabled=关闭为默认，不展示） */
const THINKING_MODE_LABEL: Record<string, string> = {
  enabled_low: '开-低',
  enabled_high: '开-高',
  enabled_max: '开-最大',
};

/** 解析方式列 Tooltip 内容：方式语义 + 实际生效的参数摘要。
 *  qa/agentic 不消费块大小/重叠（问答对整块 / LLM 自主切分），不展示避免误导；
 *  版面识别/降级说明仅 pdf/docx 展示（txt/md 直读不经解析器）。 */
const methodTooltipContent = (
  method: string,
  config: Record<string, unknown> | undefined,
  fileType: string,
): React.ReactNode => {
  const parts: React.ReactNode[] = [];
  const desc = METHOD_DESC[method];
  if (desc) {
    parts.push(
      <div key="desc" style={{ maxWidth: 320 }}>
        {desc}
      </div>,
    );
  }
  if (config) {
    const params: string[] = [];
    if (method === 'parent_child') {
      // 子块参数 + 父块参数（父块大小是超长单节兜底上限，非目标大小）
      if (config.chunk_size != null) params.push(`子块大小 ${config.chunk_size}`);
      if (config.overlap != null) params.push(`子块重叠 ${config.overlap}`);
      if (config.parent_chunk_size != null) params.push(`父块大小 ${config.parent_chunk_size}`);
      if (config.parent_split_level != null) params.push(`父块层级 H${config.parent_split_level}`);
      if (config.retrieval_mode === 'child') params.push('检索返回子块');
    } else if (method !== 'qa' && method !== 'agentic') {
      if (config.chunk_size != null) params.push(`块大小 ${config.chunk_size}`);
      if (config.overlap != null) params.push(`重叠 ${config.overlap}`);
      if (method === 'naive' && config.delimiter) params.push(`分隔符 ${String(config.delimiter)}`);
      if (method === 'title' && config.split_level != null) params.push(`标题层级 H${config.split_level}`);
      if (method === 'regex' && config.regex_pattern) params.push(`正则 ${String(config.regex_pattern)}`);
    }
    // 思考模式：非默认（关闭）时展示（图谱抽取/上下文摘要/Agentic 分块共用）
    if (config.thinking_mode && config.thinking_mode !== 'disabled') {
      params.push(
        `思考模式 ${THINKING_MODE_LABEL[String(config.thinking_mode)] ?? String(config.thinking_mode)}`,
      );
    }
    // 版面识别与降级说明：仅 pdf/docx 真实解析场景有意义（txt/md 直读不经解析器）
    const isPdfLike = fileType === 'pdf' || fileType === 'docx';
    if (isPdfLike) {
      const lr = config.layout_recognize;
      if (typeof lr === 'string' && LAYOUT_RECOGNIZE_DESC[lr]) {
        params.push(LAYOUT_RECOGNIZE_DESC[lr]);
      }
      if (typeof config.degrade === 'string' && config.degrade) params.push(config.degrade);
    }
    if (params.length > 0) {
      parts.push(
        <div key="params" style={{ marginTop: 4, maxWidth: 320 }}>
          {params.join(' / ')}
        </div>,
      );
    }
  }
  return <>{parts}</>;
};

/** 行操作回调集合（状态管理/副作用由页面主组件持有，此处只触发） */
export interface DocumentRowCallbacks {
  /** 点击文件名打开在线预览 */
  onPreview: (doc: DocumentItem) => void;
  /** 解析/重新解析按钮（主组件先清 QA 失败提示记录再打开解析配置弹窗） */
  onStartParse: (doc: DocumentItem) => void;
  /** 智能解析引导 */
  onSmartParse: (doc: DocumentItem) => void;
  /** Agentic 超限待确认 → 直接带确认标记重提入库 */
  onConfirmAgentic: (doc: DocumentItem) => void;
  /** 取消解析（parsing 状态） */
  onCancelIngestion: (doc: DocumentItem) => void;
  /** 切块详情弹窗 */
  onDetail: (doc: DocumentItem) => void;
  /** 移入回收站 */
  onDelete: (doc: DocumentItem) => void;
  /** 更多菜单：graph / graph-cancel / rename / download / portrait */
  onMoreAction: (key: string, doc: DocumentItem) => void;
}

interface DocumentTableProps extends DocumentRowCallbacks {
  /** 可见文档列表（页面主组件已按状态筛选/关键词过滤计算） */
  docs: DocumentItem[];
  loading: boolean;
  /** 分页：keyword 非空=前端分页（dataSource 全量）；否则服务端分页 */
  page: number;
  pageSize: number;
  total: number;
  keyword: string;
  canManage: boolean;
  selectedRowKeys: React.Key[];
  onSelectionChange: (keys: React.Key[]) => void;
  /** 翻页回调（页面主组件区分 keyword 模式是否发请求） */
  onPageChange: (p: number, ps: number) => void;
  /** total === 0（空库空状态文案） */
  totalIsEmpty: boolean;
}

/**
 * 文档列表表格：列渲染（状态/解析方式 Tooltip/操作列）+ 更多菜单 + 分页/勾选。
 * （原 Documents.tsx 内联 columns/操作列/菜单逻辑整体移入，行为不变）
 */
const DocumentTable: React.FC<DocumentTableProps> = ({
  docs,
  loading,
  page,
  pageSize,
  total,
  keyword,
  canManage,
  selectedRowKeys,
  onSelectionChange,
  onPageChange,
  totalIsEmpty,
  onPreview,
  onStartParse,
  onSmartParse,
  onConfirmAgentic,
  onCancelIngestion,
  onDetail,
  onDelete,
  onMoreAction,
}) => {
  /** 更多下拉菜单项（按文档状态/权限动态组装）：
   *  - 图谱补建/重建：仅已入库（ingested）显示；building 时禁用灰显（Spin），
   *    并提供「中断构建」（danger）入口（原操作列中断按钮移入）
   *  - 重命名 / 查看画像：canManage（画像接口 can_manage_kb）
   *  - 下载：读取操作，所有可访问用户可用（与切块详情一致） */
  const buildMoreItems = useCallback(
    (row: DocumentItem): MenuProps['items'] => {
      const items: NonNullable<MenuProps['items']> = [];
      if (canManage && row.status === 'ingested') {
        const building = row.graph_status === 'building';
        items.push({
          key: 'graph',
          icon: building ? <Spin size="small" /> : <ApartmentOutlined />,
          label: (
            <Tooltip
              title={
                row.graph_status === 'failed'
                  ? `上次构建失败：${row.graph_error || '未知原因'}，点击重新构建`
                  : row.graph_status === 'ready'
                    ? '重新抽取实体-关系，覆盖旧图谱'
                    : building
                      ? '图谱构建中，请稍候'
                      : '用现有切块抽取实体-关系构建图谱'
              }
            >
              {building
                ? '图谱构建中…'
                : row.graph_status === 'ready' || row.graph_status === 'failed'
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
      if (canManage) {
        items.push({ key: 'rename', icon: <EditOutlined />, label: '重命名' });
      }
      items.push({ key: 'download', icon: <DownloadOutlined />, label: '下载' });
      if (canManage) {
        items.push({
          key: 'portrait',
          icon: <ProfileOutlined />,
          label: '查看文档画像',
        });
      }
      return items;
    },
    [canManage],
  );

  const columns: ColumnsType<DocumentItem> = [
    {
      title: '文件名',
      dataIndex: 'original_name',
      key: 'name',
      ellipsis: true,
      width: 260,
      // 点击文件名即可打开预览弹窗（替代原「文档预览」按钮）
      render: (v: string, row) => (
        <Typography.Link onClick={() => onPreview(row)} title={v}>
          {v}
        </Typography.Link>
      ),
    },
    {
      title: '类型',
      dataIndex: 'file_type',
      key: 'file_type',
      width: 80,
      // URL 网页导入的文档 file_type 为 "url"，展示为"网页"
      render: (v: string) => <Tag>{v === 'url' ? '网页' : v || '-'}</Tag>,
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 100,
      // 真实后端列表接口暂未返回 size 字段时显示 '-'
      render: (v?: number) => (v == null ? '-' : formatSize(v)),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: DocumentStatus, row) => {
        const meta = statusMeta[status] ?? { color: 'default', text: status };
        const tag =
          status === 'parsing' ? (
            <Tag color={meta.color} icon={<Spin size="small" />}>
              {meta.text}
            </Tag>
          ) : (
            <Tag color={meta.color}>{meta.text}</Tag>
          );
        return (status === 'failed' || status === 'pending_confirm') && row.error ? (
          <Tooltip title={row.error}>{tag}</Tooltip>
        ) : (
          tag
        );
      },
    },
    {
      title: '切块数',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 90,
      render: (v: number, row) => (row.status === 'ingested' ? v : '-'),
    },
    {
      title: '解析方式',
      dataIndex: 'parser_id',
      key: 'parser_id',
      width: 200,
      render: (v: string | undefined, row) => {
        if (!v) return <Text type="secondary">-</Text>;
        const tag = <Tag color={methodColor(v)}>{methodLabel(v)}</Tag>;
        // Tooltip：方式语义 + 实际生效的参数摘要（qa/agentic 无块大小/重叠；
        // 版面识别/降级仅 pdf/docx 展示），无参数配置时也展示方式语义
        const parseTag = (
          <Tooltip title={methodTooltipContent(v, row.parser_config, row.file_type)}>
            {tag}
          </Tooltip>
        );
        // 已构建知识图谱的文档：解析方式后追加紫色"图谱"标签（tooltip 说明）
        const graphTag =
          row.graph_status === 'ready' ? (
            <Tooltip title="已构建知识图谱">
              <Tag color="purple">图谱</Tag>
            </Tooltip>
          ) : null;
        return (
          <Space size={4}>
            {parseTag}
            {graphTag}
          </Space>
        );
      },
    },
    {
      title: '上传时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 380,
      render: (_, row) => (
        <Space size="small">
          {canManage && row.status === 'pending_confirm' && (
            <Tooltip title="Agentic 分块超限（1 万~5 万字）待确认：点击确认后直接带确认标记重新入库">
              <Button
                key="confirm-agentic"
                size="small"
                type="primary"
                icon={<CheckOutlined />}
                onClick={() => void onConfirmAgentic(row)}
              >
                确认继续
              </Button>
            </Tooltip>
          )}
          {canManage && parseableStatuses.includes(row.status) && (
            <Tooltip title="智能解析引导：画像分析后按推荐路径生成配置并解析（未解析文档也可分析）">
              <Button
                key="smart-parse"
                size="small"
                icon={<RobotOutlined />}
                onClick={() => onSmartParse(row)}
              >
                智能解析
              </Button>
            </Tooltip>
          )}
          {canManage && parseableStatuses.includes(row.status) &&
            (row.status === 'ingested' ? (
              <Popconfirm
                key="reparse"
                title={`重新解析「${row.original_name}」？`}
                description="将清除旧切块重新解析"
                onConfirm={() => onStartParse(row)}
                okText="确认"
                cancelText="取消"
              >
                <Button size="small" type="primary" ghost icon={<ThunderboltOutlined />}>
                  重新解析
                </Button>
              </Popconfirm>
            ) : (
              <Button
                key="parse"
                size="small"
                type="primary"
                ghost
                icon={<ThunderboltOutlined />}
                onClick={() => onStartParse(row)}
              >
                {row.status === 'uploaded' ? '解析' : '重新解析'}
              </Button>
            ))}
          {canManage && row.status === 'parsing' && (
            <Popconfirm
              key="ingest-cancel"
              title={`取消解析「${row.original_name}」？`}
              description="将停止本次解析，文档回到失败状态，可重新发起解析"
              onConfirm={() => void onCancelIngestion(row)}
              okText="取消解析"
              cancelText="再想想"
              okButtonProps={{ danger: true }}
            >
              <Button size="small" danger icon={<StopOutlined />}>
                取消解析
              </Button>
            </Popconfirm>
          )}
          <Button size="small" icon={<EyeOutlined />} onClick={() => onDetail(row)}>
            切块详情
          </Button>
          {canManage && (
            <Popconfirm
              title={`移入回收站「${row.original_name}」？`}
              description="文档将不再参与检索，可在回收站恢复；彻底删除请在回收站操作"
              onConfirm={() => void onDelete(row)}
              okText="移入回收站"
              cancelText="取消"
            >
              <Button size="small" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          )}
          {/* 更多：补建/重建图谱、重命名（原操作列按钮移入）、下载、查看画像 */}
          <Dropdown
            menu={{
              items: buildMoreItems(row),
              onClick: ({ key }) => onMoreAction(key, row),
            }}
            trigger={['click']}
          >
            <Button size="small">
              更多
              <DownOutlined />
            </Button>
          </Dropdown>
        </Space>
      ),
    },
  ];

  return (
    <Table
      dataSource={docs}
      columns={columns}
      rowKey="id"
      loading={loading}
      pagination={
        keyword
          ? {
              // keyword 模式：dataSource 为全量过滤结果，
              // total 与 dataSource 等长 → antd 自动前端分页，
              // 翻页仅改本地页码不发请求
              current: page,
              pageSize,
              total: docs.length,
              showTotal: (t: number) => `共 ${t} 条`,
              onChange: (p: number, ps: number) => onPageChange(p, ps),
            }
          : {
              // P2-10 服务端分页：翻页重新请求；total 来自后端
              current: page,
              pageSize,
              total,
              showTotal: (t: number) => `共 ${t} 条`,
              onChange: (p: number, ps: number) => onPageChange(p, ps),
            }
      }
      locale={{
        emptyText:
          totalIsEmpty ? (
            <AppEmpty
              title="暂无文档"
              description="点击上方上传条上传文件，或从 URL 导入网页内容"
            />
          ) : keyword && docs.length === 0 ? (
            <AppEmpty
              title="无匹配文档"
              description={`没有文件名包含「${keyword}」的文档，可清空搜索词或切换状态筛选`}
            />
          ) : (
            <AppEmpty
              title="没有符合条件的文档"
              description="当前筛选条件下暂无文档，可切换其他状态筛选"
            />
          ),
      }}
      scroll={{ x: 1100 }}
      className="table-zebra"
      rowSelection={
        canManage
          ? {
              selectedRowKeys,
              onChange: onSelectionChange,
              // 解析中（parsing）不可勾选；选中后状态变化的行提交时兜底跳过
              getCheckboxProps: (row: DocumentItem) => ({
                disabled: row.status === 'parsing',
              }),
            }
          : undefined
      }
    />
  );
};

// ========== 回收站视图 ==========

interface TrashViewProps {
  kbId?: string;
  /** 返回文档列表 */
  onBack: () => void;
  /** 删除/恢复后同步刷新主列表 */
  onChanged: () => Promise<void>;
}

/**
 * 回收站视图（软删除文档列表）：恢复 / 彻底删除 / 清空回收站。
 * 自包含加载与操作（原 Documents.tsx 回收站 Card + Table 整体移入）。
 */
const TrashView: React.FC<TrashViewProps> = ({ kbId, onBack, onChanged }) => {
  const { message } = AntApp.useApp();
  const [trashDocs, setTrashDocs] = useState<DocumentItem[]>([]);
  const [trashLoading, setTrashLoading] = useState(false);

  const loadTrash = useCallback(
    async (silent = false) => {
      if (!kbId) {
        setTrashDocs([]);
        return;
      }
      if (!silent) setTrashLoading(true);
      try {
        // 回收站不传分页参数，后端返回全量数组；类型兼容分页响应做收窄
        const res = await listTrashDocuments(kbId);
        const data = res.data;
        setTrashDocs(Array.isArray(data) ? data : data.items);
      } catch {
        if (!silent) message.error('加载回收站失败');
      } finally {
        if (!silent) setTrashLoading(false);
      }
    },
    [kbId, message],
  );

  // 进入回收站视图时加载一次（视图由主组件条件渲染，挂载时机=打开时机）
  useEffect(() => {
    void loadTrash();
  }, [loadTrash]);

  const handleRestore = async (doc: DocumentItem) => {
    try {
      await restoreDocument(kbId!, doc.id);
      message.success('文档已恢复');
      await loadTrash(true);
      await onChanged();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '恢复失败');
    }
  };

  const handlePurge = async (doc: DocumentItem) => {
    try {
      await purgeDocument(kbId!, doc.id);
      message.success('文档已彻底删除');
      await loadTrash(true);
      await onChanged();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  const handleEmptyTrash = async () => {
    try {
      const res = await emptyTrash(kbId!);
      message.success(res.data?.message ?? '回收站已清空');
      await loadTrash(true);
      await onChanged();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '清空回收站失败');
    }
  };

  return (
    <Card
      title="回收站"
      extra={
        <Space>
          {trashDocs.length > 0 && (
            <Popconfirm
              title="清空回收站？"
              description="回收站内全部文档将被彻底删除，不可恢复"
              onConfirm={handleEmptyTrash}
              okText="清空"
              cancelText="取消"
            >
              <Button danger icon={<DeleteOutlined />}>
                清空回收站
              </Button>
            </Popconfirm>
          )}
          <Button icon={<RollbackOutlined />} onClick={onBack}>
            返回文档列表
          </Button>
        </Space>
      }
    >
      <Table
        dataSource={trashDocs}
        rowKey="id"
        loading={trashLoading}
        pagination={{ pageSize: 10 }}
        locale={{ emptyText: <AppEmpty title="回收站为空" description="已删除的文档会出现在这里，可在 30 天内恢复" /> }}
        scroll={{ x: 800 }}
        className="table-zebra"
        columns={[
          {
            title: '文件名',
            dataIndex: 'original_name',
            key: 'name',
            ellipsis: true,
            width: 260,
          },
          {
            title: '类型',
            dataIndex: 'file_type',
            key: 'file_type',
            width: 80,
            render: (v: string) => <Tag>{v === 'url' ? '网页' : v || '-'}</Tag>,
          },
          {
            title: '删除时间',
            dataIndex: 'deleted_at',
            key: 'deleted_at',
            width: 170,
            render: (v?: string | null) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
          },
          {
            title: '操作',
            key: 'actions',
            width: 200,
            render: (_, row) => (
              <Space size="small">
                <Button
                  size="small"
                  icon={<RollbackOutlined />}
                  onClick={() => void handleRestore(row)}
                >
                  恢复
                </Button>
                <Popconfirm
                  title={`彻底删除「${row.original_name}」？`}
                  description="将同时清除存储文件与向量，删除后不可恢复"
                  onConfirm={() => void handlePurge(row)}
                  okText="彻底删除"
                  cancelText="取消"
                >
                  <Button size="small" danger icon={<DeleteOutlined />}>
                    彻底删除
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
    </Card>
  );
};

export { TrashView };
export default DocumentTable;
