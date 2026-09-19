import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  App as AntApp,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  BarChartOutlined,
  ProfileOutlined,
  ReloadOutlined,
  RightOutlined,
} from '@ant-design/icons';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { ECharts, EChartsCoreOption } from 'echarts/core';
import {
  ExtQuery,
  ExtQueryLog,
  ExtQueryOverview,
  getExtQueryOverview,
  listExtQueries,
  listExtQueryLogs,
} from '../../shared/api/client';
import PageHeader from '../../shared/components/layout/PageHeader';
import { expiryColor, expiryState, expiryText } from './expiry';

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

/** 图表坐标轴配色（与日志总览/知识图谱保持一致） */
const AXIS_COLOR = 'rgba(128, 128, 128, 0.85)';
const SPLIT_COLOR = 'rgba(128, 128, 128, 0.18)';

/**
 * 两块内容区的高度上限：数据再多也不把趋势图顶出屏幕——总览只做概览，
 * 完整明细分别去「管理链接」「查看全部」的子页面看（那里有表格与分页）
 */
const PANEL_BODY_HEIGHT = 300;
/** antd small 表格表头高度（左侧表体限高后，右侧列表据此对齐总高） */
const TABLE_HEADER_HEIGHT = 40;

/** 最近访问列表拉取条数（超过高度上限可滚动查看） */
const RECENT_LIMIT = 20;

interface StatItem {
  label: string;
  value: React.ReactNode;
  /** 需要引起注意的统计（即将到期/已过期）用告警色 */
  tone?: 'warn' | 'danger';
}

interface EntryCardProps {
  icon: React.ReactNode;
  gradient: string;
  title: string;
  description: string;
  stats: StatItem[];
  onClick: () => void;
}

const toneColor: Record<string, string> = {
  warn: '#d97706',
  danger: '#dc2626',
};

/** 总览入口卡片（渐变色块图标 + 标题说明 + 统计条） */
const EntryCard: React.FC<EntryCardProps> = ({
  icon, gradient, title, description, stats, onClick,
}) => (
  <Card
    hoverable
    onClick={onClick}
    style={{ borderRadius: 14, height: '100%' }}
    styles={{ body: { padding: 20 } }}
  >
    <Space align="start" size={14} style={{ width: '100%' }}>
      <div
        style={{
          width: 48,
          height: 48,
          flex: '0 0 48px',
          borderRadius: 12,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 22,
          color: '#fff',
          background: gradient,
          boxShadow: '0 6px 16px rgba(37, 99, 235, 0.22)',
        }}
      >
        {icon}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <Typography.Text strong style={{ fontSize: 16 }}>{title}</Typography.Text>
          <RightOutlined style={{ fontSize: 11, color: '#94a3b8' }} />
        </div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {description}
        </Typography.Text>
        <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', gap: '8px 20px' }}>
          {stats.map(s => (
            <div key={s.label}>
              <div
                style={{
                  fontSize: 20,
                  fontWeight: 600,
                  lineHeight: 1.2,
                  color: s.tone ? toneColor[s.tone] : undefined,
                }}
              >
                {s.value}
              </div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {s.label}
              </Typography.Text>
            </div>
          ))}
        </div>
      </div>
    </Space>
  </Card>
);

/** 链接启用状态：停用 / 已过期 / 启用中（停用与过期是两回事，都要显式提示） */
const linkStateTag = (link: ExtQuery) => {
  if (!link.enabled) return <Tag color="default">已停用</Tag>;
  if (expiryState(link.expires_at) === 'expired') return <Tag color="red">已过期</Tag>;
  return <Tag color="green">启用中</Tag>;
};

/**
 * 近 7 天查询趋势（手写 echarts 集成：init / ResizeObserver 自适应 / dispose，
 * 与日志总览、知识图谱同款接入方式；按需引入避免打进整个 echarts）
 */
