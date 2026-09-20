import React, { useCallback, useEffect, useState } from 'react';
import AppModal from '../../shared/components/common/AppModal';
import {
  App as AntApp,  Card,  Form,  Input,  InputNumber,  Button,  Typography,  Space,
  Row,  Col,  Skeleton,  Tag,  Alert,  Popconfirm,  Tooltip,  Collapse,
  Select, theme} from 'antd';
import {
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined,
  PlusOutlined, DeleteOutlined, CheckOutlined, EditOutlined,
  ThunderboltOutlined, QuestionCircleOutlined,
} from '@ant-design/icons';
import {
  asApiError,
  listProfiles, createProfile, updateProfile, deleteProfile,
  activateProfile, testProfileConnection, testLlmConnection, getEmbeddingDim,
  testVisionConnection,
  getLlmModelList,
  ServiceProfile, ServiceProfileInput, LLMModelItem,
} from '../../shared/api/client';
import type { Department, ParserLlmModelItem, ThinkingControl, VisionModelItem } from '../../shared/api/types';
import { listDepartments } from '../../shared/api/auth';
import { getDeptConfigView } from '../../shared/api/settings';
import { useAuth } from '../../shared/auth/AuthContext';
import PageHeader from '../../shared/components/layout/PageHeader';
import {
  DOMAIN_CARDS, allFailed, allTesting, emptyTest, PANEL_TEST_SECTIONS,
  sectionLabel, toFormValues, toProfileInput, toTestItems,
} from './shared';
import type { ProfileFormValues, SectionKey, TestItem } from './shared';
import ArPanel from './ArPanel';
import ChatPanel from './ChatPanel';
import RetrievalPanel from './RetrievalPanel';
import IngestPanel from './IngestPanel';
import LlmPanel from './LlmPanel';
import EmbeddingPanel from './EmbeddingPanel';
import ParseServicesPanel from './ParseServicesPanel';
import MysqlPanel from './MysqlPanel';
import MinioPanel from './MinioPanel';
import VectorStorePanel from './VectorStorePanel';
import VisionPanel from './VisionPanel';

const { Text } = Typography;
const { Password } = Input;

/** 系统配置页：配置档案列表 + 域卡快捷导航 + 新建/编辑弹窗（面板见同目录 *Panel.tsx） */
/** 配置 JSON 展示样式：pre-wrap + break-all 让超长字段（如 system_prompt
    1200+ 字符）自动折行——比横向截断直观，也比悬浮 Tooltip 好读 */
const PRE_STYLE: React.CSSProperties = {
  maxHeight: 380,
  overflow: 'auto',
  fontSize: 12,
  padding: 8,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-all',
  background: '#fafafa',
};

