import React, { useEffect, useState, useCallback } from 'react';
import {
  Card, Form, Select, Button, InputNumber, Switch, Typography, message,
  Space, Tag, Spin, Alert, Checkbox, Input, Row, Col, Divider, Radio, Modal,
} from 'antd';
import { ThunderboltOutlined, ReloadOutlined, DeleteOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import {
  listDatasets, getMetrics, createEvaluation, listEvaluations,
  getActiveProfile,
  DatasetListItem, MetricInfo, EvalTaskListItem, EvalTask,
  getActiveLanguage, setActiveLanguage, deleteEvaluation,
} from '../api/client';

const EvaluatePage: React.FC = () => {
  const [datasets, setDatasets] = useState<DatasetListItem[]>([]);
  const [metricsMap, setMetricsMap] = useState<Record<string, MetricInfo>>({});
  const [tasks, setTasks] = useState<EvalTaskListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [activeLang, setActiveLang] = useState('zh');
  const [profileMaxTokens, setProfileMaxTokens] = useState(256);
  const [form] = Form.useForm();
  const navigate = useNavigate();

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [dsRes, mRes, tRes, langRes, profileRes] = await Promise.all([
        listDatasets(), getMetrics(), listEvaluations(),
        getActiveLanguage().catch(() => ({ data: { language: 'zh' } })),
        getActiveProfile().catch(() => ({ data: { llm_max_tokens: 256 } })),
      ]);
      setDatasets(dsRes.data);
      setMetricsMap(mRes.data);
      setTasks(tRes.data);
      setActiveLang(langRes.data.language);
      const maxTok = profileRes.data.llm_max_tokens ?? 256;
      setProfileMaxTokens(maxTok);
      form.setFieldsValue({ llm_max_tokens: maxTok });
    } catch {
      message.error('加载数据失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const handleCreate = async (values: any) => {
    setCreating(true);
    try {
      const config = {
        dataset_id: values.dataset_id,
        metrics: values.metrics || [],
        use_retrieval: values.use_retrieval || false,
        retrieval_top_k: values.retrieval_top_k || 5,
        llm_temperature: values.llm_temperature ?? 0.0,
        llm_max_tokens: values.llm_max_tokens ?? 256,
        llm_max_workers: values.llm_max_workers ?? 4,
        batch_size: values.batch_size ?? 8,
        name: values.name || '',
      };
      const res = await createEvaluation(config);
      message.success('评估任务已创建');
      await loadData();
      navigate('/results');
    } catch (e: any) {
      message.error(e.response?.data?.detail || '创建失败');
    } finally {
      setCreating(false);
    }
  };

  const statusColor: Record<string, string> = {
    queued: 'warning', pending: 'default', running: 'processing', completed: 'success', failed: 'error',
  };

  const statusLabel: Record<string, string> = {
    queued: '排队中', pending: '等待中', running: '运行中', completed: '已完成', failed: '失败',
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
          loadData();
        } catch (e: any) {
          message.error(e.response?.data?.detail || '删除失败');
        }
      },
    });
  };

  const handleLanguageChange = async (lang: string) => {
    try {
      await setActiveLanguage(lang);
      setActiveLang(lang);
      message.success(`评估语言已切换为 ${lang === 'zh' ? '中文' : 'English'}`);
    } catch {
      message.error('切换失败');
    }
  };

  return (
    <div>
      <Typography.Title level={4}>执行评估</Typography.Title>

      <Row gutter={24}>
        <Col xs={24} lg={14}>
          <Card title="评估配置">
            <Form
              form={form}
              layout="vertical"
              onFinish={handleCreate}
              initialValues={{
                use_retrieval: false,
                retrieval_top_k: 5,
                llm_temperature: 0.0,
                llm_max_workers: 4,
                batch_size: 8,
              }}
            >
              <Form.Item name="name" label="任务名称">
                <Input placeholder="留空自动生成" />
              </Form.Item>

              <Form.Item name="dataset_id" label="选择数据集" rules={[{ required: true, message: '请选择数据集' }]}>
                <Select
                  placeholder="选择要评估的数据集"
                  options={datasets.map(d => ({
                    label: `${d.name} (${d.row_count} 条)`,
                    value: d.id,
                  }))}
                />
              </Form.Item>

              <Form.Item name="metrics" label="评估指标" rules={[{ required: true, message: '请选择至少一个指标' }]}
                tooltip="可多选，至少选一个。带 * 的指标需要标准答案(ground_truth)，带 # 的指标需要 Embedding 模型。">
                <Select
                  mode="multiple"
                  placeholder="选择评估指标，可多选"
                  options={Object.entries(metricsMap).map(([k, v]) => ({
                    label: v.label,
                    value: k,
                  }))}
                />
              </Form.Item>

              <Divider>检索配置</Divider>

              <Form.Item name="use_retrieval" label="启用真实检索链路" valuePropName="checked">
                <Switch />
              </Form.Item>

              <Form.Item noStyle shouldUpdate={(prev, cur) => prev.use_retrieval !== cur.use_retrieval}>
                {({ getFieldValue }) =>
                  getFieldValue('use_retrieval') ? (
                    <Form.Item name="retrieval_top_k" label="检索 Top-K">
                      <InputNumber min={1} max={50} />
                    </Form.Item>
                  ) : null
                }
              </Form.Item>

              <Divider>评估语言</Divider>
              <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
                选择评估时使用的提示词语言。可在「提示词管理」页面查看和编辑各语言的具体内容。
              </Typography.Text>
              <Form.Item label="提示词语言">
                <Radio.Group
                  value={activeLang}
                  onChange={e => handleLanguageChange(e.target.value)}
                  optionType="button"
                  buttonStyle="solid"
                >
                  <Radio.Button value="en">English</Radio.Button>
                  <Radio.Button value="zh">中文</Radio.Button>
                </Radio.Group>
              </Form.Item>

              <Divider>LLM 参数（评估模型）</Divider>
              <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
                控制评估模型（LLM Judge）的打分行为，合理设置可提高评分速度和准确性
              </Typography.Text>

              <Space size="large" wrap>
                <Form.Item name="llm_temperature" label="Temperature" tooltip="模型输出的随机性。评估任务建议设为 0（确定不变），值越大评分越不稳定。范围 0~2">
                  <InputNumber min={0} max={2} step={0.1} />
                </Form.Item>
                <Form.Item name="llm_max_tokens" label="Max Tokens" tooltip="每个评分请求允许输出的最大 token 数。范围 256~8192">
                  <InputNumber min={256} max={8192} step={256} />
                </Form.Item>
                <Form.Item name="llm_max_workers" label="并发数" tooltip="同时发送给 LLM 的评分请求数量。本地模型建议 2~4（避免显存溢出），在线模型可设 8~16 显著加速">
                  <InputNumber min={1} max={32} />
                </Form.Item>
                <Form.Item name="batch_size" label="批大小" tooltip="每批同时评分的样本数。数据量大时适当增大可提高吞吐量，但会增加显存压力。范围 1~64">
                  <InputNumber min={1} max={64} />
                </Form.Item>
              </Space>

              <Form.Item style={{ marginTop: 16 }}>
                <Button type="primary" htmlType="submit" loading={creating} icon={<ThunderboltOutlined />} size="large">
                  {creating ? '正在创建...' : '开始评估'}
                </Button>
              </Form.Item>
            </Form>
          </Card>
        </Col>

        <Col xs={24} lg={10}>
          <Card
            title="最近任务"
            extra={<Button size="small" icon={<ReloadOutlined />} onClick={loadData}>刷新</Button>}
          >
            <Spin spinning={loading}>
              {tasks.length === 0 ? (
                <Typography.Text type="secondary">暂无评估任务</Typography.Text>
              ) : (
                tasks.slice(0, 10).map(t => (
                  <Card
                    key={t.id}
                    size="small"
                    style={{ marginBottom: 8 }}
                    extra={
                      t.status !== 'running' ? (
                        <Button type="text" size="small" danger icon={<DeleteOutlined />}
                          onClick={e => { e.stopPropagation(); handleDelete(t.id, t.name); }} />
                      ) : null
                    }
                    onClick={() => navigate('/results')}
                    hoverable
                  >
                    <Space direction="vertical" size={2} style={{ width: '100%' }}>
                      <Space>
                        <Tag color={statusColor[t.status] || 'default'}>{statusLabel[t.status] || t.status}</Tag>
                        <Typography.Text strong>{t.name}</Typography.Text>
                      </Space>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        数据集: {t.dataset_name} | 进度: {t.progress}%
                      </Typography.Text>
                    </Space>
                  </Card>
                ))
              )}
            </Spin>
          </Card>
        </Col>
      </Row>
    </div>
  );
};

export default EvaluatePage;
