import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, DatePicker, Input, Select, Space, Switch, Table, Tag, Typography } from 'antd';
import { DeleteOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { Dayjs } from 'dayjs';
import type { ResizeCallbackData } from 'react-resizable';
import {
  AuditActionOption,
  AuditLog,
  AuditLogQuery,
  asApiError,
  deleteAuditLogsByDate,
  listAuditActions,
  listAuditLogs,
} from '../../api/client';
import AppEmpty from '../../components/common/AppEmpty';
import ResizableTitle from '../../components/common/ResizableTitle';
import LeftPagination from '../../components/layout/LeftPagination';
import { useAuth } from '../../auth/AuthContext';
import { POLL_INTERVAL, VIEW_DAYS, roleMeta, targetTypeLabelMap } from './shared';
import type { AppInstance } from './shared';

const { Text } = Typography;
const { RangePicker } = DatePicker;

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
        title: '时间',
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
        title: '用户',
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
        title: '角色',
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
        title: '操作',
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
        title: '目标',
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
        title: '详情',
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
        title: 'IP',
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
      {/* Table 区撑满剩余（表头 sticky + body 自身滚动），删除 scroll.y 魔数 */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
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
          // 总宽超出容器宽度时出现横向滚动。纵向滚动交由外层 flex:1
          // 容器 + 表头 sticky 处理（跟随窗口高度，无魔数误差）
          scroll={{ x: tableWidth }}
          sticky
          locale={{
            emptyText: <AppEmpty title="暂无审计记录" description="尚无符合条件的关键操作记录" />,
          }}
          className="table-zebra"
        />
      </div>
      {/* 分页条：统一 LeftPagination（固定左下） */}
      <LeftPagination
        page={page}
        pageSize={pageSize}
        total={total}
        onChange={(p, ps) => {
          setPage(p);
          setPageSize(ps);
        }}
      />
    </div>
  );
};

export default AuditTab;