const SettingsPage: React.FC = () => {
  const { message, modal } = AntApp.useApp();
  const { token } = theme.useToken();
  const { user } = useAuth();
  // 档案卡片只读模式：dept_admin 可查看配置与连接测试，修改档案仅 super_admin
  const readOnly = user?.role !== 'super_admin';
  // 部门管理员不应进入本页（菜单/路由已限 super_admin）；保留判断做防御
  const isDeptAdmin = user?.role === 'dept_admin';
  const [profiles, setProfiles] = useState<ServiceProfile[]>([]);
  const [loading, setLoading] = useState(false);
  // 部门配置查询（超管只读）：部门列表 + 查看弹窗（null = 关闭）
  const [departments, setDepartments] = useState<Department[]>([]);
  const [deptView, setDeptView] = useState<{
    name: string;
    /** 部门显式覆盖的段（[段名, 字段dict]，只留有内容的） */
    filled: [string, unknown][];
    /** 当前生效配置（全局 + 部门覆盖的合并） */
    effective: Record<string, unknown>;
  } | null>(null);

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
  // ---- 图片解析模型（多模态，用于解析时生成图片摘要）----
  // 结构与 llm 段一致（models + active）；条目少 temperature/max_tokens
  // （摘要用固定生成参数），故用独立表单与弹窗
  const [visionModels, setVisionModels] = useState<VisionModelItem[]>([]);
  const [visionActive, setVisionActive] = useState(0);
  const [visionTestingIdx, setVisionTestingIdx] = useState<number | null>(null);
  const [visionModalOpen, setVisionModalOpen] = useState(false);
  const [visionEditIdx, setVisionEditIdx] = useState<number | null>(null);
  const [visionForm] = Form.useForm();
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

  // ---- 部门配置查询（超管只读；部门管理员在「部门配置」页自行修改） ----
  const loadDepartments = useCallback(async () => {
    if (isDeptAdmin) return;
    try {
      const res = await listDepartments();
      setDepartments(res.data ?? []);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '加载部门列表失败');
    }
  }, [isDeptAdmin, message]);

  useEffect(() => {
    void loadDepartments();
  }, [loadDepartments]);

  /** 复制配置 JSON 到剪贴板（失败提示：非 https 或浏览器未授权剪贴板） */
  const copyJson = (data: unknown, label: string) => {
    void navigator.clipboard
      .writeText(JSON.stringify(data, null, 2))
      .then(
        () => message.success(`${label}已复制到剪贴板`),
        () => message.error('复制失败（浏览器未授权剪贴板）'),
      );
  };

  /** 打开某部门配置查看弹窗（只读）：展示**当前生效值**（全局 + 部门覆盖的
      合并），并列出本部门自行覆盖了哪些字段——超管据此判断"这个部门改过什么"。
      数据在打开时算好，弹窗只负责渲染（统一走 AppModal，尺寸可记忆/拖拽）。 */
  const viewDeptConfig = async (deptId: string) => {
    try {
      const res = await getDeptConfigView(deptId);
      const d = res.data;
      const dept = (d.dept ?? {}) as Record<string, Record<string, unknown>>;
      setDeptView({
        name: d.name,
        filled: Object.entries(dept).filter(
          ([, v]) => v && typeof v === 'object' && Object.keys(v).length > 0),
        effective: {
          ...(d.chat ? { chat: d.chat } : {}),
          ...(d.retrieval ? { retrieval: d.retrieval } : {}),
          ...(d.agentic ? { agentic: d.agentic } : {}),
          ...(d.llm ? { llm: d.llm } : {}),
        },
      });
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '加载部门配置失败');
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
        // 旧数据无该字段 → 回填 0.9（与后端 LLMConfig 默认一致）
        model_top_p: m.top_p ?? 0.9,
        // 旧数据无该字段 → 回填 none（与后端默认一致）
        model_thinking_control: m.thinking_control ?? 'none',
      });
    } else {
      modelForm.setFieldsValue({
        model_temperature: 0.3, model_max_tokens: 4096, model_timeout: 120,
        model_top_p: 0.9,
        model_thinking_control: 'none',
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
      top_p: v.model_top_p ?? 0.9,
      max_tokens: v.model_max_tokens ?? 4096,
      timeout: v.model_timeout ?? 120,
      thinking_control: (v.model_thinking_control ?? 'none') as ThinkingControl,
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

  // ---- 图片解析模型操作（与 llm 同构；条目少 temperature/max_tokens）----
  const openVisionEdit = (idx: number | null) => {
    setVisionEditIdx(idx);
    visionForm.resetFields();
    if (idx !== null && visionModels[idx]) {
      const m = visionModels[idx];
      visionForm.setFieldsValue({
        vision_name: m.name, vision_base_url: m.base_url,
        vision_api_key: m.api_key, vision_model: m.model,
        vision_timeout: m.timeout,
      });
    } else {
      visionForm.setFieldsValue({ vision_timeout: 120 });
    }
    setVisionModalOpen(true);
  };

  const saveVisionModel = async () => {
    const v = await visionForm.validateFields();
    const item: VisionModelItem = {
      name: (v.vision_name ?? '').trim(),
      base_url: (v.vision_base_url ?? '').trim(),
      api_key: v.vision_api_key || '',
      model: (v.vision_model ?? '').trim(),
      timeout: v.vision_timeout ?? 120,
    };
    setVisionModels(prev => {
      const next = [...prev];
      if (visionEditIdx !== null && next[visionEditIdx]) {
        // 编辑：api_key 留空 = 保留原值（回填的是脱敏值）
        next[visionEditIdx] = {
          ...item, api_key: item.api_key || next[visionEditIdx].api_key,
        };
      } else {
        next.push(item);
        if (next.length === 1) setVisionActive(0);
      }
      return next;
    });
    setVisionModalOpen(false);
  };

  const deleteVisionModel = (idx: number) => {
    setVisionModels(prev => {
      if (prev.length <= 1) return prev; // 至少保留 1 个模型
      const next = prev.filter((_, i) => i !== idx);
      setVisionActive(a => (idx === a ? 0 : idx < a ? a - 1 : a));
      return next;
    });
  };

  /** 设为默认（部门没选模型时的兜底）：先测连接，失败可确认强制 */
  const activateVisionModel = async (idx: number) => {
    if (idx === visionActive || visionTestingIdx !== null) return;
    const item = visionModels[idx];
    if (!item) return;
    setVisionTestingIdx(idx);
    const confirmForce = (reason: string) => {
      modal.confirm({
        title: `连接失败，确认将「${item.name}」设为默认？`,
        content: reason,
        okText: '仍要设为默认',
        cancelText: '取消',
        onOk: () => {
          setVisionActive(idx);
          message.success(`已将「${item.name}」设为默认`);
        },
      });
    };
    try {
      const res = await testVisionConnection(item);
      if (res.data.ok) {
        setVisionActive(idx);
        message.success(`已将「${item.name}」设为默认`);
      } else {
        confirmForce(res.data.reason);
      }
    } catch (e: unknown) {
      confirmForce(asApiError(e).response?.data?.detail || '网络请求失败，请检查服务是否可达');
    } finally {
      setVisionTestingIdx(null);
    }
  };

  // ---- 新建/编辑 ----
  const openCreate = () => {
    setEditingId(null);
    form.resetFields();
    setLlmModels([]);
    setLlmActive(0);
    setVisionModels([]);
    setVisionActive(0);
    form.setFieldsValue({
      embedding_dimension: 1024,
      mineru_timeout: 300,
      // DeepDoc 预填后端默认值（密码留空，保存时后端保持原值）
      deepdoc_base_url: 'http://127.0.0.1:9380',
      deepdoc_email: '',
      deepdoc_timeout: 300,
      deepdoc_dataset_prefix: 'myrag-tmp-',
      // 文档转换（Gotenberg）：预填后端默认值，实际地址按部署环境改
      gotenberg_base_url: 'http://127.0.0.1:3000',
      gotenberg_timeout: 120,
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
      chat_citation_snippet_chars: 600,
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
    // vision 段回填（同为 {models, active} 结构）
    const vsec = p.vision as unknown as
      { models?: VisionModelItem[]; active?: number };
    setVisionModels(
      Array.isArray(vsec?.models) && vsec.models.length ? vsec.models : []);
    setVisionActive(vsec?.active ?? 0);
    setModalOpen(true);
  };

  const doSave = async (vals: ProfileFormValues) => {
    setSaving(true);
    try {
      const llmSection = llmModels.length
        ? { models: llmModels, active: llmActive } : undefined;
      const visionSection = visionModels.length
        ? { models: visionModels, active: visionActive } : undefined;
      const data = toProfileInput(vals, llmSection, visionSection);
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
    const pid = editingId;
    setDomainTesting(`panel:${panelKey}`);
    // 面板测试除了 toast，还要驱动标题旁的状态灯（与卡片级测试同源）：
    // 先置"测试中"，拿到结果后落回；**只覆盖本次被测的段**，其他段的灯不动
    const patchKeys = (fn: (k: SectionKey) => TestItem) =>
      setTestStates(prev => {
        const cur = prev[pid] ?? emptyTest;
        const next = { ...cur };
        for (const k of sections) next[k] = fn(k);
        return { ...prev, [pid]: next };
      });
    patchKeys(() => ({ status: 'testing', msg: '' }));
    try {
      const res = await testProfileConnection(pid);
      const items = toTestItems(res.data);
      patchKeys(k => items[k]);
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
      const msg = asApiError(e).response?.data?.detail || '测试失败';
      patchKeys(() => ({ status: 'failed', msg }));
      message.error(msg);
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
            ? '本页为超级管理员专用；部门管理员请使用左侧菜单的「部门配置」'
            : '您正在配置全局档案。LLM / 对话 / 检索等可由部门覆盖的配置，由各部门管理员在「部门配置」页自行设置；下方「部门配置查询」可查看各部门覆盖了什么。'}
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

      {/* 部门配置查询（仅超管，只读）：看各部门覆盖了什么——配置收口后超管
          不代改，修改由部门管理员在「部门配置」页自行完成 */}
      {!isDeptAdmin && (
        <Card
          size="small"
          style={{ marginBottom: 12 }}
          title={
            <Space>
              <Tag color="purple">部门配置查询</Tag>
              <Text strong>各部门覆盖的配置（只读）</Text>
            </Space>
          }
        >
          {departments.length === 0 ? (
            <Text type="secondary">暂无部门</Text>
          ) : (
            <Space wrap>
              {departments.map(d => (
                <Button key={d.id} size="small"
                  onClick={() => void viewDeptConfig(d.id)}>
                  {d.name}
                </Button>
              ))}
            </Space>
          )}
        </Card>
      )}

      {profiles.length === 0 ? (
        <Card><Text type="secondary">{readOnly ? '暂无配置档案' : '暂无配置档案，请新建'}</Text></Card>
      ) : (
        profiles.map(renderProfileCard)
      )}

      {/* 新建/编辑弹窗：固定高度（7 个 Collapse 面板默认全展开，内容超高），
          头部/关闭按钮固定，滚动只在内容区内部（滚动结构修复见
          styles/modals.css .profile-config-modal，与 .chunk-detail-modal 同一套规则） */}
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
                key: 'chat',
                label: panelLabel('chat', '聊天设置'),
                children: <ChatPanel />,
              },
              {
                key: 'prompts',
                label: '系统提示词库',
                children: (
                  <div>
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12 }}
                      message="供「外部查询」引用"
                      description={
                        '外部链接选一条即可复用同一套提示词；改这里的正文，'
                        + '所有引用它的链接立刻生效——外部链接存的是名称引用，'
                        + '不是内容副本。'
                      }
                    />
                    {/* 弹窗 body 是固定高度（见弹窗注释），条目多了必须自己滚动，
                        否则"添加提示词"按钮会被 footer 盖住 */}
                    <div style={{ maxHeight: 220, overflowY: 'auto', paddingRight: 4 }}>
                      <Form.List name={['prompts', 'items']}>
                        {(fields, { add, remove }) => (
                          <>
                            {fields.map(({ key, name, ...rest }) => (
                              <Row key={key} gutter={8} style={{ marginBottom: 8 }}>
                                <Col span={6}>
                                  <Form.Item
                                    {...rest}
                                    name={[name, 'name']}
                                    style={{ marginBottom: 0 }}
                                    rules={[{
                                      required: true, whitespace: true,
                                      message: '请输入名称',
                                    }]}
                                  >
                                    <Input placeholder="名称（如：严谨引用）" maxLength={50} />
                                  </Form.Item>
                                </Col>
                                <Col span={16}>
                                  <Form.Item
                                    {...rest}
                                    name={[name, 'content']}
                                    style={{ marginBottom: 0 }}
                                    rules={[{
                                      required: true, whitespace: true,
                                      message: '请输入提示词内容',
                                    }]}
                                  >
                                    <Input.TextArea
                                      rows={2}
                                      placeholder="提示词正文，可含 {knowledge} / {refs} 占位符"
                                    />
                                  </Form.Item>
                                </Col>
                                <Col span={2}>
                                  <Button
                                    type="text"
                                    danger
                                    icon={<DeleteOutlined />}
                                    onClick={() => remove(name)}
                                  />
                                </Col>
                              </Row>
                            ))}
                            <Button
                              type="dashed"
                              block
                              icon={<PlusOutlined />}
                              onClick={() => add({ name: '', content: '' })}
                            >
                              添加提示词
                            </Button>
                          </>
                        )}
                      </Form.List>
                    </div>
                  </div>
                ),
              },
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
                key: 'parse',
                label: panelLabel('parse', '文档解析相关（MinerU / DeepDoc / 文档转换）'),
                children: (
                  <ParseServicesPanel
                    testItems={editingId ? testStates[editingId] : undefined}
                  />
                ),
              },
              {
                key: 'vision',
                label: panelLabel('vision', '图片解析模型（多模态，生成图片摘要）'),
                children: (
                  <VisionPanel
                    visionModels={visionModels}
                    visionActive={visionActive}
                    visionTestingIdx={visionTestingIdx}
                    openModelEdit={openVisionEdit}
                    deleteModel={deleteVisionModel}
                    activateModel={activateVisionModel}
                  />
                ),
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
        // 锁定型：内容就绪后定住高度，不再跟着内容变——表单里换行/滚动条出现
        // 会改变内容高度，若继续跟随就形成"测高→出滚动条→换行变高→再测"的
        // 振荡（表现为弹窗抽搐）。内容固定，直接锁。
        autoLock
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
            <Col span={6}>
              <Form.Item
                name="model_temperature"
                label={
                  <Space size={4}>
                    Temperature
                    <Tooltip
                      overlayStyle={{ maxWidth: 380 }}
                      title={
                        <div style={{ fontSize: 12, lineHeight: '18px' }}>
                          控制回答的随机性（0~2）：越低越稳定、可复现，
                          越高越发散、越有创造性。
                          <div style={{ marginTop: 6 }}>
                            知识库问答要忠实复述原文，建议 0.1~0.3；
                            设太高模型容易改写甚至编造引用里没有的内容。
                          </div>
                        </div>
                      }
                    >
                      <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                    </Tooltip>
                  </Space>
                }
              >
                <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                name="model_top_p"
                label={
                  <Space size={4}>
                    Top P
                    <Tooltip
                      overlayStyle={{ maxWidth: 380 }}
                      title={
                        <div style={{ fontSize: 12, lineHeight: '18px' }}>
                          核采样范围（0~1）：只从累计概率最高的这部分候选词里
                          挑下一个字，越小越保守、越大越多样。
                          <div style={{ marginTop: 6 }}>
                            它与 Temperature 是两个独立的采样旋钮，通常
                            只调其中一个：保持 Temperature 小而调 Top P，
                            比单纯降温度更不容易陷入重复。
                          </div>
                          <div style={{ marginTop: 6 }}>
                            外部查询「Top P 留空」时用的就是这个值，默认 0.9。
                          </div>
                        </div>
                      }
                    >
                      <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                    </Tooltip>
                  </Space>
                }
              >
                <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                name="model_max_tokens"
                label={
                  <Space size={4}>
                    Max Tokens
                    <Tooltip
                      overlayStyle={{ maxWidth: 380 }}
                      title={
                        <div style={{ fontSize: 12, lineHeight: '18px' }}>
                          单次回答最多生成的 token 数——这是
                          <b>输出上限</b>，不是输入限制（输入多长由检索到的
                          引用决定，不在这里设）。
                          <div style={{ marginTop: 6 }}>
                            它与输入共同占用模型的上下文窗口：
                            <div style={{ marginTop: 2 }}>
                              输入 + Max Tokens ≤ 窗口长度
                            </div>
                            设得过大（尤其接近窗口长度）会在调用前被模型服务
                            直接拒绝，报"maximum context length"错误。
                          </div>
                          <div style={{ marginTop: 6 }}>
                            按期望的最长回答留 1.5 倍余量即可，
                            典型值 2048~4096。
                          </div>
                        </div>
                      }
                    >
                      <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                    </Tooltip>
                  </Space>
                }
              >
                <InputNumber min={64} max={32768} step={128} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                name="model_timeout"
                label={
                  <Space size={4}>
                    超时（秒）
                    <Tooltip
                      overlayStyle={{ maxWidth: 380 }}
                      title={
                        <div style={{ fontSize: 12, lineHeight: '18px' }}>
                          等待模型返回的最长时间，超过即本次问答失败。
                          <div style={{ marginTop: 6 }}>
                            本地大模型首字延迟高（参数大、或开启思考时更明显），
                            建议按实测调整：设太小会频繁超时，
                            设太大则真卡住时要白等很久。
                          </div>
                        </div>
                      }
                    >
                      <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                    </Tooltip>
                  </Space>
                }
              >
                <InputNumber min={1} max={600} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={24}>
              <Form.Item
                name="model_thinking_control"
                label={
                  <Space size={4}>
                    思考控制方式
                    <Tooltip
                      overlayStyle={{ maxWidth: 420 }}
                      title={
                        <div style={{ fontSize: 12, lineHeight: '18px' }}>
                          <div>
                            非思考模型指的是：模型本身不支持思考，或者支持思考但部署端
                            已经关闭（如 vLLM 启动服务时指定关闭思考）——这类模型选
                            「不处理」，系统不会再做任何事。
                          </div>
                          <div style={{ marginTop: 6 }}>
                            模型自带思考、且部署端关不掉（如 LM Studio 上的 Qwen 思考
                            模型）→ 选「注入 &lt;think&gt; 跳过思考」；
                            在线 API 且支持 thinking 参数（DeepSeek 等）→ 选「传 thinking 参数」。
                          </div>
                          <div style={{ marginTop: 6 }}>
                            选错的影响：给不思考的模型注入 prefill，会把回答压短、
                            图片标签被省略。
                          </div>
                        </div>
                      }
                    >
                      <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                    </Tooltip>
                  </Space>
                }
              >
                <Select
                  options={[
                    { value: 'none', label: '不处理（模型不思考，或部署端已关闭）' },
                    { value: 'prefill', label: '注入 <think></think> 跳过思考（Qwen 系 + LM Studio 等本地部署）' },
                    { value: 'api', label: '传 thinking 参数（DeepSeek 等在线 API）' },
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </AppModal>

      {/* 图片解析模型编辑弹窗（条目比 LLM 少 temperature/max_tokens） */}
      <AppModal
        title={visionEditIdx !== null
          ? `编辑图片模型${visionModels[visionEditIdx] ? `：${visionModels[visionEditIdx].name}` : ''}`
          : '添加图片解析模型'}
        open={visionModalOpen}
        onCancel={() => setVisionModalOpen(false)}
        onOk={saveVisionModel}
        okText="保存"
        width={600}
      >
        <Form form={visionForm} layout="vertical" size="small">
          <Row gutter={12}>
            <Col span={8}>
              <Form.Item
                name="vision_name"
                label="模型名称（显示名）"
                rules={[{ required: true, message: '请输入模型名称' }]}
              >
                <Input placeholder="如：qwen-vl" />
              </Form.Item>
            </Col>
            <Col span={16}>
              <Form.Item
                name="vision_base_url"
                label="API 地址"
                rules={[{ required: true, message: '请输入 API 地址' }]}
              >
                <Input placeholder="http://127.0.0.1:8000/v1" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={12}>
            <Col span={8}>
              <Form.Item name="vision_model" label="模型名"
                rules={[{ required: true, message: '请输入模型名' }]}>
                <Input placeholder="Qwen3.5-9B-GPTQ-4bit" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="vision_api_key" label="API Key（可空）">
                <Input.Password placeholder="本地服务通常留空" />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="vision_timeout" label="超时（秒）">
                <InputNumber min={1} max={600} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          <Alert
            type="info"
            showIcon
            message="部门管理员从这些模型里选一个用；提示词与输出格式在「部门配置 → 图片摘要」里配。"
          />
        </Form>
      </AppModal>

      {/* 部门配置查询（超管只读）：统一走 AppModal——尺寸可记忆/拖拽，
          pre-wrap 让超长字段自动折行，右上「复制」一键拷走 */}
      {deptView && (
        <AppModal
          dimension="resizable"
          defaultSize={{ w: 780, h: 560 }}
          rememberKey="dept-config-view"
          open
          width={780}
          title={`部门配置：${deptView.name}`}
          footer={[
            <Button key="close" onClick={() => setDeptView(null)}>关闭</Button>,
          ]}
          onCancel={() => setDeptView(null)}
          styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
          destroyOnClose
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 8 }}
            message={deptView.filled.length > 0
              ? `该部门自行覆盖了 ${deptView.filled.length} 个段（${deptView.filled.map(([k]) => k).join(' / ')}），其余跟随全局`
              : '该部门未做任何覆盖，以下全部是全局默认值'}
          />
          <div style={{ fontWeight: 600, fontSize: 12, margin: '8px 0 4px' }}>
            当前生效配置
            <Button size="small" style={{ marginLeft: 8 }}
              onClick={() => copyJson(deptView.effective, '当前生效配置')}>
              复制
            </Button>
          </div>
          <pre style={PRE_STYLE}>
            {JSON.stringify(deptView.effective, null, 2)}
          </pre>
          {deptView.filled.length > 0 && (
            <>
              <div style={{ fontWeight: 600, fontSize: 12, margin: '12px 0 4px' }}>
                本部门覆盖的字段
                <Button size="small" style={{ marginLeft: 8 }}
                  onClick={() => copyJson(
                    Object.fromEntries(deptView.filled), '部门覆盖字段')}>
                  复制
                </Button>
              </div>
              <pre style={{ ...PRE_STYLE, maxHeight: 240, background: '#fff7e6' }}>
                {JSON.stringify(Object.fromEntries(deptView.filled), null, 2)}
              </pre>
            </>
          )}
        </AppModal>
      )}
    </div>
  );
};

export default SettingsPage;
