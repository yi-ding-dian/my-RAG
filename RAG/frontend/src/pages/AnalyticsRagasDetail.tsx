import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Select,
  Skeleton,
  Space,
  Statistic,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import { ReloadOutlined, PlayCircleOutlined } from '@ant-design/icons';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import AppEmpty from '../components/AppEmpty';
import AppTable from '../components/AppTable';
import PageHeader from '../components/PageHeader';
import {
  DayRetrievalChart,
  overallHitRateOf,
  scoreColor,
  useAnalyticsData,
  useRagasModals,
} from './analytics-shared';
import { RagasTask, RetrievalHitDoc, RetrievalZeroHitDoc } from '../api/client';

const { Text } = Typography;

/**
 * RAGAS 评估与检索质量详情页 /analytics/ragas：
 * Tab「评估任务」= 全量任务表格（发起评估/刷新/查看报告/取消）；
 * Tab「检索质量（近 30 天）」= 知识库筛选 + 指标卡 + 每日趋势图 + 命中排行 + 零命中文档。
 */
const AnalyticsRagasDetail: React.FC = () => {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get('tab') === 'quality' ? 'quality' : 'tasks';
  const isAdmin = user?.role === 'super_admin' || user?.role === 'dept_admin';

  const {
    ragas,
    loading,
    qKbs,
    qKbId,
    quality,
    qLoading,
    handleQualityKbChange,
    loadAll,
    startPolling,
  } = useAnalyticsData(false);
  const [newTaskId, setNewTaskId] = useState<string | null>(null);
  const ragasTableWrapRef = useRef<HTMLDivElement>(null);

  // 新任务行高亮：列表刷新后滚动到新行（AntD rowKey 渲染 tr[data-row-key]），
  // 高亮 class 挂 2.6s（CSS 呼吸动画 1.5s 渐退）后移除
  useEffect(() => {
    if (!newTaskId) return;
    const scrollTimer = window.setTimeout(() => {
      const row = ragasTableWrapRef.current?.querySelector<HTMLElement>(
        `tr[data-row-key="${newTaskId}"]`,
      );
      row?.scrollIntoView({ block: 'nearest' });
    }, 120);
    const clearTimer = window.setTimeout(() => setNewTaskId(null), 2600);
    return () => {
      window.clearTimeout(scrollTimer);
      window.clearTimeout(clearTimer);
    };
  }, [newTaskId, ragas]);

  // 任务创建/取消成功：刷新列表 + 开始进度轮询
  const onTasksChanged = useCallback(
    (taskId: string) => {
      setNewTaskId(taskId);
      loadAll();
      startPolling();
    },
    [loadAll, startPolling],
  );

  const { openEvalModal, openReport, makeColumns, modals } = useRagasModals({
    user: { id: user?.id ?? '', role: user?.role } as { id: string; role?: string },
    kbOptions: qKbs.map((k) => ({ value: k.id, label: `${k.name}（${k.chunk_count} 个切块）` })),
    onTasksChanged,
  });

  const tasks = ragas?.tasks || [];
  const ragasAvailable = ragas?.available !== false;
  const rate = overallHitRateOf(quality);

  return (
    <div>
      <PageHeader
        title="RAGAS 评估与检索质量"
        description="全量评估任务与检索质量明细，返回概览可快速查看数据摘要"
      />
      <Tabs
        activeKey={activeTab}
        onChange={(k) => setSearchParams({ tab: k === 'tasks' ? 'tasks' : 'quality' })}
        items={[
          {
            key: 'tasks',
            label: `评估任务（${tasks.length}）`,
            children: (
              <Card
                title="RAGAS 评估系统"
                size="small"
                extra={
                  <Space>
                    {isAdmin ? (
                      <Button size="small" type="primary" icon={<PlayCircleOutlined />} onClick={openEvalModal}>
                        发起评估
                      </Button>
                    ) : null}
                    <Button size="small" icon={<ReloadOutlined />} onClick={loadAll} loading={loading}>
                      刷新
                    </Button>
                    <Button size="small" type="link" style={{ padding: 0 }} onClick={() => navigate('/analytics')}>
                      ← 返回概览
                    </Button>
                  </Space>
                }
              >
                {loading && tasks.length === 0 ? (
                  <Skeleton active paragraph={{ rows: 4 }} />
                ) : ragasAvailable ? (
                  <div ref={ragasTableWrapRef}>
                    <AppTable<RagasTask>
                      rowKey="id"
                      size="small"
                      dataSource={tasks}
                      columns={makeColumns()}
                      rowClassName={(record) => (record.id === newTaskId ? 'ragas-row-highlight' : '')}
                      pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 个任务` }}
                      onRow={(record) => ({
                        onClick: () => openReport(record),
                        style: { cursor: 'pointer' },
                      })}
                      locale={{ emptyText: <AppEmpty title="RAGAS 已连接，但暂无评估任务" /> }}
                      divider
                    />
                  </div>
                ) : (
                  <Alert
                    type="warning"
                    showIcon
                    message="RAGAS 评估系统不可用"
                    description={
                      <span>
                        {ragas?.message || '无法连接 RAGAS 评估系统'}。系统自身统计不受影响，可在知识库管理/文档管理中继续使用；如需查看评估报告，请先启动 RAGAS 服务（端口 8090）。
                      </span>
                    }
                  />
                )}
              </Card>
            ),
          },
          {
            key: 'quality',
            label: '检索质量（近 30 天）',
            children: (
              <Card
                title="检索质量"
                size="small"
                extra={
                  <Button size="small" type="link" style={{ padding: 0 }} onClick={() => navigate('/analytics')}>
                    ← 返回概览
                  </Button>
                }
              >
                <Space style={{ marginBottom: 16 }} wrap>
                  <Space size={8}>
                    <Text strong>知识库</Text>
                    <Select
                      value={qKbId}
                      onChange={handleQualityKbChange}
                      style={{ width: 260 }}
                      placeholder="选择知识库"
                      options={qKbs.map((k) => ({ value: k.id, label: k.name }))}
                      notFoundContent={
                        <Empty description="暂无知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />
                      }
                    />
                  </Space>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    基于检索日志（data/retrieval_logs）统计，保留 30 天
                  </Text>
                </Space>
                {qLoading ? (
                  <Skeleton active paragraph={{ rows: 6 }} />
                ) : !qKbId ? (
                  <AppEmpty title="请先选择知识库" />
                ) : quality && quality.total_retrievals > 0 ? (
                  <>
                    {/* 概览统计 */}
                    <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
                      <Col xs={12} sm={6}>
                        <Card size="small">
                          <Statistic title="总检索次数" value={quality.total_retrievals} />
                        </Card>
                      </Col>
                      <Col xs={12} sm={6}>
                        <Card size="small">
                          <Statistic
                            title="平均命中 chunk 数"
                            value={quality.avg_hits_per_retrieval}
                            precision={2}
                          />
                        </Card>
                      </Col>
                      <Col xs={12} sm={6}>
                        <Card size="small">
                          <Statistic
                            title="近 30 天命中率"
                            value={rate}
                            precision={2}
                            suffix="/ 1.0"
                            valueStyle={{ color: scoreColor(rate) }}
                          />
                        </Card>
                      </Col>
                      <Col xs={12} sm={6}>
                        <Card size="small">
                          <Statistic title="零命中文档数" value={quality.zero_hit_docs.length} />
                        </Card>
                      </Col>
                    </Row>

                    {/* 日粒度检索量柱状图（柱高=当日检索量，颜色=当日命中率） */}
                    <Card
                      size="small"
                      title="每日检索量 / 命中率"
                      style={{ marginBottom: 16 }}
                      styles={{ body: { padding: '14px 16px 10px' } }}
                    >
                      <DayRetrievalChart daily={quality.daily} />
                    </Card>

                    {/* 命中文档排行 + 零命中文档 */}
                    <Row gutter={16}>
                      <Col xs={24} lg={12}>
                        <Card size="small" title="Top 10 命中文档排行" styles={{ body: { padding: 0 } }}>
                          <AppTable<RetrievalHitDoc>
                            rowKey="doc_id"
                            size="small"
                            dataSource={quality.hit_docs}
                            columns={[
                              { title: '文档名', dataIndex: 'doc_name', key: 'doc_name', ellipsis: true },
                              {
                                title: '命中次数',
                                dataIndex: 'hits',
                                key: 'hits',
                                width: 110,
                                render: (v: number) => <Tag color="blue">{v}</Tag>,
                              },
                            ]}
                            pagination={false}
                            locale={{ emptyText: '暂无命中文档' }}
                            divider
                          />
                        </Card>
                      </Col>
                      <Col xs={24} lg={12}>
                        <Card size="small" title="零命中文档" styles={{ body: { padding: 0 } }}>
                          <AppTable<RetrievalZeroHitDoc>
                            rowKey="doc_id"
                            size="small"
                            dataSource={quality.zero_hit_docs}
                            columns={[
                              { title: '文档名', dataIndex: 'doc_name', key: 'doc_name', ellipsis: true },
                              {
                                title: '切块数',
                                dataIndex: 'chunks',
                                key: 'chunks',
                                width: 110,
                                render: (v: number) => <Text type="secondary">{v}</Text>,
                              },
                            ]}
                            pagination={false}
                            locale={{ emptyText: '无（所有已入库文档均被命中过）' }}
                            divider
                          />
                          {quality.zero_hit_docs.length > 0 ? (
                            <Alert
                              style={{ margin: 12 }}
                              type="warning"
                              showIcon
                              message="该文档从未被检索命中，建议检查切块质量或内容相关性"
                            />
                          ) : null}
                        </Card>
                      </Col>
                    </Row>
                  </>
                ) : (
                  <AppEmpty title="暂无检索记录" description="请先在聊天或检索测试页发起检索" />
                )}
              </Card>
            ),
          },
        ]}
      />
      {modals}
    </div>
  );
};

export default AnalyticsRagasDetail;
