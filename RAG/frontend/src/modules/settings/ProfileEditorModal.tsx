import React, { useEffect, useRef, useState } from 'react';
import {
  Alert, App as AntApp, Button, Collapse, Form, Input, Space, Tooltip,
} from 'antd';
import {
  DeleteOutlined, EyeOutlined, PlusOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import AppModal, { AppModalFooter } from '../../shared/components/common/AppModal';
import {
  asApiError, createProfile, updateProfile,
  testLlmConnection, testProfileConnection, testVisionConnection,
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
  // MySQL / MinIO 预填后端默认值（密码类留空，保存时后端用默认或保持原值）
  mysql_host: '127.0.0.1', mysql_port: 5455, mysql_user: 'ragflow',
  mysql_database: 'my_rag',
  minio_endpoint: '127.0.0.1:9000', minio_access_key: '',
  minio_bucket: 'my-rag', minio_secure: false, minio_region: '',
  vector_store_backend: 'chroma', vector_store_milvus_uri: '',
};

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
  const [activePanels, setActivePanels] = useState<string[]>(['ar']);
  /** 上次保存（或刚打开）时的内容指纹：关闭时拿它比，判断有没有未保存的改动 */
  const baselineRef = useRef('');
  const [closeConfirmOpen, setCloseConfirmOpen] = useState(false);
  const [promptPreview, setPromptPreview] = useState<PromptPreview | null>(null);
  /** 新建保存成功后的 id：让弹窗就地切成"编辑"态，不必回头去改页面状态 */
  const [createdId, setCreatedId] = useState<string | null>(null);

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
   * 当前编辑内容的指纹：表单全部字段 + 两个模型列表。
   * 后两者存在独立 state 里（不在 Form 中），漏掉它们就会「改了模型列表却
   * 检测不出未保存」。
   */
  const editFingerprint = () => JSON.stringify({
    form: form.getFieldsValue(true),
    llm: llmModels, llmActive,
    vision: visionModels, visionActive,
  });

  // 打开时按编辑对象初始化；顺带记一份指纹基线。表单值与模型列表都是异步
  // setState 填进去的，要等这一轮渲染落地再读，否则记下的是空表单。
  useEffect(() => {
    if (!open) return undefined;
    setCreatedId(null);
    if (profile) {
      // **不要先 form.resetFields()**：它会把 Form.List 的键一起清掉，之后
      // setFieldsValue 塞进去的 prompts.items 渲染不出条目（其余普通字段不受
      // 影响，所以档案名照样回填，只有提示词库空空如也——踩过）。
      // setFieldsValue 本身是全量的，足够盖掉上一次编辑的残留值。
      form.setFieldsValue(toFormValues(profile));
      // llm 段回填（后端已迁移为 {models, active} 结构）
      const sec = profile.llm as unknown as {
        models?: LLMModelItem[]; active?: number;
      };
      setLlmModels(Array.isArray(sec?.models) && sec.models.length ? sec.models : []);
      setLlmActive(sec?.active ?? 0);
      // vision 段回填（同为 {models, active} 结构）
      const vsec = profile.vision as unknown as {
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
    setActivePanels([focusPanel]);
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
  };

  const deleteModel = (idx: number) => {
    setLlmModels(prev => {
      if (prev.length <= 1) return prev; // 至少保留 1 个模型
      const next = prev.filter((_, i) => i !== idx);
      setLlmActive(a => (idx === a ? 0 : idx < a ? a - 1 : a));
      return next;
    });
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
  };

  const deleteVisionModel = (idx: number) => {
    setVisionModels(prev => {
      if (prev.length <= 1) return prev; // 至少保留 1 个模型
      const next = prev.filter((_, i) => i !== idx);
      setVisionActive(a => (idx === a ? 0 : idx < a ? a - 1 : a));
      return next;
    });
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

  // ---- 保存 / 关闭 ----
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
        message.success('配置档案已保存');
      } else {
        // 新建后留在弹窗里继续编辑，但必须记住 id——否则再点一次保存会又建一份
        const res = await createProfile(
          data as ServiceProfileInput & { name: string });
        setCreatedId(res.data.id);
        message.success('配置档案已创建');
      }
      // 保存后**不关弹窗**：用户可以接着改、接着存，改完自己关
      onSaved();
      // 存过了，基线随之刷新——否则关窗时会误报"有未保存的改动"
      baselineRef.current = editFingerprint();
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

  /** 关闭弹窗：有未保存改动时二次确认（打开后没动过就直接关，不打扰） */
  const requestClose = () => {
    if (editFingerprint() === baselineRef.current) {
      onClose();
      return;
    }
    setCloseConfirmOpen(true);
  };

  /** 折叠面板标题"测试"：按档案已保存配置探测该段，toast 结果 */
  const handlePanelTest = async (panelKey: string, sections: SectionKey[]) => {
    if (!editingId) {
      message.warning('请先选择档案再测试');
      return;
    }
    setPanelTesting(panelKey);
    try {
      const res = await testProfileConnection(editingId);
      // 结果回传页面，驱动卡片上的状态灯（与卡片级测试同源）
      onTestResult(editingId, toTestItems(res.data), sections);
      const fail = sections
        .map(k => ({
          key: k,
          r: (res.data as unknown as Record<string, { ok: boolean; message: string }>)[k],
        }))
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
      setPanelTesting('');
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
              loading={panelTesting === panelKey}
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

  return (
    <>
      {/* 新建/编辑弹窗：固定高度（7 个 Collapse 面板默认全展开，内容超高），
          头部/关闭按钮固定，滚动只在内容区内部（滚动结构修复见
          styles/modals.css .profile-config-modal，与 .chunk-detail-modal 同一套规则） */}
      <AppModal
        dimension="resizable"
        rememberKey="settings-1"
        className="profile-config-modal"
        title={editingId ? '编辑配置档案' : '新建配置档案'}
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
          />
        }
        width={720}
        // 初始大小交给 defaultSize：**不能传 style.height**——那会锁死弹窗高度，
        // 与 AppModal 的拖拽打架（拖拽只改正文区高度，正文一高就把 footer 顶到
        // 弹窗外面、按钮出屏）。用户拖过的尺寸由 rememberKey 记住。
        defaultSize={{ w: 720, h: 700 }}
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
                                        placeholder="可含 {knowledge} / {refs} 占位符"
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
                key: 'llm',
                label: panelLabel('llm', 'LLM 对话模型（多模型管理）'),
                children: (
                  <LlmPanel
                    llmModels={llmModels}
                    llmActive={llmActive}
                    llmTestingIdx={llmTestingIdx}
                    openModelEdit={openModelEdit}
                    deleteModel={requestDeleteModel}
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
                children: <ParseServicesPanel testItems={testState} />,
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
                    deleteModel={requestDeleteVisionModel}
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
