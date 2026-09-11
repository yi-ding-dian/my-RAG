import React, { useCallback, useEffect, useState } from 'react';
import AppModal from '../../shared/components/common/AppModal';
import {
  App as AntApp,  Card,  Form,  Input,  InputNumber,  Button,  Typography,  Space, 
  Row,  Col,  Skeleton,  Tag,  Alert,  Popconfirm,  Tooltip,  Collapse, 
  theme} from 'antd';
import {
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined,
  PlusOutlined, DeleteOutlined, CheckOutlined, EditOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import {
  asApiError,
  listProfiles, createProfile, updateProfile, deleteProfile,
  activateProfile, testProfileConnection, testLlmConnection, getEmbeddingDim,
  getChatSettings, updateChatSettings, getLlmModelList,
  ServiceProfile, ServiceProfileInput, LLMModelItem,
} from '../../shared/api/client';
import type { ParserLlmModelItem } from '../../shared/api/types';
import { useAuth } from '../../shared/auth/AuthContext';
import PageHeader from '../../shared/components/layout/PageHeader';
import {
  DOMAIN_CARDS, allFailed, allTesting, emptyTest, PANEL_TEST_SECTIONS,
  sectionLabel, toFormValues, toProfileInput, toTestItems,
} from './shared';
import type { ProfileFormValues, SectionKey, TestItem } from './shared';
import ArPanel from './ArPanel';
import RetrievalPanel from './RetrievalPanel';
import IngestPanel from './IngestPanel';
import LlmPanel from './LlmPanel';
import EmbeddingPanel from './EmbeddingPanel';
import MineruPanel from './MineruPanel';
import DeepdocPanel from './DeepdocPanel';
import MysqlPanel from './MysqlPanel';
import MinioPanel from './MinioPanel';
import VectorStorePanel from './VectorStorePanel';

const { Text } = Typography;
const { Password } = Input;

/** 系统配置页：配置档案列表 + 域卡快捷导航 + 新建/编辑弹窗（面板见同目录 *Panel.tsx） */
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
  // 域卡测试连接 loading 键（"profileId:domainKey"）
  const [domainTesting, setDomainTesting] = useState('');
  // 编辑弹窗聚焦的配置域（方案 A：域卡 → 打开弹窗默认展开该域；
  // 其余面板可自由折叠/展开——多面板并存，不做手风琴单开）
  const [activePanels, setActivePanels] = useState<string[]>(['ar']);
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
  // 「标题分层模型」下拉数据源（GET /api/settings/llm/models，与解析配置弹窗
  // 的「解析 LLM 模型」同一个接口）：仅名称+model，无敏感字段；失败静默
  // （下拉为空，不影响其余表单项）
  const [headingModelOptions, setHeadingModelOptions] =
    useState<ParserLlmModelItem[]>([]);

  // 卡片连接测试状态（按档案 id）
  const [testStates, setTestStates] = useState<Record<string, Record<SectionKey, TestItem>>>({});
  // 弹窗内连接测试状态
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

  // 「标题分层模型」下拉数据源：编辑弹窗打开时拉取模型列表（登录即可读，
  // 与解析配置弹窗同款接口）；失败静默（下拉为空，不阻塞弹窗使用）
  useEffect(() => {
    if (!modalOpen) return undefined;
    let cancelled = false;
    getLlmModelList()
      .then(res => {
        if (!cancelled) setHeadingModelOptions(res.data.models ?? []);
      })
      .catch(() => {
        if (!cancelled) setHeadingModelOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [modalOpen]);

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
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '加载本部门 LLM 配置失败');
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
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '保存本部门 LLM 配置失败');
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
    } catch (e: unknown) {
      confirmForce(asApiError(e).response?.data?.detail || '网络请求失败，请检查服务是否可达');
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
      heading_llm_model: '',
      contextual_retrieval_max_full_doc_chars: 20000,
      ingestion_concurrency: 3,
      ingestion_kb_doc_limit: 0,
      ingestion_max_upload_mb: 100,
      chat_max_query_len: 2000,
      // MySQL / MinIO 预填后端默认值（密码类留空，保存时后端用默认或保持原值）
      mysql_host: '127.0.0.1', mysql_port: 5455, mysql_user: 'ragflow',
      mysql_database: 'my_rag',
      minio_endpoint: '127.0.0.1:9000', minio_access_key: '',
      minio_bucket: 'my-rag', minio_secure: false, minio_region: '',
      vector_store_backend: 'chroma', vector_store_milvus_uri: '',
    });
    setModalOpen(true);
  };

  const openEdit = (p: ServiceProfile, panel: string = 'ar') => {
    setEditingId(p.id);
    setActivePanels([panel]);
    form.setFieldsValue(toFormValues(p));
    // llm 段回填（后端已迁移为 {models, active} 结构）
    const sec = p.llm as unknown as { models?: LLMModelItem[]; active?: number };
    const models = Array.isArray(sec?.models) && sec.models.length
      ? sec.models : [];
    setLlmModels(models);
    setLlmActive(sec?.active ?? 0);
    setModalOpen(true);
  };

  const doSave = async (vals: ProfileFormValues) => {
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
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '保存失败');
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
    } catch (e: unknown) {
      const msg = asApiError(e).response?.data?.detail || '测试失败';
      setTestStates(prev => ({ ...prev, [p.id]: allFailed(msg) }));
    }
  };

  /** 域卡测试连接：按档案配置探测该域各段，结果以 toast 反馈成败 */
  const handleDomainTest = async (
    p: ServiceProfile, domainKey: string, sections: SectionKey[],
  ) => {
    setDomainTesting(`${p.id}:${domainKey}`);
    try {
      const res = await testProfileConnection(p.id);
      const results = sections
        .map(k => ({ key: k, r: (res.data as unknown as Record<string, { ok: boolean; message: string }>)[k] }))
        .filter(x => x.r);
      const fails = results.filter(x => !x.r.ok);
      if (fails.length === 0) {
        message.success(`${DOMAIN_CARDS.find(d => d.key === domainKey)?.title ?? '配置'}：连接测试通过`);
      } else {
        fails.forEach(x =>
          message.error(`${sectionLabel[x.key] ?? x.key}：${x.r.message}`));
        const okCount = results.length - fails.length;
        if (okCount > 0) {
          message.success(`其中 ${okCount} 项连接正常`);
        }
      }
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '测试失败');
    } finally {
      setDomainTesting('');
    }
  };

  /** 编辑弹窗内折叠面板标题"测试"：按档案已保存配置探测该段，toast 结果 */
  const handlePanelTest = async (panelKey: string, sections: SectionKey[]) => {
    if (!editingId) {
      message.warning('请先选择档案再测试');
      return;
    }
    setDomainTesting(`panel:${panelKey}`);
    try {
      const res = await testProfileConnection(editingId);
      const fail = sections
        .map(k => ({ key: k, r: (res.data as unknown as Record<string, { ok: boolean; message: string }>)[k] }))
        .filter(x => x.r)
        .find(x => !x.r.ok);
      if (!fail) {
        message.success(`${sectionLabel[sections[0]] ?? '配置'}：连接测试通过`);
      } else {
        message.error(`${sectionLabel[fail.key] ?? fail.key}：${fail.r.message}`);
      }
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '测试失败');
    } finally {
      setDomainTesting('');
    }
  };

  /** 折叠面板标题：右侧可测面板夹带"测试连接"按钮（toast 成败） */
  const panelLabel = (panelKey: string, title: string) => {
    const secList = PANEL_TEST_SECTIONS[panelKey];
    return (
      <div
        style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          width: '100%',
        }}
      >
        <span>{title}</span>
        {secList && (
          <Tooltip title="测试连接（使用档案配置）">
            <Button
              type="text"
              size="small"
              icon={<ThunderboltOutlined />}
              loading={domainTesting === `panel:${panelKey}`}
              onClick={e => {
                e.stopPropagation();
                void handlePanelTest(panelKey, secList);
              }}
            />
          </Tooltip>
        )}
      </div>
    );
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
        {/* 配置域快捷导航（方案 A：概览在下、点域卡片直达对应编辑折叠，不再全量一张表） */}
        <div
          style={{
            display: 'flex', flexWrap: 'wrap', gap: 8,
            marginBottom: 12,
          }}
        >
          {DOMAIN_CARDS.map(d => (
            <div
              key={d.key}
              onClick={() => !readOnly && openEdit(p, d.key)}
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '6px 12px', borderRadius: 8, cursor: readOnly ? 'default' : 'pointer',
                background: 'rgba(var(--brand-primary-rgb, 37, 99, 235), 0.05)',
                border: '1px solid rgba(var(--brand-primary-rgb, 37, 99, 235), 0.18)',
                transition: 'all 0.2s',
              }}
            >
              <Text strong style={{ fontSize: 12, color: 'var(--brand-primary, #2563eb)' }}>
                {d.title}
              </Text>
              <Text type="secondary" style={{ fontSize: 11, maxWidth: 240 }} ellipsis={{ tooltip: d.summary(p) }}>
                {d.summary(p)}
              </Text>
              {d.sections && (
                <Tooltip title="测试连接（使用档案配置）">
                  <Button
                    type="text"
                    size="small"
                    icon={<ThunderboltOutlined />}
                    loading={domainTesting === `${p.id}:${d.key}`}
                    onClick={e => {
                      e.stopPropagation();
                      void handleDomainTest(p, d.key, d.sections!);
                    }}
                  />
                </Tooltip>
              )}
            </div>
          ))}
        </div>
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
                {p.mysql
                  ? p.mysql.url
                    ? String(p.mysql.url).slice(0, 60)
                    : `${p.mysql.host}:${p.mysql.port}/${p.mysql.database || ''}`
                  : '-'}
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
          <Col xs={24} md={12}>
            <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.vector_store}</Text>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <Text style={{ fontSize: 12 }}>
                {p.vector_store
                  ? p.vector_store.backend === 'milvus'
                    ? `Milvus: ${p.vector_store.milvus_uri || '-'}`
                    : 'Chroma（本地嵌入式）'
                  : '-'}
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
            {renderTestLine(tests.vector_store, sectionLabel.vector_store)}
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
      <AppModal
        dimension="resizable"
        defaultSize={{ w: 720, h: 560 }}
        rememberKey="settings-1"
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
            activeKey={activePanels}
            onChange={k => {
              const keys = Array.isArray(k) ? k : [k];
              setActivePanels(keys.filter(Boolean));
            }}
            items={[
              { key: 'ar', label: '档案基本设置', children: <ArPanel /> },
              {
                key: 'retrieval',
                label: panelLabel('retrieval', '检索与切块'),
                children: <RetrievalPanel headingModelOptions={headingModelOptions} />,
              },
              { key: 'ingest', label: '入库与限制', children: <IngestPanel /> },
              {
                key: 'llm',
                label: panelLabel('llm', 'LLM 对话模型（多模型管理）'),
                children: (
                  <LlmPanel
                    llmModels={llmModels}
                    llmActive={llmActive}
                    llmTestingIdx={llmTestingIdx}
                    openModelEdit={openModelEdit}
                    deleteModel={deleteModel}
                    activateModel={activateModel}
                  />
                ),
              },
              {
                key: 'embedding',
                label: panelLabel('embedding', 'Embedding 模型（OpenAI 兼容）'),
                children: <EmbeddingPanel />,
              },
              {
                key: 'mineru',
                label: panelLabel('mineru', 'MinerU 文档解析'),
                children: <MineruPanel />,
              },
              {
                key: 'deepdoc',
                label: panelLabel('deepdoc', 'DeepDoc 解析（RAGFlow）'),
                children: <DeepdocPanel />,
              },
              {
                key: 'mysql',
                label: panelLabel('mysql', '数据库（SQLite/MySQL/其他）'),
                children: <MysqlPanel />,
              },
              {
                key: 'minio',
                label: panelLabel('minio', '对象存储（MinIO）'),
                children: <MinioPanel />,
              },
              {
                key: 'vector_store',
                label: panelLabel('vector_store', '向量存储'),
                children: <VectorStorePanel />,
              },
            ]}
          />

          <div style={{ height: 4 }} />
        </Form>
      </AppModal>

      {/* 模型添加/编辑弹窗（LLM 多模型管理） */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 600, h: 460 }}
        rememberKey="settings-2"
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
      </AppModal>
    </div>
  );
};

export default SettingsPage;
