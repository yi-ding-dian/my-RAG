import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Button,
  Modal,
  Radio,
  Select,
  Space,
  Steps,
  Switch,
  Tag,
  Typography,
} from 'antd';
import {
  RobotOutlined,
  ThunderboltOutlined,
  AlignLeftOutlined,
  ScissorOutlined,
  ApartmentOutlined,
  CodeOutlined,
  MessageOutlined,
  NodeIndexOutlined,
  FolderOpenOutlined,
  GlobalOutlined,
  BulbOutlined,
  DatabaseOutlined,
} from '@ant-design/icons';
import type {
  AnalyzeResult,
  DocumentItem,
  IngestConfig,
  ParseMethod,
  ParserEngine,
  ParserLlmModelItem,
  ThinkingMode,
} from '../api/client';
import { analyzeDocument, getLlmModelList, ingestDocument, testLlmModelByName } from '../api/client';
import DocumentPortrait, { ENGINE_LABELS } from './DocumentPortrait';

const { Text } = Typography;

/** 切块方式选项（向导 Step2）：基础说明 + 画像推荐标注（推荐/可选 + 理由） */
interface MethodOption {
  value: ParseMethod;
  label: string;
  desc: string;
  badge?: '推荐' | '可选';
  reason?: string;
}

const BASE_METHODS: Array<Omit<MethodOption, 'reason'>> = [
  { value: 'naive', label: '通用切块', desc: '递归字符切块，通用稳妥，无参数负担' },
  { value: 'title', label: '按标题切块', desc: '按标题切块，标题保留块首' },
  { value: 'parent_child', label: '父子分块', desc: '父块聚合章节、子块精细切分，检索返回父块完整上下文' },
  { value: 'regex', label: '正则切块', desc: '按自定义正则匹配位置切块' },
  { value: 'qa', label: 'QA 问答', desc: '问答对整块入库（需问/答标记，占比 ≥50%）' },
  { value: 'agentic', label: 'Agentic 智能分块', desc: 'LLM 读全文语义切分逻辑段落并打标签' },
];

/** 切块方式图标（Step2 大卡片 + Step4 摘要） */
const METHOD_ICONS: Record<string, React.ReactNode> = {
  naive: <ScissorOutlined />,
  title: <AlignLeftOutlined />,
  parent_child: <ApartmentOutlined />,
  regex: <CodeOutlined />,
  qa: <MessageOutlined />,
  agentic: <RobotOutlined />,
};

interface SmartParseWizardProps {
  open: boolean;
  /** 当前待分析文档（null 时不展示内容） */
  doc: DocumentItem | null;
  kbId?: string;
  onCancel: () => void;
  /** 确定解析成功后的回调（父组件刷新列表） */
  onSuccess: () => void;
}

/** 智能解析引导向导：文档画像 → 切块方式 → 增强配置 → 确认解析
 *
 * - 独立模块，不碰现有手动解析（ParseConfigModal）：打开即调画像接口
 *   GET /analyze（未解析文档也能分析），按画像推荐切块方式/增强开关；
 * - 最后按生成配置调现有 ingest 接口（与手动解析同一 API，零改动）；
 * - 阈值实时读系统配置（超管可改），向导内展示"当前阈值 X，本文档 X"。
 */
