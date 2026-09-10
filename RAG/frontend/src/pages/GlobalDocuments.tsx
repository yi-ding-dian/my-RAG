import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  App as AntApp,
  Breadcrumb,
  Button,
  Card,
  Col,
  Input,
  Popconfirm,
  Row,
  Segmented,
  Skeleton,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  ApartmentOutlined,
  BookOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  LeftOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import {
  asApiError,
  DepartmentSummaryEntry,
  DocumentStatus,
  DocumentStatusCounts,
  GlobalDocumentItem,
  deleteDocument,
  getGlobalDocumentsStatusCounts,
  getIngestProgress,
  listGlobalDocuments,
  listGlobalDocumentsSummary,
  methodColor,
  methodLabel,
} from '../api/client';
import { buildStatusOptions } from '../components/documents/BatchActionsBar';
import AppEmpty from '../components/common/AppEmpty';
import PageLayout from '../components/layout/PageLayout';
import TableSectionLayout from '../components/layout/TableSectionLayout';
import RenameDocumentModal from '../components/documents/RenameDocumentModal';
import DocumentProfileModal from '../components/documents/DocumentProfileModal';
import DocumentPreviewModal from '../components/documents/DocumentPreviewModal';
import ResizableTitle from '../components/common/ResizableTitle';
import { useDetailModal } from '../components/documents/DocumentModals';
import { useAuth } from '../auth/AuthContext';
import { useResizableColumns } from '../hooks/useResizableColumns';

const { Text } = Typography;

/** 未分配部门的分组标识（与后端 /api/admin/documents 契约一致） */
const UNASSIGNED = '__unassigned__';

const statusMeta: Record<DocumentStatus, { color: string; text: string }> = {
  uploaded: { color: 'default', text: '待解析' },
  parsing: { color: 'processing', text: '解析中' },
  parsed: { color: 'warning', text: '已解析' },
  ingested: { color: 'success', text: '已入库' },
  failed: { color: 'error', text: '失败' },
  pending_confirm: { color: 'orange', text: '待确认' },
};

/** 入库流程轨迹渲染（同 DocumentTable.renderIngestTrace —— 两处保持同步：
 * 已入库/失败状态 Tooltip 展示各阶段耗时 + 总耗时；无 trace 兜底"—"）*/
