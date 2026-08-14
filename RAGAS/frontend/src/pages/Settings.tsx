import React, { useEffect, useState } from 'react';
import {
  Card, Form, Input, InputNumber, Button, Typography, message, Space,
  Row, Col, Spin, Tag, Alert, Modal, Popconfirm, Tooltip,
} from 'antd';
import {
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined,
  PlusOutlined, DeleteOutlined, CheckOutlined, ThunderboltOutlined,
  EditOutlined,
} from '@ant-design/icons';
import {
  listProfiles, createProfile, updateProfile, deleteProfile,
  activateProfile, testLlmConnection, testEmbeddingConnection, testEsConnection,
  Profile,
} from '../api/client';

const { Text, Title } = Typography;
const { Password } = Input;

type TestSection = 'llm' | 'embedding' | 'es';
type TestStatus = 'idle' | 'testing' | 'success' | 'failed';

const SettingsPage: React.FC = () => {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // 编辑弹窗
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  // 测试状态（按配置ID + 测试项）
  const [testStates, setTestStates] = useState<Record<string, Record<TestSection, { status: TestStatus; msg: string }>>>({});

  const getTs = (id: string, section: TestSection) =>
    testStates[id]?.[section] || { status: 'idle' as TestStatus, msg: '' };

  const setTs = (id: string, section: TestSection, status: TestStatus, msg = '') => {
    setTestStates(prev => ({
      ...prev,
      [id]: { ...(prev[id] || {} as any), [section]: { status, msg } },
    }));
  };

  const loadProfiles = async () => {
    setLoading(true);
    try {
      const res = await listProfiles();
      setProfiles(res.data);
      const active = res.data.find((p: any) => (p as any).active);
      // 通过 /settings/profiles/active 获取
      try {
        const activeRes = await (await import('../api/client')).getActiveProfile();
        setActiveId(activeRes.data.id);
      } catch {
        setActiveId(null);
      }
    } catch {
      message.error('加载配置失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadProfiles();
  }, []);

  // 打开新建/编辑弹窗
  const openCreate = () => {
    setEditingId(null);
    form.resetFields();
    form.setFieldsValue({
      llm_base_url: '', llm_api_key: '', llm_model: '',
      llm_temperature: 0, llm_max_tokens: 8192,
      embedding_base_url: '', embedding_api_key: '', embedding_model: '',
      es_host: '', es_port: 9200, es_user: '', es_password: '',
    });
    setModalOpen(true);
  };

  const openEdit = (p: Profile) => {
    setEditingId(p.id);
    form.setFieldsValue(p);
    setModalOpen(true);
  };

  // 保存配置
  const handleSave = async () => {
    const vals = await form.validateFields();
    setSaving(true);
    try {
      const data = { ...vals, name: vals.name || (editingId ? undefined : '未命名') };
      if (editingId) {
        await updateProfile(editingId, data);
        message.success('已更新');
      } else {
        await createProfile(data);
        message.success('已创建');
      }
      setModalOpen(false);
      await loadProfiles();
    } catch (e: any) {
      if (e.response) message.error(e.response.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  // 删除
  const handleDelete = async (id: string) => {
    try {
      await deleteProfile(id);
      message.success('已删除');
      await loadProfiles();
    } catch {
      message.error('删除失败');
    }
  };

  // 激活
  const handleActivate = async (id: string) => {
    try {
      const res = await activateProfile(id);
      message.success(res.data.message);
      await loadProfiles();
    } catch {
      message.error('切换失败');
    }
  };

  // 连接测试
  const handleTest = async (p: Profile, section: TestSection) => {
    setTs(p.id, section, 'testing');
    try {
      let res;
      if (section === 'llm') {
        res = await testLlmConnection({
          llm_base_url: p.llm_base_url,
          llm_api_key: p.llm_api_key,
          llm_model: p.llm_model,
        });
      } else if (section === 'embedding') {
        res = await testEmbeddingConnection({
          embedding_base_url: p.embedding_base_url,
          embedding_api_key: p.embedding_api_key,
          embedding_model: p.embedding_model,
        });
      } else {
        res = await testEsConnection({
          es_host: p.es_host,
          es_port: p.es_port,
          es_user: p.es_user,
          es_password: p.es_password,
        });
      }
      setTs(p.id, section, 'success', res.data.message);
    } catch (e: any) {
      setTs(p.id, section, 'failed', e.response?.data?.detail || '连接失败');
    }
  };

  const statusTag = (status: TestStatus, msg: string) => {
    if (status === 'idle') return null;
    if (status === 'testing') return <Tag icon={<LoadingOutlined spin />} color="processing">测试中...</Tag>;
    return (
      <div style={{ marginTop: 4, fontSize: 12 }}>
        {status === 'success'
          ? <Text type="success"><CheckCircleFilled /> {msg}</Text>
          : <Text type="danger"><CloseCircleFilled /> {msg}</Text>
        }
      </div>
    );
  };

  const renderProfileCard = (p: Profile) => {
    const isActive = activeId === p.id;
    return (
      <Card
        key={p.id}
        size="small"
        style={{
          marginBottom: 12,
          border: isActive ? '2px solid #1677ff' : '1px solid #f0f0f0',
          background: isActive ? '#f0f5ff' : undefined,
        }}
        title={
          <Space>
            {isActive && <Tag color="blue" icon={<CheckOutlined />}>默认</Tag>}
            <Text strong>{p.name}</Text>
          </Space>
        }
        extra={
          <Space size="small">
            {!isActive && (
              <Tooltip title="设为默认">
                <Button size="small" type="primary" icon={<CheckOutlined />}
                  onClick={() => handleActivate(p.id)} />
              </Tooltip>
            )}
            <Tooltip title="编辑">
              <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(p)} />
            </Tooltip>
            <Popconfirm title="确定删除?" onConfirm={() => handleDelete(p.id)}>
              <Tooltip title="删除">
                <Button size="small" danger icon={<DeleteOutlined />}
                  disabled={isActive && profiles.length <= 1} />
              </Tooltip>
            </Popconfirm>
          </Space>
        }
      >
        <Row gutter={[16, 8]}>
          <Col xs={24}>
            <Text type="secondary" style={{ fontSize: 12 }}>LLM 评估模型</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 2 }}>
              <Text code style={{ fontSize: 12 }}>{p.llm_model || '-'}</Text>
              <Text style={{ fontSize: 12 }}>{p.llm_base_url}</Text>
              <Button size="small" type="link" style={{ fontSize: 12, padding: 0 }}
                onClick={() => handleTest(p, 'llm')}>
                测试
              </Button>
              {statusTag(getTs(p.id, 'llm').status, getTs(p.id, 'llm').msg)}
            </div>
          </Col>
          <Col xs={24}>
            <Text type="secondary" style={{ fontSize: 12 }}>Embedding 模型</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 2 }}>
              <Text code style={{ fontSize: 12 }}>{p.embedding_model || '-'}</Text>
              <Text style={{ fontSize: 12 }}>{p.embedding_base_url}</Text>
              <Button size="small" type="link" style={{ fontSize: 12, padding: 0 }}
                onClick={() => handleTest(p, 'embedding')}>
                测试
              </Button>
              {statusTag(getTs(p.id, 'embedding').status, getTs(p.id, 'embedding').msg)}
            </div>
          </Col>
          <Col xs={24}>
            <Text type="secondary" style={{ fontSize: 12 }}>向量数据库</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 2 }}>
              <Text style={{ fontSize: 12 }}>{p.es_host}:{p.es_port}</Text>
              <Button size="small" type="link" style={{ fontSize: 12, padding: 0 }}
                onClick={() => handleTest(p, 'es')}>
                测试
              </Button>
              {statusTag(getTs(p.id, 'es').status, getTs(p.id, 'es').msg)}
            </div>
          </Col>
        </Row>
      </Card>
    );
  };

  if (loading) return <div style={{ textAlign: 'center', padding: 60 }}><Spin size="large" /></div>;

  return (
    <div>
      <Title level={4}>模型配置</Title>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="可创建多个模型配置，标注「默认」的配置为评估系统当前使用的配置。点击「设为默认」切换。"
      />

      <div style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建配置
        </Button>
      </div>

      {profiles.length === 0 ? (
        <Card><Text type="secondary">暂无配置，请新建</Text></Card>
      ) : (
        profiles.map(renderProfileCard)
      )}

      {/* 新建/编辑弹窗 */}
      <Modal
        title={editingId ? '编辑配置' : '新建配置'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleSave}
        confirmLoading={saving}
        okText="保存"
        width={640}
      >
        <Form form={form} layout="vertical" size="small">
          <Form.Item name="name" label="配置名称" rules={[{ required: true, message: '必填' }]}>
            <Input placeholder="例如：本地 Qwen、DeepSeek 在线" />
          </Form.Item>

          <DividerText text="LLM 评估模型" />
          <Row gutter={12}>
            <Col span={16}>
              <Form.Item name="llm_base_url" label="API 地址">
                <Input placeholder="http://127.0.0.1:8000/v1" />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="llm_model" label="模型名称">
                <Input placeholder="Qwen3.5-9B" />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="llm_api_key" label="API Key">
                <Password placeholder="not-needed" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={6}>
              <Form.Item name="llm_temperature" label="Temperature">
                <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="llm_max_tokens" label="Max Tokens"
                tooltip="每个请求允许输出的最大 token 数，取决于模型限制">
                <InputNumber min={256} max={32768} step={256} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <DividerText text="Embedding 模型" />
          <Row gutter={12}>
            <Col span={16}>
              <Form.Item name="embedding_base_url" label="API 地址">
                <Input placeholder="http://127.0.0.1:8300/v1" />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="embedding_model" label="模型名称">
                <Input placeholder="bge-m3" />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="embedding_api_key" label="API Key">
                <Password placeholder="not-needed" />
              </Form.Item>
            </Col>
          </Row>

          <DividerText text="向量数据库（可选，仅开启「真实检索链路」时需要）" />
          <Row gutter={12}>
            <Col span={8}>
              <Form.Item name="es_host" label="Host">
                <Input placeholder="localhost" />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="es_port" label="Port">
                <InputNumber min={1} max={65535} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="es_user" label="用户名">
                <Input placeholder="elastic" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="es_password" label="密码">
                <Password placeholder="<change-me>" />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>
    </div>
  );
};

const DividerText: React.FC<{ text: string }> = ({ text }) => (
  <div style={{
    fontSize: 13, fontWeight: 500, color: '#1677ff', margin: '12px 0 8px',
    paddingBottom: 4, borderBottom: '1px solid #e8e8e8',
  }}>{text}</div>
);

export default SettingsPage;
