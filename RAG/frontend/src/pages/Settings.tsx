import React, { useCallback, useEffect, useState } from 'react';
import {
  App as AntApp, Card, Form, Input, InputNumber, Button, Typography, Space,
  Row, Col, Skeleton, Tag, Alert, Modal, Popconfirm, Tooltip, Collapse, Select,
  Radio, Empty, theme,
} from 'antd';
import {
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined,
  PlusOutlined, DeleteOutlined, CheckOutlined, EditOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import {
  listProfiles, createProfile, updateProfile, deleteProfile,
  activateProfile, testProfileConnection, testLlmConnection, getEmbeddingDim,
  getChatSettings, updateChatSettings,
  ServiceProfile, ServiceProfileInput, ProfileTestResult, LLMModelItem,
} from '../api/client';
import { useAuth } from '../auth/AuthContext';
import PageHeader from '../components/PageHeader';

const { Text } = Typography;
const { Password } = Input;

interface TestItem {
  status: 'idle' | 'testing' | 'success' | 'failed';
  msg: string;
}

type SectionKey = 'llm' | 'embedding' | 'mineru' | 'deepdoc' | 'mysql' | 'minio';

const emptyTest: Record<SectionKey, TestItem> = {
  llm: { status: 'idle', msg: '' },
  embedding: { status: 'idle', msg: '' },
  mineru: { status: 'idle', msg: '' },
  deepdoc: { status: 'idle', msg: '' },
  mysql: { status: 'idle', msg: '' },
  minio: { status: 'idle', msg: '' },
};

// 测试结果 -> 展示项
const toTestItems = (res: ProfileTestResult): Record<SectionKey, TestItem> => ({
  llm: { status: res.llm.ok ? 'success' : 'failed', msg: res.llm.message },
  embedding: { status: res.embedding.ok ? 'success' : 'failed', msg: res.embedding.message },
  mineru: { status: res.mineru.ok ? 'success' : 'failed', msg: res.mineru.message },
  deepdoc: { status: res.deepdoc.ok ? 'success' : 'failed', msg: res.deepdoc.message },
  mysql: { status: res.mysql.ok ? 'success' : 'failed', msg: res.mysql.message },
  minio: { status: res.minio.ok ? 'success' : 'failed', msg: res.minio.message },
});

const sectionLabel: Record<SectionKey, string> = {
  llm: 'LLM 对话模型',
  embedding: 'Embedding 模型',
  mineru: 'MinerU 文档解析',
  deepdoc: 'DeepDoc 解析（RAGFlow）',
  mysql: 'MySQL 数据库',
  minio: 'MinIO 对象存储',
};

// 全部段测试中
const allTesting = (): Record<SectionKey, TestItem> => ({
  llm: { status: 'testing', msg: '' },
  embedding: { status: 'testing', msg: '' },
  mineru: { status: 'testing', msg: '' },
  deepdoc: { status: 'testing', msg: '' },
  mysql: { status: 'testing', msg: '' },
  minio: { status: 'testing', msg: '' },
});

// 全部段同一失败信息（接口整体报错时）
const allFailed = (msg: string): Record<SectionKey, TestItem> => ({
  llm: { status: 'failed', msg },
  embedding: { status: 'failed', msg },
  mineru: { status: 'failed', msg },
  deepdoc: { status: 'failed', msg },
  mysql: { status: 'failed', msg },
  minio: { status: 'failed', msg },
});

// 表单扁平字段 <-> 嵌套档案对象互转
const toProfileInput = (vals: any, llmSection?: {
  models: LLMModelItem[]; active: number;
}): ServiceProfileInput => ({
  name: vals.name,
  // llm 段为模型列表结构：未添加模型时省略（后端用 .env 出厂默认单模型）
  llm: llmSection && llmSection.models.length
    ? { models: llmSection.models, active: llmSection.active }
    : undefined,
  embedding: {
    base_url: vals.embedding_base_url,
    api_key: vals.embedding_api_key,
    model: vals.embedding_model,
    dimension: vals.embedding_dimension,
  },
  mineru: { url: vals.mineru_url, timeout: vals.mineru_timeout },
  deepdoc: {
    base_url: vals.deepdoc_base_url,
    email: vals.deepdoc_email,
    password: vals.deepdoc_password,
    timeout: vals.deepdoc_timeout,
    dataset_prefix: vals.deepdoc_dataset_prefix || undefined,
  },
  retrieval: {
    top_k: vals.retrieval_top_k,
    enable_hybrid: vals.retrieval_enable_hybrid,
    rerank: {
      enabled: vals.rerank_enabled,
      base_url: vals.rerank_base_url,
      model: vals.rerank_model,
      top_n: vals.rerank_top_n,
    },
  },
  chunking: { chunk_size: vals.chunk_size, overlap: vals.chunk_overlap },
  mysql: {
    host: vals.mysql_host,
    port: vals.mysql_port,
    user: vals.mysql_user,
    password: vals.mysql_password,
    database: vals.mysql_database,
    url: vals.mysql_url || undefined,
  },
  minio: {
    endpoint: vals.minio_endpoint,
    access_key: vals.minio_access_key,
    secret_key: vals.minio_secret_key,
    bucket: vals.minio_bucket,
    secure: vals.minio_secure,
    region: vals.minio_region || '',
  },
});

const toFormValues = (p: ServiceProfile) => ({
  name: p.name,
  embedding_base_url: p.embedding?.base_url,
  embedding_api_key: p.embedding?.api_key,
  embedding_model: p.embedding?.model,
  embedding_dimension: p.embedding?.dimension,
  mineru_url: p.mineru?.url,
  mineru_timeout: p.mineru?.timeout,
  deepdoc_base_url: p.deepdoc?.base_url,
  deepdoc_email: p.deepdoc?.email,
  deepdoc_password: p.deepdoc?.password,
  deepdoc_timeout: p.deepdoc?.timeout,
  deepdoc_dataset_prefix: p.deepdoc?.dataset_prefix,
  retrieval_top_k: p.retrieval?.top_k,
  retrieval_enable_hybrid: p.retrieval?.enable_hybrid ?? true,
  rerank_enabled: p.retrieval?.rerank?.enabled ?? false,
  rerank_base_url: p.retrieval?.rerank?.base_url ?? '',
  rerank_model: p.retrieval?.rerank?.model ?? '',
  rerank_top_n: p.retrieval?.rerank?.top_n ?? 10,
  chunk_size: p.chunking?.chunk_size,
  chunk_overlap: p.chunking?.overlap,
  mysql_host: p.mysql?.host,
  mysql_port: p.mysql?.port,
  mysql_user: p.mysql?.user,
  mysql_password: p.mysql?.password,
  mysql_database: p.mysql?.database,
  mysql_url: p.mysql?.url,
  minio_endpoint: p.minio?.endpoint,
  minio_access_key: p.minio?.access_key,
  minio_secret_key: p.minio?.secret_key,
  minio_bucket: p.minio?.bucket,
  minio_secure: p.minio?.secure,
  minio_region: p.minio?.region,
});

const SettingsPage: React.FC = () => {
  const { message, modal } = AntApp.useApp();
  const { token } = theme.useToken();
  const { user } = useAuth();
  // 档案卡片只读模式：dept_admin 可查看配置与连接测试，修改档案仅 super_admin
  const readOnly = user?.role !== 'super_admin';
  // 部门管理员：可配置本部门 LLM 段（其余基础设施段仍只读）
  const isDeptAdmin = user?.role === 'dept_admin';
  const [profiles, setProfiles] = useState<ServiceProfile[]>([]);
  const [loading, setLoading] = useState(false);

  // 本部门 LLM 配置表单（dept_admin 专属：GET /api/settings/chat 合并值回填）
  const [deptLlmForm] = Form.useForm();
  const [deptLlmLoading, setDeptLlmLoading] = useState(false);
  const [deptLlmSaving, setDeptLlmSaving] = useState(false);

  // 编辑弹窗
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  // ---- LLM 多模型管理（编辑弹窗内：模型列表 + 激活索引） ----
  const [llmModels, setLlmModels] = useState<LLMModelItem[]>([]);
  const [llmActive, setLlmActive] = useState(0);
  // 激活流程中正在测试连接的模型索引（防重复点击）
  const [llmTestingIdx, setLlmTestingIdx] = useState<number | null>(null);
  // 模型添加/编辑弹窗
  const [modelModalOpen, setModelModalOpen] = useState(false);
  const [modelEditIdx, setModelEditIdx] = useState<number | null>(null);
  const [modelForm] = Form.useForm();

  // 卡片连接测试状态（按档案 id）
  const [testStates, setTestStates] = useState<Record<string, Record<SectionKey, TestItem>>>({});
  // 弹窗内连接测试状态
  const [modalTest, setModalTest] = useState<Record<SectionKey, TestItem>>(emptyTest);
  const [modalTesting, setModalTesting] = useState(false);
  // 当前激活 embedding 模型的实际输出维度（实测，供维度冲突核对）
  const [embeddingDim, setEmbeddingDim] = useState<number | null>(null);
  const [embeddingDimMsg, setEmbeddingDimMsg] = useState('');

  const loadProfiles = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listProfiles();
      setProfiles(res.data);
    } catch {
      message.error('加载配置失败');
    } finally {
      setLoading(false);
    }
    // 当前激活 embedding 模型实际输出维度（实测；更换模型后此处用于核对冲突）
    try {
      const dimRes = await getEmbeddingDim();
      setEmbeddingDim(dimRes.data.dimension);
      setEmbeddingDimMsg(dimRes.data.ok ? '' : dimRes.data.message);
    } catch {
      // 非 fatal：维度检测失败不阻塞配置页
    }
  }, []);

  useEffect(() => {
    loadProfiles();
  }, [loadProfiles]);

  // ---- 本部门 LLM 配置（dept_admin 可编辑；超管/普通成员无此卡片） ----
  const loadDeptLlm = useCallback(async () => {
    if (!isDeptAdmin) return;
    setDeptLlmLoading(true);
    try {
      // 合并值回填：未设置字段显示全局值；api_key 为脱敏值（保存时原样回传=不覆盖）
      const res = await getChatSettings();
      const llm = res.data.llm ?? {};
      deptLlmForm.setFieldsValue({
        llm_base_url: llm.base_url ?? '',
        llm_api_key: llm.api_key ?? '',
        llm_model: llm.model ?? '',
        llm_temperature: llm.temperature ?? undefined,
        llm_max_tokens: llm.max_tokens ?? undefined,
        llm_timeout: llm.timeout ?? undefined,
      });
    } catch (e: any) {
      message.error(e.response?.data?.detail || '加载本部门 LLM 配置失败');
    } finally {
      setDeptLlmLoading(false);
    }
  }, [isDeptAdmin, deptLlmForm, message]);

  useEffect(() => {
    loadDeptLlm();
  }, [loadDeptLlm]);

  const saveDeptLlm = async () => {
    const vals = await deptLlmForm.validateFields();
    setDeptLlmSaving(true);
    try {
      // 只提交 llm 段（后端白名单 6 字段）：空串/null = 跟随全局；
      // api_key 脱敏值原样回传 = 保留部门原值
      await updateChatSettings({
        llm: {
          base_url: vals.llm_base_url ?? '',
          api_key: vals.llm_api_key ?? '',
          model: vals.llm_model ?? '',
          temperature: vals.llm_temperature ?? null,
          max_tokens: vals.llm_max_tokens ?? null,
          timeout: vals.llm_timeout ?? null,
        },
      });
      message.success('本部门 LLM 配置已保存，对本部门成员即时生效');
      await loadDeptLlm();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '保存本部门 LLM 配置失败');
    } finally {
      setDeptLlmSaving(false);
    }
  };

  // ---- LLM 模型操作（添加/编辑/删除/勾选激活） ----
  const openModelEdit = (idx: number | null) => {
    setModelEditIdx(idx);
    modelForm.resetFields();
    if (idx !== null && llmModels[idx]) {
      const m = llmModels[idx];
      modelForm.setFieldsValue({
        model_name: m.name, model_base_url: m.base_url,
        model_api_key: m.api_key, model_model: m.model,
        model_temperature: m.temperature, model_max_tokens: m.max_tokens,
        model_timeout: m.timeout,
      });
    } else {
      modelForm.setFieldsValue({
        model_temperature: 0.3, model_max_tokens: 4096, model_timeout: 120,
      });
    }
    setModelModalOpen(true);
  };

  const saveModel = async () => {
    const v = await modelForm.validateFields();
    const item: LLMModelItem = {
      name: (v.model_name ?? '').trim(),
      base_url: (v.model_base_url ?? '').trim(),
      api_key: v.model_api_key || '',
      model: (v.model_model ?? '').trim(),
      temperature: v.model_temperature ?? 0.3,
      max_tokens: v.model_max_tokens ?? 4096,
      timeout: v.model_timeout ?? 120,
    };
    setLlmModels(prev => {
      const next = [...prev];
      if (modelEditIdx !== null && next[modelEditIdx]) {
        // 编辑：api_key 留空 = 保留原值（回填的脱敏值原样保留）
        next[modelEditIdx] = { ...item, api_key: item.api_key || next[modelEditIdx].api_key };
      } else {
        next.push(item);
        if (next.length === 1) setLlmActive(0); // 从空添加第一个 → 自动激活
      }
      return next;
    });
    setModelModalOpen(false);
  };

  const deleteModel = (idx: number) => {
    setLlmModels(prev => {
      if (prev.length <= 1) return prev; // 至少保留 1 个模型
      const next = prev.filter((_, i) => i !== idx);
      setLlmActive(a => (idx === a ? 0 : idx < a ? a - 1 : a));
      return next;
    });
  };

  /** 勾选激活：先测连接（GET {base_url}/models）→ 成功直接激活；
   *  失败弹原因 + 可确认强制激活（管理员自行判断网络抖动等场景） */
  const activateModel = async (idx: number) => {
    if (idx === llmActive || llmTestingIdx !== null) return;
    const item = llmModels[idx];
    if (!item) return;
    setLlmTestingIdx(idx);
    const confirmForce = (reason: string) => {
      modal.confirm({
        title: `连接失败，确认激活「${item.name}」？`,
        content: reason,
        okText: '仍要激活',
        cancelText: '取消',
        onOk: () => {
          setLlmActive(idx);
          message.success(`已激活「${item.name}」`);
        },
      });
    };
    try {
      const res = await testLlmConnection(item);
      if (res.data.ok) {
        setLlmActive(idx);
        message.success(`已激活「${item.name}」（连接成功，${res.data.latency_ms}ms）`);
      } else {
        confirmForce(res.data.reason);
      }
    } catch (e: any) {
      confirmForce(e.response?.data?.detail || '网络请求失败，请检查服务是否可达');
    } finally {
      setLlmTestingIdx(null);
    }
  };

  // ---- 新建/编辑 ----
  const openCreate = () => {
    setEditingId(null);
    form.resetFields();
    setLlmModels([]);
    setLlmActive(0);
    form.setFieldsValue({
      embedding_dimension: 1024,
      mineru_timeout: 300,
      // DeepDoc 预填后端默认值（密码留空，保存时后端保持原值）
      deepdoc_base_url: 'http://127.0.0.1:9380',
      deepdoc_email: '',
      deepdoc_timeout: 300,
      deepdoc_dataset_prefix: 'myrag-tmp-',
      retrieval_top_k: 5,
      retrieval_enable_hybrid: true,
      rerank_enabled: false,
      rerank_top_n: 10,
      chunk_size: 800, chunk_overlap: 100,
      // MySQL / MinIO 预填后端默认值（密码类留空，保存时后端用默认或保持原值）
      mysql_host: '127.0.0.1', mysql_port: 5455, mysql_user: 'ragflow',
      mysql_database: 'my_rag',
      minio_endpoint: '127.0.0.1:9000', minio_access_key: '',
      minio_bucket: 'my-rag', minio_secure: false, minio_region: '',
    });
    setModalTest(emptyTest);
    setModalOpen(true);
  };

  const openEdit = (p: ServiceProfile) => {
    setEditingId(p.id);
    form.setFieldsValue(toFormValues(p));
    // llm 段回填（后端已迁移为 {models, active} 结构）
    const sec = p.llm as unknown as { models?: LLMModelItem[]; active?: number };
    const models = Array.isArray(sec?.models) && sec.models.length
      ? sec.models : [];
    setLlmModels(models);
    setLlmActive(sec?.active ?? 0);
    setModalTest(emptyTest);
    setModalOpen(true);
  };

  const doSave = async (vals: any) => {
    setSaving(true);
    try {
      const llmSection = llmModels.length
        ? { models: llmModels, active: llmActive } : undefined;
      const data = toProfileInput(vals, llmSection);
      if (editingId) {
        await updateProfile(editingId, data);
        message.success('配置档案已更新');
      } else {
        await createProfile(data as ServiceProfileInput & { name: string });
        message.success('配置档案已创建');
      }
      setModalOpen(false);
      await loadProfiles();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleSave = async () => {
    const vals = await form.validateFields();
    await doSave(vals);
  };

  // ---- 激活 / 删除 ----
  /** 切换「当前使用」：二次确认后执行（一键切换影响所有用户的检索与对话，企业验收反馈） */
  const handleActivate = (p: ServiceProfile) => {
    modal.confirm({
      title: `确认切换为「${p.name}」？`,
      content: '切换后所有用户的检索与对话将立即使用该配置档案',
      okText: '确认切换',
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await activateProfile(p.id);
          message.success(res.data.message);
          await loadProfiles();
        } catch {
          message.error('切换失败');
        }
      },
    });
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteProfile(id);
      message.success('已删除');
      await loadProfiles();
    } catch {
      message.error('删除失败');
    }
  };

  // ---- 连接测试（卡片：按档案已保存值） ----
  const handleTest = async (p: ServiceProfile) => {
    setTestStates(prev => ({ ...prev, [p.id]: allTesting() }));
    try {
      const res = await testProfileConnection(p.id);
      setTestStates(prev => ({ ...prev, [p.id]: toTestItems(res.data) }));
    } catch (e: any) {
      const msg = e.response?.data?.detail || '测试失败';
      setTestStates(prev => ({ ...prev, [p.id]: allFailed(msg) }));
    }
  };

  // ---- 连接测试（弹窗：传表单未保存值） ----
  const handleModalTest = async () => {
    if (!editingId) {
      message.warning('请先保存档案后再测试');
      return;
    }
    setModalTesting(true);
    setModalTest(allTesting());
    try {
      const vals = await form.validateFields();
      const llmSection = llmModels.length
        ? { models: llmModels, active: llmActive } : undefined;
      const res = await testProfileConnection(
        editingId, toProfileInput(vals, llmSection));
      setModalTest(toTestItems(res.data));
    } catch (e: any) {
      const msg = e.response?.data?.detail || '测试失败';
      setModalTest(allFailed(msg));
    } finally {
      setModalTesting(false);
    }
  };

  const renderTestLine = (item: TestItem, label: string) => {
    if (item.status === 'idle') return null;
    return (
      <div style={{ marginTop: 2, fontSize: 12 }}>
        <Text type="secondary" style={{ marginRight: 8 }}>{label}:</Text>
        {item.status === 'testing' ? (
          <Tag icon={<LoadingOutlined spin />} color="processing">测试中...</Tag>
        ) : item.status === 'success' ? (
          <Text type="success"><CheckCircleFilled /> {item.msg}</Text>
        ) : (
          <Text type="danger"><CloseCircleFilled /> {item.msg}</Text>
        )}
      </div>
    );
  };

  const renderProfileCard = (p: ServiceProfile) => {
    const isActive = p.active;
    const tests = testStates[p.id] || emptyTest;
    // LLM 段摘要：激活模型（后端已统一为 {models, active} 结构）
    const llmSec = p.llm as unknown as {
      models?: LLMModelItem[]; active?: number;
    };
    const llmModelList = Array.isArray(llmSec?.models) ? llmSec.models : [];
    const activeLlm = llmModelList[llmSec?.active ?? 0] ?? null;
    return (
      <Card
        key={p.id}
        size="small"
        style={{
          marginBottom: 12,
          border: isActive ? '2px solid var(--brand-primary, #2563eb)' : '1px solid #eef2f7',
          background: isActive ? 'rgba(var(--brand-primary-rgb, 37, 99, 235), 0.06)' : undefined,
          boxShadow: '0 1px 3px rgba(16,24,40,0.04)',
          transition: 'border-color 0.2s ease, box-shadow 0.2s ease',
        }}
        title={
          <Space>
            {isActive && <Tag color="blue" icon={<CheckOutlined />}>当前使用</Tag>}
            <Text strong>{p.name}</Text>
          </Space>
        }
        extra={
          <Space size="small">
            {!isActive && !readOnly && (
              <Tooltip title="设为当前使用">
                <Button size="small" type="primary" icon={<CheckOutlined />}
                  onClick={() => handleActivate(p)} />
              </Tooltip>
            )}
            {!readOnly && (
              <Tooltip title="编辑">
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(p)} />
              </Tooltip>
            )}
            <Tooltip title="连接测试">
              <Button size="small" icon={<ThunderboltOutlined />}
                onClick={() => handleTest(p)}>
                测试连接
              </Button>
            </Tooltip>
            {!readOnly && (
              <Popconfirm title="确定删除该配置档案?" onConfirm={() => handleDelete(p.id)}>
                <Tooltip title="删除">
                  <Button size="small" danger icon={<DeleteOutlined />}
                    disabled={isActive && profiles.length <= 1} />
                </Tooltip>
              </Popconfirm>
            )}
          </Space>
        }
      >
        <Row gutter={[16, 4]}>
          <Col xs={24} md={12}>
            <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.llm}</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              {activeLlm ? (
                <>
                  <Tag color="blue" style={{ fontSize: 11 }}>激活: {activeLlm.name}</Tag>
                  <Text code style={{ fontSize: 12 }}>{activeLlm.model || '-'}</Text>
                  <Text style={{ fontSize: 12 }}>{activeLlm.base_url}</Text>
                  <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>
                    {llmModelList.length > 1 ? `共 ${llmModelList.length} 个模型` : '单模型'}
                  </Text>
                </>
              ) : (
                <Text style={{ fontSize: 12 }}>-</Text>
              )}
            </div>
          </Col>
          <Col xs={24} md={12}>
            <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.embedding}</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <Text code style={{ fontSize: 12 }}>{p.embedding?.model || '-'}</Text>
              <Text style={{ fontSize: 12 }}>{p.embedding?.base_url}</Text>
              <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>dim={p.embedding?.dimension}</Text>
            </div>
          </Col>
          <Col xs={24} md={12}>
            <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.mineru}</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <Text style={{ fontSize: 12 }}>{p.mineru?.url || '-'}</Text>
            </div>
          </Col>
          <Col xs={24} md={12}>
            <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.deepdoc}</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <Text style={{ fontSize: 12 }}>{p.deepdoc?.base_url || '-'}</Text>
            </div>
          </Col>
          <Col xs={24} md={12}>
            <Text type="secondary" style={{ fontSize: 12 }}>检索 / 切块</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <Text style={{ fontSize: 12 }}>top_k={p.retrieval?.top_k}</Text>
              <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>
                hybrid={p.retrieval?.enable_hybrid === false ? '关' : '开'}
              </Text>
              <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>
                rerank={p.retrieval?.rerank?.enabled
                  ? `开(${p.retrieval?.rerank?.model || '-'})` : '关'}
              </Text>
              <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>chunk={p.chunking?.chunk_size}/{p.chunking?.overlap}</Text>
            </div>
          </Col>
          <Col xs={24} md={12}>
            <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.mysql}</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <Text style={{ fontSize: 12 }}>
                {p.mysql ? `${p.mysql.host}:${p.mysql.port}/${p.mysql.database}` : '-'}
              </Text>
            </div>
          </Col>
          <Col xs={24} md={12}>
            <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.minio}</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <Text style={{ fontSize: 12 }}>
                {p.minio ? `${p.minio.endpoint}/${p.minio.bucket}` : '-'}
              </Text>
            </div>
          </Col>
          <Col xs={24}>
            {renderTestLine(tests.llm, sectionLabel.llm)}
            {renderTestLine(tests.embedding, sectionLabel.embedding)}
            {renderTestLine(tests.mineru, sectionLabel.mineru)}
            {renderTestLine(tests.deepdoc, sectionLabel.deepdoc)}
            {renderTestLine(tests.mysql, sectionLabel.mysql)}
            {renderTestLine(tests.minio, sectionLabel.minio)}
          </Col>
        </Row>
      </Card>
    );
  };

  if (loading) return <Skeleton active paragraph={{ rows: 12 }} />;

  return (
    <div>
      <PageHeader
        title="系统配置"
        description="管理服务配置档案：LLM / Embedding / 存储与检索参数"
        extra={
          !readOnly && (
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新建配置档案
            </Button>
          )
        }
      />

      {readOnly && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message={isDeptAdmin
            ? "您正在配置本部门配置（对本部门所有成员生效）；未设置的项使用超级管理员全局配置；其他系统配置仅超管可修改，聊天设置请在聊天页面配置。"
            : "您正在查看系统配置（只读）。仅超级管理员可修改系统配置；聊天相关配置请在聊天页面设置。"}
        />
      )}
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 8 }}
        message="服务配置档案：修改并保存后即时生效（无需重启服务），切换「当前使用」即切换整套配置。"
        description="支持多套配置档案（例如：本地 Qwen、云端 DeepSeek），api_key 保存后仅显示脱敏值；「测试连接」可逐项验证 LLM / Embedding / MinerU / DeepDoc 是否可用。"
      />
      <Typography.Paragraph style={{ marginBottom: 16 }}>
        {embeddingDim != null ? (
          <Text>
            当前模型维度（已检测）：<Text strong>{embeddingDim} 维</Text>
            <Text type="secondary">
              {' '}
              —— 更换 Embedding 模型后若知识库出现「维度不匹配」，请在知识库管理页「重建向量」
            </Text>
          </Text>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>
            当前模型维度：{embeddingDimMsg || '检测中…'}
          </Text>
        )}
      </Typography.Paragraph>

      {/* 本部门 LLM 配置（dept_admin）：字段留空 = 跟随超管全局配置 */}
      {isDeptAdmin && (
        <Card
          size="small"
          style={{ marginBottom: 12 }}
          title={
            <Space>
              <Tag color="blue">本部门配置</Tag>
              <Text strong>LLM 对话模型（OpenAI 兼容）</Text>
            </Space>
          }
          extra={
            <Button type="primary" size="small" loading={deptLlmSaving}
              onClick={saveDeptLlm}>
              保存
            </Button>
          }
        >
          <Form form={deptLlmForm} layout="vertical" size="small"
            disabled={deptLlmLoading}>
            <Row gutter={12}>
              <Col span={12}>
                <Form.Item name="llm_base_url" label="API 地址">
                  <Input placeholder="留空使用全局配置" />
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item name="llm_model" label="模型名称">
                  <Input placeholder="留空使用全局配置" />
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item
                  name="llm_api_key"
                  label="API Key"
                  tooltip="留空使用全局配置；填写后本部门成员使用该密钥（保存后仅显示脱敏值）"
                >
                  <Password placeholder="留空使用全局配置" />
                </Form.Item>
              </Col>
            </Row>
            <Row gutter={12}>
              <Col span={6}>
                <Form.Item name="llm_temperature" label="Temperature">
                  <InputNumber min={0} max={2} step={0.1}
                    style={{ width: '100%' }} placeholder="跟随全局" />
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item name="llm_max_tokens" label="Max Tokens">
                  <InputNumber min={64} max={32768} step={128}
                    style={{ width: '100%' }} placeholder="跟随全局" />
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item name="llm_timeout" label="超时（秒）">
                  <InputNumber min={1} max={600}
                    style={{ width: '100%' }} placeholder="跟随全局" />
                </Form.Item>
              </Col>
            </Row>
          </Form>
        </Card>
      )}

      {profiles.length === 0 ? (
        <Card><Text type="secondary">{readOnly ? '暂无配置档案' : '暂无配置档案，请新建'}</Text></Card>
      ) : (
        profiles.map(renderProfileCard)
      )}

      {/* 新建/编辑弹窗：固定高度（7 个 Collapse 面板默认全展开，内容超高），
          头部/关闭按钮固定，滚动只在内容区内部（滚动结构修复见
          index.css .profile-config-modal，与 .chunk-detail-modal 同一套规则） */}
      <Modal
        className="profile-config-modal"
        title={editingId ? '编辑配置档案' : '新建配置档案'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleSave}
        confirmLoading={saving}
        okText="保存"
        width={720}
        style={{ top: '8vh', height: 'min(88vh, calc(100vh - 120px))' }}
        styles={{
          content: { display: 'flex', flexDirection: 'column', height: '100%' },
          header: { flexShrink: 0 },
          body: { padding: '16px 20px', flex: 1, minHeight: 0, overflow: 'auto' },
          footer: { flexShrink: 0 },
        }}
      >
        <Form form={form} layout="vertical" size="small" disabled={readOnly}>
          <Collapse
            size="small"
            defaultActiveKey={['base', 'llm', 'embedding', 'mineru', 'deepdoc', 'mysql', 'minio']}
            items={[
              {
                key: 'base',
                label: '基础服务',
                children: (
                  <>
                    <Form.Item
                      name="name"
                      label="档案名称"
                      rules={[{ required: true, message: '请输入档案名称' }]}
                    >
                      <Input placeholder="例如：本地 Qwen 默认、云端 DeepSeek" />
                    </Form.Item>
                    <Row gutter={12}>
                      <Col span={6}>
                        <Form.Item name="retrieval_top_k" label="检索 top_k">
                          <InputNumber min={1} max={50} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item
                          name="retrieval_enable_hybrid"
                          label="混合检索（BM25+向量）"
                          tooltip="开启后关键词检索（BM25）与向量检索 RRF 融合，关键词精准命中也能找回"
                        >
                          <Select
                            options={[
                              { value: true, label: '开启' },
                              { value: false, label: '关闭' },
                            ]}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item
                          name="rerank_enabled"
                          label="Rerank 重排序"
                          tooltip="开启且服务地址/模型均填写才生效；调用失败自动降级为原排序，不影响检索"
                        >
                          <Select
                            options={[
                              { value: true, label: '开启' },
                              { value: false, label: '关闭' },
                            ]}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name="rerank_top_n" label="重排候选数 top_n">
                          <InputNumber min={1} max={100} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={12}>
                      <Col span={14}>
                        <Form.Item
                          name="rerank_base_url"
                          label="Rerank 服务地址（OpenAI 兼容）"
                          tooltip="如 http://127.0.0.1:8300/v1，POST {base_url}/rerank"
                        >
                          <Input placeholder="http://127.0.0.1:8300/v1" />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name="rerank_model" label="Rerank 模型">
                          <Input placeholder="bge-reranker-v2-m3" />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={12}>
                      <Col span={6}>
                        <Form.Item name="chunk_size" label="切块大小（字符）">
                          <InputNumber min={100} max={4000} step={100} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name="chunk_overlap" label="切块重叠（字符）">
                          <InputNumber min={0} max={1000} step={50} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>
                ),
              },
              {
                key: 'llm',
                label: 'LLM 对话模型（多模型管理）',
                children: (
                  <>
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 8 }}
                      message="可添加多个模型；勾选激活的模型用于问答 / 上下文摘要 / 评估等全部 LLM 场景。激活时自动测试连接（GET {base_url}/models），连接失败可确认后仍激活。"
                    />
                    {llmModels.length === 0 ? (
                      <Empty
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                        style={{ margin: '8px 0' }}
                        description="未添加模型：保存档案后将使用系统出厂默认模型"
                      />
                    ) : (
                      llmModels.map((m, i) => (
                        <div
                          key={i}
                          style={{
                            display: 'flex', alignItems: 'center', gap: 12,
                            padding: '8px 12px', marginBottom: 8,
                            borderRadius: 6,
                            border: llmActive === i
                              ? '1px solid var(--brand-primary, #2563eb)'
                              : '1px solid #eef2f7',
                            background: llmActive === i
                              ? 'rgba(37, 99, 235, 0.05)' : undefined,
                          }}
                        >
                          <Tooltip title={llmActive === i ? '已激活（勾选可切换）' : '勾选激活（先测试连接）'}>
                            <Radio
                              checked={llmActive === i}
                              disabled={llmTestingIdx !== null}
                              onClick={() => activateModel(i)}
                            />
                          </Tooltip>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <Space>
                              <Text strong style={{ fontSize: 13 }}>{m.name}</Text>
                              <Text code style={{ fontSize: 12 }}>{m.model}</Text>
                              {llmActive === i && (
                                <Tag color="blue" icon={<CheckOutlined />}>激活中</Tag>
                              )}
                              {llmTestingIdx === i && (
                                <Tag icon={<LoadingOutlined spin />} color="processing">测试中</Tag>
                              )}
                            </Space>
                            <div style={{
                              fontSize: 12, color: token.colorTextTertiary,
                              overflow: 'hidden', textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}>
                              {m.base_url}　Key: {m.api_key || '-'}
                            </div>
                          </div>
                          <Space size="small">
                            <Button size="small" icon={<EditOutlined />}
                              onClick={() => openModelEdit(i)} />
                            <Popconfirm title="确定删除该模型?" onConfirm={() => deleteModel(i)}>
                              <Tooltip title={llmModels.length <= 1 ? '至少保留 1 个模型' : '删除'}>
                                <Button size="small" danger icon={<DeleteOutlined />}
                                  disabled={llmModels.length <= 1} />
                              </Tooltip>
                            </Popconfirm>
                          </Space>
                        </div>
                      ))
                    )}
                    <Button size="small" icon={<PlusOutlined />}
                      onClick={() => openModelEdit(null)}>
                      添加模型
                    </Button>
                  </>
                ),
              },
              {
                key: 'embedding',
                label: 'Embedding 模型（OpenAI 兼容）',
                children: (
                  <>
                    <Row gutter={12}>
                      <Col span={14}>
                        <Form.Item
                          name="embedding_base_url"
                          label="API 地址"
                          rules={[{ required: true, message: '必填' }]}
                        >
                          <Input placeholder="http://127.0.0.1:8300/v1" />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item
                          name="embedding_model"
                          label="模型名称"
                          rules={[{ required: true, message: '必填' }]}
                        >
                          <Input placeholder="bge-m3" />
                        </Form.Item>
                      </Col>
                      <Col span={4}>
                        <Form.Item
                          name="embedding_api_key"
                          label="API Key"
                          tooltip="保存后仅显示脱敏值；不修改请留空"
                        >
                          <Password placeholder="***" />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={12}>
                      <Col span={6}>
                        <Form.Item name="embedding_dimension" label="向量维度">
                          <InputNumber min={64} max={8192} step={64} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>
                ),
              },
              {
                key: 'mineru',
                label: 'MinerU 文档解析',
                children: (
                  <Row gutter={12}>
                    <Col span={14}>
                      <Form.Item name="mineru_url" label="服务地址">
                        <Input placeholder="http://localhost:8001" />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item name="mineru_timeout" label="超时（秒）">
                        <InputNumber min={10} max={3600} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  </Row>
                ),
              },
              {
                key: 'deepdoc',
                label: 'DeepDoc 解析（RAGFlow）',
                children: (
                  <>
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12 }}
                      message="DeepDoc 通过 RAGFlow 服务解析 PDF，表格输出为可检索的 HTML（vs MinerU 表格为图片不可检索）"
                    />
                    <Row gutter={12}>
                      <Col span={12}>
                        <Form.Item name="deepdoc_base_url" label="服务地址">
                          <Input placeholder="http://127.0.0.1:9380" />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name="deepdoc_timeout" label="超时（秒）">
                          <InputNumber min={30} max={3600} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={12}>
                      <Col span={6}>
                        <Form.Item name="deepdoc_email" label="邮箱">
                          <Input placeholder="user@example.com" />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item
                          name="deepdoc_password"
                          label="密码"
                          tooltip="保存后仅显示脱敏值；不修改请留空"
                        >
                          <Password placeholder="******" />
                        </Form.Item>
                      </Col>
                      <Col span={8}>
                        <Form.Item
                          name="deepdoc_dataset_prefix"
                          label="临时数据集前缀"
                          tooltip="解析时在 RAGFlow 创建临时数据集（自动清理），此前缀便于排查残留"
                        >
                          <Input placeholder="myrag-tmp-" />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>
                ),
              },
              {
                key: 'mysql',
                label: '数据库（MySQL）',
                children: (
                  <>
                    <Row gutter={12}>
                      <Col span={10}>
                        <Form.Item name="mysql_host" label="主机">
                          <Input placeholder="127.0.0.1" />
                        </Form.Item>
                      </Col>
                      <Col span={4}>
                        <Form.Item name="mysql_port" label="端口">
                          <InputNumber min={1} max={65535} style={{ width: '100%' }} placeholder="5455" />
                        </Form.Item>
                      </Col>
                      <Col span={10}>
                        <Form.Item name="mysql_database" label="数据库名">
                          <Input placeholder="my_rag" />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={12}>
                      <Col span={10}>
                        <Form.Item name="mysql_user" label="用户名">
                          <Input placeholder="ragflow" />
                        </Form.Item>
                      </Col>
                      <Col span={10}>
                        <Form.Item
                          name="mysql_password"
                          label="密码"
                          tooltip="保存后仅显示脱敏值；不修改请留空"
                        >
                          <Password placeholder="******" />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={12}>
                      <Col span={20}>
                        <Form.Item
                          name="mysql_url"
                          label="连接串覆盖（可选）"
                          tooltip="非空时优先生效（如测试注入 sqlite+aiosqlite://）；正常使用留空"
                        >
                          <Input placeholder="mysql+aiomysql://user:pass@host:port/db" />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>
                ),
              },
              {
                key: 'minio',
                label: '对象存储（MinIO）',
                children: (
                  <>
                    <Row gutter={12}>
                      <Col span={10}>
                        <Form.Item name="minio_endpoint" label="Endpoint">
                          <Input placeholder="127.0.0.1:9000" />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name="minio_bucket" label="Bucket">
                          <Input placeholder="my-rag" />
                        </Form.Item>
                      </Col>
                      <Col span={4}>
                        <Form.Item name="minio_secure" label="HTTPS">
                          <Select
                            options={[
                              { value: true, label: '是' },
                              { value: false, label: '否' },
                            ]}
                          />
                        </Form.Item>
                      </Col>
                      <Col span={4}>
                        <Form.Item name="minio_region" label="Region">
                          <Input placeholder="us-east-1" />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={12}>
                      <Col span={10}>
                        <Form.Item name="minio_access_key" label="Access Key">
                          <Input placeholder="rag_flow" />
                        </Form.Item>
                      </Col>
                      <Col span={10}>
                        <Form.Item
                          name="minio_secret_key"
                          label="Secret Key"
                          tooltip="保存后仅显示脱敏值；不修改请留空"
                        >
                          <Password placeholder="******" />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>
                ),
              },
            ]}
          />

          <div style={{ textAlign: 'right', marginTop: 8 }}>
            <Button
              icon={<ThunderboltOutlined />}
              onClick={handleModalTest}
              loading={modalTesting}
              disabled={readOnly}
            >
              测试连接（使用当前表单值）
            </Button>
          </div>
          {renderTestLine(modalTest.llm, sectionLabel.llm)}
          {renderTestLine(modalTest.embedding, sectionLabel.embedding)}
          {renderTestLine(modalTest.mineru, sectionLabel.mineru)}
          {renderTestLine(modalTest.deepdoc, sectionLabel.deepdoc)}
          {renderTestLine(modalTest.mysql, sectionLabel.mysql)}
          {renderTestLine(modalTest.minio, sectionLabel.minio)}
        </Form>
      </Modal>

      {/* 模型添加/编辑弹窗（LLM 多模型管理） */}
      <Modal
        title={modelEditIdx !== null
          ? `编辑模型${llmModels[modelEditIdx] ? `：${llmModels[modelEditIdx].name}` : ''}`
          : '添加 LLM 模型'}
        open={modelModalOpen}
        onCancel={() => setModelModalOpen(false)}
        onOk={saveModel}
        okText="保存"
        width={600}
      >
        <Form form={modelForm} layout="vertical" size="small">
          <Row gutter={12}>
            <Col span={8}>
              <Form.Item
                name="model_name"
                label="模型名称（显示名）"
                rules={[{ required: true, message: '请输入模型名称' }]}
              >
                <Input placeholder="如：本地 Qwen" />
              </Form.Item>
            </Col>
            <Col span={16}>
              <Form.Item
                name="model_base_url"
                label="API 地址"
                rules={[{ required: true, message: '请输入 API 地址' }]}
              >
                <Input placeholder="http://127.0.0.1:1234/v1" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item
                name="model_model"
                label="模型标识"
                rules={[{ required: true, message: '请输入模型标识' }]}
              >
                <Input placeholder="qwen3.6-35b-a3b-apex-quality" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="model_api_key"
                label="API Key"
                tooltip="编辑时留空 = 保留原值；保存后仅显示脱敏值"
              >
                <Password placeholder="sk-***" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}>
              <Form.Item name="model_temperature" label="Temperature">
                <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="model_max_tokens" label="Max Tokens">
                <InputNumber min={64} max={32768} step={128} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="model_timeout" label="超时（秒）">
                <InputNumber min={1} max={600} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>
    </div>
  );
};

export default SettingsPage;
