/**
 * RAGAS 评估报告弹窗：聚合指标卡 + 逐样本明细表。
 *
 * 从 RagasDetail 抽出来——这组东西（状态 4 个 + 加载逻辑 + 聚合卡渲染 +
 * 明细列定义 + 弹窗 JSX，约 130 行）自成闭环：只依赖 `task.id` 拉一份报告，
 * 不碰页面上任何其它状态。
 *
 * 数据加载从页面的 `openReport` 挪进本组件（open 且 task 变化时拉），
 * 页面只负责"开哪个任务的报告"。
 */
import { useEffect, useState } from 'react';
import {
  Card, Col, Row, Skeleton, Space, Statistic, Table, Tag, Tooltip, Typography,
} from 'antd';

import AppModal from '../../../../shared/components/common/AppModal';
import AppEmpty from '../../../../shared/components/common/AppEmpty';
import { getRagasReport, RagasReport, RagasTask } from '../../../../shared/api/client';
import { statusOf, scoreColor } from '../../shared';
import { metricLabel, metricLabelMap } from '../ragas-shared';

const { Text } = Typography;

export interface RagasReportModalProps {
  open: boolean;
  /** 要展示的任务（null = 尚未选择） */
  task: RagasTask | null;
  onClose: () => void;
}

export const RagasReportModal = ({ open, task, onClose }: RagasReportModalProps) => {
  const [report, setReport] = useState<RagasReport | null>(null);
  const [loading, setLoading] = useState(false);

  // 打开（或换任务）时拉报告：原先这段在页面的 openReport 里
  useEffect(() => {
    if (!open || !task) return;
    setLoading(true);
    setReport(null);
    getRagasReport(task.id)
      .then(res => setReport(res.data))
      .catch(() => setReport(null))
      .finally(() => setLoading(false));
  }, [open, task]);

  const renderAggregate = () => {
    if (!report) return null;
    const scores = report.aggregate?.scores || {};
    const entries = Object.entries(scores);
    if (entries.length === 0) {
      return <AppEmpty title="该任务无聚合评分数据" />;
    }
    return (
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        {entries.map(([k, v]) => (
          <Col key={k} xs={12} sm={8} md={6} lg={4}>
            <Card size="small">
              <Tooltip title={metricLabelMap[k] ? k : undefined}>
                <Statistic
                  title={metricLabel(k)}
                  value={v}
                  precision={4}
                  suffix="/ 1.0"
                  valueStyle={{ color: scoreColor(v) }}
                />
              </Tooltip>
            </Card>
          </Col>
        ))}
      </Row>
    );
  };

  // 明细表：指标列从 aggregate.scores 动态生成（缺失显示 —）
  const detailColumns = [
    { title: '#', dataIndex: 'idx', key: 'idx', width: 44 },
    {
      title: '问题', dataIndex: 'question', key: 'question', ellipsis: true, width: 220,
      render: (v: string) => <Tooltip title={v}>{v.length > 60 ? v.slice(0, 60) + '...' : v}</Tooltip>,
    },
    {
      title: '答案', dataIndex: 'answer', key: 'answer', ellipsis: true, width: 220,
      render: (v: string) => <Tooltip title={v}>{v.length > 60 ? v.slice(0, 60) + '...' : v}</Tooltip>,
    },
    ...Object.keys(report?.aggregate?.scores || {}).map(m => ({
      title: (
        <Tooltip title={metricLabelMap[m] ? m : undefined}>
          <span>{metricLabel(m)}</span>
        </Tooltip>
      ),
      dataIndex: ['scores', m],
      key: m,
      width: 110,
      render: (v: number | null | undefined) =>
        v != null ? (
          <Tag color={v >= 0.8 ? 'success' : v >= 0.5 ? 'warning' : 'error'}>{v.toFixed(4)}</Tag>
        ) : <Text type="secondary">—</Text>,
    })),
  ];

  return (
    <AppModal
      dimension="resizable"
      defaultSize={{ w: 960, h: 600 }}
      rememberKey="ana-4"
      title={
        <Space>
          <span>评估报告</span>
          {task ? <Tag color={statusOf(task.status).color}>{statusOf(task.status).label}</Tag> : null}
        </Space>
      }
      open={open}
      onCancel={onClose}
      footer={null}
      width={960}
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : report ? (
        <>
          {renderAggregate()}
          <Card
            title={`逐样本明细 (${report.results?.length ?? 0} 条)`}
            size="small"
            styles={{ body: { padding: 0 } }}
          >
            <Table
              rowKey="idx"
              size="small"
              dataSource={(report.results || []).map((r, i) => ({ ...r, idx: i + 1 }))}
              columns={detailColumns}
              scroll={{ x: 'max-content' }}
              pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
            />
          </Card>
        </>
      ) : (
        <AppEmpty title="报告加载失败" description="RAGAS 任务可能已删除或尚未完成" />
      )}
    </AppModal>
  );
};
