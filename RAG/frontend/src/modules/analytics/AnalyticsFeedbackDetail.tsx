/**
 * 用户反馈详情页（/analytics/feedback）：统计分析概览页点击「用户反馈」摘要卡下钻。
 * - 汇总统计（总数/好评/差评）+ 反馈记录表：时间/用户/部门/知识库/评价/原因/关联会话
 * - 服务端分页与筛选：用户名模糊 / 部门 / 关键词（点踩原因）/ 时间范围 / 点赞点踩，
 *   多条件 AND。必须走服务端——前端分页只能筛最近若干条，搜索会变成假搜索
 * - 用户名/部门名/知识库名由后端 join 回填，前端不再补拉用户与知识库列表
 * - 「关联会话」可点：打开抽屉回溯问答现场（用户已删除的会话读归档，仍可回看）
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Breadcrumb } from 'antd';
import { useNavigate } from 'react-router-dom';
import {
  Button, Card, DatePicker, Input, Segmented, Select, Skeleton, Space, Table, Tag,
  Tooltip, Typography,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import type { Dayjs } from 'dayjs';
import {
  getChatFeedbackStats, listChatFeedbackLogs, listDepartments,
  type ChatFeedbackLogItem, type ChatFeedbackStats, type Department,
} from '../../shared/api/client';
import { useAuth } from '../../shared/auth/AuthContext';
import AppEmpty from '../../shared/components/common/AppEmpty';
import PageLayout from '../../shared/components/layout/PageLayout';
import TableSectionLayout from '../../shared/components/layout/TableSectionLayout';
import ResizableTitle from '../../shared/components/common/ResizableTitle';
import { useResizableColumns } from '../../shared/hooks/useResizableColumns';
import SessionReplayDrawer from './SessionReplayDrawer';

const { Text } = Typography;
const { RangePicker } = DatePicker;

type RatingFilter = 'all' | 'up' | 'down';

interface Filters {
  username: string;
  departmentId?: string;
  keyword: string;
  rating: RatingFilter;
  dateRange: [Dayjs, Dayjs] | null;
}

const EMPTY_FILTERS: Filters = {
  username: '',
  departmentId: undefined,
  keyword: '',
  rating: 'all',
  dateRange: null,
};

const AnalyticsFeedbackDetailPage: React.FC = () => {
  const { user } = useAuth();
  const navigate = useNavigate();

  // 列宽拖拽：拖拽后的列宽存 colWidths（按列 key），scroll.x 动态对齐列宽和
  const { colWidths, handleResize, tableWidth } =
    useResizableColumns<ChatFeedbackLogItem>();

  const [stats, setStats] = useState<ChatFeedbackStats | null>(null);
  const [items, setItems] = useState<ChatFeedbackLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  // 加载失败（含无权限：该接口仅 super_admin，dept_admin/普通用户后端拒绝）
  const [loadError, setLoadError] = useState(false);

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);

  // draft = 输入中的条件，applied = 已提交查询的条件：点「查询」才生效，
  // 避免每敲一个字就打一次接口；点赞/点踩切换即时生效（与原有交互一致）
  const [draft, setDraft] = useState<Filters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<Filters>(EMPTY_FILTERS);

  const [departments, setDepartments] = useState<Department[]>([]);

  // 回溯抽屉：记录要回看的会话、消息下标与该条反馈（null = 关闭）
  const [replay, setReplay] = useState<{
    sessionId: string;
    msgIdx: number;
    rating: string;
    reason: string;
  } | null>(null);

  const loadStats = useCallback(async () => {
    try {
      const res = await getChatFeedbackStats();
      setStats(res.data);
    } catch {
      // 汇总失败不阻断列表（描述区退化为加载中/空）
      setStats(null);
    }
  }, []);

  const loadList = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const res = await listChatFeedbackLogs({
        page,
        page_size: pageSize,
        ...(applied.rating !== 'all' ? { rating: applied.rating } : {}),
        ...(applied.username.trim() ? { username: applied.username.trim() } : {}),
        ...(applied.departmentId ? { department_id: applied.departmentId } : {}),
        ...(applied.keyword.trim() ? { keyword: applied.keyword.trim() } : {}),
        ...(applied.dateRange
          ? {
              date_from: applied.dateRange[0].format('YYYY-MM-DD'),
              date_to: applied.dateRange[1].format('YYYY-MM-DD'),
            }
          : {}),
      });
      setItems(res.data.items || []);
      setTotal(res.data.total || 0);
    } catch {
      setItems([]);
      setTotal(0);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, applied]);

  useEffect(() => {
    loadList();
  }, [loadList]);

  useEffect(() => {
    loadStats();
  }, [loadStats]);

  // 部门下拉数据源（失败静默：筛选项拿不到不影响主列表）
  useEffect(() => {
    listDepartments()
      .then(res => setDepartments(res.data || []))
      .catch(() => setDepartments([]));
  }, []);

  const handleSearch = () => {
    setApplied(draft);
    setPage(1);
  };

  const handleReset = () => {
    setDraft(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
    setPage(1);
  };

  // 点赞/点踩：即时生效（沿用原 Segmented 的交互习惯）
  const handleRating = (v: RatingFilter) => {
    const next = { ...draft, rating: v };
    setDraft(next);
    setApplied(next);
    setPage(1);
  };

  const refreshAll = () => {
    loadList();
    loadStats();
  };

  const hasFilter = useMemo(
    () => Boolean(applied.rating !== 'all' || applied.username.trim()
      || applied.departmentId || applied.keyword.trim() || applied.dateRange),
    [applied],
  );

  const columns = [
    {
      title: '时间', dataIndex: 'created_at', key: 'created_at',
      width: colWidths.created_at ?? 160,
      onHeaderCell: () => ({ width: colWidths.created_at ?? 160, onResize: handleResize('created_at'), title: '时间' }),
      render: (v: string) => v || '—',
    },
    {
      title: '用户', dataIndex: 'user_id', key: 'user_id', ellipsis: true,
      width: colWidths.user_id ?? 120,
      onHeaderCell: () => ({ width: colWidths.user_id ?? 120, onResize: handleResize('user_id'), title: '用户' }),
      render: (_v: string, r: ChatFeedbackLogItem) => {
        const name = r.display_name || r.username;
        // 用户已注销：后端回填空串 → 显式标注而非留空白，避免看起来像数据缺失
        return name
          ? <Tooltip title={r.username}><span>{name}</span></Tooltip>
          : (
            <Tooltip title={`用户 ${r.user_id} 已注销`}>
              <Text type="secondary">已注销</Text>
            </Tooltip>
          );
      },
    },
    {
      title: '部门', dataIndex: 'department_id', key: 'department_id', ellipsis: true,
      width: colWidths.department_id ?? 120,
      onHeaderCell: () => ({ width: colWidths.department_id ?? 120, onResize: handleResize('department_id'), title: '部门' }),
      // 无部门（直属全局）与部门已删除都回填空串 → 统一显示 —
      render: (_v: string | null, r: ChatFeedbackLogItem) => (
        r.department_name || <Text type="secondary">—</Text>
      ),
    },
    {
      title: '知识库', dataIndex: 'kb_id', key: 'kb_id', ellipsis: true,
      width: colWidths.kb_id ?? 140,
      onHeaderCell: () => ({ width: colWidths.kb_id ?? 140, onResize: handleResize('kb_id'), title: '知识库' }),
      // kb 已删除/未入库时后端回退 kb_id，此处原样展示
      render: (_v: string | null, r: ChatFeedbackLogItem) => (
        r.kb_name || <Text type="secondary">—</Text>
      ),
    },
    {
      title: '评价', dataIndex: 'rating', key: 'rating',
      width: colWidths.rating ?? 90,
      onHeaderCell: () => ({ width: colWidths.rating ?? 90, onResize: handleResize('rating'), title: '评价' }),
      render: (v: string) => (v === 'up'
        ? <Tag color="green" style={{ marginRight: 0 }}>👍 点赞</Tag>
        : <Tag color="red" style={{ marginRight: 0 }}>👎 点踩</Tag>),
    },
    {
      // 原因重点展示：差评原因红色高亮；空显示 —（文本长、默认给较宽初始，可拖）
      title: '原因（用户补充说明）', dataIndex: 'reason', key: 'reason', ellipsis: true,
      width: colWidths.reason ?? 300,
      onHeaderCell: () => ({ width: colWidths.reason ?? 300, onResize: handleResize('reason'), title: '原因（用户补充说明）' }),
      render: (v: string | null | undefined, r: ChatFeedbackLogItem) => {
        if (!v) return <Text type="secondary">—</Text>;
        return r.rating === 'down'
          ? <Text style={{ color: '#f5222d' }} strong>{v}</Text>
          : v;
      },
    },
    {
      // 回溯入口：点开抽屉恢复问答现场（用户已删除的会话读归档，仍可回看）
      title: '关联会话', dataIndex: 'session_id', key: 'session_id', ellipsis: true,
      width: colWidths.session_id ?? 110,
      onHeaderCell: () => ({ width: colWidths.session_id ?? 110, onResize: handleResize('session_id'), title: '关联会话' }),
      render: (v: string | null, r: ChatFeedbackLogItem) => (v
        ? (
          <Tooltip title={`会话 ${v}`}>
            <Button
              type="link"
              size="small"
              style={{ padding: 0 }}
              onClick={() => setReplay({
                sessionId: v, msgIdx: r.msg_idx,
                rating: r.rating, reason: r.reason,
              })}
            >
              查看现场
            </Button>
          </Tooltip>
        )
        : <Text type="secondary">—</Text>),
    },
  ];

  const rate = stats && stats.total > 0 ? Math.round((stats.up / stats.total) * 100) : 0;

  const renderContent = () => {
    if (loading && items.length === 0 && !loadError) {
      return (
        <Card style={{ flex: 1, minHeight: 0 }}>
          <Skeleton active paragraph={{ rows: 6 }} />
        </Card>
      );
    }
    if (loadError && items.length === 0) {
      return (
        <Card style={{ flex: 1, minHeight: 0 }}>
          <AppEmpty
            title={user?.role === 'super_admin' ? '反馈数据加载失败' : '暂无权限查看用户反馈'}
            description={user?.role === 'super_admin'
              ? '请稍后重试；若持续失败请检查后端统计服务'
              : '用户回答反馈仅超级管理员可查看'}
          />
        </Card>
      );
    }
    return (
      <TableSectionLayout
        total={total}
        page={page}
        pageSize={pageSize}
        onPageChange={(p, ps) => { setPage(p); setPageSize(ps); }}
        toolbar={
          <>
            <Segmented
              value={applied.rating}
              onChange={(v) => handleRating(v as RatingFilter)}
              options={[
                { label: '全部', value: 'all' },
                { label: `点赞 ${stats?.up ?? 0}`, value: 'up' },
                { label: `点踩 ${stats?.down ?? 0}`, value: 'down' },
              ]}
            />
            <Input
              allowClear
              placeholder="用户名"
              style={{ width: 130 }}
              value={draft.username}
              onChange={e => setDraft(prev => ({ ...prev, username: e.target.value }))}
              onPressEnter={handleSearch}
            />
            <Select
              allowClear
              placeholder="部门"
              style={{ width: 150 }}
              value={draft.departmentId}
              onChange={(v) => setDraft(prev => ({ ...prev, departmentId: v }))}
              options={departments.map(d => ({ value: d.id, label: d.name }))}
            />
            <Input
              allowClear
              placeholder="关键词（点踩原因）"
              style={{ width: 170 }}
              value={draft.keyword}
              onChange={e => setDraft(prev => ({ ...prev, keyword: e.target.value }))}
              onPressEnter={handleSearch}
            />
            <RangePicker
              value={draft.dateRange}
              onChange={(v) => setDraft(prev => ({
                ...prev,
                dateRange: (v as [Dayjs, Dayjs] | null) ?? null,
              }))}
            />
            <Button type="primary" onClick={handleSearch}>查询</Button>
            <Button onClick={handleReset}>重置</Button>
            <Text type="secondary" style={{ fontSize: 12 }}>点踩原因红字高亮（最新在前）</Text>
          </>
        }
        emptyOrLoading={
          items.length === 0 && !loading ? (
            <AppEmpty
              title={hasFilter ? '当前条件下暂无反馈' : '暂无用户反馈'}
              description={hasFilter
                ? '试试放宽或重置筛选条件'
                : '用户在问答消息上的点赞/点踩会记录在这里'}
            />
          ) : undefined
        }
      >
        <Table
          rowKey={(r: ChatFeedbackLogItem) => r.id}
          size="small"
          dataSource={items}
          columns={columns}
          loading={loading}
          pagination={false}
          sticky
          className="table-zebra"
          components={{ header: { cell: ResizableTitle } }}
          scroll={{ x: tableWidth(columns) }}
        />
      </TableSectionLayout>
    );
  };

  return (
    <PageLayout
      title="用户反馈"
      description={
        <span>
          汇总统计：{stats
            ? `共 ${stats.total} 条，好评 ${stats.up}（${rate}%），差评 ${stats.down}`
            : '加载中…'}
        </span>
      }
      breadcrumb={
        <Breadcrumb
          items={[
            { title: <a onClick={() => navigate('/analytics')}>统计分析</a> },
            { title: '用户反馈' },
          ]}
        />
      }
      extra={
        <Space wrap>
          <Button icon={<ReloadOutlined />} onClick={refreshAll} loading={loading}>刷新</Button>
        </Space>
      }
    >
      {renderContent()}
      <SessionReplayDrawer
        open={replay !== null}
        sessionId={replay?.sessionId ?? null}
        msgIdx={replay?.msgIdx ?? -1}
        feedback={replay ? { rating: replay.rating, reason: replay.reason } : null}
        onClose={() => setReplay(null)}
      />
    </PageLayout>
  );
};

export default AnalyticsFeedbackDetailPage;
