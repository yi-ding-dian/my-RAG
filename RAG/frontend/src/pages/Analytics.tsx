import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  List,
  Row,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  DatabaseOutlined,
  EyeOutlined,
  FileTextOutlined,
  MessageOutlined,
  PartitionOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import AppEmpty from '../components/AppEmpty';
import PageHeader from '../components/PageHeader';
import {
  DayRetrievalChart,
  KpiCard,
  QualitySummaryCard,
  metricLabel,
  statusOf,
  useAnalyticsData,
  useRagasModals,
} from './analytics-shared';

const { Text } = Typography;

/**
 * 统计分析概览页：一屏无滚动。
 * 顶部 4 个 KPI 卡 + 中部（近 30 天检索趋势图 / 检索质量概览）+ 底部 RAGAS 最近 5 条评估任务；
 * 完整数据点击「查看全部」进入 /analytics/ragas 详情页。
 */
const AnalyticsPage: React.FC = () => {
  const { user } = useAuth();
  const navigate = useNavigate();
  // 发起评估仅 super_admin / dept_admin（与后端权限一致）
  const isAdmin = user?.role === 'super_admin' || user?.role === 'dept_admin';

  const {
    stats,
    ragas,
    loading,
    qKbs,
    qKbId,
    quality,
    qLoading,
    handleQualityKbChange,
    loadAll,
    startPolling,
  } = useAnalyticsData(true);
  const [newTaskId, setNewTaskId] = useState<string | null>(null);

  // 新任务高亮：列表刷新后 2.6s 移除（CSS 呼吸动画 1.5s 渐退）
  useEffect(() => {
    if (!newTaskId) return;
    const t = window.setTimeout(() => setNewTaskId(null), 2600);
    return () => window.clearTimeout(t);
  }, [newTaskId]);

  // 任务创建/取消成功：刷新列表 + 开始进度轮询
  const onTasksChanged = useCallback(
    (taskId: string) => {
      setNewTaskId(taskId);
      loadAll();
      startPolling();
    },
    [loadAll, startPolling],
  );

  const { openEvalModal, openReport, modals } = useRagasModals({
    user: { id: user?.id ?? '', role: user?.role } as { id: string; role?: string },
    kbOptions: qKbs.map((k) => ({ value: k.id, label: `${k.name}（${k.chunk_count} 个切块）` })),
    onTasksChanged,
  });

  const tasks = ragas?.tasks || [];
  const recentTasks = tasks.slice(0, 5);
  const ragasAvailable = ragas?.available !== false;

  return (
    <div
      style={{
        height: 'calc(100vh - 48px)',
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
        overflow: 'hidden',
      }}
    >
      <div style={{ flex: 'none' }}>
        <PageHeader title="统计分析" description="系统整体运行统计与 RAGAS 检索质量评估" />
      </div>

      {/* 顶部：自身统计 KPI */}
      <Row gutter={[16, 16]} style={{ flex: 'none' }}>
        <Col xs={12} sm={6}>
          <KpiCard title="知识库数量" value={stats?.kb_count ?? 0} color="blue" icon={<DatabaseOutlined />} />
        </Col>
        <Col xs={12} sm={6}>
          <KpiCard title="文档数量" value={stats?.doc_count ?? 0} color="green" icon={<FileTextOutlined />} />
        </Col>
        <Col xs={12} sm={6}>
          <KpiCard title="切块数量" value={stats?.chunk_count ?? 0} color="purple" icon={<PartitionOutlined />} />
        </Col>
        <Col xs={12} sm={6}>
          <KpiCard title="问答消息数" value={stats?.message_count ?? 0} color="orange" icon={<MessageOutlined />} />
        </Col>
      </Row>

      {/* 中部：近 30 天检索趋势 + 检索质量概览 */}
      <Row gutter={[16, 16]} style={{ flex: 1, minHeight: 0 }}>
        <Col xs={24} lg={16} style={{ height: '100%' }}>
          <Card
            title="近 30 天 · 每日检索量 / 命中率"
            styles={{ body: { height: 'calc(100% - 49px)', overflow: 'auto', padding: 16 } }}
          >
            {!qKbId ? (
              <AppEmpty title="请先选择知识库" description="在右侧「检索质量」中选择知识库后查看" />
            ) : qLoading ? (
              <div>
                <AppEmpty title="正在加载…" />
              </div>
            ) : quality && quality.total_retrievals > 0 ? (
              <DayRetrievalChart daily={quality.daily} barHeight={132} />
            ) : (
              <AppEmpty title="暂无检索记录" description="请先在聊天或检索测试页发起检索" />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={8} style={{ height: '100%' }}>
          <Card
            title="检索质量（近 30 天）"
            styles={{ body: { height: 'calc(100% - 49px)', overflow: 'hidden', padding: 16 } }}
          >
            <QualitySummaryCard
              qKbs={qKbs}
              qKbId={qKbId}
              onKbChange={handleQualityKbChange}
              qLoading={qLoading}
              quality={quality}
            />
          </Card>
        </Col>
      </Row>

      {/* 底部：RAGAS 最近评估任务（预览） */}
      <Card
        style={{ flex: 1, minHeight: 0 }}
        styles={{ body: { height: 'calc(100% - 49px)', overflow: 'auto', padding: 16 } }}
        title={
          <Space size={8}>
            <span>RAGAS 评估</span>
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              {tasks.length > 0 ? `共 ${tasks.length} 个任务 · 最近 ${recentTasks.length} 个` : ''}
            </Text>
          </Space>
        }
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
            <Button
              size="small"
              type="link"
              icon={<EyeOutlined />}
              onClick={() => navigate('/analytics/ragas')}
              style={{ padding: 0 }}
            >
              查看全部{tasks.length ? ` ${tasks.length}` : ''}
            </Button>
          </Space>
        }
      >
        {loading && tasks.length === 0 ? (
          <div style={{ height: '100%' }}>
            <AppEmpty title="正在加载…" />
          </div>
        ) : ragasAvailable ? (
          recentTasks.length ? (
            <div style={{ height: '100%' }}>
              <List
                size="small"
                dataSource={recentTasks}
                split
                renderItem={(t, i) => {
                  const cfg = statusOf(t.status);
                  return (
                    <List.Item
                      key={t.id}
                      className={t.id === newTaskId ? 'ragas-row-highlight' : ''}
                      style={{ cursor: 'pointer', padding: '7px 0' }}
                      onClick={() => openReport(t)}
                      actions={[
                        <Button
                          key="report"
                          size="small"
                          type="link"
                          icon={<EyeOutlined />}
                          style={{ padding: 0, fontSize: 12 }}
                          onClick={(e) => {
                            e.stopPropagation();
                            openReport(t);
                          }}
                        >
                          查看报告
                        </Button>,
                      ]}
                    >
                      <Space size={12} style={{ overflow: 'hidden' }}>
                        <Text type="secondary" style={{ fontSize: 12, width: 20, textAlign: 'right' }}>
                          {i + 1}
                        </Text>
                        <Tooltip title={`${t.name}（${t.id}）`}>
                          <Text ellipsis style={{ maxWidth: '55%', flex: 'none', fontWeight: 500 }}>
                            {t.name}
                          </Text>
                        </Tooltip>
                        {t.kb_name ? (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {t.kb_name}
                          </Text>
                        ) : null}
                        <Tag color={cfg.color}>{cfg.label}</Tag>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          指标{' '}
                          {(t.metrics || [])
                            .slice(0, 2)
                            .map((m) => metricLabel(m))
                            .join(' · ')}
                          {t.metrics && t.metrics.length > 2 ? ` +${t.metrics.length - 2}` : ''}
                        </Text>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {t.created_at || '—'}
                        </Text>
                      </Space>
                    </List.Item>
                  );
                }}
              />
            </div>
          ) : (
            <div style={{ height: '100%' }}>
              <AppEmpty title="RAGAS 已连接，但暂无评估任务" description="点击「发起评估」创建第一个任务" />
            </div>
          )
        ) : (
          <Alert
            type="warning"
            showIcon
            message="RAGAS 评估系统不可用"
            description={
              <span>
                {ragas?.message || '无法连接 RAGAS 评估系统'}。系统自身统计不受影响；如需查看评估报告，请先启动
                RAGAS 服务（端口 8090）。
              </span>
            }
          />
        )}
      </Card>

      {modals}
    </div>
  );
};

export default AnalyticsPage;
