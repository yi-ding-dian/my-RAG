import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, App as AntApp, Button, Collapse, Form, Input, Space, Tag, Tooltip,
} from 'antd';
import {
  UndoOutlined, DeleteOutlined, EyeOutlined, PlusOutlined,
} from '@ant-design/icons';
import AppModal, { AppModalFooter } from '../../../shared/components/common/AppModal';
import {
  testLlmConnection, testVisionConnection,
  getLlmModelList, getChatSettings, LLMModelItem,
} from '../../../shared/api/client';
import type {
  ParserLlmModelItem, ServiceProfile,
  SettingsReferences, VisionModelItem,
} from '../../../shared/api/types';
import {
  HEADING_END_PUNCT_DEFAULT, toFormValues,
} from '../shared';
import type { SectionKey, TestItem } from '../shared';
import { useModelListEditor } from './useModelListEditor';
import ChatPanel from '../panels/ingest/ChatPanel';
import RetrievalPanel from '../panels/retrieval/RetrievalPanel';
import IngestPanel from '../panels/ingest/IngestPanel';
import LlmPanel from '../panels/models/LlmPanel';
import EmbeddingPanel from '../panels/retrieval/EmbeddingPanel';
import ParseServicesPanel from '../panels/parsers/ParseServicesPanel';
import MinioPanel from '../panels/storage/MinioPanel';
import VectorStorePanel from '../panels/storage/VectorStorePanel';
import VisionPanel from '../panels/models/VisionPanel';
import ImageSummaryPanel from '../panels/models/ImageSummaryPanel';
import { LlmModelEditModal, VisionModelEditModal } from './ModelEditModals';
import PromptDetailModal from './PromptDetailModal';
import type { PromptPreview } from './PromptDetailModal';
import CloseConfirmModal from './CloseConfirmModal';
import type { DeleteTarget } from './DeleteConfirmModal';
import { useProfileDirty } from './useProfileDirty';
import { useProfileSave } from './useProfileSave';
import { usePanelTests } from './usePanelTests';

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

