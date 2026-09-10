/**
 * 用户反馈详情页（/analytics/feedback）：统计分析概览页点击「用户反馈」摘要卡下钻。
 * - 汇总统计（总数/好评/差评）+ 完整反馈记录表：时间/用户/知识库/评价/原因/关联会话
 * - 差评原因重点展示（红色高亮）；好评/差评 Segmented 前端过滤；分页左下固定
 *
 * TODO(后端就绪后):后端 /api/stats/chat-feedback/logs 支持服务端分页 + 用户/知识库名
 * 后，可改为服务端分页（当前先按汇总接口 recent 20 条前端分页，另经 listUsers/listKbs
 * 前端补齐用户与知识库名）。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Breadcrumb } from 'antd';
import { useNavigate } from 'react-router-dom';
import {
  Card, Segmented, Skeleton, Table, Tag, Tooltip, Typography, Space, Button,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { getChatFeedbackStats, listKbs, KnowledgeBase } from '../../api/client';
import type { ChatFeedbackStats } from '../../api/other';
import { listUsers } from '../../api/auth';
import type { User } from '../../auth/token';
import { useAuth } from '../../auth/AuthContext';
import AppEmpty from '../../components/common/AppEmpty';
import PageLayout from '../../components/layout/PageLayout';
import TableSectionLayout from '../../components/layout/TableSectionLayout';
import ResizableTitle from '../../components/common/ResizableTitle';
import { useResizableColumns } from '../../hooks/useResizableColumns';

const { Text } = Typography;

type RatingFilter = 'all' | 'up' | 'down';

const AnalyticsFeedbackDetailPage: React.FC = () => {
  const { user } = useAuth();
  const navigate = useNavigate();

  // 列宽拖拽：拖拽后的列宽存 colWidths（按列 key），scroll.x 动态对齐列宽和
  const { colWidths, handleResize, tableWidth } = useResizableColumns<ChatFeedbackStats['recent'][number]>();

  const [stats, setStats] = useState<ChatFeedbackStats | null>(null);
  const [loading, setLoading] = useState(false);
  // 加载失败（含无权限：该接口仅 super_admin，dept_admin/普通用户后端拒绝）
  const [loadError, setLoadError] = useState(false);
  const [filter, setFilter] = useState<RatingFilter>('all');

  // 用户/知识库名映射（接口 recent 项仅含 id，前端补齐显示名）
  const [userMap, setUserMap] = useState<Map<string, User>>(new Map());
  const [kbMap, setKbMap] = useState<Map<string, KnowledgeBase>>(new Map());

  const loadAll = useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      // 并行拉取：汇总 + 用户列表（补用户名）+ 知识库列表（补库名）
      const [statsRes, usersRes, kbsRes] = await Promise.all([
        getChatFeedbackStats(),
        listUsers(),
        listKbs(),
      ]);
      setStats(statsRes.data);
      setUserMap(new Map((usersRes.data || []).map(u => [u.id, u])));
      setKbMap(new Map((kbsRes.data || []).map(k => [k.id, k])));
    } catch {
      setStats(null);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // 评价过滤（汇总接口最近 20 条，前端过滤即可）
  const filtered = useMemo(() => {
    const rows = stats?.recent || [];
    if (filter === 'all') return rows;
    return rows.filter(r => r.rating === filter);
  }, [stats, filter]);

  // 前端分页（recent 条数有限；LeftPagination 固定左下统一分页样式）
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const pageRows = useMemo(
    () => filtered.slice((page - 1) * pageSize, page * pageSize),
    [filtered, page, pageSize],
  );
  useEffect(() => {
    const max = Math.max(1, Math.ceil(filtered.length / pageSize));
    if (page > max) setPage(max);
  }, [filtered.length, page, pageSize]);

  const columns = [
    {
      title: '时间', dataIndex: 'created_at', key: 'created_at',
      width: colWidths.created_at ?? 160,
      onHeaderCell: () => ({ width: colWidths.created_at ?? 160, onResize: handleResize('created_at'), title: '时间' }),
      render: (v: string) => v || '—',
    },
    {
      title: '用户', dataIndex: 'user_id', key: 'user_id', ellipsis: true,
      width: colWidths.user_id ?? 130,
      onHeaderCell: () => ({ width: colWidths.user_id ?? 130, onResize: handleResize('user_id'), title: '用户' }),
      render: (id: string) => {
        const u = userMap.get(id);
        const name = u ? (u.display_name || u.username) : null;
        return name || (
          <Tooltip title={id}>
            <Text type="secondary">{id.slice(0, 8)}…</Text>
          </Tooltip>
        );
      },
    },
    {
      title: '知识库', dataIndex: 'kb_id', key: 'kb_id', ellipsis: true,
      width: colWidths.kb_id ?? 150,
      onHeaderCell: () => ({ width: colWidths.kb_id ?? 150, onResize: handleResize('kb_id'), title: '知识库' }),
      render: (id: string | null) => {
        const kb = id ? kbMap.get(id) : null;
        return kb?.name || <Text type="secondary">—</Text>;
      },
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
      width: colWidths.reason ?? 340,
      onHeaderCell: () => ({ width: colWidths.reason ?? 340, onResize: handleResize('reason'), title: '原因（用户补充说明）' }),
      render: (v: string | null | undefined, r: ChatFeedbackStats['recent'][number]) => {
        if (!v) return <Text type="secondary">—</Text>;
        return r.rating === 'down'
          ? <Text style={{ color: '#f5222d' }} strong>{v}</Text>
          : v;
      },
    },
    {
      title: '关联会话', dataIndex: 'session_id', key: 'session_id', ellipsis: true,
      width: colWidths.session_id ?? 150,
      onHeaderCell: () => ({ width: colWidths.session_id ?? 150, onResize: handleResize('session_id'), title: '关联会话' }),
      render: (v: string | null) => (v
        ? <Tooltip title={v}><Text code style={{ fontSize: 12 }}>{v}</Text></Tooltip>
        : <Text type="secondary">—</Text>),
    },
  ];

  const rate = stats && stats.total > 0 ? Math.round((stats.up / stats.total) * 100) : 0;

  const renderContent = () => {
    if (loading && !stats) {
      return (
        <Card style={{ flex: 1, minHeight: 0 }}>
          <Skeleton active paragraph={{ rows: 6 }} />
        </Card>
      );
    }
    if (loadError || !stats) {
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
        total={filtered.length}
        page={page}
        pageSize={pageSize}
        onPageChange={(p, ps) => { setPage(p); setPageSize(ps); }}
        toolbar={
          <>
            <Segmented
              value={filter}
              onChange={(v) => { setFilter(v as RatingFilter); setPage(1); }}
              options={[
                { label: '全部', value: 'all' },
                { label: `点赞 ${stats.up}`, value: 'up' },
                { label: `点踩 ${stats.down}`, value: 'down' },
              ]}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              点踩原因红字高亮（最新在前）
            </Text>
          </>
        }
        emptyOrLoading={
          filtered.length === 0 ? (
            <AppEmpty
              title={stats.total === 0 ? '暂无用户反馈' : '当前评价类别下暂无反馈'}
              description={stats.total === 0
                ? '用户在问答消息上的点赞/点踩会记录在这里'
                : undefined}
            />
          ) : undefined
        }
      >
        <Table
          rowKey={(r: ChatFeedbackStats['recent'][number]) => r.id}
          size="small"
          dataSource={pageRows}
          columns={columns}
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
          <Button icon={<ReloadOutlined />} onClick={loadAll} loading={loading}>刷新</Button>
        </Space>
      }
    >
      {renderContent()}
    </PageLayout>
  );
};

export default AnalyticsFeedbackDetailPage;
