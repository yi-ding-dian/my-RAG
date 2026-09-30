import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, App as AntApp, Button, Collapse, Form, Input, Space, Tag, Tooltip, Typography,
} from 'antd';
import {
  UndoOutlined, DeleteOutlined, EyeOutlined, PlusOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import AppModal, { AppModalFooter } from '../../shared/components/common/AppModal';
import {
  asApiError, createProfile, updateProfile,
  testDraftConnection, testLlmConnection, testProfileConnection, testVisionConnection,
  getLlmModelList, LLMModelItem,
} from '../../shared/api/client';
import type {
  ParserLlmModelItem, ServiceProfile, ServiceProfileInput,
  SettingsReferences, VisionModelItem,
} from '../../shared/api/types';
import {
  HEADING_END_PUNCT_DEFAULT, PANEL_TEST_SECTIONS, sectionLabel, toFormValues,
  toProfileInput, toTestItems,
} from './shared';
import type { ProfileFormValues, SectionKey, TestItem } from './shared';
import ChatPanel from './ChatPanel';
import RetrievalPanel from './RetrievalPanel';
import IngestPanel from './IngestPanel';
import LlmPanel from './LlmPanel';
import EmbeddingPanel from './EmbeddingPanel';
import ParseServicesPanel from './ParseServicesPanel';
import MinioPanel from './MinioPanel';
import VectorStorePanel from './VectorStorePanel';
import VisionPanel from './VisionPanel';
import ImageSummaryPanel from './ImageSummaryPanel';
import { LlmModelEditModal, VisionModelEditModal } from './ModelEditModals';
import PromptDetailModal from './PromptDetailModal';
import type { PromptPreview } from './PromptDetailModal';
import CloseConfirmModal from './CloseConfirmModal';
import type { DeleteTarget } from './DeleteConfirmModal';

/** 提示词条目的标签列：固定宽度 + 右对齐，两个标签的冒号才能对齐成一条竖线；
    nowrap 防止窄屏下"提示词："被折成两行 */
const LABEL_COL: React.CSSProperties = {
  width: 62,
  textAlign: 'right',
  whiteSpace: 'nowrap',
  lineHeight: '24px', // 与单行 Input（small）等高 → 垂直居中
};

/** 多行正文的标签：对齐正文**首行**（textarea 自带内边距，标签也补一点） */
const LABEL_COL_MULTILINE: React.CSSProperties = {
  lineHeight: '22px',
  paddingTop: 4,
};

/** 新建档案时各字段的初值（后端默认值的镜像，密码类留空表示"用后端默认/保持原值"） */
const NEW_PROFILE_DEFAULTS = {
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
  heading_end_punct_whitelist: HEADING_END_PUNCT_DEFAULT.slice(),
  contextual_retrieval_max_full_doc_chars: 20000,
  ingestion_concurrency: 3,
  ingestion_kb_doc_limit: 0,
  ingestion_max_upload_mb: 100,
  chat_max_query_len: 2000,
  chat_citation_snippet_chars: 600,
  // 进 prompt 的检索片段总量预算（token）：按当前生产模型反推
  // （Qwen3.5-9B 窗口 15000 − 输出 4096 − 历史/系统提示余量 ≈ 6000）
  chat_prompt_total_max_tokens: 6000,
  // 聊天识图六项：与后端 config.ChatConfig 默认值对齐
  chat_image_enabled: true,
  chat_image_max_count: 3,
  chat_image_max_mb: 5,
  // 空 = 跟随「图片摘要」选的模型 / 用内置读图提示词（都是合法值）
  chat_image_model: '',
  chat_image_prompt: '',
  chat_image_desc_max_chars: 800,
  // MySQL / MinIO 预填后端默认值（密码类留空，保存时后端用默认或保持原值）
  mysql_host: '127.0.0.1', mysql_port: 5455, mysql_user: 'ragflow',
  mysql_database: 'my_rag',
  minio_endpoint: '127.0.0.1:9000', minio_access_key: '',
  minio_bucket: 'my-rag', minio_secure: false, minio_region: '',
  vector_store_backend: 'chroma', vector_store_milvus_uri: '',
};

/** 面板最近一次测试结果（折叠面板内持久展示，关掉面板不丢） */
interface PanelTestResult {
  ok: boolean;
  /** 失败项摘要；全通过时为空串 */
  msg: string;
  /** 测试时刻（本地时间字符串） */
  at: string;
}

/** 折叠区的全部面板 key（保存前要展开它们，见 handleSave） */
const ALL_PANELS = [
  'llm', 'retrieval', 'ingest', 'chat', 'prompts',
  'embedding', 'parse', 'vision', 'image_summary', 'minio', 'vector_store',
];

/**
 * 配置档案编辑弹窗（新建 / 编辑共用）
 *
 * 表单、模型列表、指纹基线等状态都在本组件内自持——它们是"这次编辑"的一部分，
 * 页面只负责开关它、并在保存后刷新列表。几个跨组件的口子：
 *
 * - `onSaved`      保存成功后让页面刷新档案列表
 * - `onRequestDelete` 删除提示词/模型时把确认框交给页面统一渲染（页面还要处理
 *                  档案本身的删除，两处用同一个确认框）
 * - `onTestResult` 面板里的"测试连接"结果回传页面，用于更新卡片上的状态灯
 */
const ProfileEditorModal: React.FC<{
  open: boolean;
  /** 编辑对象；null = 新建 */
  profile: ServiceProfile | null;
  /** 打开时默认展开哪个折叠面板（域卡点进来的） */
  focusPanel: string;
  readOnly: boolean;
  /** 引用关系：删提示词/模型时查"谁在用它" */
  references: SettingsReferences | null;
  /** 该档案的连接测试状态（面板里的状态灯、解析面板用） */
  testState?: Record<SectionKey, TestItem>;
  onRequestDelete: (target: DeleteTarget) => void;
  onClose: () => void;
  onSaved: () => void;
  /** 面板测试完成：把**本次被测的段**回传页面更新卡片状态灯 */
  onTestResult: (
    profileId: string,
    items: Record<SectionKey, TestItem>,
    sections: SectionKey[],
  ) => void;
}> = ({
  open, profile, focusPanel, readOnly, references, testState,
  onRequestDelete, onClose, onSaved, onTestResult,
}) => {
  const { message, modal } = AntApp.useApp();

  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [activePanels, setActivePanels] = useState<string[]>([]);
  /**
   * 用户是否**真的编辑过**表单或模型列表——关闭时唯一的"脏"判据。
   *
   * 不能用"内容指纹变了"当判据（原先是这么做的）：模型列表存在独立 state 里，
   * 打开弹窗时是异步填进去的，指纹基线却在同一轮 effect 里就记下了，于是
   * **打开后什么都不改直接关闭也会报"有未保存的改动"**。改成只认用户的编辑
   * 动作：Form.onValuesChange 只在交互时触发，setFieldsValue 不触发（见
   * rc-field-form useForm.js，setFieldsValue 不调 onValuesChange）。
   */
  const [dirty, setDirty] = useState(false);
  /** 上次保存（或刚打开）时的内容指纹：脏了之后用它排除"改了又改回原值" */
  const baselineRef = useRef('');
  const [closeConfirmOpen, setCloseConfirmOpen] = useState(false);
  const [promptPreview, setPromptPreview] = useState<PromptPreview | null>(null);
  /** 新建保存成功后的 id：让弹窗就地切成"编辑"态，不必回头去改页面状态 */
  const [createdId, setCreatedId] = useState<string | null>(null);
  /** 最后一次保存成功后的档案快照：「放弃改动」回到这里（新建保存后也能正确回退） */
  const [savedSnapshot, setSavedSnapshot] = useState<ServiceProfile | null>(null);
  /** 各面板最近一次测试结果（面板内持久展示，不像 toast 一闪而过） */
  const [panelResults, setPanelResults] =
    useState<Record<string, PanelTestResult>>({});

  // ---- LLM 多模型管理 ----
  const [llmModels, setLlmModels] = useState<LLMModelItem[]>([]);
  const [llmActive, setLlmActive] = useState(0);
  // 激活流程中正在测试连接的模型索引（防重复点击）
  const [llmTestingIdx, setLlmTestingIdx] = useState<number | null>(null);
  const [modelModalOpen, setModelModalOpen] = useState(false);
  const [modelEditIdx, setModelEditIdx] = useState<number | null>(null);
  // ---- 图片解析模型 ----
  const [visionModels, setVisionModels] = useState<VisionModelItem[]>([]);
  const [visionActive, setVisionActive] = useState(0);
  const [visionTestingIdx, setVisionTestingIdx] = useState<number | null>(null);
  const [visionModalOpen, setVisionModalOpen] = useState(false);
  const [visionEditIdx, setVisionEditIdx] = useState<number | null>(null);
  // 「标题分层模型」下拉数据源（仅名称+model，无敏感字段；失败静默）
  const [headingModelOptions, setHeadingModelOptions] =
    useState<ParserLlmModelItem[]>([]);
  /** 正在测试的面板 key（折叠面板标题上的按钮 loading） */
  const [panelTesting, setPanelTesting] = useState('');

  /** 实际操作的档案 id：编辑取传入的，新建成功后用 createdId 顶上 */
  const editingId = profile?.id ?? createdId;

  /**
   * 模型列表的"最新值"镜像。
   *
   * editFingerprint() 会被 setTimeout 回调这类**旧闭包**调用，直接读 state 拿到的
   * 是那次渲染的快照——弹窗刚打开时模型列表还在异步填充，读到的永远是空数组，
   * 基线因此记成 `llm: [], vision: []`，与关闭时的真实列表必然对不上。
   * 改读 ref：任何闭包拿到的都是最新值。渲染期直接赋值，ref 只用于"读最新"，
   * 不参与渲染输出。
   */
  const llmModelsRef = useRef(llmModels);
  const llmActiveRef = useRef(llmActive);
  const visionModelsRef = useRef(visionModels);
  const visionActiveRef = useRef(visionActive);
  llmModelsRef.current = llmModels;
  llmActiveRef.current = llmActive;
  visionModelsRef.current = visionModels;
  visionActiveRef.current = visionActive;

  /**
   * 当前编辑内容的指纹：表单全部字段 + 两个模型列表。
   * 后两者存在独立 state 里（不在 Form 中），漏掉它们就会「改了模型列表却
   * 检测不出未保存」。
   */
  const editFingerprint = () => JSON.stringify({
    form: form.getFieldsValue(true),
    llm: llmModelsRef.current, llmActive: llmActiveRef.current,
    vision: visionModelsRef.current, visionActive: visionActiveRef.current,
  });

  /** 把一份档案（或新建默认值）填进表单与模型列表：打开弹窗、放弃改动共用 */
  const fillForm = useCallback((p: ServiceProfile | null) => {
    if (p) {
      // **不要先 form.resetFields()**：它会把 Form.List 的键一起清掉，之后
      // setFieldsValue 塞进去的 prompts.items 渲染不出条目（其余普通字段不受
      // 影响，所以档案名照样回填，只有提示词库空空如也——踩过）。
      // setFieldsValue 本身是全量的，足够盖掉上一次编辑的残留值。
      form.setFieldsValue(toFormValues(p));
      // llm 段回填（后端已迁移为 {models, active} 结构）
      const sec = p.llm as unknown as {
        models?: LLMModelItem[]; active?: number;
      };
      setLlmModels(Array.isArray(sec?.models) && sec.models.length ? sec.models : []);
      setLlmActive(sec?.active ?? 0);
      // vision 段回填（同为 {models, active} 结构）
      const vsec = p.vision as unknown as {
        models?: VisionModelItem[]; active?: number;
      };
      setVisionModels(
        Array.isArray(vsec?.models) && vsec.models.length ? vsec.models : []);
      setVisionActive(vsec?.active ?? 0);
    } else {
      setLlmModels([]);
      setLlmActive(0);
      setVisionModels([]);
      setVisionActive(0);
      form.setFieldsValue(NEW_PROFILE_DEFAULTS);
    }
  }, [form]);

  // 打开时按编辑对象初始化；顺带记一份指纹基线（基线取值见 editFingerprint 注释）
  useEffect(() => {
    if (!open) return undefined;
    setCreatedId(null);
    setSavedSnapshot(null);
    setDirty(false);
    // 显式收起"未保存确认框"：万一上次是被异常路径关掉的，残留的 open=true
    // 会让它在新一轮编辑刚开始就冒出来
    setCloseConfirmOpen(false);
    setPanelResults({});
    fillForm(profile);
    // 「档案名称」已提到折叠区之外常驻，没有 'ar' 这个组可展开（域卡的「档案」
    // 入口只是打开弹窗）
    setActivePanels(focusPanel && focusPanel !== 'ar' ? [focusPanel] : []);
    const timer = window.setTimeout(() => {
      baselineRef.current = editFingerprint();
    }, 0);
    return () => window.clearTimeout(timer);
    // 只跟「打开 / 换了编辑对象」走。**不能**把 llmModels 等加进依赖：
    // 它们每次编辑都会变，一进依赖基线就被重置，等于永远检测不出改动。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, profile, focusPanel]);

  // 「标题分层模型」下拉数据源：弹窗打开时拉取（登录即可读）；失败静默
  useEffect(() => {
    if (!open) return undefined;
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
  }, [open]);

  // ---- LLM 模型操作（添加/编辑/删除/勾选激活） ----
  /** 打开模型编辑弹窗（表单回填在 ModelEditModals 内部做） */
  const openModelEdit = (idx: number | null) => {
    setModelEditIdx(idx);
    setModelModalOpen(true);
  };

  /** 模型弹窗保存：插入或替换（api_key 留空 = 保留原值） */
  const saveLlmModel = (item: LLMModelItem) => {
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
    setDirty(true);
  };

  const deleteModel = (idx: number) => {
    setLlmModels(prev => {
      if (prev.length <= 1) return prev; // 至少保留 1 个模型
      const next = prev.filter((_, i) => i !== idx);
      setLlmActive(a => (idx === a ? 0 : idx < a ? a - 1 : a));
      return next;
    });
    setDirty(true);
  };

  /** 删除 LLM 模型前的确认：谁在用它（外部查询是按名字指定的） */
  const requestDeleteModel = (idx: number) => {
    const m = llmModels[idx];
    if (!m) return;
    onRequestDelete({
      title: `LLM 模型「${m.name}」`,
      references: references?.llm_models?.[m.name] ?? [],
      fallbackHint: '会自动回退到全局激活模型',
      extraNote: idx === llmActive
        ? '注意：它正是当前激活的模型，删除后默认模型会换成列表里的第一个。'
        : undefined,
      onConfirm: () => deleteModel(idx),
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
          setDirty(true);
          message.success(`已激活「${item.name}」`);
        },
      });
    };
    try {
      const res = await testLlmConnection(item);
      if (res.data.ok) {
        setLlmActive(idx);
        setDirty(true);
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
  /** 打开图片模型编辑弹窗（表单回填在 ModelEditModals 内部做） */
  const openVisionEdit = (idx: number | null) => {
    setVisionEditIdx(idx);
    setVisionModalOpen(true);
  };

  /** 图片模型弹窗保存：插入或替换（api_key 留空 = 保留原值） */
  const saveVisionModel = (item: VisionModelItem) => {
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
    setDirty(true);
  };

  const deleteVisionModel = (idx: number) => {
    setVisionModels(prev => {
      if (prev.length <= 1) return prev; // 至少保留 1 个模型
      const next = prev.filter((_, i) => i !== idx);
      setVisionActive(a => (idx === a ? 0 : idx < a ? a - 1 : a));
      return next;
    });
    setDirty(true);
  };

  /** 删除图片模型前的确认：部门是按名字选它的（image_summary.model） */
  const requestDeleteVisionModel = (idx: number) => {
    const m = visionModels[idx];
    if (!m) return;
    onRequestDelete({
      title: `图片解析模型「${m.name}」`,
      references: references?.vision_models?.[m.name] ?? [],
      fallbackHint: '会自动回退到全局激活的图片模型',
      extraNote: idx === visionActive
        ? '注意：它正是当前默认的图片解析模型，删除后默认会换成列表里的第一个。'
        : undefined,
      onConfirm: () => deleteVisionModel(idx),
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
          setDirty(true);
          message.success(`已将「${item.name}」设为默认`);
        },
      });
    };
    try {
      const res = await testVisionConnection(item);
      if (res.data.ok) {
        setVisionActive(idx);
        setDirty(true);
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

  // ---- 保存 / 关闭 ----
  /** 当前表单 + 模型列表组装成后端入参（保存 与"按当前值测试"共用） */
  const buildInput = (): ServiceProfileInput => toProfileInput(
    form.getFieldsValue(true) as ProfileFormValues,
    llmModels.length ? { models: llmModels, active: llmActive } : undefined,
    visionModels.length ? { models: visionModels, active: visionActive } : undefined,
  );

  const doSave = async (vals: ProfileFormValues) => {
    setSaving(true);
    try {
      const llmSection = llmModels.length
        ? { models: llmModels, active: llmActive } : undefined;
      const visionSection = visionModels.length
        ? { models: visionModels, active: visionActive } : undefined;
      const data = toProfileInput(vals, llmSection, visionSection);
      let saved: ServiceProfile | null = null;
      if (editingId) {
        const res = await updateProfile(editingId, data);
        saved = res.data;
        message.success('配置档案已保存');
      } else {
        // 新建后留在弹窗里继续编辑，但必须记住 id——否则再点一次保存会又建一份
        const res = await createProfile(
          data as ServiceProfileInput & { name: string });
        setCreatedId(res.data.id);
        saved = res.data;
        message.success('配置档案已创建');
      }
      // 保存后**不关弹窗**：用户可以接着改、接着存，改完自己关
      onSaved();
      // 存过了：基线刷新、脏标记清掉，关窗时不再拦人
      baselineRef.current = editFingerprint();
      setDirty(false);
      // 「放弃改动」要能回到最近一次保存的状态（新建保存后 profile 仍是 null）
      if (saved) setSavedSnapshot(saved);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  /**
   * 保存：先把所有面板展开再校验。
   *
   * 折叠面板是懒渲染的——没展开的面板不进 DOM，它的字段也就没注册进表单，
   * `validateFields()` 会**直接跳过**它们：必填项空着也能存进去，存出一份
   * 不完整的配置。所以保存前展开全部，让所有字段都参与到校验里。
   *
   * 曾经的做法是给每个面板加 `forceRender`（一直是全渲染），但那把弹窗打开
   * 拖慢了约 100ms（10 个面板 48 个字段一次性渲染，实测 40ms → 150ms）——
   * 打开是高频动作、校验漏填是低频场景，成本花错了地方，改成按需展开。
   *
   * 展开是异步生效的，得等一帧让 React 渲染完、字段真正注册上，再校验。
   */
  const handleSave = async () => {
    if (activePanels.length < ALL_PANELS.length) {
      setActivePanels([...ALL_PANELS]);
      await new Promise(resolve => { window.setTimeout(resolve, 0); });
    }
    try {
      const vals = await form.validateFields();
      await doSave(vals);
    } catch (e: unknown) {
      // 校验没过：antd 会把出错的字段标红，这里再把它滚进视野——
      // 展开全部后表单很长，不滚的话用户看不到错在哪
      const first = (e as { errorFields?: { name: (string | number)[] }[] })
        ?.errorFields?.[0]?.name;
      if (first) {
        form.scrollToField(first as never, { behavior: 'smooth', block: 'center' });
      }
    }
  };

  /** 放弃未保存的改动：回到最近一次保存（或本次打开时）的状态 */
  const discardChanges = () => {
    fillForm(savedSnapshot ?? profile);
    setDirty(false);
    message.info('已放弃未保存的改动');
  };

  /**
   * 关闭弹窗：有未保存改动时二次确认。
   *
   * 判据是**用户编辑过**（dirty），而不是"内容变了"——折叠面板是懒渲染的，
   * 展开一个面板会让它的字段注册进表单、内容随之变化，但那是渲染产物不是
   * 用户的改动，不该拦人。dirty 之后再比一次指纹，是为了放过"改了又改回
   * 原值"的情况。
   */
  const requestClose = () => {
    if (!dirty) {
      onClose();
      return;
    }
    if (editFingerprint() === baselineRef.current) {
      onClose();
      return;
    }
    setCloseConfirmOpen(true);
  };

  /**
   * 折叠面板标题"测试"：探测该面板对应的段，toast 结果并留在面板里备查。
   *
   * **测的是表单里当前填的值**，不是已保存的配置——改完地址先测通再保存是配置
   * 时的常规动作。后端只探测传入的段，点「向量存储」不会顺带把 LLM / DeepDoc
   * 也等一遍。新建档案还没落库（无 id）时走 draft 接口，同样能测。
   */
  const handlePanelTest = async (panelKey: string, sections: SectionKey[]) => {
    setPanelTesting(panelKey);
    try {
      const body = buildInput();
      const res = editingId
        ? await testProfileConnection(editingId, body, sections)
        : await testDraftConnection(body, sections);
      // 结果回传页面，驱动卡片上的状态灯（新建尚无档案，无处可回传）
      if (editingId) onTestResult(editingId, toTestItems(res.data), sections);
      const failed = sections
        .map(k => ({
          key: k,
          r: (res.data as unknown as
            Record<string, { ok: boolean; message: string } | undefined>)[k],
        }))
        .filter((x): x is { key: SectionKey; r: { ok: boolean; message: string } } =>
          Boolean(x.r))
        .filter(x => !x.r.ok);
      setPanelResults(prev => ({
        ...prev,
        [panelKey]: {
          ok: failed.length === 0,
          at: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
          msg: failed.map(x =>
            `${sectionLabel[x.key] ?? x.key}：${x.r.message}`).join('；'),
        },
      }));
      if (failed.length === 0) {
        message.success(`${sectionLabel[sections[0]] ?? '配置'}：连接测试通过`);
      } else {
        failed.forEach(x =>
          message.error(`${sectionLabel[x.key] ?? x.key}：${x.r.message}`));
      }
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '测试失败');
    } finally {
      setPanelTesting('');
    }
  };

  /** 折叠面板内容顶部：最近一次测试的结果条（可关闭，不像 toast 一闪而过） */
  const withTestResult = (panelKey: string, node: React.ReactNode) => {
    const r = panelResults[panelKey];
    if (!r) return node;
    return (
      <>
        <Alert
          type={r.ok ? 'success' : 'error'}
          showIcon
          closable
          style={{ marginBottom: 12, padding: '5px 12px' }}
          onClose={() => setPanelResults(prev => {
            const next = { ...prev };
            delete next[panelKey];
            return next;
          })}
          message={
            <span style={{ fontSize: 12 }}>
              {r.ok ? '连接测试通过' : r.msg}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {' '}· {r.at}
              </Typography.Text>
            </span>
          }
        />
        {node}
      </>
    );
  };

  /** 折叠面板标题：右侧可测面板夹带"测试"按钮（按当前填写值探测该段） */
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
          <Tooltip title="用当前填写的内容测试这一段（不必先保存）">
            <Button
              type="text"
              size="small"
              icon={<ThunderboltOutlined />}
              loading={panelTesting === panelKey}
              onClick={e => {
                e.stopPropagation();
                void handlePanelTest(panelKey, secList);
              }}
            >
              测试
            </Button>
          </Tooltip>
        )}
      </div>
    );
  };

  return (
    <>
      {/* 新建/编辑弹窗：固定高度（7 个 Collapse 面板默认全展开，内容超高），
          头部/关闭按钮固定，滚动只在内容区内部（滚动结构修复见
          styles/modals.css .profile-config-modal，与 .chunk-detail-modal 同一套规则） */}
      <AppModal
        dimension="resizable"
        rememberKey="settings-1"
        className="profile-config-modal"
        title={
          <Space size={8}>
            <span>{editingId ? '编辑配置档案' : '新建配置档案'}</span>
            {dirty && <Tag color="warning" style={{ marginInlineEnd: 0 }}>未保存</Tag>}
          </Space>
        }
        open={open}
        onCancel={requestClose}
        // 不要「取消」按钮——右上角已有 ✕，两个关闭入口重复。
        // ✕ / Esc 都走 requestClose：有未保存改动会先弹二次确认
        footer={
          <AppModalFooter
            okText="保存"
            cancelText={null}
            okLoading={saving}
            onOk={handleSave}
            // 左侧：改了才给「放弃改动」——回到最近一次保存的状态，省得关掉重开
            extra={dirty && (
              <Button
                size="small"
                icon={<UndoOutlined />}
                disabled={saving}
                onClick={discardChanges}
              >
                放弃改动
              </Button>
            )}
          />
        }
        width={720}
        // 初始大小交给 defaultSize：**不能传 style.height**——那会锁死弹窗高度，
        // 与 AppModal 的拖拽打架（拖拽只改正文区高度，正文一高就把 footer 顶到
        // 弹窗外面、按钮出屏）。用户拖过的尺寸由 rememberKey 记住。
        defaultSize={{ w: 720, h: 700 }}
      >
        <Form
          form={form}
          layout="vertical"
          size="small"
          disabled={readOnly}
          // 脏判据只认用户的实际编辑：setFieldsValue 不触发本回调（rc-field-form），
          // 所以回填、放弃改动、切换档案都不会误标"未保存"
          onValuesChange={() => setDirty(true)}
        >
          {/* 档案名称常驻在折叠区外：它只有一个字段，单独占一个折叠组既多一次
              点击、展开后又留一大片空白（原先就是「档案基本设置」那一块） */}
          <Form.Item
            name="name"
            label="档案名称"
            rules={[{ required: true, message: '请输入档案名称' }]}
            style={{ marginBottom: 12 }}
          >
            <Input placeholder="例如：本地 Qwen 默认、云端 DeepSeek" />
          </Form.Item>
          <Collapse
            size="small"
            activeKey={activePanels}
            onChange={k => {
              const keys = Array.isArray(k) ? k : [k];
              setActivePanels(keys.filter(Boolean));
            }}
            items={[
              {
                key: 'llm',
                label: panelLabel('llm', 'LLM 模型管理'),
                children: withTestResult('llm', (
                  <LlmPanel
                    llmModels={llmModels}
                    llmActive={llmActive}
                    llmTestingIdx={llmTestingIdx}
                    openModelEdit={openModelEdit}
                    deleteModel={requestDeleteModel}
                    activateModel={activateModel}
                  />
                )),
              },
              {
                key: 'retrieval',
                label: panelLabel('retrieval', '检索与切块'),
                children: withTestResult('retrieval', (
                  <RetrievalPanel headingModelOptions={headingModelOptions} />
                )),
              },
              {
                key: 'ingest',
                label: '入库与限制',
                children: <IngestPanel />,
              },
              {
                key: 'chat',
                label: panelLabel('chat', '聊天设置'),
                // 识图模型下拉的选项：LLM 模型 + 图片解析模型两份列表（见 ChatPanel）
                children: (
                  <ChatPanel
                    llmModels={llmModels}
                    visionModels={visionModels}
                    onEdit={() => setDirty(true)}
                  />
                ),
              },
              {
                key: 'prompts',
                label: '系统提示词库',
                children: (
                  <div>
                    {/* 与下面「LLM 对话模型」面板同款 Alert；内容压成一行小字 */}
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12, padding: '6px 12px' }}
                      message={
                        <span style={{ fontSize: 12 }}>
                          供配置使用：选一条即可复用，改正文即全生效（存名称引用，非内容副本）
                        </span>
                      }
                    />
                    {/* 弹窗 body 是固定高度（见弹窗注释），条目多了必须自己滚动，
                        否则"添加提示词"按钮会被 footer 盖住 */}
                    <div style={{ maxHeight: 260, overflowY: 'auto', paddingRight: 4 }}>
                      <Form.List name={['prompts', 'items']}>
                        {(fields, { add, remove }) => (
                          <>
                            {fields.map(({ key, name, ...rest }) => (
                              // 卡片式：名称与提示词**并排**同一行，各自标签在左、内容在右。
                              // 并排的代价是提示词框只剩约 250px 宽，长文本会频繁折行——
                              // 这是用户权衡后的选择（优先让每条只占 4 行高）
                              <div
                                key={key}
                                style={{
                                  border: '1px solid #f0f0f0',
                                  borderRadius: 8,
                                  padding: 12,
                                  marginBottom: 8,
                                  background: '#fafafa',
                                }}
                              >
                                {/* 外层并排：名称列定宽、提示词列吃剩余。用原生 flex 而不是
                                    Row/Col——Col 在定宽容器里 flex-basis 会取 Input 的固有
                                    宽度，把"标签+输入框"挤成上下两行（踩过）；配合
                                    minWidth: 0 才能让输入框真正收缩到容器宽度 */}
                                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
                                  <div style={{ width: 210, flexShrink: 0, display: 'flex', gap: 8 }}>
                                    <span style={{ ...LABEL_COL, flexShrink: 0 }}>名称：</span>
                                    <Form.Item
                                      {...rest}
                                      name={[name, 'name']}
                                      style={{ flex: 1, minWidth: 0, marginBottom: 0 }}
                                      rules={[{
                                        required: true, whitespace: true,
                                        message: '请输入名称',
                                      }]}
                                    >
                                      <Input placeholder="如：严谨引用" maxLength={50} />
                                    </Form.Item>
                                  </div>
                                  <div style={{ flex: 1, minWidth: 0, display: 'flex', gap: 8 }}>
                                    <span
                                      style={{ ...LABEL_COL, ...LABEL_COL_MULTILINE, flexShrink: 0 }}
                                    >
                                      提示词：
                                    </span>
                                    <Form.Item
                                      {...rest}
                                      name={[name, 'content']}
                                      style={{ flex: 1, minWidth: 0, marginBottom: 0 }}
                                      rules={[{
                                        required: true, whitespace: true,
                                        message: '请输入提示词内容',
                                      }]}
                                    >
                                      {/* 框窄，占位符说明只能留最关键的一句；
                                          全文看「查看详情」 */}
                                      <Input.TextArea
                                        rows={3}
                                        placeholder="可含 {refs} 占位符"
                                      />
                                    </Form.Item>
                                  </div>
                                  {/* 两个图标按钮，size=small 省宽度——每一像素都是
                                      从提示词框那儿匀过来的 */}
                                  <Space size={0} style={{ flexShrink: 0 }}>
                                    <Tooltip title="查看详情">
                                      <Button
                                        type="text"
                                        size="small"
                                        icon={<EyeOutlined />}
                                        onClick={() => setPromptPreview({
                                          name: String(form.getFieldValue(
                                            ['prompts', 'items', name, 'name']) ?? ''),
                                          content: String(form.getFieldValue(
                                            ['prompts', 'items', name, 'content']) ?? ''),
                                        })}
                                      />
                                    </Tooltip>
                                    <Tooltip title="删除该提示词">
                                      <Button
                                        type="text"
                                        size="small"
                                        danger
                                        icon={<DeleteOutlined />}
                                        onClick={() => {
                                          // 用**当前编辑值**里的名字查引用：改完名字
                                          // 还没保存时，按旧名字查会漏掉引用方
                                          const promptName = String(form.getFieldValue(
                                            ['prompts', 'items', name, 'name']) ?? '').trim();
                                          onRequestDelete({
                                            title: `提示词「${promptName || '未命名'}」`,
                                            references: references?.prompts?.[promptName] ?? [],
                                            fallbackHint: '会自动回退到全局默认提示词',
                                            onConfirm: () => remove(name),
                                          });
                                        }}
                                      />
                                    </Tooltip>
                                  </Space>
                                </div>
                              </div>
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
                key: 'embedding',
                label: panelLabel('embedding', 'Embedding 模型（OpenAI 兼容）'),
                children: withTestResult('embedding', <EmbeddingPanel />),
              },
              {
                key: 'parse',
                label: panelLabel('parse', '文档解析相关（MinerU / DeepDoc / 文档转换）'),
                children: withTestResult('parse', (
                  <ParseServicesPanel testItems={testState} />
                )),
              },
              {
                key: 'vision',
                label: panelLabel('vision', '图片解析模型（多模态，生成图片摘要）'),
                children: withTestResult('vision', (
                  <VisionPanel
                    visionModels={visionModels}
                    visionActive={visionActive}
                    visionTestingIdx={visionTestingIdx}
                    openModelEdit={openVisionEdit}
                    deleteModel={requestDeleteVisionModel}
                    activateModel={activateVisionModel}
                  />
                )),
              },
              {
                key: 'image_summary',
                label: '图片摘要（全局默认）',
                children: (
                  // 模型下拉的候选来自「图片解析模型」那份列表
                  <ImageSummaryPanel
                    visionModels={visionModels}
                    onEdit={() => setDirty(true)}
                  />
                ),
              },
              {
                key: 'minio',
                label: panelLabel('minio', '对象存储（MinIO）'),
                children: withTestResult('minio', <MinioPanel />),
              },
              {
                key: 'vector_store',
                label: panelLabel('vector_store', '向量存储'),
                children: withTestResult('vector_store', <VectorStorePanel />),
              },
            ]}
          />

          <div style={{ height: 4 }} />
        </Form>
      </AppModal>

      <CloseConfirmModal
        open={closeConfirmOpen}
        onKeepEditing={() => setCloseConfirmOpen(false)}
        onDiscard={() => {
          setCloseConfirmOpen(false);
          onClose();
        }}
      />

      {/* 模型添加/编辑弹窗：表单实例在组件内部，这里只管开关与保存 */}
      <LlmModelEditModal
        open={modelModalOpen}
        models={llmModels}
        editIdx={modelEditIdx}
        onSave={saveLlmModel}
        onCancel={() => setModelModalOpen(false)}
      />
      <VisionModelEditModal
        open={visionModalOpen}
        models={visionModels}
        // 「模型名」下拉的数据源：从 LLM 模型列表里挑，地址/Key 一并带出
        llmModels={llmModels}
        editIdx={visionEditIdx}
        onSave={saveVisionModel}
        onCancel={() => setVisionModalOpen(false)}
      />

      {/* 提示词详情：正文框窄，看全文/复制靠它（内容取表单当前值） */}
      <PromptDetailModal
        data={promptPreview}
        onClose={() => setPromptPreview(null)}
      />
    </>
  );
};

export default ProfileEditorModal;