const SmartParseWizard: React.FC<SmartParseWizardProps> = ({ open, doc, kbId, onCancel, onSuccess }) => {
  const { message } = AntApp.useApp();
  const [step, setStep] = useState(0);
  const [loading, setLoading] = useState(false);
  const [analyze, setAnalyze] = useState<AnalyzeResult | null>(null);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  // 打开时初始化过的标记（画像加载完成后回填默认值，只回填一次）
  const initRef = React.useRef(false);

  // Step2 切块方式（默认 = 画像主推荐）
  const [method, setMethod] = useState<ParseMethod>('naive');
  // 正则切块 pattern（选 regex 时必填，提交给 ingest）
  const [regexPattern, setRegexPattern] = useState('');
  // Step3 增强配置
  const [contextualRetrieval, setContextualRetrieval] = useState(false);
  const [headingInContent, setHeadingInContent] = useState(false);
  const [knowledgeGraph, setKnowledgeGraph] = useState(false);
  const [thinkingMode, setThinkingMode] = useState<ThinkingMode>('disabled');
  const [parseLlmModel, setParseLlmModel] = useState<string | undefined>(undefined);
  // 解析 LLM 模型列表（上下文摘要/图谱抽取/Agentic 专用，登录即可读）
  const [llmModels, setLlmModels] = useState<ParserLlmModelItem[]>([]);
  const [activeLlmIdx, setActiveLlmIdx] = useState(0);
  const [testingLlm, setTestingLlm] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const isAgentic = method === 'agentic';
  const docFileType = (doc?.file_type ?? '').toLowerCase().replace(/^\./, '');
  const isPdfLike = docFileType === 'pdf' || docFileType === 'docx';
  const rec = analyze?.recommendations;

  // 拉取画像（打开/重试共用）：成功后回填推荐默认值（主推荐切块方式 + 增强开关）
  const loadAnalyze = React.useCallback(() => {
    if (!kbId || !doc) return;
    setLoading(true);
    setAnalyzeError(null);
    analyzeDocument(kbId, doc.id)
      .then(res => {
        setAnalyze(res.data);
        if (!initRef.current) {
          initRef.current = true;
          const r = res.data.recommendations;
          if (r?.chunk_method?.method) setMethod(r.chunk_method.method);
          setContextualRetrieval(r?.contextual_retrieval?.recommended ?? false);
          setHeadingInContent(r?.enable_heading_in_content ?? false);
        }
      })
      .catch(e => {
        setAnalyzeError(e.response?.data?.detail || '画像分析失败，请重试');
      })
      .finally(() => setLoading(false));
  }, [kbId, doc]);

  // 打开时重置状态并拉取画像 + LLM 模型列表
  useEffect(() => {
    if (!open || !doc || !kbId) return;
    initRef.current = false;
    setStep(0);
    setAnalyze(null);
    setAnalyzeError(null);
    setMethod('naive');
    setContextualRetrieval(false);
    setHeadingInContent(false);
    setKnowledgeGraph(false);
    setThinkingMode('disabled');
    setParseLlmModel(undefined);
    loadAnalyze();
    // LLM 模型列表（切换前测试连接，通过才生效）
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
  }, [open, doc, kbId, loadAnalyze]);

  // 解析 LLM 模型默认值兜底：字段为空 → 回填当前激活模型；值已不在列表 → 回退激活模型
  useEffect(() => {
    if (!open) return;
    const activeName = llmModels[activeLlmIdx]?.name;
    if (!activeName) return;
    if (!parseLlmModel || !llmModels.some(m => m.name === parseLlmModel)) {
      setParseLlmModel(activeName);
    }
  }, [open, llmModels, activeLlmIdx, parseLlmModel]);

  // 互斥联动：选 Agentic 自动关闭上下文检索增强（与手动解析弹窗同规则）
  useEffect(() => {
    if (isAgentic && contextualRetrieval) {
      setContextualRetrieval(false);
    }
  }, [isAgentic, contextualRetrieval]);

  // 切换解析 LLM 模型：先测试连接 → 通过才更新本地值；失败提示并保持原模型
  const handleParseLlmChange = async (name: string) => {
    if (testingLlm || !name || name === parseLlmModel) return;
    setTestingLlm(true);
    try {
      const res = await testLlmModelByName(name);
      if (res.data.ok) {
        message.success(`「${name}」连接成功（${res.data.latency_ms}ms），已切换解析模型`);
        setParseLlmModel(name);
      } else {
        message.error(`「${name}」连接失败，保持原模型：${res.data.reason}`);
      }
    } catch (e: any) {
      message.error(`连接测试失败，保持原模型：${e.response?.data?.detail || '网络请求失败'}`);
    } finally {
      setTestingLlm(false);
    }
  };

  // Step2 选项：基础说明 + 画像推荐（主推荐标"推荐"+理由，agentic 标"可选"）
  const methodOptions = useMemo<MethodOption[]>(() => {
    if (!rec) return BASE_METHODS.map(m => ({ ...m }));
    const byMethod = new Map<string, { recommended: boolean; reason: string }>();
    byMethod.set(rec.chunk_method.method, rec.chunk_method);
    rec.alternatives.forEach(a => byMethod.set(a.method, a));
    return BASE_METHODS.map(m => {
      const r = byMethod.get(m.value);
      if (r && r.recommended) {
        return { ...m, badge: '推荐' as const, reason: r.reason };
      }
      if (m.value === 'agentic') {
        return { ...m, badge: '可选' as const, reason: r?.reason };
      }
      if (r) return { ...m, reason: r.reason }; // 备选（如 QA 文档的父子分块）
      return { ...m };
    });
  }, [rec]);

  // Step4 确认解析：按向导选择生成配置，调现有 ingest 接口（与手动解析同一 API）
  const handleOk = async () => {
    if (!kbId || !doc) return;
    const config: IngestConfig = { method };
    // 切块参数默认值（与手动解析弹窗缺省一致）
    if (method === 'parent_child') {
      config.chunk_size = 512;
      config.overlap = 50;
      config.parent_chunk_size = 1024;
      config.parent_chunk_overlap = 100;
      config.parent_split_level = 2;
      config.retrieval_mode = 'parent';
    } else if (method === 'title') {
      config.split_level = 2;
      config.chunk_size = 800;
      config.overlap = 100;
    } else if (method === 'naive' || method === 'regex') {
      config.chunk_size = 800;
      config.overlap = 100;
      if (method === 'regex') {
        if (!regexPattern.trim()) {
          message.error('请填写正则表达式');
          setStep(1);
          return;
        }
        config.regex_pattern = regexPattern.trim();
      }
    }
    // 引擎建议（仅 pdf/docx 提交；txt/md 直读不传，后端默认）
    const suggested = analyze?.engine_suggestion?.suggested;
    if (isPdfLike && suggested && ['mineru', 'deepdoc', 'plain'].includes(suggested)) {
      config.parser_engine = suggested as ParserEngine;
    }
    config.enable_heading_in_content = headingInContent;
    config.contextual_retrieval = contextualRetrieval;
    config.knowledge_graph = knowledgeGraph;
    config.thinking_mode = thinkingMode;
    if ((contextualRetrieval || knowledgeGraph || isAgentic) && parseLlmModel) {
      config.parse_llm_model = parseLlmModel;
    }
    setSubmitting(true);
    try {
      const res = await ingestDocument(kbId, doc.id, config);
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

  // 正则切块需填 pattern 才能进入下一步
  const canNext = step !== 1 || method !== 'regex' || regexPattern.trim().length > 0;

  const stepContent = (
    <div className="spw-fade" key={step}>
      {step === 0 && (
        <DocumentPortrait
          analyze={analyze}
          loading={loading}
          error={analyzeError}
          onRetry={loadAnalyze}
        />
      )}

      {step === 1 && (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          {method === 'regex' && (
            <div>
              <div style={{ marginBottom: 4 }}>
                <Text strong>正则表达式</Text>
              </div>
              <Select
                style={{ width: '100%' }}
                placeholder="例如：第[一二三四五六七八九十百千万\d]+条"
                value={regexPattern || undefined}
                onChange={setRegexPattern}
                showSearch
                allowClear
                options={[
                  { value: '第[一二三四五六七八九十百千万\\d]+条', label: '第X条' },
                  { value: '^第[一二三四五六七八九十百千万\\d]+[章节条]', label: '第X章/节/条' },
                  { value: '^\\d{1,3}(\\.\\d{1,3}){1,4}\\s', label: 'X.X 编号' },
                  { value: '^[一二三四五六七八九十]{1,3}、', label: '一、中文编号' },
                ]}
              />
              <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12 }}>
                按正则匹配位置切块，匹配片段与片段间文本都成块（文本不丢）
              </div>
            </div>
          )}
          <Radio.Group value={method} onChange={e => setMethod(e.target.value)} style={{ width: '100%' }}>
            <div className="spw-methods-grid">
              {methodOptions.map(opt => (
                <Radio
                  key={opt.value}
                  value={opt.value}
                  className="spw-method-card"
                  style={{ display: 'block', width: '100%', margin: 0 }}
                >
                  <div className="spw-method-inner">
                    <span className="spw-p-icon">{METHOD_ICONS[opt.value]}</span>
                    <div className="spw-method-body">
                      <div className="spw-method-head">
                        <span className="spw-method-name">{opt.label}</span>
                        {opt.badge && (
                          <span className={`spw-badge spw-badge--${opt.badge === '推荐' ? 'rec' : 'opt'}`}>{opt.badge}</span>
                        )}
                      </div>
                      <div className="spw-method-desc">{opt.reason ?? opt.desc}</div>
                    </div>
                  </div>
                </Radio>
              ))}
            </div>
          </Radio.Group>
        </Space>
      )}

      {step === 2 && (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            增强配置均与文档格式无关，开启后将调用 LLM 产生额外 token 费用
          </Text>

          <div className="spw-group-title">文本处理增强</div>
          <div className="spw-toggle-card">
            <span className="spw-p-icon"><GlobalOutlined /></span>
            <div className="spw-toggle-body">
              <div className="spw-toggle-head">
                <Text strong>上下文检索增强</Text>
                {rec?.contextual_retrieval.recommended && <span className="spw-chip spw-chip--rec">推荐开启</span>}
                {isAgentic && <span className="spw-chip spw-chip--warn">与 Agentic 互斥</span>}
              </div>
              <div className="spw-toggle-desc">
                切块后为每个块生成 LLM 上下文摘要
              </div>
              <div className="spw-toggle-desc">
                当前阈值 {analyze?.length.threshold_label ?? '-'}，本文档 {analyze?.length.doc_label ?? '-'}
              </div>
            </div>
            <Switch
              checked={contextualRetrieval}
              disabled={isAgentic}
              onChange={setContextualRetrieval}
              style={{ flexShrink: 0 }}
            />
          </div>
          {analyze?.length.over_threshold && (
            <Alert
              type="warning"
              showIcon
              message={`本文档 ${analyze.length.doc_label} 超过完整文档阈值，效果不佳不建议开启`}
            />
          )}
          {contextualRetrieval && (
            <Alert
              type="warning"
              showIcon
              message="开启后每次解析将对每个切块调用 LLM 生成上下文摘要，将产生额外 token 费用"
            />
          )}

          <div className="spw-toggle-card">
            <span className="spw-p-icon spw-p-icon--teal"><FolderOpenOutlined /></span>
            <div className="spw-toggle-body">
              <div className="spw-toggle-head">
                <Text strong>包含父标题</Text>
                {rec?.enable_heading_in_content && <span className="spw-chip spw-chip--rec">推荐开启</span>}
              </div>
              <div className="spw-toggle-desc">
                切块时在块前补标题路径
              </div>
            </div>
            <Switch checked={headingInContent} onChange={setHeadingInContent} style={{ flexShrink: 0 }} />
          </div>

          <div className="spw-group-title">知识加工</div>
          <div className="spw-toggle-card">
            <span className="spw-p-icon spw-p-icon--violet"><NodeIndexOutlined /></span>
            <div className="spw-toggle-body">
              <div className="spw-toggle-head">
                <Text strong>知识图谱</Text>
                <span className="spw-chip spw-chip--opt">可选</span>
              </div>
              <div className="spw-toggle-desc">
                入库时用 LLM 抽取实体关系构建知识图谱
              </div>
            </div>
            <Switch checked={knowledgeGraph} onChange={setKnowledgeGraph} style={{ flexShrink: 0 }} />
          </div>
          {knowledgeGraph && (
            <Alert
              type="warning"
              showIcon
              message="开启后每次解析将对每个切块调用 LLM 抽取实体与关系，将产生额外 token 费用"
            />
          )}

          {(contextualRetrieval || knowledgeGraph || isAgentic) && (
            <>
              <div className="spw-group-title">LLM 配置</div>
              <div className="spw-llm-card">
                <div className="spw-llm-row">
                  <div className="spw-llm-label"><BulbOutlined /><span>思考模式</span></div>
                  <Select
                    style={{ width: '100%' }}
                    value={thinkingMode}
                    onChange={setThinkingMode}
                    options={[
                      { value: 'disabled', label: '关闭思考（推荐，更快更省 token）' },
                      { value: 'enabled_low', label: '开启-低' },
                      { value: 'enabled_high', label: '开启-高' },
                      { value: 'enabled_max', label: '开启-最大' },
                    ]}
                  />
                </div>
                <div className="spw-llm-row">
                  <div className="spw-llm-label"><DatabaseOutlined /><span>解析 LLM 模型</span></div>
                  <Select
                    loading={testingLlm}
                    style={{ width: '100%' }}
                    placeholder="默认使用当前激活模型"
                    value={parseLlmModel}
                    onChange={handleParseLlmChange}
                    options={llmModels.map((m, i) => ({
                      value: m.name,
                      label: `${m.name}${m.model && m.model !== m.name ? `（${m.model}）` : ''}${i === activeLlmIdx ? ' — 当前使用' : ''}`,
                    }))}
                  />
                  <div className="spw-llm-hint">
                    仅影响上下文摘要/知识图谱抽取/Agentic 分块，对话仍用当前激活模型
                  </div>
                </div>
              </div>
            </>
          )}
        </Space>
      )}

      {step === 3 && (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <div className="spw-summary">
            <div className="spw-summary-item">
              <span className="spw-summary-label"><ThunderboltOutlined />解析引擎</span>
              <span className="spw-summary-value">
                {ENGINE_LABELS[analyze?.engine_suggestion?.suggested ?? 'auto']}
                {analyze?.engine_suggestion?.reason && (
                  <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                    {analyze.engine_suggestion.reason}
                  </Text>
                )}
              </span>
            </div>
            <div className="spw-summary-item">
              <span className="spw-summary-label">{METHOD_ICONS[method]}切块方式</span>
              <span className="spw-summary-value">
                <Text strong>{BASE_METHODS.find(m => m.value === method)?.label ?? method}</Text>
                {methodOptions.find(m => m.value === method)?.badge && (
                  <span className={`spw-badge spw-badge--${methodOptions.find(m => m.value === method)?.badge === '推荐' ? 'rec' : 'opt'}`} style={{ position: 'static', marginLeft: 8 }}>
                    {methodOptions.find(m => m.value === method)?.badge}
                  </span>
                )}
              </span>
            </div>
            <div className="spw-summary-item">
              <span className="spw-summary-label"><GlobalOutlined />上下文检索</span>
              <span className="spw-summary-value">
                {contextualRetrieval ? (
                  <Text type="success" strong>开启</Text>
                ) : (
                  <Text type="secondary">关闭</Text>
                )}
                {isAgentic && <span className="spw-chip spw-chip--warn" style={{ marginLeft: 8 }}>与 Agentic 互斥已关闭</span>}
              </span>
            </div>
            <div className="spw-summary-item">
              <span className="spw-summary-label"><FolderOpenOutlined />包含父标题</span>
              <span className="spw-summary-value">
                {headingInContent ? <Text type="success" strong>开启</Text> : <Text type="secondary">关闭</Text>}
              </span>
            </div>
            <div className="spw-summary-item">
              <span className="spw-summary-label"><NodeIndexOutlined />知识图谱</span>
              <span className="spw-summary-value">
                {knowledgeGraph ? <Text type="success" strong>开启</Text> : <Text type="secondary">关闭</Text>}
              </span>
            </div>
            <div className="spw-summary-item">
              <span className="spw-summary-label"><BulbOutlined />思考模式</span>
              <span className="spw-summary-value">
                {thinkingMode === 'disabled' ? <Text type="secondary">关闭</Text> : <Tag color="geekblue">{thinkingMode}</Tag>}
              </span>
            </div>
            {(contextualRetrieval || knowledgeGraph || isAgentic) && (
              <div className="spw-summary-item">
                <span className="spw-summary-label"><DatabaseOutlined />解析 LLM 模型</span>
                <span className="spw-summary-value"><Tag color="purple">{parseLlmModel ?? '当前激活模型'}</Tag></span>
              </div>
            )}
          </div>
          {analyze?.length.over_threshold && contextualRetrieval && (
            <Alert type="warning" showIcon message="本文档超过完整文档阈值，上下文检索效果不佳，建议关闭后重新确认" />
          )}
          <Text type="secondary" style={{ fontSize: 12 }}>
            确定后将按上述配置调用现有入库接口解析（与手动解析同链路，可随时返回上一步修改）
          </Text>
        </Space>
      )}
    </div>
  );

  return (
    <Modal
      className="parse-config-modal"
      title={
        <div className="spw-title">
          <span className="spw-title-icon"><RobotOutlined /></span>
          <div className="spw-title-texts">
            <span className="spw-title-text">智能解析引导</span>
            <span className="spw-title-sub">{doc?.original_name ?? ''}</span>
          </div>
        </div>
      }
      open={open}
      onCancel={onCancel}
      width={760}
      style={{ top: '8vh', height: 'min(88vh, calc(100vh - 120px))' }}
      footer={
        <div className="spw-footer">
          <Steps
            current={step}
            size="small"
            className="spw-steps"
            items={[{ title: '文档画像' }, { title: '切块方式' }, { title: '增强配置' }, { title: '确认解析' }]}
            style={{ flex: 1, marginRight: 24 }}
          />
          <Space>
            <Button onClick={onCancel}>取消</Button>
            {step > 0 && <Button onClick={() => setStep(step - 1)}>上一步</Button>}
            {step < 3 ? (
              <Button type="primary" disabled={!canNext} onClick={() => setStep(step + 1)}>
                下一步
              </Button>
            ) : (
              <Button type="primary" icon={<RobotOutlined />} loading={submitting} onClick={() => void handleOk()}>
                确定解析
              </Button>
            )}
          </Space>
        </div>
      }
      styles={{
        content: { display: 'flex', flexDirection: 'column', height: '100%' },
        header: { flexShrink: 0 },
        body: { padding: '16px 20px', flex: 1, minHeight: 0, overflow: 'auto' },
        footer: { flexShrink: 0 },
      }}
    >
      {stepContent}
    </Modal>
  );
};

export default SmartParseWizard;
