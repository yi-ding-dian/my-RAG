import React, { useEffect, useState, useCallback, useRef } from 'react';
import {
  Card, Table, Tag, Typography, Button, Space, message, Spin,
  Select, Row, Col, Statistic, Empty, Tooltip, Modal,
} from 'antd';
import {
  ReloadOutlined, DownloadOutlined, CheckCircleOutlined,
  CloseCircleOutlined, SyncOutlined, FileTextOutlined, DeleteOutlined,
  ClockCircleOutlined,
} from '@ant-design/icons';
import ReactEChartsCore from 'echarts-for-react';
import {
  listEvaluations, getEvaluation, getEvaluationResults, getEvaluationLogs, getMetrics,
  cancelEvaluation, deleteEvaluation, getExportUrl, createEvaluation,
  EvalTaskListItem, EvalResults, EvalTask, MetricInfo, EvalConfig,
} from '../api/client';
import { useNavigate } from 'react-router-dom';

const statusConfig: Record<string, { color: string; icon: React.ReactNode }> = {
  queued: { color: 'warning', icon: <ClockCircleOutlined /> },
  pending: { color: 'default', icon: <SyncOutlined spin /> },
  running: { color: 'processing', icon: <SyncOutlined spin /> },
  completed: { color: 'success', icon: <CheckCircleOutlined /> },
  failed: { color: 'error', icon: <CloseCircleOutlined /> },
};

const statusLabel: Record<string, string> = {
  queued: '排队中', pending: '等待中', running: '运行中', completed: '已完成', failed: '失败',
};

