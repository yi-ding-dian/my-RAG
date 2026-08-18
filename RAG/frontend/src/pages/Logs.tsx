import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  App as AntApp,
  Button,
  Card,
  DatePicker,
  Input,
  List,
  Pagination,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import {
  ArrowLeftOutlined,
  ClearOutlined,
  DeleteOutlined,
  DownloadOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { Dayjs } from 'dayjs';
import {
  asApiError,
  AuditActionOption,
  AuditLog,
  AuditLogQuery,
  LogFileInfo,
  LogLine,
  deleteAllLogFiles,
  deleteAuditLogsByDate,
  deleteLogFile,
  downloadLogFile,
  listAuditActions,
  listAuditLogs,
  listLogFiles,
  tailSystemLogs,
} from '../api/client';
import AppEmpty from '../components/AppEmpty';
import PageHeader from '../components/PageHeader';
import ResizableTitle from '../components/ResizableTitle';
import { useAuth } from '../auth/AuthContext';
import type { ResizeCallbackData } from 'react-resizable';

const { Text } = Typography;
const { RangePicker } = DatePicker;

/** 系统日志保留行数上限（超出丢最旧） */
const MAX_LOG_LINES = 500;
/** 每次 tail 拉取行数（与后端默认一致） */
const TAIL_LIMIT = 200;
/** 轮询间隔（毫秒） */
const POLL_INTERVAL = 5000;
/** 日志/审计默认查看天数（最近 7 天） */
const VIEW_DAYS = 7;

/** 角色 → 颜色/文案（与 Users.tsx 一致） */
const roleMeta: Record<string, { color: string; text: string }> = {
  super_admin: { color: 'red', text: '超级管理员' },
  dept_admin: { color: 'blue', text: '部门管理员' },
  user: { color: 'default', text: '普通用户' },
};

/** 目标类型 → 中文（与 Users.tsx 一致） */
const targetTypeLabelMap: Record<string, string> = {
  user: '用户',
  dept: '部门',
  kb: '知识库',
  doc: '文档',
  chat: '会话',
  config: '配置',
};

/** 日志级别 → Tag 颜色（INFO 蓝 / WARNING 橙 / ERROR 红 / DEBUG 灰，非标准行默认灰） */
const levelColor = (level: string | null): string => {
  switch (level) {
    case 'INFO':
      return 'blue';
    case 'WARNING':
      return 'orange';
    case 'ERROR':
      return 'red';
    case 'DEBUG':
      return 'default';
    default:
      return 'default';
  }
};

const LEVEL_OPTIONS = ['INFO', 'WARNING', 'ERROR', 'DEBUG'].map(l => ({
  value: l,
  label: l,
}));

/** 文件大小格式化（B/KB/MB） */
const formatBytes = (bytes: number) => {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
};

/** 最近 7 天日期（YYYY-MM-DD，旧→新） */
const recentDates = (): string[] => {
  const out: string[] = [];
  for (let i = VIEW_DAYS - 1; i >= 0; i--) {
    out.push(dayjs().subtract(i, 'day').format('YYYY-MM-DD'));
  }
  return out;
};

/**
 * 日志查看页（super_admin 全量 / dept_admin 仅操作审计）：
 * - Tab1 操作审计：样式/筛选/分页与 Users.tsx 审计 Tab 一致；时间范围默认最近
 *   7 天；5s 自动轮询刷新当前页（「自动刷新」开关默认开）；按天删除审计记录仅
 *   super_admin 可见（dept_admin 只读，数据已由后端限本部门人员）
 * - Tab2 系统运行日志（仅 super_admin）：按天文件（data/logs/kb-YYYY-MM-DD.log，
 *   最近 7 天可切换），tail 字节游标增量 + 5s 轮询；倒序最新在上；关键字/级别
 *   过滤；「清空」清前端缓存并归位游标；「暂停/继续」开关；日志文件管理
 *   （大小/合计/删单天/清空全部，删除前二次确认）
 */
const LogsPage: React.FC = () => {
  const app = AntApp.useApp();
  const { user } = useAuth();
  const [tab, setTab] = useState('audit');
  // 系统日志 Tab 仅 super_admin（dept_admin 无运行日志查看权限，接口 403）
  const isSuperAdmin = user?.role === 'super_admin';

  // 页面固定撑满视口（Content 上下 padding 24×2，与 Chat.tsx 同款布局）：
  // 页头/Tab/筛选栏固定不动，内容区在内部滚动（flex 链见 index.css .logs-page-tabs）
  return (
    <div
      style={{
        height: 'calc(100vh - 48px)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      <PageHeader
        title="日志查看"
        description={
          isSuperAdmin
            ? '超管实时查看：操作审计与系统运行日志（5 秒自动轮询，默认最近 7 天）'
            : '本部门人员操作审计（5 秒自动轮询，默认最近 7 天）'
        }
        style={{ flexShrink: 0 }}
      />
      <Card
        style={{
          flex: 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
        styles={{ body: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' } }}
      >
        <Tabs
          className="logs-page-tabs"
          activeKey={tab}
          onChange={setTab}
          style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
          items={
            isSuperAdmin
              ? [
                  { key: 'audit', label: '操作审计', children: <AuditTab app={app} /> },
                  { key: 'system', label: '系统日志', children: <SystemLogTab app={app} /> },
                ]
              : [
                  { key: 'audit', label: '操作审计', children: <AuditTab app={app} /> },
                ]
          }
        />
      </Card>
    </div>
  );
};

/** 统一的 App.useApp 实例类型（message/modal） */
type AppInstance = ReturnType<typeof AntApp.useApp>;

// ==================== Tab1：操作审计（与 Users.tsx 审计 Tab 一致） ====================

const AuditTab: React.FC<{ app: AppInstance }> = ({ app }) => {
  const { message, modal } = app;
  const { user } = useAuth();
  // 按天删除审计记录仅超管（dept_admin 只读：后端 DELETE 仍 403）
  const canDeleteAudit = user?.role === 'super_admin';

  const [actionOptions, setActionOptions] = useState<AuditActionOption[]>([]);
  const [auditFilters, setAuditFilters] = useState<{
    action?: string;
    username?: string;
    timeRange?: [Dayjs, Dayjs] | null;
  }>({
    // 默认最近 7 天（不强制，用户可改）
    timeRange: [dayjs().subtract(VIEW_DAYS - 1, 'day').startOf('day'), dayjs().endOf('day')],
  });
  const [autoRefresh, setAutoRefresh] = useState(true);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState<AuditLog[]>([]);
  const [loading, setLoading] = useState(false);
  // 按天删除审计记录
  const [delDate, setDelDate] = useState<Dayjs | null>(null);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const params: AuditLogQuery = {
        page,
        page_size: pageSize,
        action: auditFilters.action || undefined,
        username: auditFilters.username?.trim() || undefined,
      };
      if (auditFilters.timeRange?.[0] && auditFilters.timeRange[1]) {
        params.start_time = auditFilters.timeRange[0].format('YYYY-MM-DD HH:mm:ss');
        params.end_time = auditFilters.timeRange[1].format('YYYY-MM-DD HH:mm:ss');
      }
      const res = await listAuditLogs(params);
      setItems(res.data.items);
      setTotal(res.data.total);
    } catch {
      if (!silent) message.error('加载审计日志失败');
    } finally {
      if (!silent) setLoading(false);
    }
  }, [page, pageSize, auditFilters, message]);

  // 首次挂载：操作类型下拉（首屏审计数据由下方 [load] effect 加载）
  useEffect(() => {
    void listAuditActions()
      .then(res => setActionOptions(res.data.actions))
      .catch(() => message.error('加载操作类型列表失败'));
  }, [message]);

  // 筛选/页码/每页条数变化 → 自动重新加载（与 Users.tsx 审计 Tab 行为一致）
  useEffect(() => {
    void load(false);
  }, [load]);

  // 5s 自动轮询：只静默刷新当前页（不打断翻页）；关开关即停
  const loadRef = useRef(load);
  useEffect(() => { loadRef.current = load; }, [load]);
  useEffect(() => {
    if (!autoRefresh) return;
    const timer = setInterval(() => { void loadRef.current(true); }, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [autoRefresh]);

  // 查询：筛选条件新引用 + 回第 1 页（[load] effect 驱动重新加载）
  const handleSearch = () => {
    setAuditFilters(prev => ({ ...prev }));
    setPage(1);
  };
  const handleReset = () => {
    setAuditFilters({
      timeRange: [dayjs().subtract(VIEW_DAYS - 1, 'day').startOf('day'), dayjs().endOf('day')],
    });
    setPage(1);
  };

  // 按天删除审计记录（删除前二次确认）
  const handleDeleteByDate = () => {
    if (!delDate) {
      message.warning('请先选择要删除的日期');
      return;
    }
    const date = delDate.format('YYYY-MM-DD');
    modal.confirm({
      title: `删除 ${date} 的全部审计记录？`,
      content: '将删除该天所有用户的关键操作记录，删除后不可恢复。',
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await deleteAuditLogsByDate(date);
          message.success(res.data.message);
          setDelDate(null);
          void load(false);
        } catch (e: unknown) {
          message.error(asApiError(e).response?.data?.detail || '删除失败');
        }
      },
    });
  };

  const actionLabelMap = useMemo(() => {
    const m: Record<string, string> = {};
    for (const a of actionOptions) m[a.action] = a.label;
    return m;
  }, [actionOptions]);

  // 列宽拖拽：拖拽后的宽度存在 colWidths（按列 key），未拖过的列用初始 width
  const [colWidths, setColWidths] = useState<Record<string, number>>({});
  const handleResize = useCallback(
    (key: React.Key) =>
      (_: React.SyntheticEvent<Element>, { size }: ResizeCallbackData) => {
        setColWidths(prev => ({ ...prev, [String(key)]: size.width }));
      },
    [],
  );

  const columns = useMemo<ColumnsType<AuditLog>>(() => [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: colWidths.created_at ?? 170,
      onHeaderCell: () => ({
        width: colWidths.created_at ?? 170,
        onResize: handleResize('created_at'),
      }),
      render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm:ss'),
    },
    {
      title: '用户',
      dataIndex: 'username',
      key: 'username',
      width: colWidths.username ?? 130,
      onHeaderCell: () => ({
        width: colWidths.username ?? 130,
        onResize: handleResize('username'),
      }),
      render: (v: string) => (v ? <Text strong>{v}</Text> : <Text type="secondary">未认证</Text>),
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: colWidths.role ?? 110,
      onHeaderCell: () => ({
        width: colWidths.role ?? 110,
        onResize: handleResize('role'),
      }),
      render: (role: string) => {
        const meta = roleMeta[role];
        return meta ? <Tag color={meta.color}>{meta.text}</Tag> : <Tag>{role || '-'}</Tag>;
      },
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: colWidths.action ?? 150,
      onHeaderCell: () => ({
        width: colWidths.action ?? 150,
        onResize: handleResize('action'),
      }),
      render: (a: string) => <Tag color="blue">{actionLabelMap[a] ?? a}</Tag>,
    },
    {
      title: '目标',
      key: 'target',
      width: colWidths.target ?? 200,
      onHeaderCell: () => ({
        width: colWidths.target ?? 200,
        onResize: handleResize('target'),
      }),
      ellipsis: true,
      render: (_, row) => {
        const type = targetTypeLabelMap[row.target_type ?? ''] ?? row.target_type ?? '';
        return row.target_name
          ? `${type ? `${type} · ` : ''}${row.target_name}`
          : (type || '-');
      },
    },
    {
      title: '详情',
      key: 'detail',
      width: colWidths.detail ?? 420,
      onHeaderCell: () => ({
        width: colWidths.detail ?? 420,
        onResize: handleResize('detail'),
      }),
      ellipsis: true,
      render: (_, row) => (row.detail ? row.detail.slice(0, 80) : '-'),
    },
    {
      title: 'IP',
      dataIndex: 'ip',
      key: 'ip',
      width: colWidths.ip ?? 140,
      onHeaderCell: () => ({
        width: colWidths.ip ?? 140,
        onResize: handleResize('ip'),
      }),
      render: (v: string) => v || '-',
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      // 最后一列不提供拖拽手柄（避免拖出表格边界）
      width: colWidths.status ?? 90,
      render: (s: string) =>
        s === 'success' ? <Tag color="green">成功</Tag> : <Tag color="red">失败</Tag>,
    },
  ], [actionLabelMap, colWidths, handleResize]);

  // 表格总宽 = 当前各列宽度之和（跟随列宽拖拽动态变化）。
  // 必须让 scroll.x === 总宽：antd 表格为 fixed 布局，scroll.x > 总宽时浏览器会按
  // 比例放大各列渲染宽度，拖拽中比例随总宽变化 → 列实际位移量 ≠ 鼠标位移量，
  // 导致列宽拖拽严重漂移（实测拖 -100 实际变 -145）。动态对齐后缩放比例恒为 1。
  const tableWidth = useMemo(
    () => columns.reduce((s, c) => s + ((c.width as number) || 0), 0),
    [columns],
  );

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <Space wrap style={{ marginBottom: 16, flexShrink: 0 }}>
        <Switch
          size="small"
          checked={autoRefresh}
          onChange={setAutoRefresh}
          checkedChildren="自动刷新"
          unCheckedChildren="已暂停"
        />
        <Select
          allowClear
          placeholder="操作类型"
          style={{ width: 170 }}
          value={auditFilters.action}
          onChange={v => setAuditFilters(prev => ({ ...prev, action: v }))}
          options={actionOptions.map(a => ({ value: a.action, label: a.label }))}
        />
        <Input
          allowClear
          placeholder="用户名"
          style={{ width: 140 }}
          value={auditFilters.username}
          onChange={e => setAuditFilters(prev => ({ ...prev, username: e.target.value }))}
        />
        <RangePicker
          showTime={{ format: 'HH:mm:ss' }}
          format="YYYY-MM-DD HH:mm:ss"
          value={auditFilters.timeRange}
          onChange={v => setAuditFilters(prev => ({
            ...prev,
            timeRange: v as [Dayjs, Dayjs] | null,
          }))}
        />
        <Button type="primary" onClick={handleSearch}>查询</Button>
        <Button onClick={handleReset}>重置</Button>
        {canDeleteAudit && (
          <>
            <DatePicker
              value={delDate}
              onChange={setDelDate}
              placeholder="选择删除日期"
              style={{ width: 150 }}
            />
            <Button danger icon={<DeleteOutlined />} onClick={handleDeleteByDate}>删除该天</Button>
          </>
        )}
      </Space>
      <Table
        size="middle"
        rowKey="id"
        columns={columns}
        dataSource={items}
        loading={loading}
        pagination={false}
        // 表头单元格替换为 ResizableTitle：列头右侧出现拖拽手柄，可自由调整列宽
        components={{ header: { cell: ResizableTitle } }}
        // x = 当前列宽总和（动态，见上方 tableWidth 注释）：保证拖拽精确且
        // 总宽超出容器宽度时出现横向滚动
        // y 用 calc 相对视口计算：扣除 Content padding(48) + 页头(72) + Card body
        // padding(48) + Tab 头(56) + 筛选栏(48) + 表头(39) + 分页(48) 后，剩余高度
        // 给表格 body 内部滚动（分页器与筛选栏固定）
        scroll={{ x: tableWidth, y: 'calc(100vh - 365px)' }}
        locale={{
          emptyText: <AppEmpty title="暂无审计记录" description="尚无符合条件的关键操作记录" />,
        }}
        className="table-zebra"
      />
      <div style={{ marginTop: 16, textAlign: 'right', flexShrink: 0 }}>
        <Pagination
          current={page}
          pageSize={pageSize}
          total={total}
          showSizeChanger
          pageSizeOptions={[10, 20, 50]}
          showTotal={t => `共 ${t} 条`}
          onChange={(p, ps) => {
            setPage(p);
            setPageSize(ps);
          }}
        />
      </div>
    </div>
  );
};

// ==================== Tab2：系统运行日志（按天文件） ====================

const SystemLogTab: React.FC<{ app: AppInstance }> = ({ app }) => {
  const { message, modal } = app;

  const today = dayjs().format('YYYY-MM-DD');
  const [selectedDate, setSelectedDate] = useState(today);
  const [files, setFiles] = useState<LogFileInfo[]>([]);
  const [lines, setLines] = useState<LogLine[]>([]); // 正序（旧→新），渲染时倒序
  const offsetRef = useRef<number>(-1); // 字节游标（-1 = 尾部模式，首次加载/切换日期用）
  const [keyword, setKeyword] = useState('');
  const [levelFilter, setLevelFilter] = useState<string[]>([]);
  const [paused, setPaused] = useState(false); // true = 暂停自动刷新
  // 文件详情视图：view='detail' 时展示该天文件内容（点击文件名进入）
  const [view, setView] = useState<'list' | 'detail'>('list');
  const [detailFile, setDetailFile] = useState<LogFileInfo | null>(null);
  const [detailLines, setDetailLines] = useState<LogLine[]>([]); // 正序（旧→新）
  const [detailLoading, setDetailLoading] = useState(false);

  // 最近 7 天日期下拉（倒序：今天在最上）
  const dateOptions = useMemo(() => recentDates().reverse().map(d => ({ value: d, label: d })), []);

  const loadFiles = useCallback(async () => {
    try {
      const res = await listLogFiles();
      setFiles(res.data.files);
    } catch {
      // 静默（文件列表刷新失败不影响日志查看）
    }
  }, []);

  const fetchTail = useCallback(async (initial = false) => {
    try {
      const res = await tailSystemLogs(selectedDate, initial ? -1 : offsetRef.current, TAIL_LIMIT);
      offsetRef.current = res.data.offset;
      if (res.data.lines.length > 0) {
        setLines(prev => {
          const merged = [...prev, ...res.data.lines];
          return merged.length > MAX_LOG_LINES
            ? merged.slice(merged.length - MAX_LOG_LINES)
            : merged;
        });
      }
    } catch {
      if (initial) message.error('加载系统日志失败');
    }
  }, [selectedDate, message]);

  // 挂载：文件列表（今天日志尾部由下方 [selectedDate] effect 加载）
  useEffect(() => {
    void loadFiles();
  }, [loadFiles]);

  // 挂载 + 切换日期：清缓存、游标归位、重新加载
  useEffect(() => {
    setLines([]);
    offsetRef.current = -1;
    void fetchTail(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate]);

  // 5s 轮询：拉新行 + 刷新文件列表（暂停即停）
  const fetchTailRef = useRef(fetchTail);
  useEffect(() => { fetchTailRef.current = fetchTail; }, [fetchTail]);
  const loadFilesRef = useRef(loadFiles);
  useEffect(() => { loadFilesRef.current = loadFiles; }, [loadFiles]);
  useEffect(() => {
    if (paused) return;
    const timer = setInterval(() => {
      void fetchTailRef.current(false);
      void loadFilesRef.current();
    }, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [paused]);

  // 清空日志行缓存：清前端缓存 + 游标归位（下次拉取回到尾部最近行）
  const clearLines = () => {
    setLines([]);
    offsetRef.current = -1;
  };

  // 删除指定天日志文件（删除前二次确认）
  const handleDeleteFile = (date: string) => {
    modal.confirm({
      title: `删除 ${date} 的日志文件？`,
      content: '删除后该天运行日志不可恢复。',
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await deleteLogFile(date);
          message.success(res.data.message);
          if (selectedDate === date) {
            setLines([]);
            offsetRef.current = -1;
            void fetchTailRef.current(true);
          }
          await loadFiles();
        } catch (e: unknown) {
          message.error(asApiError(e).response?.data?.detail || '删除失败');
        }
      },
    });
  };

  // 清空全部日志文件（删除前二次确认）
  const handleClearAll = () => {
    modal.confirm({
      title: '清空全部运行日志？',
      content: '将删除所有历史日志文件（今天的日志清空后继续写入），删除后不可恢复。',
      okText: '确认清空',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await deleteAllLogFiles();
          message.success(res.data.message);
          setLines([]);
          offsetRef.current = -1;
          await loadFiles();
        } catch (e: unknown) {
          message.error(asApiError(e).response?.data?.detail || '删除失败');
        }
      },
    });
  };

  // 点击文件名 → 进入详情视图：tail 尾部模式一次拉取该天最近 2000 行
  // （文件 ≤10MB，2000 行够日常查看；满额即视为截断，提示下载完整文件）
  const loadDetail = useCallback(async (file: LogFileInfo) => {
    setView('detail');
    setDetailFile(file);
    setDetailLines([]);
    setDetailLoading(true);
    try {
      const res = await tailSystemLogs(file.date, -1, 2000);
      setDetailLines(res.data.lines);
    } catch {
      message.error('加载日志文件内容失败');
    } finally {
      setDetailLoading(false);
    }
  }, [message]);

  // 返回文件管理列表（详情数据清空，列表侧轮询数据不受影响）
  const backToList = () => {
    setView('list');
    setDetailFile(null);
    setDetailLines([]);
  };

  // 下载日志文件：fetch 带鉴权头取 Blob → 本地触发浏览器下载
  const handleDownload = async (file: LogFileInfo) => {
    try {
      const blob = await downloadLogFile(file.date);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = file.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      message.success(`已开始下载 ${file.filename}`);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || asApiError(e).message || '下载失败');
    }
  };

  // 文件管理：只展示最近 7 天（按日期倒序）
  const visibleFiles = useMemo(() => {
    const cutoff = dayjs().subtract(VIEW_DAYS - 1, 'day').format('YYYY-MM-DD');
    return files.filter(f => f.date >= cutoff);
  }, [files]);
  const totalBytes = visibleFiles.reduce((s, f) => s + f.size_bytes, 0);

  const fileColumns: ColumnsType<LogFileInfo> = [
    { title: '日期', dataIndex: 'date', key: 'date', width: 120 },
    {
      title: '文件',
      dataIndex: 'filename',
      key: 'filename',
      ellipsis: true,
      // 点击文件名 → 进入详情视图查看该天日志内容
      render: (v: string, row: LogFileInfo) => (
        <Typography.Link onClick={() => void loadDetail(row)}>{v}</Typography.Link>
      ),
    },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      key: 'size_bytes',
      width: 110,
      render: (v: number) => formatBytes(v),
    },
    {
      title: '修改时间',
      dataIndex: 'mtime',
      key: 'mtime',
      width: 180,
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_, row) => (
        <Space size={8}>
          <Button size="small" icon={<DownloadOutlined />} onClick={() => void handleDownload(row)}>
            下载
          </Button>
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDeleteFile(row.date)}>
            删除
          </Button>
        </Space>
      ),
    },
  ];

  // 过滤（仅作用已加载行）+ 倒序（最新在上）
  const filteredLines = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    const out = lines.filter(l =>
      (!levelFilter.length || (l.level != null && levelFilter.includes(l.level)))
      && (!kw || l.line.toLowerCase().includes(kw)));
    return [...out].reverse();
  }, [lines, keyword, levelFilter]);

  // ===== 详情视图：查看单天日志文件内容（正序时间从上到下，行渲染与列表一致） =====
  if (view === 'detail' && detailFile) {
    const file = detailFile; // 局部 const：闭包内保留非空窄化
    // tail 返回最近 limit 行，拉满 2000 行 = 文件行数更多被截断
    const truncated = detailLines.length >= 2000;
    return (
      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {/* 页头：返回 + 文件名 + 大小（固定不随内容滚动） */}
        <Space wrap style={{ marginBottom: 16, flexShrink: 0 }} size={12}>
          <Button icon={<ArrowLeftOutlined />} onClick={backToList}>返回</Button>
          <Text strong style={{ fontSize: 14 }}>{file.filename}</Text>
          <Text type="secondary">大小 {formatBytes(file.size_bytes)}</Text>
          <Text type="secondary">修改于 {dayjs(file.mtime).format('YYYY-MM-DD HH:mm:ss')}</Text>
          {truncated && (
            <Text type="secondary">
              该文件超过 2000 行，仅显示最近 2000 行，完整内容可点击「下载」
            </Text>
          )}
          <Button
            size="small"
            type="primary"
            icon={<DownloadOutlined />}
            onClick={() => void handleDownload(file)}
          >
            下载
          </Button>
        </Space>
        {/* 内容区：flex 撑满剩余高度，滚动条在内部（与列表视图一致） */}
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          {detailLoading ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '48px 0' }}>
              <Spin size="large" />
            </div>
          ) : (
            <List
              size="small"
              dataSource={detailLines}
              locale={{
                emptyText: (
                  <AppEmpty
                    title="暂无日志内容"
                    description={`${file.date} 无日志内容`}
                  />
                ),
              }}
              renderItem={item => (
                <List.Item style={{ padding: '4px 0' }}>
                  <Space size={10} align="start" style={{ width: '100%' }}>
                    <Tag
                      color={levelColor(item.level)}
                      style={{ minWidth: 68, textAlign: 'center', marginInlineEnd: 0 }}
                    >
                      {item.level ?? 'LOG'}
                    </Tag>
                    <Text type="secondary" style={{ whiteSpace: 'nowrap', fontSize: 12 }}>
                      {item.ts ?? ''}
                    </Text>
                    <Text style={{ wordBreak: 'break-all', fontSize: 13 }}>{item.message}</Text>
                  </Space>
                </List.Item>
              )}
            />
          )}
        </div>
      </div>
    );
  }

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <Space wrap style={{ marginBottom: 16, flexShrink: 0 }}>
        <Select
          value={selectedDate}
          onChange={setSelectedDate}
          options={dateOptions}
          style={{ width: 150 }}
        />
        <Switch
          size="small"
          checked={!paused}
          onChange={v => setPaused(!v)}
          checkedChildren="自动刷新"
          unCheckedChildren="已暂停"
        />
        <Input
          allowClear
          placeholder="关键字过滤（当前已加载行）"
          style={{ width: 200 }}
          value={keyword}
          onChange={e => setKeyword(e.target.value)}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder="级别过滤"
          style={{ minWidth: 190 }}
          value={levelFilter}
          onChange={setLevelFilter}
          options={LEVEL_OPTIONS}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void fetchTail(false)}>刷新</Button>
        <Button icon={<ClearOutlined />} onClick={clearLines}>清空</Button>
        <Text type="secondary">
          已加载 {lines.length} 行{levelFilter.length || keyword.trim()
            ? `（当前显示 ${filteredLines.length} 行）` : ''}，保留最近 {MAX_LOG_LINES} 行
        </Text>
      </Space>

      {/* 日志文件管理（最近 7 天，空间展示 + 删除）——固定不随列表滚动 */}
      <Card
        size="small"
        title="日志文件管理"
        style={{ marginBottom: 16, flexShrink: 0 }}
        extra={
          <Space size={12}>
            <Text type="secondary">合计占用 {formatBytes(totalBytes)}</Text>
            <Button size="small" danger icon={<DeleteOutlined />} onClick={handleClearAll}>
              清空全部
            </Button>
          </Space>
        }
      >
        {visibleFiles.length === 0 ? (
          <Text type="secondary">最近 {VIEW_DAYS} 天无运行日志文件</Text>
        ) : (
          <Table
            size="small"
            rowKey="filename"
            columns={fileColumns}
            dataSource={visibleFiles}
            pagination={false}
          />
        )}
      </Card>

      {/* 日志行列表：flex 撑满剩余高度，滚动条在列表内部（页头/Tab/筛选/文件管理固定） */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        <List
          size="small"
          dataSource={filteredLines}
        locale={{
          emptyText: (
            <AppEmpty
              title="暂无日志"
              description={paused ? '已暂停自动刷新，点击「刷新」手动拉取' : `${selectedDate} 无日志内容`}
            />
          ),
        }}
        renderItem={item => (
          <List.Item style={{ padding: '4px 0' }}>
            <Space size={10} align="start" style={{ width: '100%' }}>
              <Tag
                color={levelColor(item.level)}
                style={{ minWidth: 68, textAlign: 'center', marginInlineEnd: 0 }}
              >
                {item.level ?? 'LOG'}
              </Tag>
              <Text type="secondary" style={{ whiteSpace: 'nowrap', fontSize: 12 }}>
                {item.ts ?? ''}
              </Text>
              <Text style={{ wordBreak: 'break-all', fontSize: 13 }}>{item.message}</Text>
            </Space>
          </List.Item>
        )}
        />
      </div>
    </div>
  );
};

export default LogsPage;
