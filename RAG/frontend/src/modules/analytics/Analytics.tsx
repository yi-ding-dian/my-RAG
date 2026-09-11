/**
 * 统计分析概览页（/analytics）：KPI 卡 + 3 张摘要卡（点击整卡下钻详情页）。
 * 概览一屏不滚动；检索质量 / RAGAS 评测 / 用户反馈的完整能力分别迁移到
 * AnalyticsQualityDetail / AnalyticsRagasDetail / AnalyticsFeedbackDetail。
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  App as AntApp, Card, Col, Row, Skeleton, Statistic, Tag, Typography, Alert,
  Space, Tooltip, Select, Empty, Button, theme,
} from 'antd';
import {
  DatabaseOutlined, FileTextOutlined, MessageOutlined, PartitionOutlined,
  ReloadOutlined, RightOutlined, SyncOutlined,
} from '@ant-design/icons';
import {
  getStats, getRagasStatus, getRetrievalQuality, listKbs,
  KnowledgeBase, RetrievalQuality, Stats, RagasStatus,
} from '../../shared/api/client';
import PageLayout from '../../shared/components/layout/PageLayout';
import { FeedbackSummary, statusOf } from './analytics-shared';

const { Text } = Typography;

const AnalyticsPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const navigate = useNavigate();

  // 自身统计 / RAGAS 任务 / 知识库列表
  const [stats, setStats] = useState<Stats | null>(null);
  const [ragas, setRagas] = useState<RagasStatus | null>(null);
  const [loading, setLoading] = useState(false);

  // 检索质量（近 30 天）摘要
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [kbId, setKbId] = useState<string | undefined>(undefined);
  const [quality, setQuality] = useState<RetrievalQuality | null>(null);
  const [qLoading, setQLoading] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [statsRes, ragasRes, kbsRes] = await Promise.all([
        getStats(),
        getRagasStatus(),
        listKbs(),
      ]);
      setStats(statsRes.data);
      setRagas(ragasRes.data);
      setKbs(kbsRes.data);
    } catch {
      setStats(null);
      setRagas({ available: false, tasks: [], message: '自身统计接口异常' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // 选择知识库 → 加载近 30 天检索质量统计（摘要卡数据）
  const handleKbChange = useCallback(async (id: string) => {
    setKbId(id);
    setQuality(null);
    setQLoading(true);
    try {
      const res = await getRetrievalQuality(id);
      setQuality(res.data);
    } catch {
      message.error('检索质量统计加载失败');
      setQuality(null);
    } finally {
      setQLoading(false);
    }
  }, [message]);

  // 知识库列表就绪后默认选中第一个：摘要卡默认即有内容（仍可手动切换）
  const autoPickedRef = useRef(false);
  useEffect(() => {
    if (kbId || autoPickedRef.current || kbs.length === 0) return;
    autoPickedRef.current = true;
    handleKbChange(kbs[0].id);
  }, [kbs, kbId, handleKbChange]);

  // 近 30 天整体命中率（有命中的检索占比，日粒度加权）
  const overallHitRate = quality && quality.total_retrievals > 0
    ? quality.daily.reduce((acc, d) => acc + d.retrievals * d.hit_rate, 0)
      / quality.total_retrievals
    : 0;

  const kpiItems = [
    {
      title: '知识库数量', value: stats?.kb_count ?? 0, color: 'blue',
      icon: <DatabaseOutlined />,
    },
    {
      title: '文档数量', value: stats?.doc_count ?? 0, color: 'green',
      icon: <FileTextOutlined />,
    },
    {
      title: '切块数量', value: stats?.chunk_count ?? 0, color: 'purple',
      icon: <PartitionOutlined />,
    },
    {
      title: '问答消息数', value: stats?.message_count ?? 0, color: 'orange',
      icon: <MessageOutlined />,
    },
  ];

  // 摘要卡（点击整卡下钻详情）统一外壳：标题 + hover「查看详情」提示 + 底部常显入口
  const summaryCard = (title: string, to: string, children: React.ReactNode) => (
    <Card
      size="small"
      className="analytics-nav-card"
      onClick={() => navigate(to)}
      style={{
        flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column',
        borderRadius: 12,
      }}
      styles={{ body: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' } }}
      title={
        <Space size={8}>
          <span>{title}</span>
          <Text className="analytics-nav-hint" style={{ fontSize: 12 }}>
            查看详情 <RightOutlined style={{ fontSize: 10 }} />
          </Text>
        </Space>
      }
    >
      <div style={{ flex: 1, minHeight: 0 }}>{children}</div>
      <div
        style={{
          paddingTop: 10, marginTop: 12, flexShrink: 0,
          borderTop: `1px solid ${token.colorBorderSecondary}`,
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>完整统计与明细</Text>
        <Text style={{ fontSize: 12, color: token.colorPrimary }}>
          进入详情 <RightOutlined style={{ fontSize: 10 }} />
        </Text>
      </div>
    </Card>
  );

  // ---------- 检索质量摘要卡：4 指标单行 + 近 30 天 Top5 ----------
  const renderQualityCard = () => {
    const metrics = [
      { label: '总次数', value: quality ? quality.total_retrievals : null },
      {
        label: '命中率',
        value: quality && quality.total_retrievals > 0
          ? `${Math.round(overallHitRate * 100)}%`
          : null,
      },
      { label: '文档命中', value: quality ? quality.hit_docs.length : null },
      { label: '零命中文档', value: quality ? quality.zero_hit_docs.length : null },
    ];
    const top5 = quality?.hit_docs.slice(0, 5) || [];
    return summaryCard('检索质量（近 30 天）', '/analytics/quality', (
      <>
        {/* 知识库选择（点击不冒泡触发整卡跳转） */}
        <div onClick={e => e.stopPropagation()} style={{ marginBottom: 12 }}>
          <Space wrap>
            <Select
              size="small"
              value={kbId}
              onChange={handleKbChange}
              style={{ width: 220 }}
              placeholder="选择知识库"
              options={kbs.map(k => ({ value: k.id, label: k.name }))}
              notFoundContent={<Empty description="暂无知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>基于检索日志统计</Text>
          </Space>
        </div>
        {qLoading ? (
          <Skeleton active paragraph={{ rows: 4 }} style={{ marginTop: 4 }} />
        ) : quality && quality.total_retrievals > 0 ? (
          <>
            {/* 4 指标单行 */}
            <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap', marginBottom: 10 }}>
              {metrics.map(m => (
                <div key={m.label}>
                  <div style={{ fontSize: 20, fontWeight: 600, lineHeight: 1.2 }}>
                    {m.value ?? '—'}
                  </div>
                  <Text type="secondary" style={{ fontSize: 12 }}>{m.label}</Text>
                </div>
              ))}
            </div>
            {/* 命中 Top5（近 30 天） */}
            <Text type="secondary" style={{ fontSize: 12 }}>命中 Top 5</Text>
            {top5.map((d, i) => (
              <div
                key={d.doc_id}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8,
                  padding: '3px 0', fontSize: 13,
                }}
              >
                <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>{i + 1}</Text>
                <Tooltip title={d.doc_name}>
                  <span style={{
                    flex: 1, minWidth: 0, overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    {d.doc_name}
                  </span>
                </Tooltip>
                <Tag color="blue" style={{ marginRight: 0 }}>{d.hits} 次</Tag>
              </div>
            ))}
            {quality.hit_docs.length > 5 ? (
              <Text type="secondary" style={{ fontSize: 11 }}>
                共 {quality.hit_docs.length} 篇文档被命中，其余见详情页
              </Text>
            ) : null}
          </>
        ) : kbId ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {quality ? '近 30 天暂无检索记录，请先在聊天或检索测试页发起检索' : '数据加载中…'}
          </Text>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>
            选择知识库后查看：总检索次数 / 命中率 / 命中文档 / 零命中文档与命中 Top 5
          </Text>
        )}
      </>
    ));
  };

  // ---------- RAGAS 摘要卡：最近 5 条评测任务 ----------
  const renderRagasCard = () => {
    const recent = (ragas?.tasks || []).slice(0, 5);
    const total = ragas?.tasks?.length || 0;
    const running = (ragas?.tasks || []).some(t =>
      t.status === 'queued' || t.status === 'pending' || t.status === 'running');
    return summaryCard('RAGAS 评测', '/analytics/ragas', (
      <>
        {loading && !ragas ? (
          <Skeleton active paragraph={{ rows: 4 }} />
        ) : !ragas?.available ? (
          <Alert
            type="warning"
            showIcon
            message="RAGAS 评测系统不可用"
            description={<span>{ragas?.message || '无法连接 RAGAS 评测系统'}（端口 8090）</span>}
          />
        ) : recent.length === 0 ? (
          <Text type="secondary" style={{ fontSize: 12 }}>暂无评估任务，可进入详情页发起评测</Text>
        ) : (
          <>
            {recent.map(t => (
              <div
                key={t.id}
                style={{
                  padding: '5px 0', fontSize: 13,
                  borderBottom: `1px solid ${token.colorBorderSecondary}`,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <Tooltip title={t.name}>
                    <Text strong ellipsis style={{ flex: 1, minWidth: 0, fontSize: 13 }}>
                      {t.name}
                    </Text>
                  </Tooltip>
                  {(() => {
                    const cfg = statusOf(t.status);
                    return (
                      <Tag color={cfg.color} style={{ marginRight: 0 }}>
                        {t.status === 'running' ? <SyncOutlined spin /> : null} {cfg.label}
                      </Tag>
                    );
                  })()}
                </div>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {t.kb_name || '—'} · {t.created_at || '—'}
                  {running ? ' · 运行中自动刷新' : ''}
                </Text>
              </div>
            ))}
            {total > 5 ? (
              <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 6 }}>
                共 {total} 个评测任务，其余见详情页
              </Text>
            ) : null}
          </>
        )}
      </>
    ));
  };

  // ---------- 用户反馈摘要卡 ----------
  const renderFeedbackCard = () => (
    summaryCard('用户反馈', '/analytics/feedback', (
      <FeedbackSummary maxRecent={5} />
    ))
  );

  return (
    <PageLayout
      title="统计分析"
      description="系统整体运行统计与 RAGAS 检索质量评估；点击下方摘要卡片进入对应详情页"
      extra={
        <Button icon={<ReloadOutlined />} onClick={loadAll} loading={loading}>
          刷新
        </Button>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, gap: 16 }}>
        {/* KPI 卡（自身统计） */}
        <Row gutter={[16, 16]}>
          {kpiItems.map(k => (
            <Col xs={12} sm={6} key={k.title}>
              <Card
                size="small"
                className={`stat-card stat-card--${k.color}`}
                styles={{ body: { padding: '18px 16px' } }}
              >
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                  <Statistic
                    title={k.title}
                    value={k.value}
                    valueStyle={{ fontSize: 24, fontWeight: 600 }}
                  />
                  <div className="stat-icon">{k.icon}</div>
                </div>
              </Card>
            </Col>
          ))}
        </Row>

        {/* 3 张摘要卡：点击整卡进入详情页 */}
        <div style={{ flex: 1, minHeight: 0, display: 'flex', gap: 16 }}>
          {renderQualityCard()}
          {renderRagasCard()}
          {renderFeedbackCard()}
        </div>
      </div>
    </PageLayout>
  );
};

export default AnalyticsPage;