const formatTraceMs = (ms?: number): string => {
  if (ms == null || ms < 0) return '—';
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.round(ms)} ms`;
};

const renderIngestTrace = (
  trace: { stage: string; ms: number; status?: string }[],
  totalMs?: number | null,
  isFailed?: boolean,
  startedAt?: string | null,
  finishedAt?: string | null,
): React.ReactNode => (
  <div style={{ fontSize: 12, lineHeight: 1.8 }}>
    <div style={{ fontWeight: 600, marginBottom: 2 }}>
      {isFailed ? '入库执行（失败）' : '入库执行流程'}
    </div>
    {startedAt && (
      <div>开始：{startedAt}</div>
    )}
    {trace.map((t, i) => (
      <div key={i}>
        {t.stage}：{formatTraceMs(t.ms)}
        {t.status === 'failed' && <span style={{ color: '#ff4d4f' }}>（失败）</span>}
      </div>
    ))}
    {finishedAt && (
      <div>结束：{finishedAt}</div>
    )}
    {totalMs != null && (
      <div style={{ marginTop: 2, fontWeight: 600 }}>
        总耗时：{formatTraceMs(totalMs)}
      </div>
    )}
  </div>
);

const formatSize = (bytes: number): string => {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
};

/** 解析参数摘要（供解析方式列 Tooltip 展示，与部门内文档页一致） */
const parserConfigSummary = (
  config: Record<string, unknown> | undefined,
  method: string,
): string | null => {
  if (!config) return null;
  const parts: string[] = [];
  if (config.chunk_size != null) parts.push(`块大小 ${config.chunk_size}`);
  if (config.overlap != null) parts.push(`重叠 ${config.overlap}`);
  if (method === 'title' && config.split_level != null) parts.push(`标题层级 H${config.split_level}`);
  if (method === 'regex' && config.regex_pattern) parts.push(`正则 ${String(config.regex_pattern)}`);
  return parts.length > 0 ? parts.join(' / ') : null;
};

/** 状态筛选（与部门内文档管理 M3 语义统一：「未入库」= uploaded+parsed；
 * 带徽标选项由 buildStatusOptions(statusCounts) 生成） */
type StatusFilter = 'all' | 'unparsed' | 'parsing' | 'ingested' | 'failed';

const toBackendStatus = (filter: StatusFilter): string | undefined =>
  filter === 'all' ? undefined : filter;

/** 三级下钻状态：department | kbs | docs（dept_admin 自动从 kbs 起） */
type ViewLevel = 'departments' | 'dept' | 'docs';

/**
 * 全局文档管理页（super_admin 全量 / dept_admin 限本部门）：
 * 三级下钻——部门卡片 → 部门详情(知识库卡片) → 文档列表(卡片网格)。
 * 顶部面包屑导航返回上一级；第 3 层保留状态筛选/搜索/刷新/分页。
 * dept_admin 数据已由后端强制限定本部门，页面直接从本部门知识库层开始。
 */
const GlobalDocumentsPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { user } = useAuth();
  // dept_admin：本部门视图（后端强制 department_id，前端隐藏部门筛选）
  const isDeptAdmin = user?.role === 'dept_admin';

  // 列宽拖拽（第 3 层文档表）：拖拽后的列宽存 colWidths，scroll.x 动态对齐列宽和
  const { colWidths, handleResize, tableWidth } = useResizableColumns<GlobalDocumentItem>();

  // ---------- 三级下钻导航 ----------
  const [level, setLevel] = useState<ViewLevel>('departments');
  // 当前选中的部门（第 2 层数据源）
  const [deptKey, setDeptKey] = useState<string | null>(null);
  // 当前选中的知识库（第 3 层数据源）
  const [kbId, setKbId] = useState<string | null>(null);

  // ---------- 部⻔汇总树数据（第 1/2 层） ----------
  const [summary, setSummary] = useState<DepartmentSummaryEntry[]>([]);
  const [summaryLoading, setSummaryLoading] = useState(true);

  // ---------- 筛选（第 3 层） ----------
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [keywordInput, setKeywordInput] = useState('');
  const [keyword, setKeyword] = useState('');
  // 状态徽标计数（Segmented 标签数据源；null=未加载完成按 0 占位。
  // 仅在进入第 3 层/列表刷新时顺带拉取，与列表并行不串行）
  const [statusCounts, setStatusCounts] = useState<DocumentStatusCounts | null>(null);
  // 入库任务阶段进度（doc_id → {stage, since}；解析中/已入库/失败 Tooltip 展示；
  // 与列表刷新同节奏拉取，任务结束后端清空）
  const [ingestProgress, setIngestProgress] = useState<
    Record<string, { stage: string; since: string }>
  >({});

  // ---------- 文档列表（第 3 层） ----------
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState<GlobalDocumentItem[]>([]);
  const [loading, setLoading] = useState(false);

  // 解析中计时：每秒 tick 驱动「已耗时」刷新（仅当列表存在解析中文档时才起
  // 定时器，空闲不空转）；起点用 updated_at —— 状态转入 parsing 的时刻
  const [nowTick, setNowTick] = useState(() => Date.now());
  const hasParsing = items.some(d => d.status === 'parsing');
  useEffect(() => {
    if (!hasParsing) return;
    const timer = setInterval(() => setNowTick(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [hasParsing]);

  /** 已解析耗时文本（秒 → m:ss，超 1 小时 → h:mm:ss）；起点缺失/非法返回空串 */
  const formatElapsed = (startAt: string | undefined): string => {
    if (!startAt) return '';
    const t = Date.parse(startAt.replace(' ', 'T'));
    if (Number.isNaN(t)) return '';
    const s = Math.max(0, Math.floor((nowTick - t) / 1000));
    const pad = (n: number) => String(n).padStart(2, '0');
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
  };

  // 重命名弹窗 / 档案弹窗
  const [renameDoc, setRenameDoc] = useState<GlobalDocumentItem | null>(null);
  const [profileDoc, setProfileDoc] = useState<GlobalDocumentItem | null>(null);
  // 在线预览弹窗（点击文件名：预览原始文件）
  const [previewDoc, setPreviewDoc] = useState<GlobalDocumentItem | null>(null);
  // 切块详情弹窗（操作列"切块预览"；useDetailModal 需文档所属 kb_id）
  const [detailKbId, setDetailKbId] = useState<string | undefined>();
  const detailModal = useDetailModal(detailKbId);

  // ---------- 加载：部门汇总树 ----------
  const loadSummary = useCallback(async (silent = false) => {
    if (!silent) setSummaryLoading(true);
    try {
      const res = await listGlobalDocumentsSummary();
      const list = res.data.items ?? [];
      setSummary(list);
      // dept_admin：仅本部门一个，直接从部门详情层开始
      if (isDeptAdmin && list.length > 0) {
        setLevel('dept');
        setDeptKey(list[0].department_id);
      }
    } catch {
      if (!silent) message.error('加载部门汇总失败');
    } finally {
      if (!silent) setSummaryLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [message, isDeptAdmin]);

  /** 状态徽标计数刷新（过滤粒度同列表的 department_id/kb_id 子集；
   *  失败静默保留上次计数） */
  const refreshStatusCounts = useCallback(async () => {
    if (!kbId) return;
    try {
      const res = await getGlobalDocumentsStatusCounts({
        department_id: deptKey ?? undefined,
        kb_id: kbId,
      });
      setStatusCounts(res.data);
    } catch {
      // 徽标非关键路径：拉取失败不打扰，保持旧值等下次刷新
    }
  }, [kbId, deptKey]);

  /** 入库阶段进度刷新（解析中/已入库/失败 Tooltip 用；失败静默） */
  const refreshIngestProgress = useCallback(async () => {
    if (!kbId) return;
    try {
      const res = await getIngestProgress(kbId);
      setIngestProgress(res.data);
    } catch {
      // 进度非关键路径：失败保持旧值
    }
  }, [kbId]);

  // ---------- 加载：知识库文档列表（第 3 层） ----------
  const loadDocs = useCallback(
    async (silent = false, p = page, ps = pageSize) => {
      if (!kbId) return;
      if (!silent) setLoading(true);
      try {
        const res = await listGlobalDocuments({
          department_id: deptKey ?? undefined,
          kb_id: kbId,
          status: toBackendStatus(statusFilter),
          keyword: keyword || undefined,
          page: p,
          page_size: ps,
        });
        setItems(res.data.items);
        setTotal(res.data.total);
        // 列表刷新完成顺带刷一次徽标计数（文档增删/状态变化后自动跟上）
        void refreshStatusCounts();
        void refreshIngestProgress();
      } catch {
        if (!silent) message.error('加载文档列表失败');
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [kbId, deptKey, statusFilter, keyword, message, page, pageSize, refreshStatusCounts, refreshIngestProgress],
  );

  // 初次加载（dept_admin 进入即部门层；super_admin 见部门层）
  useEffect(() => {
    void loadSummary();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 进入第 3 层/筛选变化 → 回第 1 页重新请求
  useEffect(() => {
    if (level !== 'docs' || !kbId) return;
    setPage(1);
    void loadDocs(false, 1, pageSize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [level, kbId, deptKey, statusFilter, keyword]);

  // ---------- 派生数据 ----------
  const curDept = useMemo(
    () => (deptKey ? summary.find(s => s.department_id === deptKey) : null)
      ?? (deptKey === UNASSIGNED
        ? summary.find(s => s.department_id === UNASSIGNED)
        : null),
    [summary, deptKey],
  );
  const curKb = useMemo(
    () => (curDept?.kbs ?? []).find(k => k.kb_id === kbId) ?? null,
    [curDept, kbId],
  );

  // ---------- 操作 ----------
  const handleDelete = async (doc: GlobalDocumentItem) => {
    try {
      await deleteDocument(doc.kb_id, doc.id);
      message.success('已移入回收站（可恢复）');
      void loadDocs(true);
      void loadSummary(true);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  const goBack = (target: ViewLevel) => {
    setLevel(target);
    if (target === 'departments') {
      setDeptKey(null);
      setKbId(null);
    } else if (target === 'dept') {
      setKbId(null);
    }
  };

  // 面包屑：文档管理（全部部门）/ {部门} / {知识库}
  const breadcrumbItems = [
    {
      title: (
        <span style={{ cursor: 'pointer' }} onClick={() => goBack('departments')}>
          文档管理（全部部门）
        </span>
      ),
    },
    ...(level !== 'departments' && deptKey
      ? [{
          title: (
            <span style={{ cursor: 'pointer' }} onClick={() => goBack('dept')}>
              {curDept?.department_name ?? '部门'}
            </span>
          ),
        }]
      : []),
    ...(level === 'docs' && kbId
      ? [{ title: curKb?.kb_name ?? '知识库' }]
      : []),
  ];

  // ---------- 第 1 层：部门卡片网格 ----------
  const renderDepartments = () => (
    <div style={{ marginTop: 4 }}>
      <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
        共 {summary.length} 个部门，按部门查看知识库与文档；点击部门卡片进入详情
      </Text>
      <Row gutter={[14, 14]}>
        {summary.map(dept => (
          <Col key={dept.department_id} xs={24} sm={12} md={8} lg={6}>
            <Card className="gen-card" onClick={() => {
              setDeptKey(dept.department_id);
              setLevel('dept');
            }}>
              <div className="gen-card__head">
                <div className="gen-card__icon" style={{
                  background: 'linear-gradient(135deg, var(--brand-primary, #2563eb) 0%, var(--brand-primary-deep, #1d4ed8) 100%)',
                }}>
                  <ApartmentOutlined />
                </div>
                <Text strong ellipsis className="gen-card__name">
                  {dept.department_name}
                </Text>
              </div>
              <div className="gen-card__tags">
                <Tag color="blue">{dept.kb_count} 知识库</Tag>
                <Tag color="green">{dept.doc_count} 文档</Tag>
              </div>
              <div className="gen-card__footer">
                <Button
                  type="link"
                  size="small"
                  className="gen-card__enter"
                  onClick={() => {
                    setDeptKey(dept.department_id);
                    setLevel('dept');
                  }}
                >
                  查看详情 <LeftOutlined style={{ transform: 'rotate(180deg)', fontSize: 10 }} />
                </Button>
              </div>
            </Card>
          </Col>
        ))}
      </Row>
    </div>
  );

  // ---------- 第 2 层：部门详情(知识库卡片) ----------
  const renderDept = () => (
    <div style={{ marginTop: 4 }}>
      <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
        「{curDept?.department_name ?? ''}」下共 {curDept?.kb_count ?? 0} 个知识库，
        点击知识库卡片查看文档
      </Text>
      {curDept?.kbs.length === 0 ? (
        <AppEmpty title="暂无知识库" description="该部门还没有创建知识库" />
      ) : (
        <Row gutter={[14, 14]}>
          {(curDept?.kbs ?? []).map(kb => (
            <Col key={kb.kb_id} xs={24} sm={12} md={8} lg={6}>
              <Card className="gen-card" onClick={() => {
                setKbId(kb.kb_id);
                setLevel('docs');
              }}>
                <div className="gen-card__head">
                  <div className="gen-card__icon" style={{
                    background: 'linear-gradient(135deg, #0ea5e9 0%, #0284c7 100%)',
                  }}>
                    <BookOutlined />
                  </div>
                  <Text strong ellipsis className="gen-card__name" title={kb.kb_name}>
                    {kb.kb_name}
                  </Text>
                </div>
                <div className="gen-card__tags">
                  <Tag color="blue">{kb.doc_count} 文档</Tag>
                  <Tag color="cyan">{kb.chunk_count} 切块</Tag>
                </div>
                <div className="gen-card__footer">
                  <Button
                    type="link"
                    size="small"
                    className="gen-card__enter"
                    onClick={() => {
                      setKbId(kb.kb_id);
                      setLevel('docs');
                    }}
                  >
                    查看文档 <LeftOutlined style={{ transform: 'rotate(180deg)', fontSize: 10 }} />
                  </Button>
                </div>
              </Card>
            </Col>
          ))}
        </Row>
      )}
    </div>
  );

  // ---------- 第 3 层：文档列表（表格） ----------
  const columns: ColumnsType<GlobalDocumentItem> = [
    {
      title: '文件名',
      dataIndex: 'original_name',
      key: 'name',
      ellipsis: true,
      width: colWidths.name ?? 240,
      onHeaderCell: () => ({ width: colWidths.name ?? 240, onResize: handleResize('name'), title: '文件名' }),
      render: (v: string, row) => (
        <Typography.Link onClick={() => setPreviewDoc(row)} title="点击在线预览">
          {v}
        </Typography.Link>
      ),
    },
    {
      title: '类型',
      dataIndex: 'file_type',
      key: 'file_type',
      width: colWidths.file_type ?? 80,
      onHeaderCell: () => ({ width: colWidths.file_type ?? 80, onResize: handleResize('file_type'), title: '类型' }),
      render: (v: string) => <Tag>{v === 'url' ? '网页' : v || '-'}</Tag>,
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: colWidths.size ?? 90,
      onHeaderCell: () => ({ width: colWidths.size ?? 90, onResize: handleResize('size'), title: '大小' }),
      render: (v?: number) => (v == null ? '-' : formatSize(v)),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: colWidths.status ?? 100,
      onHeaderCell: () => ({ width: colWidths.status ?? 100, onResize: handleResize('status'), title: '状态' }),
      render: (status: DocumentStatus, row) => {
        const meta = statusMeta[status] ?? { color: 'default', text: status };
        // 解析中：状态标签内附已耗时（如「解析中 2:35」），与部门文档页同口径
        const elapsed = status === 'parsing' ? formatElapsed(row.updated_at) : '';
        const tag =
          status === 'parsing' ? (
            <Tag color={meta.color} icon={<Spin size="small" />}>
              {meta.text}{elapsed ? ` ${elapsed}` : ''}
            </Tag>
          ) : (
            <Tag color={meta.color}>{meta.text}</Tag>
          );
        // 解析中：悬浮展示实时阶段进度（与部门文档页同节奏拉取）
        if (status === 'parsing') {
          const prog = ingestProgress?.[row.id];
          return (
            <Tooltip
              title={
                `正在解析：${prog?.stage ?? '进行中'}` +
                (prog?.since ? `（${prog.since} 开始）` : '')
              }
            >
              {tag}
            </Tooltip>
          );
        }
        // 已入库/失败：悬浮展示入库全流程（各阶段耗时 + 总耗时；同 DocumentTable）
        const trace = Array.isArray(row.ingest_trace) ? row.ingest_trace : null;
        if ((status === 'ingested' || status === 'failed') && trace) {
          return (
            <Tooltip
              title={renderIngestTrace(trace, row.ingest_total_ms, status === 'failed',
                row.ingest_started_at, row.ingest_finished_at)}
            >
              {tag}
            </Tooltip>
          );
        }
        // 失败无 trace：走原错误 Tooltip
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
      width: colWidths.chunk_count ?? 80,
      onHeaderCell: () => ({ width: colWidths.chunk_count ?? 80, onResize: handleResize('chunk_count'), title: '切块数' }),
      render: (v: number, row) => (row.status === 'ingested' ? v : '-'),
    },
    {
      title: '解析方式',
      dataIndex: 'parser_id',
      key: 'parser_id',
      width: colWidths.parser_id ?? 120,
      onHeaderCell: () => ({ width: colWidths.parser_id ?? 120, onResize: handleResize('parser_id'), title: '解析方式' }),
      render: (v: string | undefined, row) => {
        if (!v) return <Text type="secondary">-</Text>;
        const tag = <Tag color={methodColor(v)}>{methodLabel(v)}</Tag>;
        const summary = parserConfigSummary(row.parser_config, v);
        return summary ? <Tooltip title={summary}>{tag}</Tooltip> : tag;
      },
    },
    {
      title: '上传时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: colWidths.created_at ?? 160,
      onHeaderCell: () => ({ width: colWidths.created_at ?? 160, onResize: handleResize('created_at'), title: '上传时间' }),
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 230,
      render: (_, row) => (
        <Space size="small">
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => {
              setDetailKbId(row.kb_id);
              void detailModal.openDetail(row);
            }}
          >
            切块预览
          </Button>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => setRenameDoc(row)}
          >
            重命名
          </Button>
          <Popconfirm
            title={`移入回收站「${row.original_name}」？`}
            description="文档将不再参与检索，可在该知识库的回收站恢复；彻底删除请在回收站操作"
            onConfirm={() => void handleDelete(row)}
            okText="移入回收站"
            cancelText="取消"
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const renderDocs = () => (
    <TableSectionLayout
      total={total}
      page={page}
      pageSize={pageSize}
      onPageChange={(p, ps) => {
        setPage(p);
        setPageSize(ps);
        void loadDocs(false, p, ps);
      }}
      toolbar={
        <>
          <Segmented
            value={statusFilter}
            onChange={v => setStatusFilter(v as StatusFilter)}
            options={buildStatusOptions(statusCounts)}
          />
          <Input.Search
            allowClear
            placeholder="搜索文件名"
            style={{ width: 200 }}
            value={keywordInput}
            onChange={e => setKeywordInput(e.target.value)}
            onSearch={v => setKeyword(v.trim())}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void loadDocs(false, page, pageSize)}>
            刷新
          </Button>
        </>
      }
      emptyOrLoading={
        loading && items.length === 0 ? (
          <Skeleton active paragraph={{ rows: 6 }} />
        ) : total === 0 ? (
          <AppEmpty
            title="暂无文档"
            description="当前筛选条件下没有文档，可调整状态/关键字筛选"
          />
        ) : undefined
      }
    >
      <Table
        size="small"
        dataSource={items}
        columns={columns}
        rowKey="id"
        pagination={false}
        components={{ header: { cell: ResizableTitle } }}
        scroll={{ x: tableWidth(columns) }}
        sticky
        className="table-zebra"
        onRow={row => ({
          style: { cursor: 'pointer' },
          onClick: () => setProfileDoc(row),
        })}
      />
    </TableSectionLayout>
  );

  return (
    <PageLayout
      breadcrumb={<Breadcrumb items={breadcrumbItems} />}
      // dept_admin 保留标题以区分部门视图（super_admin 有多级面包屑+卡片即可）
      title={isDeptAdmin ? '文档管理（本部门）' : undefined}
      description={
        isDeptAdmin
          ? '本部门视图：查看本部门知识库的文档，可重命名或移入回收站'
          : undefined
      }
    >
      {summaryLoading && level === 'departments' ? (
        <Card><Skeleton active paragraph={{ rows: 4 }} /></Card>
      ) : (
        level === 'departments'
          ? renderDepartments()
          : level === 'dept'
            ? renderDept()
            : renderDocs()
      )}

      {/* 文档档案弹窗：点击卡片打开 */}
      <DocumentProfileModal
        open={!!profileDoc}
        doc={profileDoc}
        onCancel={() => setProfileDoc(null)}
        extra={
          profileDoc && (
            <>
              <Button onClick={() => { setRenameDoc(profileDoc); setProfileDoc(null); }}>
                重命名
              </Button>
              <Button danger onClick={() => { void handleDelete(profileDoc as GlobalDocumentItem); setProfileDoc(null); }}>
                移入回收站
              </Button>
            </>
          )
        }
      />

      {/* 在线预览弹窗：点击文件名打开 */}
      <DocumentPreviewModal
        open={!!previewDoc}
        doc={previewDoc}
        kbId={previewDoc?.kb_id}
        onCancel={() => setPreviewDoc(null)}
      />

      {/* 切块详情弹窗：操作列"切块预览"打开 */}
      {detailModal.node}

      {/* 重命名弹窗（复用部门内文档管理组件，kbId 取文档所属知识库） */}
      <RenameDocumentModal
        open={!!renameDoc}
        doc={renameDoc}
        kbId={renameDoc?.kb_id}
        onCancel={() => setRenameDoc(null)}
        onSuccess={() => { void loadDocs(true); void loadSummary(true); }}
      />
    </PageLayout>
  );
};

export default GlobalDocumentsPage;
