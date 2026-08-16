import React, { useEffect, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Card,
  Collapse,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Tooltip,
} from 'antd';
import type { DocumentItem, IngestConfig, LayoutRecognize, MinerUBackend, ParseLang, ParseMethod, ParserEngine, ParserStatus, ParserStatusEntry, ParserLlmModelItem, ThinkingMode } from '../api/client';
import { getLlmModelList, getParserStatus, ingestDocument, testLlmModelByName } from '../api/client';
import LayoutRecognizeField from './parse-fields/LayoutRecognizeField';
import MinerUBackendField from './parse-fields/MinerUBackendField';
import PagesRangeField from './parse-fields/PagesRangeField';
import TaskPageSizeField from './parse-fields/TaskPageSizeField';
import SwitchField from './parse-fields/SwitchField';
import LangSelectField from './parse-fields/LangSelectField';

interface ParseConfigFormValues {
  parser_engine: ParserEngine;
  method: ParseMethod;
  chunk_size: number;
  overlap: number;
  split_level: number;
  regex_pattern: string;
  parent_chunk_size: number;
  parent_chunk_overlap: number;
  parent_split_level: number;
  retrieval_mode: 'parent' | 'child';
  // ===== PDF 解析配置（仅 PDF/混排文档场景有意义，简化：始终随表单发送当前值） =====
  layout_recognize: LayoutRecognize;
  /** MinerU 解析后端（仅解析引擎=mineru 时显示/提交；auto=不传跟随服务端默认） */
  backend: MinerUBackend;
  /** Form.List 页码范围（每项 {from, to}），提交时转 [[from, to]] */
  pages?: Array<{ from?: number; to?: number }>;
  task_page_size: number;
  table_enable: boolean;
  formula_enable: boolean;
  return_images: boolean;
  enable_heading_in_content: boolean;
  contextual_retrieval: boolean;
  knowledge_graph: boolean;
  /** 思考模式（DeepSeek thinking 控制）：disabled=关闭（推荐，更快）| enabled_low/high/max=开启+强度 */
  thinking_mode: ThinkingMode;
  /** 解析 LLM 模型（上下文摘要/知识图谱抽取专用，值为模型列表的 name；空=用当前激活模型） */
  parse_llm_model?: string;
  lang_list: ParseLang;
}

interface ParseConfigModalProps {
  open: boolean;
  /** 当前待解析文档（null 时弹窗不展示表单内容） */
  doc: DocumentItem | null;
  kbId?: string;
  onCancel: () => void;
  /** 解析任务触发成功后的回调（父组件刷新列表） */
  onSuccess: () => void;
}

/** 解析器可用性徽标：✅ 可用 / ❌ 不可用（红色），Tooltip 显示原因 */
const ParserStatusBadge: React.FC<{ name: string; entry: ParserStatusEntry | undefined }> = ({
  name,
  entry,
}) => {
  if (!entry) {
    return <span style={{ color: '#999' }}>{name} 检测中…</span>;
  }
  if (entry.available) {
    return (
      <Tooltip title={`${name} 可用`}>
        <span style={{ color: '#52c41a', fontWeight: 500 }}>✅ {name}</span>
      </Tooltip>
    );
  }
  return (
    <Tooltip title={`${name} 不可用${entry.reason ? `：${entry.reason}` : ''}，解析时将自动切换其他解析方式`}>
      <span style={{ color: '#ff4d4f', fontWeight: 500 }}>❌ {name}</span>
    </Tooltip>
  );
};