const TrendChart: React.FC<{ daily: { date: string; count: number }[] }> = ({
  daily,
}) => {
  const elRef = useRef<HTMLDivElement>(null);
  const [chart, setChart] = useState<ECharts | null>(null);

  useEffect(() => {
    if (!elRef.current) return;
    const instance = echarts.init(elRef.current);
    setChart(instance);
    const ro = new ResizeObserver(() => instance.resize());
    ro.observe(elRef.current);
    return () => {
      ro.disconnect();
      instance.dispose();
      setChart(null);
    };
  }, []);

  useEffect(() => {
    if (!chart) return;
    const option: EChartsCoreOption = {
      grid: { left: 8, right: 12, top: 16, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: {
        type: 'category',
        data: daily.map(d => d.date.slice(5)),
        axisTick: { show: false },
        axisLine: { lineStyle: { color: SPLIT_COLOR } },
        axisLabel: { color: AXIS_COLOR },
      },
      yAxis: {
        type: 'value',
        minInterval: 1, // 次数为整数，避免出现 0.5 这类刻度
        splitLine: { lineStyle: { color: SPLIT_COLOR } },
        axisLabel: { color: AXIS_COLOR },
      },
      series: [{
        name: '查询次数',
        type: 'bar',
        barMaxWidth: 36,
        itemStyle: { color: '#2563eb', borderRadius: [4, 4, 0, 0] },
        data: daily.map(d => d.count),
      }],
    };
    chart.setOption(option, true);
  }, [chart, daily]);

  return <div ref={elRef} style={{ width: '100%', height: 160 }} />;
};

/**
 * 外部查询总览（仅 super_admin）
 *
 * 模块入口页：上方两张卡片分别进「外部查询配置」与「外部查询记录」；
 * 下方「链接速览 + 最近访问」让首页一眼可见当前状态——哪些链接快到期、
 * 哪些没人用、外部最近在问什么，不必再点进去翻。
 */
const ExtQueries: React.FC = () => {
  const navigate = useNavigate();
  const { message } = AntApp.useApp();
  const [overview, setOverview] = useState<ExtQueryOverview | null>(null);
  const [links, setLinks] = useState<ExtQuery[]>([]);
  const [recent, setRecent] = useState<ExtQueryLog[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [ovRes, linkRes, logRes] = await Promise.all([
        getExtQueryOverview(),
        listExtQueries(),
        listExtQueryLogs({ page: 1, page_size: RECENT_LIMIT }),
      ]);
      setOverview(ovRes.data);
      setLinks(linkRes.data);
      setRecent(logRes.data.items);
    } catch {
      message.error('加载总览数据失败');
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    load();
  }, [load]);

  const linkStats = overview?.link_stats ?? {};
  const summary = overview?.links;
  const logs = overview?.logs;

  const linkColumns = [
    {
      title: '名称',
      dataIndex: 'name',
      ellipsis: true,
      render: (name: string, row: ExtQuery) => (
        <Link to={`/ext-queries/logs/${row.id}`}>{name}</Link>
      ),
    },
    {
      title: '状态',
      key: 'state',
      width: 76,
      render: (_: unknown, row: ExtQuery) => linkStateTag(row),
    },
    {
      title: '有效期',
      dataIndex: 'expires_at',
      width: 96,
      render: (v: string | null) => (
        <Tag color={expiryColor(v)}>{expiryText(v)}</Tag>
      ),
    },
    {
      title: '查询次数',
      key: 'count',
      width: 72,
      align: 'right' as const,
      render: (_: unknown, row: ExtQuery) => {
        const n = linkStats[row.id]?.count ?? 0;
        return n > 0 ? n : <Typography.Text type="secondary">0</Typography.Text>;
      },
    },
    {
      title: '最近访问',
      key: 'last_at',
      width: 98,
      render: (_: unknown, row: ExtQuery) => {
        const t = linkStats[row.id]?.last_at;
        if (!t) {
          return (
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              从未访问
            </Typography.Text>
          );
        }
        // 列宽有限：只显示「月-日 时:分」，完整时间放 tooltip
        return (
          <Tooltip title={t}>
            <Typography.Text style={{ fontSize: 13 }}>{t.slice(5, 16)}</Typography.Text>
          </Tooltip>
        );
      },
    },
  ];

  return (
    <div>
      <PageHeader
        title="外部查询"
        description="将知识库开放给外部人员查询（无需系统账号）：管理带令牌的对外链接，并查看外部访问记录"
        extra={
          <Button icon={<ReloadOutlined />} onClick={load}>
            刷新
          </Button>
        }
      />

      {loading && !overview ? (
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          <Spin />
        </div>
      ) : (
        <>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))',
              gap: 16,
            }}
          >
            <EntryCard
              icon={<ProfileOutlined />}
              gradient="linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%)"
              title="外部查询配置"
              description="新建链接、配置查询参数与有效期、重置令牌 / 停用"
              stats={[
                { label: '链接总数', value: summary?.total ?? 0 },
                { label: '启用中', value: summary?.enabled ?? 0 },
                {
                  label: '即将到期',
                  value: summary?.expiring_soon ?? 0,
                  tone: (summary?.expiring_soon ?? 0) > 0 ? 'warn' : undefined,
                },
                {
                  label: '已过期',
                  value: summary?.expired ?? 0,
                  tone: (summary?.expired ?? 0) > 0 ? 'danger' : undefined,
                },
              ]}
              onClick={() => navigate('/ext-queries/config')}
            />

            <EntryCard
              icon={<BarChartOutlined />}
              gradient="linear-gradient(135deg, #0ea5e9 0%, #0284c7 100%)"
              title="外部查询记录"
              description={`外部链接的访问日志（含来源 IP），保留 ${logs?.retain_days ?? 90} 天`}
              stats={[
                { label: '今日查询', value: logs?.today ?? 0 },
                { label: '累计查询', value: logs?.total ?? 0 },
                { label: '最近访问', value: logs?.last_at ? logs.last_at.slice(5, 16) : '—' },
              ]}
              onClick={() => navigate('/ext-queries/logs')}
            />
          </div>

          {summary && summary.expiring_soon > 0 && (
            <Typography.Paragraph type="warning" style={{ marginTop: 12, marginBottom: 0 }}>
              有 {summary.expiring_soon} 条链接将在 {overview?.expiring_soon_days ?? 7} 天内到期，
              到期后外部访问一律返回「链接无效或已失效」，可在「外部查询配置」中续期。
            </Typography.Paragraph>
          )}

          {/* 下方两块：链接速览（谁快到期 / 谁没人用）+ 最近访问（外部在问什么）
              左右列宽与上方入口卡一致（12/12）——上下分栏线对齐，视觉成组：
              左列=配置相关、右列=记录相关 */}
          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} xl={12}>
              <Card
                size="small"
                title="链接速览"
                extra={<Link to="/ext-queries/config">管理链接</Link>}
                styles={{ body: { padding: 0 } }}
              >
                <Table
                  rowKey="id"
                  size="small"
                  dataSource={links}
                  columns={linkColumns}
                  pagination={false}
                  scroll={{ y: PANEL_BODY_HEIGHT }}
                  locale={{ emptyText: '暂无外部查询链接' }}
                />
              </Card>
            </Col>
            <Col xs={24} xl={12}>
              <Card
                size="small"
                title="最近访问"
                extra={<Link to="/ext-queries/logs">查看全部</Link>}
                styles={{ body: { padding: recent.length ? '8px 0' : 24 } }}
              >
                {recent.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无访问记录" />
                ) : (
                  // 限高与左侧表体对齐（含表头高度），超出滚动
                  <div
                    style={{
                      maxHeight: PANEL_BODY_HEIGHT + TABLE_HEADER_HEIGHT,
                      overflowY: 'auto',
                    }}
                  >
                    {recent.map((r, idx) => (
                      <div
                        key={r.id}
                        style={{
                          padding: '8px 16px',
                          borderTop: idx ? '1px solid #f1f5f9' : 'none',
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                          <Typography.Text style={{ fontSize: 13 }} ellipsis>
                            {r.config_name || '(已删除的链接)'}
                          </Typography.Text>
                          <Typography.Text type="secondary" style={{ fontSize: 12, flex: '0 0 auto' }}>
                            {r.created_at.slice(5, 16)}
                          </Typography.Text>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginTop: 2 }}>
                          <Typography.Text
                            type="secondary"
                            style={{ fontSize: 12 }}
                            ellipsis={{ tooltip: r.query }}
                          >
                            {r.query}
                          </Typography.Text>
                          <Typography.Text type="secondary" style={{ fontSize: 12, flex: '0 0 auto' }}>
                            {r.client_ip || '-'}
                          </Typography.Text>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            </Col>
            <Col span={24}>
              <Card
                size="small"
                title="近 7 天查询趋势"
                styles={{ body: { padding: '8px 12px 0' } }}
              >
                <TrendChart daily={overview?.daily ?? []} />
              </Card>
            </Col>
          </Row>
        </>
      )}
    </div>
  );
};

export default ExtQueries;
