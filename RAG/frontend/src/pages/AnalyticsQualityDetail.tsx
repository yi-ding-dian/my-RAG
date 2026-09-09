/**
 * 检索质量详情页（/analytics/quality）：统计分析概览页点击「检索质量（近 30 天）」摘要卡下钻。
 * - 4 指标卡 + 近 30 天每日检索量/命中率图 + Top10 命中文档排行 + 零命中文档全量（前端分页）
 * - 整页不滚：4 指标/图表固定，下方两表在卡片内内部滚动（表头 sticky），分页左下固定
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Breadcrumb } from 'antd';
import { useNavigate } from 'react-router-dom';
import {
  App as AntApp, Card, Col, Row, Skeleton, Statistic, Table, Tag, Typography, Alert, Space, Tooltip, Empty,
  Select,
} from 'antd';
import { getRetrievalQuality, listKbs, KnowledgeBase, RetrievalQuality } from '../api/client';
import AppEmpty from '../components/AppEmpty';
import PageLayout from '../components/layout/PageLayout';
import LeftPagination from '../components/layout/LeftPagination';
import ResizableTitle from '../components/ResizableTitle';
import { useResizableColumns } from '../hooks/useResizableColumns';
import { scoreColor } from './analytics-shared';

const { Text } = Typography;

const AnalyticsQualityDetailPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();

  // 列宽拖拽：拖拽后的列宽存 colWidths（按列 key），scroll.x 动态对齐列宽和；
  // 两个排行表各自独立标识（hit_/zero_ 前缀），拖宽互不串扰
  const { colWidths, handleResize, tableWidth } = useResizableColumns();

  // 知识库选择 + 检索质量统计（近 30 天）
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [kbId, setKbId] = useState<string | undefined>(undefined);
  const [quality, setQuality] = useState<RetrievalQuality | null>(null);
  const [loading, setLoading] = useState(false);

  // 零命中文档全量列表前端分页（LeftPagination 固定左下）
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const zeroHitDocs = quality?.zero_hit_docs || [];
  const pageZeroHit = useMemo(
    () => zeroHitDocs.slice((page - 1) * pageSize, page * pageSize),
    [zeroHitDocs, page, pageSize],
  );
  useEffect(() => {
    const max = Math.max(1, Math.ceil(zeroHitDocs.length / pageSize));
    if (page > max) setPage(max);
  }, [zeroHitDocs.length, page, pageSize]);

  useEffect(() => {
    let cancelled = false;
    listKbs()
      .then(res => { if (!cancelled) setKbs(res.data); })
      .catch(() => { /* 知识库列表加载失败：选择框空态兜底 */ });
    return () => { cancelled = true; };
  }, []);

  // 选择知识库 → 加载近 30 天检索质量统计
  const handleKbChange = useCallback(async (id: string) => {
    setKbId(id);
    setQuality(null);
    setLoading(true);
    try {
      const res = await getRetrievalQuality(id);
      setQuality(res.data);
    } catch {
      message.error('检索质量统计加载失败');
      setQuality(null);
    } finally {
      setLoading(false);
    }
  }, [message]);

  // 知识库列表就绪后默认选中第一个：详情默认即有内容（仍可手动切换）
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

  // 命中文档排行列（Top10）
  const hitDocColumns = [
    {
      title: '文档名', dataIndex: 'doc_name', key: 'doc_name', ellipsis: true,
      width: colWidths.hit_doc_name ?? 260,
      onHeaderCell: () => ({ width: colWidths.hit_doc_name ?? 260, onResize: handleResize('hit_doc_name'), title: '文档名' }),
    },
    {
      title: '命中次数', dataIndex: 'hits', key: 'hits', width: colWidths.hit_hits ?? 110,
      onHeaderCell: () => ({ width: colWidths.hit_hits ?? 110, onResize: handleResize('hit_hits'), title: '命中次数' }),
      render: (v: number) => <Tag color="blue">{v}</Tag>,
    },
  ];

  // 零命中文档列
  const zeroHitColumns = [
    {
      title: '文档名', dataIndex: 'doc_name', key: 'doc_name', ellipsis: true,
      width: colWidths.zero_doc_name ?? 260,
      onHeaderCell: () => ({ width: colWidths.zero_doc_name ?? 260, onResize: handleResize('zero_doc_name'), title: '文档名' }),
    },
    {
      title: '切块数', dataIndex: 'chunks', key: 'chunks', width: colWidths.zero_chunks ?? 110,
      onHeaderCell: () => ({ width: colWidths.zero_chunks ?? 110, onResize: handleResize('zero_chunks'), title: '切块数' }),
      render: (v: number) => <Text type="secondary">{v}</Text>,
    },
  ];

  const renderContent = () => {
    if (!kbId) {
      return (
        <Card style={{ flex: 1, minHeight: 0 }}>
          <AppEmpty title="请先选择知识库" />
        </Card>
      );
    }
    if (loading) {
      return (
        <Card style={{ flex: 1, minHeight: 0 }}>
          <Skeleton active paragraph={{ rows: 6 }} />
        </Card>
      );
    }
    if (!quality || quality.total_retrievals === 0) {
      return (
        <Card style={{ flex: 1, minHeight: 0 }}>
          <AppEmpty title="暂无检索记录" description="请先在聊天或检索测试页发起检索" />
        </Card>
      );
    }
    return (
      <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
        {/* 概览统计：4 指标 */}
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={6}>
            <Card size="small">
              <Statistic title="总检索次数" value={quality.total_retrievals} />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small">
              <Statistic title="平均命中 chunk 数" value={quality.avg_hits_per_retrieval} precision={2} />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small">
              <Statistic
                title="近 30 天命中率"
                value={overallHitRate}
                precision={2}
                suffix="/ 1.0"
                valueStyle={{ color: scoreColor(overallHitRate) }}
              />
            </Card>
          </Col>
          <Col xs={12} sm={6}>
            <Card size="small">
              <Statistic title="零命中文档数" value={zeroHitDocs.length} />
            </Card>
          </Col>
        </Row>

        {/* 日粒度检索量 mini 柱状图（柱高=当日检索量，颜色=当日命中率；无检索为灰底） */}
        <Card
          size="small"
          title="每日检索量 / 命中率"
          style={{ marginTop: 16, flexShrink: 0 }}
          styles={{ body: { padding: '14px 16px 10px' } }}
        >
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 96 }}>
            {quality.daily.map(d => {
              const max = Math.max(...quality.daily.map(x => x.retrievals), 1);
              const h = d.retrievals > 0 ? Math.max(4, (d.retrievals / max) * 68) : 4;
              const color = d.retrievals === 0 ? '#94a3b8'
                : d.hit_rate >= 0.8 ? '#52c41a'
                : d.hit_rate >= 0.5 ? '#faad14' : '#f5222d';
              return (
                <div
                  key={d.date}
                  style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3 }}
                >
                  <Tooltip title={`${d.date}：${d.retrievals} 次检索，命中率 ${Math.round(d.hit_rate * 100)}%`}>
                    <div style={{ width: '72%', maxWidth: 18, height: h, borderRadius: 2, background: color }} />
                  </Tooltip>
                  <Text type="secondary" style={{ fontSize: 9, lineHeight: '12px', whiteSpace: 'nowrap' }}>
                    {d.date.slice(5)}
                  </Text>
                </div>
              );
            })}
          </div>
          <div style={{ marginTop: 8, display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            <Text type="secondary" style={{ fontSize: 11 }}>
              <span style={{ display: 'inline-block', width: 10, height: 10, background: '#52c41a', borderRadius: 2, marginRight: 4 }} />
              命中率 ≥ 80%
            </Text>
            <Text type="secondary" style={{ fontSize: 11 }}>
              <span style={{ display: 'inline-block', width: 10, height: 10, background: '#faad14', borderRadius: 2, marginRight: 4 }} />
              ≥ 50%
            </Text>
            <Text type="secondary" style={{ fontSize: 11 }}>
              <span style={{ display: 'inline-block', width: 10, height: 10, background: '#f5222d', borderRadius: 2, marginRight: 4 }} />
              &lt; 50%
            </Text>
            <Text type="secondary" style={{ fontSize: 11 }}>
              <span style={{ display: 'inline-block', width: 10, height: 10, background: '#94a3b8', borderRadius: 2, marginRight: 4 }} />
              无检索
            </Text>
          </div>
        </Card>

        {/* 命中文档排行 + 零命中文档：两表占满剩余高度，各自卡片内内部滚动 */}
        <div
          style={{
            flex: 1, minHeight: 0, display: 'flex', gap: 16, marginTop: 16,
          }}
        >
          <Card
            size="small"
            title="Top 10 命中文档排行"
            style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}
            styles={{ body: { padding: 0, flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' } }}
          >
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
              <Table
                rowKey="doc_id"
                size="small"
                dataSource={quality.hit_docs}
                columns={hitDocColumns}
                pagination={false}
                sticky
                components={{ header: { cell: ResizableTitle } }}
                scroll={{ x: tableWidth(hitDocColumns) }}
                locale={{ emptyText: '暂无命中文档' }}
              />
            </div>
          </Card>
          <Card
            size="small"
            title="零命中文档"
            style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}
            styles={{ body: { padding: 0, flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' } }}
          >
            {zeroHitDocs.length > 0 ? (
              <Alert
                style={{ margin: 12, flexShrink: 0 }}
                type="warning"
                showIcon
                message="这些文档从未被检索命中，建议检查切块质量或内容相关性"
              />
            ) : null}
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
              <Table
                rowKey="doc_id"
                size="small"
                dataSource={pageZeroHit}
                columns={zeroHitColumns}
                pagination={false}
                sticky
                components={{ header: { cell: ResizableTitle } }}
                scroll={{ x: tableWidth(zeroHitColumns) }}
                locale={{ emptyText: '无（所有已入库文档均被命中过）' }}
              />
            </div>
            {zeroHitDocs.length > 0 ? (
              <div style={{ padding: '0 12px 12px', flexShrink: 0 }}>
                <LeftPagination
                  page={page}
                  pageSize={pageSize}
                  total={zeroHitDocs.length}
                  onChange={(p, ps) => { setPage(p); setPageSize(ps); }}
                />
              </div>
            ) : null}
          </Card>
        </div>
      </div>
    );
  };

  return (
    <PageLayout
      title="检索质量（近 30 天）"
      description="基于检索日志（data/retrieval_logs）统计，保留 30 天"
      breadcrumb={
        <Breadcrumb
          items={[
            { title: <a onClick={() => navigate('/analytics')}>统计分析</a> },
            { title: '检索质量' },
          ]}
        />
      }
      extra={
        <Space wrap>
          <Text strong>知识库</Text>
          <Select
            value={kbId}
            onChange={handleKbChange}
            style={{ width: 260 }}
            placeholder="选择知识库"
            options={kbs.map(k => ({ value: k.id, label: k.name }))}
            notFoundContent={<Empty description="暂无知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          />
        </Space>
      }
    >
      {renderContent()}
    </PageLayout>
  );
};

export default AnalyticsQualityDetailPage;