/** 解析配置弹窗：选择切块方式与参数后触发解析（参考 KnowFlow chunk-method-modal / chunking-config 交互） */
const ParseConfigModal: React.FC<ParseConfigModalProps> = ({ open, doc, kbId, onCancel, onSuccess }) => {
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<ParseConfigFormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [parserStatus, setParserStatus] = useState<ParserStatus | null>(null);
  // 解析 LLM 模型列表（GET /api/settings/llm/models，登录即可读）：上下文检索
  // 增强/知识图谱开关开启时显示"解析 LLM 模型"下拉，切换前先测试连接
  const [llmModels, setLlmModels] = useState<ParserLlmModelItem[]>([]);
  const [activeLlmIdx, setActiveLlmIdx] = useState(0);
  // 切换模型时正在测试连接的标记（防重复点击）
  const [testingLlm, setTestingLlm] = useState(false);
  const method = Form.useWatch('method', form) ?? 'naive';
  // Agentic 智能分块：LLM 读全文自主切逻辑段落并打标签。与上下文检索增强/
  // 知识图谱三选一互斥（用户约束：三个大模型功能只能选一个）——选 agentic
  // 自动关闭并禁用这两个开关（后端 resolve_parser_config 同样强制关闭双保险）
  const isAgentic = method === 'agentic';
  useEffect(() => {
    if (!open || !isAgentic) return;
    if (form.getFieldValue('contextual_retrieval')) {
      form.setFieldValue('contextual_retrieval', false);
    }
    if (form.getFieldValue('knowledge_graph')) {
      form.setFieldValue('knowledge_graph', false);
    }
  }, [isAgentic, open, form]);

  // 打开时探测解析器可用性（GET /kbs/parsers/status，并行 ≤8s）：显示状态徽标；
  // 探测失败静默（徽标不显示，不阻塞弹窗使用）
  useEffect(() => {
    if (open && doc) {
      let cancelled = false;
      getParserStatus()
        .then(res => {
          if (!cancelled) setParserStatus(res.data);
        })
        .catch(() => {
          if (!cancelled) setParserStatus(null);
        });
      return () => {
        cancelled = true;
      };
    }
    return undefined;
  }, [open, doc]);

  // 打开时拉取 LLM 模型列表（GET /api/settings/llm/models，登录即可读）：
  // "解析 LLM 模型"下拉数据源（仅名称+model，无敏感字段）；失败静默
  // （下拉不显示模型，开关功能不受影响）
  useEffect(() => {
    if (open) {
      let cancelled = false;
      getLlmModelList()
        .then(res => {
          if (cancelled) return;
          setLlmModels(res.data.models ?? []);
          setActiveLlmIdx(res.data.active ?? 0);
        })
        .catch(() => {
          if (!cancelled) setLlmModels([]);
        });
      return () => {
        cancelled = true;
      };
    }
    return undefined;
  }, [open]);

  // 打开时预填：重解析（ingested）预填上次 parser_config，其余用默认值
  useEffect(() => {
    if (open && doc) {
      const cfg = doc.parser_config ?? {};
      const toNumber = (v: unknown, fallback: number) => {
        if (v == null || v === '') return fallback;
        const n = Number(v);
        return Number.isFinite(n) ? n : fallback;
      };
      const isParentChild = doc.parser_id === 'parent_child';
      const initMethod: ParseMethod =
        doc.parser_id === 'parent_child' || doc.parser_id === 'title' ||
        doc.parser_id === 'regex' || doc.parser_id === 'agentic'
          ? (doc.parser_id as ParseMethod)
          : 'naive';
      // PDF 解析配置回填：parser_config 有对应字段时沿用（含页码范围），缺失用默认值
      const layoutRecognize: LayoutRecognize =
        cfg.layout_recognize === 'DeepDOC' || cfg.layout_recognize === 'PlainText'
          ? (cfg.layout_recognize as LayoutRecognize)
          : 'MinerU';
      const cfgPages =
        Array.isArray(cfg.pages) && cfg.pages.length > 0
          ? cfg.pages
              .filter(p => Array.isArray(p) && p.length >= 2)
              .map(p => ({ from: Math.max(1, Number(p[0]) || 1), to: Math.max(1, Number(p[1]) || 1000000) }))
          : [];
      form.setFieldsValue({
        // 重解析沿用上次解析引擎（非法/缺失回退自动）
        parser_engine: cfg.parser_engine === 'mineru' || cfg.parser_engine === 'deepdoc' || cfg.parser_engine === 'plain'
          ? (cfg.parser_engine as ParserEngine)
          : 'auto',
        method: initMethod,
        chunk_size: toNumber(cfg.chunk_size, isParentChild ? 512 : 800),
        overlap: toNumber(cfg.overlap, isParentChild ? 50 : 100),
        split_level: toNumber(cfg.split_level, 2),
        regex_pattern: typeof cfg.regex_pattern === 'string' ? cfg.regex_pattern : '',
        parent_chunk_size: toNumber(cfg.parent_chunk_size, 1024),
        parent_chunk_overlap: toNumber(cfg.parent_chunk_overlap, 100),
        parent_split_level: toNumber(cfg.parent_split_level, 2),
        retrieval_mode: cfg.retrieval_mode === 'child' ? 'child' : 'parent',
        // PDF 解析配置
        layout_recognize: layoutRecognize,
        // MinerU 解析后端：重解析沿用上次持久化值；缺失/非法回退"自动"（不传跟随服务端默认）
        backend:
          cfg.backend === 'hybrid-auto-engine' || cfg.backend === 'pipeline'
            ? (cfg.backend as MinerUBackend)
            : 'auto',
        pages: cfgPages.length > 0 ? cfgPages : [{ from: 1, to: 1000000 }],
        task_page_size: toNumber(cfg.task_page_size, 12),
        table_enable: typeof cfg.table_enable === 'boolean' ? cfg.table_enable : true,
        formula_enable: typeof cfg.formula_enable === 'boolean' ? cfg.formula_enable : true,
        return_images: typeof cfg.return_images === 'boolean' ? cfg.return_images : true,
        enable_heading_in_content:
          typeof cfg.enable_heading_in_content === 'boolean' ? cfg.enable_heading_in_content : false,
        contextual_retrieval:
          typeof cfg.contextual_retrieval === 'boolean' ? cfg.contextual_retrieval : false,
        knowledge_graph:
          typeof cfg.knowledge_graph === 'boolean' ? cfg.knowledge_graph : false,
        // 思考模式：重解析沿用上次持久化值；缺失/非法回退默认"关闭思考"
        thinking_mode: (
          ['disabled', 'enabled_low', 'enabled_high', 'enabled_max'] as const
        ).includes(cfg.thinking_mode as ThinkingMode)
          ? (cfg.thinking_mode as ThinkingMode)
          : 'disabled',
        // 解析 LLM 模型：重解析沿用上次持久化值；缺失/空 → 由下方 useEffect
        // 在模型列表加载后兜底为当前激活模型（"当前使用"）
        parse_llm_model:
          typeof cfg.parse_llm_model === 'string' && cfg.parse_llm_model
            ? cfg.parse_llm_model
            : undefined,
        lang_list: cfg.lang_list === 'en' ? 'en' : 'ch',
      });
    }
  }, [open, doc, form]);

  // 解析 LLM 模型默认值兜底：字段为空 → 回填当前激活模型；值已不在模型列表
  // （模型被删除/改名）→ 同样回退激活模型，保证提交值始终合法
  useEffect(() => {
    if (!open) return;
    const activeName = llmModels[activeLlmIdx]?.name;
    if (!activeName) return; // 列表未加载/为空：不动（提交空值=后端用激活模型）
    const val = form.getFieldValue('parse_llm_model');
    if (!val || !llmModels.some(m => m.name === val)) {
      form.setFieldValue('parse_llm_model', activeName);
    }
  }, [open, llmModels, activeLlmIdx, form]);

  // 切换解析 LLM 模型：先测试连接（POST /api/settings/llm/test-model，后端按
  // name 查完整配置探测）→ 通过才更新本地值；失败提示并保持原模型（绝不静默切换）
  const handleParseLlmChange = async (name: string) => {
    if (testingLlm) return;
    const prev = form.getFieldValue('parse_llm_model');
    if (!name || name === prev) return;
    setTestingLlm(true);
    try {
      const res = await testLlmModelByName(name);
      if (res.data.ok) {
        message.success(`「${name}」连接成功（${res.data.latency_ms}ms），已切换解析模型`);
        form.setFieldValue('parse_llm_model', name);
      } else {
        message.error(`「${name}」连接失败，保持原模型：${res.data.reason}`);
        form.setFieldValue('parse_llm_model', prev);
      }
    } catch (e: any) {
      message.error(`连接测试失败，保持原模型：${e.response?.data?.detail || '网络请求失败'}`);
      form.setFieldValue('parse_llm_model', prev);
    } finally {
      setTestingLlm(false);
    }
  };

  // MinerU 解析后端仅解析引擎=mineru 时显示（auto/其他引擎不发送，跟随服务端默认）
  const parserEngine = Form.useWatch('parser_engine', form) ?? 'auto';
  // 上下文检索增强/知识图谱开关状态（开启时显示额外 token 费用提示）
  const contextualRetrieval = Form.useWatch('contextual_retrieval', form);
  const knowledgeGraph = Form.useWatch('knowledge_graph', form);

  // 版面识别=DeepDOC 时解析引擎强制 deepdoc、=PlainText 时强制 plain
  // （与后端规则一致：auto+DeepDOC→deepdoc / auto+PlainText→plain；前端
  // 直接强制，避免出现 layout=DeepDOC + engine=mineru 之类的冲突组合）
  const layoutRecognize = Form.useWatch('layout_recognize', form) ?? 'MinerU';

  // ===== 版面识别联动显隐（设置不了的就不显示；依据后端实际生效范围）=====
  // 后端 resolve_parser_config 后 _PARSER_PARSE_OPTS 透传解析器：
  //   engine=deepdoc 时 parse_opts={}（表格/公式/图片/页码/任务页大小/语言全部不透传）；
  //   engine=plain 时 parser_client._extract_plain 不消费 parse_opts（纯文本直提无这些链路）；
  //   parse_opts 仅对 MinerU（API 请求体）有意义；plain 无需解析器探测。
  // 显隐对照表：
  //   配置项                  MinerU   DeepDOC   PlainText
  //   解析器状态               显示     显示      隐藏（plain 无需探测）
  //   页码范围 pages           显示     隐藏      隐藏（非 MinerU 不透传）
  //   任务页面大小             显示     隐藏      隐藏（同上）
  //   表格识别                 显示     隐藏      隐藏（同上；MinerU 下服务端固定开启暂不生效）
  //   公式识别                 显示     隐藏      隐藏（同上；取决于 MinerU 服务端）
  //   图片提取                 显示     隐藏      隐藏（同上；DeepDOC 无图片链路）
  //   包含父标题               显示     显示      隐藏（切块后处理与引擎无关，仅 UI 隐藏、提交保留）
  //   上下文检索/知识图谱/思考模式/语言/切块  均显示（与版面识别无关）
  const isMinerU = layoutRecognize === 'MinerU';
  const isPlainText = layoutRecognize === 'PlainText';
  useEffect(() => {
    if (open && layoutRecognize === 'DeepDOC') {
      form.setFieldValue('parser_engine', 'deepdoc');
    } else if (open && layoutRecognize === 'PlainText') {
      form.setFieldValue('parser_engine', 'plain');
    }
  }, [layoutRecognize, open, form]);

  // 切换切块方式时补齐该方式的默认值（已有值不覆盖）
  useEffect(() => {
    if (!open) return;
    const isParentChild = method === 'parent_child';
    const get = (name: keyof ParseConfigFormValues) => form.getFieldValue(name);
    if (isParentChild) {
      if (get('chunk_size') == null) form.setFieldValue('chunk_size', 512);
      if (get('overlap') == null) form.setFieldValue('overlap', 50);
      if (get('parent_chunk_size') == null) form.setFieldValue('parent_chunk_size', 1024);
      if (get('parent_chunk_overlap') == null) form.setFieldValue('parent_chunk_overlap', 100);
      if (get('parent_split_level') == null) form.setFieldValue('parent_split_level', 2);
      if (get('retrieval_mode') == null) form.setFieldValue('retrieval_mode', 'parent');
    } else {
      if (get('chunk_size') == null) form.setFieldValue('chunk_size', 800);
      if (get('overlap') == null) form.setFieldValue('overlap', 100);
    }
  }, [method, open, form]);

  const handleOk = async () => {
    if (!kbId || !doc) return;
    let values: ParseConfigFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    const config: IngestConfig = {
      // 解析引擎始终携带当前选择（auto/mineru/plain，后端默认 auto 行为一致）
      parser_engine: values.parser_engine,
      method: values.method,
      chunk_size: values.chunk_size,
      overlap: values.overlap,
    };
    if (values.method === 'title') config.split_level = values.split_level;
    if (values.method === 'regex') config.regex_pattern = values.regex_pattern;
    // 父子分块：发送子块 + 父块 + 检索模式全部参数；其他方式不发送 parent_* 字段
    if (values.method === 'parent_child') {
      config.parent_chunk_size = values.parent_chunk_size;
      config.parent_chunk_overlap = values.parent_chunk_overlap;
      config.parent_split_level = values.parent_split_level;
      config.retrieval_mode = values.retrieval_mode;
    }
    config.layout_recognize = values.layout_recognize;
    // MinerU 解析后端：仅解析引擎=mineru 时发送；选"自动"（auto）不传（跟随服务端默认）
    if (values.parser_engine === 'mineru' && values.backend && values.backend !== 'auto') {
      config.backend = values.backend;
    }
    // 版面识别联动提交（隐藏即不提交）：非 MinerU 时后端不读
    // pages/task_page_size/table_enable/formula_enable/return_images
    // （engine=deepdoc 时 parse_opts={}；plain 分支不消费 parse_opts），
    // 剔除避免持久化无关配置；缺省由后端 _DEFAULT_PARSER_CONFIG 兜底，
    // 重跑回填（前端缺省默认值）兼容
    if (values.layout_recognize === 'MinerU') {
      // 页码范围转 [[from,to]] 数组；空时发默认全篇 [[1, 1000000]]
      const pages: number[][] = (values.pages ?? [])
        .filter(p => p && typeof p.from === 'number' && typeof p.to === 'number')
        .map(p => [p.from as number, p.to as number]);
      config.pages = pages.length > 0 ? pages : [[1, 1000000]];
      config.task_page_size = values.task_page_size;
      config.table_enable = values.table_enable;
      config.formula_enable = values.formula_enable;
      config.return_images = values.return_images;
    }
    // 包含父标题：切块后处理（不依赖解析引擎，PlainText 下同样生效），
    // 始终提交（仅 UI 隐藏，不影响实际行为）
    config.enable_heading_in_content = values.enable_heading_in_content;
    config.contextual_retrieval = values.contextual_retrieval;
    config.knowledge_graph = values.knowledge_graph;
    config.thinking_mode = values.thinking_mode;
    // 解析 LLM 模型（摘要/图谱抽取专用）：空/未选不发 → 后端默认用激活模型
    if (values.parse_llm_model) config.parse_llm_model = values.parse_llm_model;
    config.lang_list = values.lang_list;
    // 提交前检查所选解析器可用性（探测结果来自弹窗打开时的状态接口）：
    // 不可用 → warning 提示将自动降级（后端解析前检测会执行降级链，仍可继续）；
    // 引擎联动（与后端 resolve_parser_engine 一致）：auto+DeepDOC→deepdoc、
    // auto+PlainText→plain（plain 无需探测）
    const chosenEngine: ParserEngine =
      values.parser_engine === 'auto' && values.layout_recognize === 'DeepDOC'
        ? 'deepdoc'
        : values.parser_engine === 'auto' && values.layout_recognize === 'PlainText'
          ? 'plain'
          : values.parser_engine;
    if (chosenEngine === 'deepdoc' && parserStatus?.deepdoc && !parserStatus.deepdoc.available) {
      message.warning(
        `DeepDoc 服务不可用（${parserStatus.deepdoc.reason || '未知原因'}），将自动切换 MinerU/纯文本解析`,
      );
    } else if (chosenEngine !== 'plain' && parserStatus?.mineru && !parserStatus.mineru.available) {
      message.warning(
        `MinerU 服务不可用（${parserStatus.mineru.reason || '未知原因'}），将自动切换纯文本解析`,
      );
    }
    setSubmitting(true);
    try {
      const res = await ingestDocument(kbId, doc.id, config);
      // 后端解析前检测确认降级时，响应带 degrade 提示（与提交前提示同源，二次确认）
      if (res.data?.degrade) {
        message.warning(`已降级：${res.data.degrade}`);
      } else {
        message.success(`已触发「${doc.original_name}」解析任务`);
      }
      onCancel();
      onSuccess();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '触发解析失败');
    } finally {
      setSubmitting(false);
    }
  };

  const isParentChild = method === 'parent_child';

  // 固定高度弹窗（内容长 → 88vh 档）：头部/关闭按钮固定，滚动只在内容区
  // 内部（滚动结构修复见 index.css .parse-config-modal，与
  // .chunk-detail-modal 同一套规则）
  return (
    <Modal
      className="parse-config-modal"
      title={doc ? `解析配置 - ${doc.original_name}` : '解析配置'}
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      confirmLoading={submitting}
      okText="开始解析"
      cancelText="取消"
      width={720}
      style={{ top: '8vh', height: 'min(88vh, calc(100vh - 120px))' }}
      styles={{
        content: { display: 'flex', flexDirection: 'column', height: '100%' },
        header: { flexShrink: 0 },
        body: { padding: '16px 20px', flex: 1, minHeight: 0, overflow: 'auto' },
        footer: { flexShrink: 0 },
      }}
    >
      <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
        {/* 解析引擎：auto=自动探测（MinerU 优先、不可用自动降级）/ mineru=强制高精度 / deepdoc=强制 DeepDoc / plain=纯文本直提 */}
        <Form.Item
          name="parser_engine"
          label="解析引擎"
          rules={[{ required: true, message: '请选择解析引擎' }]}
          extra="MinerU 适用于 PDF 混排（图文表格）文档；DeepDoc 通过 RAGFlow 服务解析，表格输出为可检索 HTML（仅 PDF）；自动模式优先 MinerU，不可用时降级纯文本；版面识别选 DeepDOC 时引擎自动切为 DeepDoc"
        >
          <Select
            options={[
              { value: 'auto', label: '自动（推荐）' },
              { value: 'mineru', label: 'MinerU 高精度（PDF 混排）' },
              { value: 'deepdoc', label: 'DeepDoc（表格输出可检索 HTML）' },
              { value: 'plain', label: '纯文本提取' },
            ]}
          />
        </Form.Item>

        {/* PDF 解析配置：版面识别 / 页码范围 / 任务页面大小 / 开关组 / 语言（参考 KnowFlow 每配置项一组件范式） */}
        <Collapse
          ghost
          style={{ marginBottom: 16, marginTop: 4 }}
          defaultActiveKey={['pdf-parse']}
          items={[
            {
              key: 'pdf-parse',
              label: 'PDF 解析配置',
              children: (
                <>
                  <LayoutRecognizeField />
                  {/* MinerU 解析后端：仅解析引擎=mineru 时显示（auto/其他引擎不传，
                      跟随 MinerU 服务端默认 hybrid-auto-engine） */}
                  {parserEngine === 'mineru' && <MinerUBackendField />}
                  {/* 解析器可用性徽标（弹窗打开时探测；不可用红色，Tooltip 显示原因）；
                      PlainText 无需解析器探测（后端 plain 分支直接 pypdf/python-docx 提取），不显示 */}
                  {parserStatus && !isPlainText && (
                    <Form.Item label="解析器状态" style={{ marginBottom: 16 }}>
                      <Space size="large">
                        <ParserStatusBadge name="MinerU" entry={parserStatus.mineru} />
                        <ParserStatusBadge name="DeepDoc" entry={parserStatus.deepdoc} />
                      </Space>
                    </Form.Item>
                  )}
                  {/* 页码范围/任务页面大小/表格/公式/图片：仅 MinerU 生效（非 MinerU 时
                      后端 parse_opts={} 或 plain 分支不消费），隐藏即不提交 */}
                  {isMinerU && <PagesRangeField />}
                  {isMinerU && <TaskPageSizeField />}
                  {isMinerU && (
                    <SwitchField
                      name="table_enable"
                      label="表格识别"
                      desc="当前 MinerU 服务端固定开启表格识别（以图片输出），此开关暂不生效"
                      defaultValue
                    />
                  )}
                  {isMinerU && (
                    <SwitchField
                      name="formula_enable"
                      label="公式识别"
                      desc="此开关暂不生效，取决于 MinerU 服务端配置"
                      defaultValue
                    />
                  )}
                  {isMinerU && (
                    <SwitchField
                      name="return_images"
                      label="图片提取"
                      desc="提取文档图片并保存（存 MinIO）"
                      defaultValue
                    />
                  )}
                  {/* 包含父标题是切块后处理（不依赖解析引擎，PlainText 下同样生效），
                      仅 UI 隐藏以精简表单，提交值保留（见 handleOk） */}
                  {!isPlainText && (
                    <SwitchField
                      name="enable_heading_in_content"
                      label="包含父标题"
                      desc="切块时在块前补标题路径"
                      defaultValue={false}
                    />
                  )}
                  {/* 上下文检索增强：对所有文档类型生效（切块后处理，不依赖解析器）；
                      开关开启时 Alert 提示额外 token 费用（用户确认的提示文案） */}
                  <SwitchField
                    name="contextual_retrieval"
                    label="上下文检索增强"
                    desc="切块后用 LLM 为每个块生成简短上下文摘要附在块头部，解决孤立分块缺乏全局背景的问题，提升检索质量"
                    defaultValue={false}
                    disabled={isAgentic}
                    tooltip={isAgentic ? '与 Agentic 分块互斥，只能选一个' : undefined}
                  />
                  {contextualRetrieval && (
                    <Alert
                      message="开启后每次解析将对每个切块调用 LLM 生成上下文摘要，将产生额外 token 费用"
                      type="warning"
                      showIcon
                      style={{ marginBottom: 12 }}
                    />
                  )}
                  {/* 知识图谱：对所有文档类型生效（切块后处理，不依赖解析器）；
                      开启时 Alert 提示额外 token 费用（与上下文检索增强并列） */}
                  <SwitchField
                    name="knowledge_graph"
                    label="知识图谱"
                    desc="入库时用 LLM 抽取实体关系构建知识图谱（切块详情可查看实体与关系）"
                    defaultValue={false}
                    disabled={isAgentic}
                    tooltip={isAgentic ? '与 Agentic 分块互斥，只能选一个' : undefined}
                  />
                  {knowledgeGraph && (
                    <Alert
                      message="开启后每次解析将对每个切块调用 LLM 抽取实体与关系，将产生额外 token 费用"
                      type="warning"
                      showIcon
                      style={{ marginBottom: 12 }}
                    />
                  )}
                  {/* 解析 LLM 模型：上下文检索增强/知识图谱任一开启时显示（下拉数据源=
                      系统配置模型列表，仅名称+model 无敏感字段；默认=当前激活模型，
                      切换前自动测试连接，通过才生效） */}
                  {(contextualRetrieval || knowledgeGraph || isAgentic) && (
                    <Form.Item
                      name="parse_llm_model"
                      label="解析 LLM 模型"
                      extra="使用模型仅影响上下文摘要/知识图谱抽取/Agentic 分块，对话仍用当前激活模型；切换前自动测试连接，通过才生效"
                    >
                      <Select
                        loading={testingLlm}
                        placeholder="默认使用当前激活模型"
                        options={llmModels.map((m, i) => ({
                          value: m.name,
                          label: `${m.name}${m.model && m.model !== m.name ? `（${m.model}）` : ''}${i === activeLlmIdx ? ' — 当前使用' : ''}`,
                        }))}
                        onChange={handleParseLlmChange}
                      />
                    </Form.Item>
                  )}
                  {/* 思考模式（DeepSeek thinking 控制）：图谱抽取/上下文摘要调用的
                      extra_body 组装；默认关闭——图谱抽取/摘要属简单延迟敏感任务，
                      关闭思考可加快解析速度并节省 token（DeepSeek 推理模型
                      reasoning 会大量消耗 token 拖慢响应） */}
                  <Form.Item
                    name="thinking_mode"
                    label="思考模式"
                    extra="控制图谱抽取/上下文摘要调用的 DeepSeek 思考（reasoning）。关闭思考可加快解析速度并节省 token（推荐）"
                  >
                    <Select
                      options={[
                        { value: 'disabled', label: '关闭思考（推荐，更快）' },
                        { value: 'enabled_low', label: '开启-低' },
                        { value: 'enabled_high', label: '开启-高' },
                        { value: 'enabled_max', label: '开启-最大' },
                      ]}
                    />
                  </Form.Item>
                  <LangSelectField />
                </>
              ),
            },
          ]}
        />

        <Form.Item name="method" label="切块方式" rules={[{ required: true }]}>
          <Select
            options={[
              { value: 'naive', label: '通用切块' },
              { value: 'title', label: '按标题切块' },
              { value: 'regex', label: '正则切块' },
              { value: 'parent_child', label: '父子分块' },
              { value: 'qa', label: 'QA 问答' },
              { value: 'agentic', label: 'Agentic 智能分块' },
            ]}
          />
        </Form.Item>

        {/* Agentic 智能分块：LLM 读全文自主切逻辑段落并打标签（说明与互斥提示） */}
        {method === 'agentic' && (
          <Alert
            message="Agentic 智能分块说明"
            description="LLM 自主判断完整逻辑段落切割并打标签（论述类/事实类/操作类/数据类/其他），仅支持 ≤1 万字文档（超过将提示换用其他切块方式）。与上下文检索增强、知识图谱互斥（只能选一个）。LLM 调用失败时自动回退按标题切块，不阻塞入库。"
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
          />
        )}

        {/* 父子分块：子块/父块双 Card 配置（参考 KnowFlow chunking-config 布局） */}
        {isParentChild && (
          <>
            <Alert
              message="父子分块模式说明"
              description="父分块按标题层级聚合，子分块精细切分；检索时子块精确匹配、返回父块完整上下文"
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
            />
            <Row gutter={16}>
              <Col span={12}>
                <Card title="子块配置" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item
                    name="chunk_size"
                    label="分块大小（字符）"
                    rules={[
                      { required: true, message: '请输入分块大小' },
                      { type: 'number', min: 50, max: 2000, message: '子块大小范围 50-2000' },
                    ]}
                  >
                    <InputNumber min={50} max={2000} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item name="overlap" label="重叠字符" rules={[{ required: true, message: '请输入重叠字符' }]}>
                    <InputNumber min={0} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item
                    name="retrieval_mode"
                    label="检索模式"
                    rules={[{ required: true, message: '请选择检索模式' }]}
                    extra="parent：子块精确匹配、返回父块上下文（推荐）"
                  >
                    <Select
                      options={[
                        { value: 'parent', label: 'parent（返回父块）' },
                        { value: 'child', label: 'child（返回子块）' },
                      ]}
                    />
                  </Form.Item>
                </Card>
              </Col>
              <Col span={12}>
                <Card title="父块配置" size="small" style={{ marginBottom: 16 }}>
                  <Form.Item
                    name="parent_chunk_size"
                    label="父块大小（字符，超长章节兜底上限）"
                    rules={[
                      { required: true, message: '请输入父块大小' },
                      { type: 'number', min: 200, max: 4000, message: '父块大小范围 200-4000' },
                    ]}
                  >
                    <InputNumber min={200} max={4000} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item
                    name="parent_chunk_overlap"
                    label="父块重叠（字符）"
                    rules={[
                      { required: true, message: '请输入父块重叠' },
                      { type: 'number', min: 0, max: 500, message: '父块重叠范围 0-500' },
                    ]}
                  >
                    <InputNumber min={0} max={500} style={{ width: '100%' }} />
                  </Form.Item>
                  <Form.Item
                    name="parent_split_level"
                    label="父块分割层级"
                    rules={[{ required: true, message: '请选择父块分割层级' }]}
                    extra="按标题层级聚合父块边界"
                  >
                    <Select
                      options={[
                        { value: 1, label: 'H1 - 最大章节' },
                        { value: 2, label: 'H2 - 主要章节（推荐）' },
                        { value: 3, label: 'H3 - 子章节' },
                        { value: 4, label: 'H4 - 小节' },
                        { value: 5, label: 'H5 - 段落级' },
                        { value: 6, label: 'H6 - 细粒度' },
                      ]}
                    />
                  </Form.Item>
                </Card>
              </Col>
            </Row>
          </>
        )}

        {/* QA 问答切块：说明（qa 无需额外参数；入库失败后由文档列表确认继续） */}
        {!isParentChild && method === 'qa' && (
          <Alert
            message="QA 问答切块说明"
            description="按“问：/答：”标记聚合问答对为整块（答案可跨多段，保留原文标记）。入库时会检测问答对占比（问答对/总段落），低于 50% 判定不符合 QA 文档规范，需在文档列表确认后继续入库。"
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
          />
        )}

        {/* 按标题切块：标题层级 */}
        {!isParentChild && method === 'title' && (
          <Form.Item name="split_level" label="标题层级">
            <Select
              options={[
                { value: 1, label: 'H1' },
                { value: 2, label: 'H2' },
                { value: 3, label: 'H3' },
              ]}
            />
          </Form.Item>
        )}

        {/* 正则切块：正则表达式 */}
        {!isParentChild && method === 'regex' && (
          <Form.Item
            name="regex_pattern"
            label="正则表达式"
            rules={[{ required: true, message: '请输入正则表达式' }]}
          >
            <Input placeholder="例如：第[一二三四五六七八九十百千万\d]+条" />
          </Form.Item>
        )}

        {/* 通用（非父子分块）：分块大小与重叠 */}
        {!isParentChild && (
          <>
            <Form.Item
              name="chunk_size"
              label="分块大小（字符）"
              rules={[{ required: true, message: '请输入分块大小' }]}
            >
              <InputNumber min={50} max={20000} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="overlap" label="重叠字符" rules={[{ required: true, message: '请输入重叠字符' }]}>
              <InputNumber min={0} style={{ width: '100%' }} />
            </Form.Item>
          </>
        )}
      </Form>
    </Modal>
  );
};

export default ParseConfigModal;
