import React, { useEffect, useState } from 'react';
import {
  Card, Table, Tag, Button, Space, Typography, Spin, message,
  Drawer, Input, Radio, Divider, Tooltip, Alert,
} from 'antd';
import {
  ReloadOutlined, TranslationOutlined, SaveOutlined,
  EyeOutlined, FileTextOutlined,
} from '@ant-design/icons';
import {
  getPrompts, getMetricPrompts, translateMetricPrompts,
  updateMetricPrompts, getActiveLanguage, setActiveLanguage,
  checkLlmStatus,
  MetricPromptsSummary, MetricPromptsDetail,
} from '../api/client';

const PromptsPage: React.FC = () => {
  const [metrics, setMetrics] = useState<MetricPromptsSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [translating, setTranslating] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Drawer state
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedMetric, setSelectedMetric] = useState<string | null>(null);
  const [metricDetail, setMetricDetail] = useState<MetricPromptsDetail | null>(null);
  const [editedPrompts, setEditedPrompts] = useState<Record<string, string>>({});

  // Language
  const [activeLang, setActiveLang] = useState('zh');
  const [llmAvailable, setLlmAvailable] = useState<boolean | null>(null);

  useEffect(() => { loadData(); }, []);

  useEffect(() => {
    checkLlmStatus().then(r => setLlmAvailable(r.data.available)).catch(() => setLlmAvailable(false));
  }, []);

  const loadData = async () => {
    setLoading(true);
    setError(null);
    try {
      const [mRes, lRes] = await Promise.all([
        getPrompts(),
        getActiveLanguage(),
      ]);
      setMetrics(mRes.data);
      setActiveLang(lRes.data.language);
    } catch (e: any) {
      setError(e.response?.data?.detail || '加载数据失败');
    } finally {
      setLoading(false);
    }
  };

  const openDrawer = async (metric: string) => {
    setSelectedMetric(metric);
    setDrawerOpen(true);
    setMetricDetail(null);
    setEditedPrompts({});
    try {
      const res = await getMetricPrompts(metric);
      setMetricDetail(res.data);
      const edits: Record<string, string> = {};
      res.data.prompts.forEach(p => { edits[p.name] = p.zh; });
      setEditedPrompts(edits);
    } catch {
      message.error('加载提示词失败');
    }
  };

  const closeDrawer = () => {
    setDrawerOpen(false);
    setSelectedMetric(null);
    setMetricDetail(null);
    setEditedPrompts({});
  };

  const handleTranslate = async (metric: string) => {
    setTranslating(metric);
    try {
      const res = await translateMetricPrompts(metric);
      setMetricDetail(res.data);
      const edits: Record<string, string> = {};
      res.data.prompts.forEach(p => { edits[p.name] = p.zh; });
      setEditedPrompts(edits);
      message.success('AI 翻译完成');
      loadData();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '翻译失败');
    } finally {
      setTranslating(null);
    }
  };

  const handleSave = async () => {
    if (!selectedMetric || !metricDetail) return;
    setSaving(true);
    try {
      const data = metricDetail.prompts.map(p => ({
        name: p.name,
        zh: editedPrompts[p.name] || '',
      }));
      const res = await updateMetricPrompts(selectedMetric, { prompts: data });
      setMetricDetail(res.data);
      message.success('已保存');
      loadData();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
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

  const llmTooltip = llmAvailable === false ? '请先在「模型配置」页面配置并激活 LLM' : '';

  const columns = [
    {
      title: '指标名称',
      dataIndex: 'name',
      key: 'name',
      width: 120,
    },
    {
      title: '说明',
      dataIndex: 'desc',
      key: 'desc',
    },
    {
      title: '提示词数',
      dataIndex: 'prompt_count',
      key: 'prompt_count',
      width: 100,
      render: (v: number) => `${v} 个`,
    },
    {
      title: '中文状态',
      dataIndex: 'has_chinese',
      key: 'has_chinese',
      width: 100,
      render: (v: boolean) => (
        <Tag color={v ? 'success' : 'default'}>{v ? '已翻译' : '未翻译'}</Tag>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 220,
      render: (_: any, record: MetricPromptsSummary) => (
        <Space>
          <Button size="small" icon={<EyeOutlined />} onClick={() => openDrawer(record.metric)}>
            查看/编辑
          </Button>
          <Tooltip title={!llmAvailable ? llmTooltip : ''}>
            <Button
              size="small"
              icon={<TranslationOutlined />}
              loading={translating === record.metric}
              disabled={record.has_chinese || !llmAvailable}
              onClick={() => handleTranslate(record.metric)}
            >
              AI翻译
            </Button>
          </Tooltip>
        </Space>
      ),
    },
  ];

  const drawerTitle = metricDetail
    ? `${metricDetail.name} — 提示词编辑`
    : '提示词编辑';

  return (
    <div>
      {/* 顶部标题栏 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          <FileTextOutlined style={{ marginRight: 8 }} />
          提示词管理
        </Typography.Title>
        <Space>
          <Typography.Text type="secondary">评估语言：</Typography.Text>
          <Radio.Group
            value={activeLang}
            onChange={e => handleLanguageChange(e.target.value)}
            optionType="button"
            buttonStyle="solid"
            size="small"
          >
            <Radio.Button value="en">English</Radio.Button>
            <Radio.Button value="zh">中文</Radio.Button>
          </Radio.Group>
          <Button icon={<ReloadOutlined />} onClick={loadData} size="small">
            刷新
          </Button>
        </Space>
      </div>

      {/* 说明提示 */}
      <Alert
        message="每个 RAGAS 评估指标包含一个或多个提示词（Prompt）。您可以在此查看中英文内容、使用 AI 翻译为中文、手动编辑调整，并选择评估时使用的语言。"
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
      />

      {/* 表格 */}
      <Card>
        {error ? (
          <Alert message={error} type="error" showIcon />
        ) : (
          <Spin spinning={loading}>
            {metrics.length === 0 ? (
              <Typography.Text type="secondary">暂无评估指标</Typography.Text>
            ) : (
              <Table
                dataSource={metrics}
                columns={columns}
                rowKey="metric"
                pagination={false}
                size="middle"
              />
            )}
          </Spin>
        )}
      </Card>

      {/* 编辑抽屉 */}
      <Drawer
        title={drawerTitle}
        placement="right"
        width={640}
        open={drawerOpen}
        onClose={closeDrawer}
        footer={
          metricDetail ? (
            <Space style={{ float: 'right' }}>
              <Tooltip title={!llmAvailable ? llmTooltip : ''}>
                <Button
                  icon={<TranslationOutlined />}
                  loading={translating === selectedMetric}
                  disabled={!llmAvailable}
                  onClick={() => selectedMetric && handleTranslate(selectedMetric)}
                >
                  AI翻译
                </Button>
              </Tooltip>
              <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={handleSave}>
                保存
              </Button>
              <Button onClick={closeDrawer}>关闭</Button>
            </Space>
          ) : null
        }
      >
        {!metricDetail ? (
          <Spin />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size="large">
            <Typography.Text type="secondary">{metricDetail.desc}</Typography.Text>
            <Typography.Text>
              当前语言：<Tag color={activeLang === 'zh' ? 'blue' : 'green'}>{activeLang === 'zh' ? '中文' : 'English'}</Tag>
            </Typography.Text>

            {metricDetail.prompts.map((prompt, idx) => (
              <div key={prompt.name}>
                {idx > 0 && <Divider />}
                <Typography.Text strong style={{ fontSize: 15, display: 'block', marginBottom: 8 }}>
                  {prompt.name}
                </Typography.Text>

                <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                  English（只读）
                </Typography.Text>
                <Input.TextArea
                  value={prompt.en}
                  readOnly
                  rows={4}
                  style={{ fontSize: 13, marginBottom: 12, background: '#f5f5f5' }}
                />

                <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                  中文（可编辑）
                </Typography.Text>
                <Input.TextArea
                  value={editedPrompts[prompt.name] || ''}
                  onChange={e => setEditedPrompts(prev => ({ ...prev, [prompt.name]: e.target.value }))}
                  rows={4}
                  style={{ fontSize: 13 }}
                  placeholder="暂无中文翻译，点击下方「AI翻译」按钮或手动输入"
                />
              </div>
            ))}

            {metricDetail.prompts.length === 0 && (
              <Typography.Text type="secondary">该指标没有提示词</Typography.Text>
            )}
          </Space>
        )}
      </Drawer>
    </div>
  );
};

export default PromptsPage;