/** 折叠区的全部面板 key（保存前要展开它们，见 handleSave） */
const ALL_PANELS = [
  'llm', 'retrieval', 'ingest', 'chat', 'prompts', 'image_templates',
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
  const { message } = AntApp.useApp();

  const [form] = Form.useForm();
  const [activePanels, setActivePanels] = useState<string[]>([]);
  const [promptPreview, setPromptPreview] = useState<PromptPreview | null>(null);

  // ---- LLM 多模型管理 ----
  const [llmModels, setLlmModels] = useState<LLMModelItem[]>([]);
  const [llmActive, setLlmActive] = useState(0);
  // 激活流程中正在测试连接的模型索引（防重复点击）
  // ---- 图片解析模型 ----
  const [visionModels, setVisionModels] = useState<VisionModelItem[]>([]);
  // ---- 读图模板（聊天识图与入库摘要共用的「看图策略」）----
  /** 可选模板列表（后端下发，带正文——「载入内置模板」要用） */
  const [imageTemplateOptions, setImageTemplateOptions] =
    useState<Array<{ name: string; prompt: string; preview: string }>>([]);
  /** 没填自定义提示词时**实际会发**的提示词——给 placeholder 用，跟着模板走 */
  const [defaultImagePrompt, setDefaultImagePrompt] = useState('');
  /** 档案里配的读图模板（空 = 正在用代码内置那几套） */
  const watchedTemplates = Form.useWatch(['image_templates', 'items'], form)
    ?? [];
  /** 把内置模板载入表单（之后可编辑；不保存就不会固化，仍跟随代码更新） */
  const loadBuiltinTemplates = () => {
    form.setFieldsValue({
      image_templates: {
        items: imageTemplateOptions.map(t => ({ name: t.name,
                                                prompt: t.prompt })),
      },
    });
    setDirty(true);
  };
  const [visionActive, setVisionActive] = useState(0);
  // 「标题分层模型」下拉数据源（仅名称+model，无敏感字段；失败静默）
  const [headingModelOptions, setHeadingModelOptions] =
    useState<ParserLlmModelItem[]>([]);

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

  // ---- 以下三块逻辑已抽成 hook（同目录 useProfileDirty / useProfileSave / usePanelTests）----
  /** 取模型列表的最新值：指纹与入参组装都要含它们，用回调避开旧闭包 */
  const getModels = () => ({
    llm: llmModelsRef.current, llmActive: llmActiveRef.current,
    vision: visionModelsRef.current, visionActive: visionActiveRef.current,
  });

  const {
    dirty, setDirty, closeConfirmOpen, setCloseConfirmOpen,
    markClean, resetForOpen: resetDirty, shouldConfirmClose,
  } = useProfileDirty({ form, getModels });

  const { saving, editingId, savedSnapshot, buildInput, handleSave, resetForOpen: resetSave }
    = useProfileSave({
      form, profile, getModels, onSaved, markClean,
      expandAllPanels: async () => {
        // 折叠面板懒渲染：不展开则字段没注册进表单，validateFields 会跳过它们
        if (activePanels.length < ALL_PANELS.length) {
          setActivePanels([...ALL_PANELS]);
          await new Promise(resolve => { window.setTimeout(resolve, 0); });
        }
      },
    });

  const { setPanelResults, withTestResult, panelLabel }
    = usePanelTests({ buildInput, editingId, onTestResult });

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

  // 打开时按编辑对象初始化（脏状态的清理由 useProfileDirty 负责）
  useEffect(() => {
    if (!open) return undefined;
    resetSave();
    setPanelResults({});
    fillForm(profile);
    // 「档案名称」已提到折叠区之外常驻，没有 'ar' 这个组可展开（域卡的「档案」
    // 入口只是打开弹窗）
    setActivePanels(focusPanel && focusPanel !== 'ar' ? [focusPanel] : []);
    // 清脏标记 + 收起可能残留的确认框 + 延一帧记基线（见 useProfileDirty.resetForOpen）
    return resetDirty();
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

  // 读图模板下拉数据源 + 当前默认提示词（placeholder 用）：弹窗打开时拉取。
  // 与上面的「标题分层模型」同款：登录即可读；失败静默（表单退回空列表，
  // 用户仍能填自定义提示词）
  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    getChatSettings()
      .then(res => {
        if (cancelled) return;
        const d = res.data as {
          image_template_options?: Array<{ name: string; prompt: string;
                                           preview: string }>;
          default_image_prompt?: string;
        };
        setImageTemplateOptions(d.image_template_options ?? []);
        setDefaultImagePrompt(d.default_image_prompt ?? '');
      })
      .catch(() => {
        if (cancelled) return;
        setImageTemplateOptions([]);
        setDefaultImagePrompt('');
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // ---- 模型列表编辑（LLM / 图片解析两组逻辑逐字相同，收敛在 useModelListEditor）----
  const llmEditor = useModelListEditor<LLMModelItem>({
    entity: 'LLM 模型',
    models: llmModels,
    setModels: setLlmModels,
    active: llmActive,
    setActive: setLlmActive,
    setDirty,
    testConnection: testLlmConnection,
    referencesOf: name => references?.llm_models?.[name] ?? [],
    onRequestDelete,
    messages: {
      done: (name, ms) => `已激活「${name}」（连接成功，${ms}ms）`,
      doneForced: name => `已激活「${name}」`,
      forceTitle: name => `连接失败，确认激活「${name}」？`,
      forceOkText: '仍要激活',
    },
    fallbackHint: '会自动回退到全局激活模型',
    activeDeleteNote: '注意：它正是当前激活的模型，删除后默认模型会换成列表里的第一个。',
  });

  const visionEditor = useModelListEditor<VisionModelItem>({
    entity: '图片解析模型',
    models: visionModels,
    setModels: setVisionModels,
    active: visionActive,
    setActive: setVisionActive,
    setDirty,
    testConnection: testVisionConnection,
    referencesOf: name => references?.vision_models?.[name] ?? [],
    onRequestDelete,
    messages: {
      done: name => `已将「${name}」设为默认`,
      doneForced: name => `已将「${name}」设为默认`,
      forceTitle: name => `连接失败，确认将「${name}」设为默认？`,
      forceOkText: '仍要设为默认',
    },
    fallbackHint: '会自动回退到全局激活的图片模型',
    activeDeleteNote: '注意：它正是当前默认的图片解析模型，删除后默认会换成列表里的第一个。',
  });


  /** 放弃未保存的改动：回到最近一次保存（或本次打开时）的状态 */
  const discardChanges = () => {
    fillForm(savedSnapshot ?? profile);
    setDirty(false);
    message.info('已放弃未保存的改动');
  };

  /** 关闭弹窗：有未保存改动时二次确认（判据见 useProfileDirty） */
  const requestClose = () => {
    if (shouldConfirmClose()) setCloseConfirmOpen(true);
    else onClose();
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
                    llmTestingIdx={llmEditor.testingIdx}
                    openModelEdit={llmEditor.openEdit}
                    deleteModel={llmEditor.requestDelete}
                    activateModel={llmEditor.activate}
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
                    imageTemplateOptions={imageTemplateOptions}
                    defaultImagePrompt={defaultImagePrompt}
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
                key: 'image_templates',
                label: '读图模板库',
                children: (
                  <div>
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12, padding: '6px 12px' }}
                      message={
                        <span style={{ fontSize: 12 }}>
                          入库摘要与聊天识图<b>共用</b>这套「看图策略」，部门按名选一套。
                          模板<b>只写「看什么」</b>（逐字抄录关键文字、排除头像/时间戳等
                          装饰、看不清就明说），<b>不写输出格式</b>——输出形态由入库
                          （字段行）与聊天（自然语言 + 用户问题）各自追加。
                        </span>
                      }
                    />
                    {watchedTemplates.length === 0 && (
                      <Alert
                        type="warning"
                        showIcon
                        style={{ marginBottom: 12, padding: '6px 12px' }}
                        message={
                          <span style={{ fontSize: 12 }}>
                            当前用的是<b>内置模板</b>（{imageTemplateOptions.length} 套，
                            随代码更新）。要把它们改成自己的一份，先
                            <Button
                              type="link"
                              size="small"
                              style={{ padding: '0 4px', height: 'auto' }}
                              onClick={loadBuiltinTemplates}
                            >
                              载入内置模板
                            </Button>
                            再编辑——载入并保存后即固化为本档案的副本。
                          </span>
                        }
                      />
                    )}
                    <div style={{ maxHeight: 260, overflowY: 'auto', paddingRight: 4 }}>
                      <Form.List name={['image_templates', 'items']}>
                        {(fields, { add, remove }) => (
                          <>
                            {fields.map(({ key, name, ...rest }) => (
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
                                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
                                  <div style={{ width: 190, flexShrink: 0, display: 'flex', gap: 8 }}>
                                    <span style={{ ...LABEL_COL, flexShrink: 0 }}>名称：</span>
                                    <Form.Item
                                      {...rest}
                                      name={[name, 'name']}
                                      style={{ flex: 1, minWidth: 0, marginBottom: 0 }}
                                      rules={[{
                                        required: true, whitespace: true,
                                        message: '请输入模板名',
                                      }]}
                                    >
                                      <Input placeholder="如：界面与截图" maxLength={30} />
                                    </Form.Item>
                                  </div>
                                  <div style={{ flex: 1, minWidth: 0, display: 'flex', gap: 8 }}>
                                    <span
                                      style={{ ...LABEL_COL, ...LABEL_COL_MULTILINE, flexShrink: 0 }}
                                    >
                                      正文：
                                    </span>
                                    <Form.Item
                                      {...rest}
                                      name={[name, 'prompt']}
                                      style={{ flex: 1, minWidth: 0, marginBottom: 0 }}
                                      rules={[{
                                        required: true, whitespace: true,
                                        message: '请输入模板正文',
                                      }]}
                                    >
                                      <Input.TextArea
                                        rows={4}
                                        placeholder="该看什么、不该看什么——不含输出格式"
                                      />
                                    </Form.Item>
                                  </div>
                                  <Tooltip title="删除该模板">
                                    <Button
                                      type="text"
                                      size="small"
                                      danger
                                      icon={<DeleteOutlined />}
                                      onClick={() => remove(name)}
                                    />
                                  </Tooltip>
                                </div>
                              </div>
                            ))}
                            <Button
                              type="dashed"
                              block
                              icon={<PlusOutlined />}
                              onClick={() => add({ name: '', prompt: '' })}
                            >
                              添加模板
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
                    visionTestingIdx={visionEditor.testingIdx}
                    openModelEdit={visionEditor.openEdit}
                    deleteModel={visionEditor.requestDelete}
                    activateModel={visionEditor.activate}
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
        open={llmEditor.modalOpen}
        models={llmModels}
        editIdx={llmEditor.editIdx}
        onSave={llmEditor.save}
        onCancel={llmEditor.closeModal}
      />
      <VisionModelEditModal
        open={visionEditor.modalOpen}
        models={visionModels}
        // 「模型名」下拉的数据源：从 LLM 模型列表里挑，地址/Key 一并带出
        llmModels={llmModels}
        editIdx={visionEditor.editIdx}
        onSave={visionEditor.save}
        onCancel={visionEditor.closeModal}
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