const ResultsPage: React.FC = () => {
  const [tasks, setTasks] = useState<EvalTaskListItem[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [task, setTask] = useState<EvalTask | null>(null);
  const [results, setResults] = useState<EvalResults | null>(null);
  const [loading, setLoading] = useState(false);
  const [polling, setPolling] = useState(false);
  const [metricLabels, setMetricLabels] = useState<Record<string, string>>({});
  const [logOpen, setLogOpen] = useState(false);
  const [logEntries, setLogEntries] = useState<Array<{time: string; level: string; message: string}>>([]);
  const [logPolling, setLogPolling] = useState<ReturnType<typeof setInterval> | null>(null);
  const [elapsed, setElapsed] = useState('');
  const navigate = useNavigate();

  // 重新评估
  const [reEvalOpen, setReEvalOpen] = useState(false);
  const [reEvalMetrics, setReEvalMetrics] = useState<string[]>([]);
  const [reEvalLoading, setReEvalLoading] = useState(false);

  const openReEval = () => {
    if (!results) return;
    setReEvalMetrics(Object.keys(results.aggregate.scores));
    setReEvalOpen(true);
  };

  const handleReEval = async () => {
    if (!task || reEvalMetrics.length === 0) return;
    setReEvalLoading(true);
    try {
      const config: EvalConfig = {
        dataset_id: task.dataset_id,
        metrics: reEvalMetrics,
        use_retrieval: task.config.use_retrieval,
        retrieval_top_k: task.config.retrieval_top_k,
        llm_temperature: task.config.llm_temperature,
        llm_max_tokens: task.config.llm_max_tokens,
        llm_max_workers: task.config.llm_max_workers,
        batch_size: task.config.batch_size,
        name: `${task.name}(重新评估)`,
      };
      const res = await createEvaluation(config);
      message.success('重新评估任务已创建');
      setReEvalOpen(false);
      await loadTasks();
      loadResults(res.data.id);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '创建失败');
    } finally {
      setReEvalLoading(false);
    }
  };

  // 计时器: 每秒更新已用时间
  useEffect(() => {
    if (!task || task.status !== 'running') {
      setElapsed('');
      return;
    }
    const started = task.started_at || task.created_at;
    const tick = () => {
      if (!started) return;
      const diff = Date.now() - new Date(started).getTime();
      if (diff <= 0) return;
      const h = Math.floor(diff / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      setElapsed(`${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`);
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [task?.id, task?.status, task?.started_at, task?.created_at]);

  // 计算完成任务的用时
  const getDuration = (): string => {
    if (!task || task.status === 'running') return '';
    if (!task.created_at) return '';
    const end = task.completed_at ? new Date(task.completed_at).getTime() : Date.now();
    const start = new Date(task.created_at).getTime();
    const diff = end - start;
    if (diff <= 0) return '';
    const h = Math.floor(diff / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    const s = Math.floor((diff % 60000) / 1000);
    if (h > 0) return `${h}时${m}分${s}秒`;
    if (m > 0) return `${m}分${s}秒`;
    return `${s}秒`;
  };

  const handleCancel = async () => {
    if (!task) return;
    const isQueued = task.status === 'queued';
    Modal.confirm({
      title: isQueued ? '取消排队' : '停止评估',
      content: isQueued ? '确定要取消这个排队中的评估任务吗？' : '确定要停止当前评估任务吗？已完成的评分不会被保存。',
      okText: isQueued ? '取消排队' : '停止',
      cancelText: '继续',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await cancelEvaluation(task.id);
          message.info(isQueued ? '已取消排队' : '正在停止评估...');
        } catch (e: any) {
          message.error(e.response?.data?.detail || '取消失败');
        }
      },
    });
  };

  const handleDelete = (taskId: string, taskName: string) => {
    Modal.confirm({
      title: '删除评估任务',
      content: `确定要删除「${taskName}」吗？删除后无法恢复。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteEvaluation(taskId);
          message.success('已删除');
          if (selectedTaskId === taskId) {
            setSelectedTaskId(null);
            setTask(null);
            setResults(null);
          }
          loadTasks();
        } catch (e: any) {
          message.error(e.response?.data?.detail || '删除失败');
        }
      },
    });
  };

  const loadTasks = useCallback(async () => {
    try {
      const res = await listEvaluations();
      setTasks(res.data);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { loadTasks(); }, [loadTasks]);

  // 加载指标名称映射
  useEffect(() => {
    getMetrics().then(res => {
      const map: Record<string, string> = {};
      Object.entries(res.data).forEach(([k, v]) => { map[k] = v.label; });
      setMetricLabels(map);
    }).catch(() => {});
  }, []);

  const loadResults = useCallback(async (taskId: string) => {
    setLoading(true);
    try {
      const [taskRes, resultsRes] = await Promise.all([
        getEvaluation(taskId),
        getEvaluationResults(taskId).catch(() => null),
      ]);
      setTask(taskRes.data);
      setResults(resultsRes?.data || null);
      setSelectedTaskId(taskId);
    } catch {
      message.error('加载结果失败');
    } finally {
      setLoading(false);
    }
  }, []);

  // 轮询 running 任务
  useEffect(() => {
    if (!task || task.status !== 'running') {
      setPolling(false);
      return;
    }
    setPolling(true);
    const interval = setInterval(async () => {
      try {
        const taskRes = await getEvaluation(task.id);
        setTask(taskRes.data);
        if (taskRes.data.status === 'completed') {
          const resultsRes = await getEvaluationResults(task.id);
          setResults(resultsRes.data);
          setPolling(false);
          clearInterval(interval);
        }
      } catch {
        clearInterval(interval);
        setPolling(false);
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [task?.id, task?.status]);

  // 自动选中第一个 running 或第一个非排队任务
  useEffect(() => {
    if (!selectedTaskId && tasks.length > 0) {
      const running = tasks.find(t => t.status === 'running');
      if (running) {
        loadResults(running.id);
      } else {
        const first = tasks.find(t => t.status !== 'queued');
        if (first) loadResults(first.id);
      }
    }
  }, [tasks, selectedTaskId, loadResults]);

  // 实时日志
  const logCountRef = useRef(0);
  const openLog = (taskId: string) => {
    setLogOpen(true);
    setLogEntries([]);
    logCountRef.current = 0;
    // 拉取已有日志
    getEvaluationLogs(taskId).then(res => {
      setLogEntries(res.data.logs);
      logCountRef.current = res.data.total;
    }).catch(() => {});
    // 每 2 秒轮询新日志
    const interval = setInterval(async () => {
      try {
        const res = await getEvaluationLogs(taskId, logCountRef.current);
        if (res.data.logs.length > 0) {
          setLogEntries(prev => [...prev, ...res.data.logs]);
          logCountRef.current += res.data.logs.length;
        }
      } catch { /* ignore */ }
    }, 2000);
    setLogPolling(interval);
  };

  const closeLog = () => {
    if (logPolling) clearInterval(logPolling);
    setLogPolling(null);
    setLogOpen(false);
  };

  // 日志颜色
  const logColor = (level: string) => {
    if (level === 'error') return '#ff4d4f';
    if (level === 'warn') return '#faad14';
    return 'inherit';
  };

  const handleRefresh = () => {
    if (selectedTaskId) loadResults(selectedTaskId);
    loadTasks();
  };

  const handleTaskSelect = (id: string) => {
    loadResults(id);
  };

  // 雷达图
  const radarOption = results?.aggregate?.scores ? {
    radar: {
      indicator: Object.entries(results.aggregate.scores).map(([key]) => ({
        name: metricLabels[key] || key,
        max: 1,
      })),
      center: ['50%', '55%'],
      radius: '65%',
    },
    series: [{
      type: 'radar',
      data: [{
        value: Object.values(results.aggregate.scores),
        name: '评分',
        areaStyle: { color: 'rgba(24, 144, 255, 0.2)' },
        lineStyle: { color: '#1890ff', width: 2 },
        itemStyle: { color: '#1890ff' },
      }],
    }],
  } : null;

  // 柱状图
  const barOption = results?.aggregate?.scores ? {
    xAxis: { type: 'category', data: Object.keys(results.aggregate.scores).map(k => metricLabels[k] || k) },
    yAxis: { type: 'value', min: 0, max: 1 },
    tooltip: { trigger: 'axis' },
    grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
    series: [{
      type: 'bar',
      data: Object.values(results.aggregate.scores).map((v: number) => ({
        value: v,
        itemStyle: {
          color: v >= 0.8 ? '#52c41a' : v >= 0.5 ? '#faad14' : '#f5222d',
        },
      })),
      barWidth: '50%',
      label: {
        show: true,
        position: 'top',
        formatter: (p: any) => p.value.toFixed(4),
        fontSize: 11,
      },
    }],
  } : null;

  const detailColumns = [
    { title: '#', dataIndex: 'idx', key: 'idx', width: 40 },
    {
      title: '问题', dataIndex: 'question', key: 'question', ellipsis: true, width: 200,
      render: (v: string) => <Tooltip title={v}>{v.length > 50 ? v.slice(0, 50) + '...' : v}</Tooltip>,
    },
    {
      title: '答案', dataIndex: 'answer', key: 'answer', ellipsis: true, width: 200,
      render: (v: string) => <Tooltip title={v}>{v.length > 60 ? v.slice(0, 60) + '...' : v}</Tooltip>,
    },
    ...(Object.keys(results?.aggregate?.scores || {}).map(m => ({
      title: metricLabels[m] || m, dataIndex: ['scores', m], key: m, width: 100,
      render: (v: number | null) => v != null ? (
        <Tag color={v >= 0.8 ? 'success' : v >= 0.5 ? 'warning' : 'error'}>{v.toFixed(4)}</Tag>
      ) : <Tag color="default">-</Tag>,
    }))),
  ];

  return (
    <div>
      <Typography.Title level={4}>评估结果</Typography.Title>

      <Card
        title={
          <Space>
            <span>选择评估任务</span>
            <Select
              style={{ minWidth: 300 }}
              placeholder="选择任务"
              value={selectedTaskId}
              onChange={handleTaskSelect}
              options={tasks.map(t => ({
                label: `[${t.status}] ${t.name} (${t.dataset_name})`,
                value: t.id,
              }))}
            />
          </Space>
        }
        extra={
          <Space>
            {polling && <Tag icon={<SyncOutlined spin />} color="processing">轮询中</Tag>}
            {task && task.status !== 'running' && (
              <>
                {results && Object.keys(results.aggregate.scores).length > 0 && (
                  <Button icon={<SyncOutlined />} onClick={openReEval}>重新评估</Button>
                )}
                <Button icon={<DeleteOutlined />} danger onClick={() => handleDelete(task.id, task.name)}>删除</Button>
              </>
            )}
            <Button icon={<ReloadOutlined />} onClick={handleRefresh} loading={loading}>刷新</Button>
          </Space>
        }
        style={{ marginBottom: 24 }}
      >
        {task && (
          <Space wrap>
            <Tag color={statusConfig[task.status]?.color}>
              {statusConfig[task.status]?.icon} {statusLabel[task.status] || task.status}
            </Tag>
            <span>数据集: {task.dataset_name}</span>
            <span>进度: {task.progress}%</span>
            {task.status === 'running' && elapsed && (
              <Tag color="blue">{elapsed}</Tag>
            )}
            {task.status === 'running' && task.eta_seconds != null && (
              <Tag color="orange">预计剩余 {Math.ceil(task.eta_seconds / 60)} 分钟</Tag>
            )}
            {task.status !== 'running' && task.status !== 'pending' && task.status !== 'queued' && (
              <span style={{ color: '#888', fontSize: 13 }}>用时: {getDuration()}</span>
            )}
            <Typography.Text type="secondary">{task.message}</Typography.Text>
            {task.status === 'running' && (
              <>
                <Spin size="small" />
                <Button size="small" icon={<CloseCircleOutlined />} danger onClick={handleCancel}>停止</Button>
                <Button size="small" icon={<FileTextOutlined />} onClick={() => openLog(task.id)}>日志</Button>
              </>
            )}
            {task.status === 'queued' && (
              <Button size="small" icon={<CloseCircleOutlined />} danger onClick={handleCancel}>取消排队</Button>
            )}
            {task.error && <Typography.Text type="danger">错误: {task.error}</Typography.Text>}
            {task.status !== 'running' && (
              <Button size="small" type="text" icon={<FileTextOutlined />} onClick={() => openLog(task.id)}>日志</Button>
            )}
          </Space>
        )}
      </Card>

      {results && (
        <>
          {/* 聚合评分 */}
          <Row gutter={16} style={{ marginBottom: 24 }}>
            {Object.entries(results.aggregate.scores).map(([k, v]) => (
              <Col key={k} xs={12} sm={8} md={6} lg={4}>
                <Card size="small">
                  <Statistic
                    title={metricLabels[k] || k}
                    value={v}
                    precision={4}
                    suffix="/ 1.0"
                    valueStyle={{ color: v >= 0.8 ? '#52c41a' : v >= 0.5 ? '#faad14' : '#f5222d' }}
                  />
                </Card>
              </Col>
            ))}
          </Row>

          {/* 图表 */}
          <Row gutter={16} style={{ marginBottom: 24 }}>
            <Col xs={24} lg={12}>
              <Card title="评估指标雷达图" size="small">
                {radarOption ? <ReactEChartsCore option={radarOption} style={{ height: 320 }} /> : <Empty />}
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title="评估指标柱状图" size="small">
                {barOption ? <ReactEChartsCore option={barOption} style={{ height: 320 }} /> : <Empty />}
              </Card>
            </Col>
          </Row>

          {/* 导出 */}
          <Card
            title={`详细评分 (${results.results.length} 条)`}
            size="small"
            style={{ marginBottom: 16 }}
            extra={
              <Space>
                <Button size="small" icon={<DownloadOutlined />}
                  onClick={() => window.open(getExportUrl(results.task_id, 'json'))}>
                  JSON
                </Button>
                <Button size="small" icon={<DownloadOutlined />}
                  onClick={() => window.open(getExportUrl(results.task_id, 'csv'))}>
                  CSV
                </Button>
                <Button size="small" icon={<DownloadOutlined />}
                  onClick={() => window.open(getExportUrl(results.task_id, 'html'))}>
                  HTML 报告
                </Button>
              </Space>
            }
          >
            <Table
              dataSource={results.results.map((r, i) => ({ ...r, idx: i + 1 }))}
              columns={detailColumns}
              rowKey="idx"
              size="small"
              scroll={{ x: 'max-content' }}
              pagination={{ pageSize: 10, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
            />
          </Card>
        </>
      )}

      {!results && !loading && selectedTaskId && (
        <Card><Empty description={
          task?.status === 'running' ? '评估正在执行中，请等待...' :
          task?.status === 'failed' ? '评估执行失败' :
          '暂无结果数据'
        } /></Card>
      )}

      {loading && <div style={{ textAlign: 'center', padding: 60 }}><Spin size="large" /></div>}
      {/* 重新评估弹窗 */}
      <Modal
        title="重新评估"
        open={reEvalOpen}
        onCancel={() => setReEvalOpen(false)}
        onOk={handleReEval}
        confirmLoading={reEvalLoading}
        okText="开始评估"
      >
        <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
          选择需要重新评估的指标，将使用相同的数据集和参数创建新的评估任务。
        </Typography.Text>
        <Typography.Text strong style={{ display: 'block', marginBottom: 8 }}>数据集: {task?.dataset_name}</Typography.Text>
        <Typography.Text strong style={{ display: 'block', marginBottom: 8 }}>已选指标：</Typography.Text>
        <Select
          mode="multiple"
          style={{ width: '100%' }}
          value={reEvalMetrics}
          onChange={setReEvalMetrics}
          options={Object.keys(results?.aggregate?.scores || {}).map(k => ({
            label: metricLabels[k] || k,
            value: k,
          }))}
        />
      </Modal>

      {/* 实时日志弹窗 */}
      <Modal
        title="评估日志"
        open={logOpen}
        onCancel={closeLog}
        footer={null}
        width={700}
        bodyStyle={{ maxHeight: 500, overflow: 'auto', background: '#1e1e1e', padding: 12, fontFamily: 'monospace', fontSize: 13 }}
      >
        {logEntries.length === 0 ? (
          <div style={{ color: '#888', textAlign: 'center', padding: 40 }}>暂无日志</div>
        ) : (
          logEntries.map((entry, i) => (
            <div key={i} style={{ color: '#d4d4d4', lineHeight: 1.8 }}>
              <span style={{ color: '#888' }}>[{entry.time}]</span>{' '}
              <span style={{
                color: entry.level === 'error' ? '#ff4d4f' : entry.level === 'warn' ? '#faad14' : '#6a9955',
                fontWeight: entry.level === 'error' ? 700 : 400,
              }}>
                {entry.level.toUpperCase()}
              </span>{' '}
              <span>{entry.message}</span>
            </div>
          ))
        )}
      </Modal>
    </div>
  );
};

export default ResultsPage;
