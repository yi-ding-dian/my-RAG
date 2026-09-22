import React, { useEffect, useState } from 'react';
import AppModal, { AppModalFooter } from '../../../shared/components/common/AppModal';
import {
  Alert, 
  App as AntApp, 
  Button, 
  Input, 
  Progress, 
  Radio, 
  Select, 
  Space, 
  Switch, 
  Tag, 
  Typography, 
  Upload} from 'antd';
import { UploadOutlined } from '@ant-design/icons';
import type { UploadFile } from 'antd';
import type { ImgSummaryFormat } from '../../../shared/api/client';
import ImgSummaryFormatPicker from './ImgSummaryFormatPicker';
import { buildConfig, useIngestDefaults } from '../ingestDefaults';
import {
  asApiError,
  DocumentItem,
  IngestConfig,
  ParseMethod,
  MinerUBackend,
  MinerUEffort,
  analyzeDocument,
  ingestDocument,
  uploadDocument,
} from '../../../shared/api/client';

const { Text } = Typography;

/** 解析方式选项（统一选择模式：与手动解析弹窗同语义，批量共用一套配置） */
const METHOD_OPTIONS: { value: ParseMethod; label: string }[] = [
  { value: 'naive', label: '通用切块' },
  { value: 'title', label: '按标题切块' },
  { value: 'parent_child', label: '父子分块' },
  { value: 'regex', label: '正则切块' },
  { value: 'qa', label: 'QA 问答' },
  { value: 'agentic', label: 'Agentic 智能分块' },
];

/** MinerU 解析引擎显示名（可选档由超管在系统配置里声明，这里只负责翻译） */
const MINERU_ENGINE_LABELS: Record<string, string> = {
  'pipeline': '流水线 pipeline（最快，表格结构弱）',
  'hybrid-engine': '混合引擎 hybrid-engine（推荐）',
  'vlm-engine': '视觉大模型 vlm-engine（复杂版面最准，慢）',
};

/* 配置装配统一走 ingestDefaults（默认值来源在后端 /kbs/ingest-defaults）：
 *  - 智能模式：直接用后端给出的 plan.config —— 画像、引擎建议、切块参数是一起
 *    定好的，前端零加工
 *  - 统一模式：默认值查表 + 用户选的增强开关覆盖
 * 这里此前有两份硬编码参数表，且智能模式漏了"结构解析 → 层级聚合切块"那层联动，
 * 导致同一份 docx 在向导里给层级聚合、在批量智能里给父子分块。 */

/** 批量导入支持的文件类型（注：比单文件上传少表格类，两侧白名单本来就不完全一致） */
const BATCH_ACCEPT_EXTS = ['.txt', '.md', '.pdf', '.docx', '.doc',
                           '.ppt', '.pptx'];
/** **上传时**就会经文档转换服务（Gotenberg）转成 PDF 的格式（原文件不保留） */
const BATCH_CONVERT_EXTS = ['.ppt', '.pptx'];

interface BatchResult {
  name: string;
  ok: boolean;
  /** 失败原因 / 成功备注（如智能画像失败回退默认配置） */
  note: string;
}

interface BatchImportModalProps {
  open: boolean;
  kbId?: string;
  onCancel: () => void;
  /** 全部文档处理完成后的回调（父组件刷新列表） */
  onSuccess: () => void;
}

/** 批量导入并解析：一次选择多个文件 → 逐个上传 + 逐个解析入库
 *
 * - 两种解析方式：
 *   智能解析（推荐）：逐个文档调画像接口（GET /analyze）取推荐配置入库，
 *   画像失败（网络异常等）→ 回退默认配置（naive）继续，不中断批量；
 *   统一选择：用户手动选一次解析方式，所有文档同配置入库。
 * - 上传/入库复用现有接口（can_manage_kb）；串行逐个处理（入库接口本身
 *   是异步后台任务，并发无收益，串行便于进度/失败原因展示）。
 * - 失败不中断：上传失败（如同名 409）→ 该文件标记失败继续下一个；
 *   入库触发失败 → 已上传文档留在列表（uploaded 状态），可手动解析。
 * - 智能模式下入库任务在后台执行（状态 parsing → ingested），列表自动轮询刷新。
 */
const BatchImportModal: React.FC<BatchImportModalProps> = ({
  open,
  kbId,
  onCancel,
  onSuccess,
}) => {
  const { message } = AntApp.useApp();
  /** 默认值表（后端 /kbs/ingest-defaults）；null=未拉到，buildConfig 不发参数 */
  const ingestDefaults = useIngestDefaults();

  // 解析方式：smart=逐个画像推荐（默认）/ uniform=统一配置
  const [mode, setMode] = useState<'smart' | 'uniform'>('smart');
  // 统一模式表单
  const [method, setMethod] = useState<ParseMethod>('naive');
  /** MinerU 解析后端（'' = 尚未选择，按系统默认档）。仅对走 MinerU 的
   *  文档（pdf/docx/doc）生效；可选档由超管在系统配置里声明 */
  const [mineruBackend, setMineruBackend] = useState<string>('');
  // 下拉初始选中系统配置的默认档（不设"跟随系统默认"选项——它与"选默认档"
  // 结果完全一样，只是多一个不透明的可选项）
  const engineDefault = ingestDefaults?.mineru_backends?.default ?? 'pipeline';
  const engineValue = mineruBackend || engineDefault;
  /** 解析力度（仅 hybrid-engine 有效）：'' = 用后端默认（high） */
  const [mineruEffort, setMineruEffort] = useState<string>('');
  const [regexPattern, setRegexPattern] = useState('');
  const [contextualRetrieval, setContextualRetrieval] = useState(false);
  const [knowledgeGraph, setKnowledgeGraph] = useState(false);
  /** 图片摘要：解析后用多模态模型把图内文字读成描述回填正文（默认关） */
  const [imageSummary, setImageSummary] = useState(false);
  /** 图片摘要输出格式：''=跟随部门/全局配置（仅本次入库生效，不持久化） */
  const [imageFormat, setImageFormat] = useState<ImgSummaryFormat | ''>('');
  // 文件选择（antd Upload 受控：beforeUpload 收集，禁止自动上传）
  const [files, setFiles] = useState<File[]>([]);
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  /** 待确认转换的 ppt/pptx（统一弹窗确认后才收进列表） */
  const [pendingConvert, setPendingConvert] = useState<File | null>(null);
  // 执行状态
  const [running, setRunning] = useState(false);
  const [current, setCurrent] = useState<{ index: number; total: number; name: string } | null>(null);
  const [results, setResults] = useState<BatchResult[] | null>(null);

  // 打开时重置状态（上次结果清空，模式保持默认智能）
  useEffect(() => {
    if (open) {
      setMode('smart');
      setMethod('naive');
      setRegexPattern('');
      setContextualRetrieval(false);
      setKnowledgeGraph(false);
      setImageSummary(false);
      setFiles([]);
      setFileList([]);
      setRunning(false);
      setCurrent(null);
      setResults(null);
    }
  }, [open]);

  // Agentic 与上下文检索互斥（与后端 resolve_parser_config 双保险）
  useEffect(() => {
    if (method === 'agentic' && contextualRetrieval) {
      setContextualRetrieval(false);
    }
  }, [method, contextualRetrieval]);

  const addFile = (file: File): boolean => {
    const dot = file.name.lastIndexOf('.');
    const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : '';
    if (!BATCH_ACCEPT_EXTS.includes(ext)) {
      message.warning(
        `不支持的文件类型：${file.name}（仅支持 ${BATCH_ACCEPT_EXTS.join('/')}）`);
      return false;
    }
    if (files.some(f => f.name === file.name)) {
      message.warning(`已选择同名文件：${file.name}`);
      return false;
    }
    // ppt/pptx：**上传时**就会经转换服务转成 PDF（原文件不保留），先让用户确认
    // —— 用项目统一的 AppModal（见 docs/弹窗规范(AppModal).md）
    if (BATCH_CONVERT_EXTS.includes(ext)) {
      setPendingConvert(file);
      return false;
    }
    collect(file);
    return false; // 阻止 antd 自动上传
  };

  /** 收进待导入列表 */
  const collect = (file: File) => {
    setFiles(prev => [...prev, file]);
    setFileList(prev => [
      ...prev,
      { uid: `${file.name}-${prev.length}`, name: file.name, status: 'done' as const },
    ]);
  };

  const removeFile = (file: UploadFile) => {
    setFiles(prev => prev.filter(f => f.name !== file.name));
    setFileList(prev => prev.filter(f => f.uid !== file.uid));
  };

  const handleStart = async () => {
    if (!kbId) return;
    if (files.length === 0) {
      message.warning('请先选择要导入的文档');
      return;
    }
    if (mode === 'uniform' && method === 'regex' && !regexPattern.trim()) {
      message.error('请填写正则表达式');
      return;
    }
    setRunning(true);
    setResults(null);
    const out: BatchResult[] = [];
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      setCurrent({ index: i + 1, total: files.length, name: file.name });
      // 1) 上传（复用现有 upload API；409 同名等失败 → 该文件失败继续）
      let doc: DocumentItem;
      try {
        const up = await uploadDocument(kbId, file);
        doc = up.data;
      } catch (e: unknown) {
        out.push({
          name: file.name,
          ok: false,
          note: asApiError(e).response?.data?.detail || '上传失败',
        });
        continue;
      }
      // 2) 解析配置：智能=后端给的入库方案（失败回退最小配置，不中断批量）；
      //    统一=默认值查表 + 用户开关
      let config: IngestConfig;
      let note = '';
      if (mode === 'smart') {
        try {
          const a = await analyzeDocument(kbId, doc.id);
          const plan = a.data.parse_plan;
          // plan 为 null = 后端决策矩阵异常（画像仍照常返回）→ 退回最小配置。
          // 解析引擎两种模式通用：选了就覆盖进 config（仅走 MinerU 的文档消费）
          config = {
            ...(plan?.config ?? { method: 'naive' }),
            backend: engineValue as MinerUBackend,
          // 解析力度总是带上（值合法即可）：后端只在 backend=hybrid-engine 时
          // 写进配置，其余档忽略——省去前端再判一次条件
          effort: (mineruEffort || 'high') as MinerUEffort,
          };
          if (!plan) note = '画像未给出方案，已按默认配置入库';
        } catch {
          config = {
            method: 'naive',
            backend: engineValue as MinerUBackend,
          // 解析力度总是带上（值合法即可）：后端只在 backend=hybrid-engine 时
          // 写进配置，其余档忽略——省去前端再判一次条件
          effort: (mineruEffort || 'high') as MinerUEffort,
          };
          note = '画像分析失败，已按默认配置入库';
        }
      } else {
        config = buildConfig(ingestDefaults, method, {
          contextual_retrieval: contextualRetrieval,
          knowledge_graph: knowledgeGraph,
          image_summary: imageSummary,
          // 输出格式：'' = 跟随系统配置（不带该参数）
          ...(imageSummary && imageFormat
            ? { image_summary_format: imageFormat } : {}),
          ...(method === 'regex'
            ? { regex_pattern: regexPattern.trim() } : {}),
          // MinerU 解析引擎：'' = 跟随系统默认（不带该参数，后端用配置的默认档）；
          // 仅对走 MinerU 的文档生效，其余类型后端不消费
          backend: engineValue as MinerUBackend,
          // 解析力度总是带上（值合法即可）：后端只在 backend=hybrid-engine 时
          // 写进配置，其余档忽略——省去前端再判一次条件
          effort: (mineruEffort || 'high') as MinerUEffort,
        });
      }
      // 3) 触发入库（后台任务：parsing → ingested，列表轮询刷新）
      try {
        await ingestDocument(kbId, doc.id, config);
        out.push({ name: file.name, ok: true, note });
      } catch (e: unknown) {
        out.push({
          name: file.name,
          ok: false,
          note: `${asApiError(e).response?.data?.detail || '触发解析失败'}（文件已上传，可在列表手动解析）`,
        });
      }
    }
    setRunning(false);
    setCurrent(null);
    setResults(out);
    onSuccess();
    const ok = out.filter(r => r.ok).length;
    if (ok === out.length) {
      message.success(`批量导入并解析完成：${ok} 个文档已提交入库（后台解析中，列表自动刷新）`);
    } else {
      message.warning(`批量导入完成：成功 ${ok} 个，失败 ${out.length - ok} 个，详见结果`);
    }
  };

  const okCount = results?.filter(r => r.ok).length ?? 0;

  return (
    <>
    <AppModal
      dimension="auto"
      defaultSize={{ w: 680, h: 520 }}
      rememberKey="batch-import"
      // 选好文件（列表出现）后定住高度：运行中进度、完成后的结果列表都不再
      // 把弹窗撑大撑小，内容多则内部滚动
      autoLock={files.length > 0}
      title="批量导入并解析"
      open={open}
      onCancel={onCancel}
      width={680}
      style={{ top: '10vh' }}
      footer={
        <Space>
          {running && current && (
            <Text type="secondary">
              处理中：第 {current.index}/{current.total} 个（{current.name}）
            </Text>
          )}
          <Button onClick={onCancel} disabled={running}>
            关闭
          </Button>
          <Button
            type="primary"
            icon={<UploadOutlined />}
            loading={running}
            onClick={() => void handleStart()}
          >
            {running ? '导入解析中…' : '确认导入并解析'}
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {/* 解析方式选择 */}
        <div>
          <div style={{ marginBottom: 8 }}>
            <Text strong>解析方式</Text>
          </div>
          <Radio.Group
            value={mode}
            onChange={e => setMode(e.target.value)}
            options={[
              {
                value: 'smart',
                label: (
                  <Space size={6}>
                    <span>根据每个文档内容智能解析</span>
                    <Tag color="blue">推荐</Tag>
                  </Space>
                ),
              },
              { value: 'uniform', label: '手动统一选择解析方式' },
            ]}
          />
          <div style={{ fontSize: 12, color: 'var(--ant-color-text-tertiary)', marginTop: 4 }}>
            {mode === 'smart'
              ? '逐个文档分析画像（格式/标题结构/篇幅等），按推荐配置入库；画像失败自动回退默认配置'
              : '所选解析方式应用于全部文档，各文档均按同一配置入库'}
          </div>
        </div>

        {/* MinerU 解析引擎：**两种模式都显示**——智能解析只决定"切块方式与增强开关"
            的推荐，引擎选择是"这台机器用哪个后端解析"，与推荐无关，统一在这里选，
            选了之后本批所有走 MinerU 的文档（pdf/docx/doc）都用它；不选则用系统
            配置的默认档。可选档由超管在系统配置里按服务端资源声明 */}
        <div>
          <div style={{ marginBottom: 4 }}>
            <Text strong>MinerU 解析引擎</Text>{' '}
            <Text type="secondary" style={{ fontSize: 12 }}>
              仅 PDF / Office 文档走 MinerU 解析时使用（txt / Excel 等直读文档不涉及）；
              不选则用系统配置的默认档，可选档由超管按服务端资源声明
            </Text>
          </div>
          <Select
            style={{ width: '100%' }}
            value={engineValue}
            onChange={setMineruBackend}
            options={(ingestDefaults?.mineru_backends?.enabled ?? ['pipeline'])
              .map((b) => ({ value: b, label: MINERU_ENGINE_LABELS[b] ?? b }))}
          />
          {/* 解析力度：仅混合引擎有效（服务端标注 Edited only for hybrid backend），
              与解析配置弹窗同语义——不选则后端按 high 处理 */}
          {engineValue === 'hybrid-engine' && (
            <Select
              style={{ width: '100%', marginTop: 8 }}
              value={mineruEffort || 'high'}
              onChange={setMineruEffort}
              options={[
                { value: 'high', label: '解析力度：高（开启图片/图表分析，推荐）' },
                { value: 'medium', label: '解析力度：中（更快，但关闭图片分析）' },
              ]}
            />
          )}
        </div>

        {/* 统一模式：解析方式选择（复用手动解析弹窗语义的最小表单） */}
        {mode === 'uniform' && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <div>
              <div style={{ marginBottom: 4 }}>
                <Text strong>切块方式</Text>
              </div>
              <Select
                style={{ width: '100%' }}
                value={method}
                onChange={setMethod}
                options={METHOD_OPTIONS}
              />
              {method === 'regex' && (
                <div style={{ marginTop: 8 }}>
                  <Input
                    placeholder="正则表达式，如：^第[一二三四五六七八九十百千万\d]+[章节条]"
                    value={regexPattern}
                    onChange={e => setRegexPattern(e.target.value)}
                  />
                </div>
              )}
              {(method === 'qa') && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  QA 问答方式要求文档含问/答标记，入库时检测问答对占比 ≥50%，不达标将失败
                </Text>
              )}
              {method === 'agentic' && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  Agentic 分块超过 1 万字需逐文档确认，批量场景下超限文档会进入「待确认」状态，可在列表手动确认继续
                </Text>
              )}
            </div>
            <Space size={24}>
              <span>
                <Switch
                  size="small"
                  checked={contextualRetrieval}
                  disabled={method === 'agentic'}
                  onChange={setContextualRetrieval}
                  style={{ marginRight: 6 }}
                />
                上下文检索增强
                {method === 'agentic' && <Tag style={{ marginLeft: 4 }}>与 Agentic 互斥</Tag>}
              </span>
              <span>
                <Switch
                  size="small"
                  checked={knowledgeGraph}
                  onChange={setKnowledgeGraph}
                  style={{ marginRight: 6 }}
                />
                知识图谱
              </span>
              <span>
                <Switch
                  size="small"
                  checked={imageSummary}
                  onChange={setImageSummary}
                  style={{ marginRight: 6 }}
                />
                图片摘要
              </span>
            </Space>
            {imageSummary && (
              <ImgSummaryFormatPicker
                kbId={kbId}
                value={imageFormat}
                onChange={setImageFormat}
                size="small"
                style={{ marginTop: 10 }}
              />
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              增强开关开启后将调用 LLM 产生额外 token 费用
            </Text>
          </Space>
        )}

        {/* 文件选择 */}
        <div>
          <div style={{ marginBottom: 8 }}>
            <Text strong>选择文档</Text>
            <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
              支持多选批量导入并解析（{BATCH_ACCEPT_EXTS.join('/')}，最多 100MB/个）
            </Text>
          </div>
          <Upload
            accept={BATCH_ACCEPT_EXTS.join(',')}
            multiple
            beforeUpload={file => addFile(file)}
            fileList={fileList}
            onRemove={removeFile}
            disabled={running}
          >
            <Button icon={<UploadOutlined />} disabled={running}>
              选择文件
            </Button>
          </Upload>
        </div>

        {/* 进度与结果 */}
        {running && current && (
          <Progress
            percent={Math.round((current.index / current.total) * 100)}
            format={() => `${current.index}/${current.total}`}
            size="small"
          />
        )}
        {results && (
          <Alert
            type={okCount === results.length ? 'success' : 'warning'}
            showIcon
            message={`处理完成：成功 ${okCount} 个，失败 ${results.length - okCount} 个（入库在后台执行，列表将自动刷新）`}
            description={
              results.some(r => !r.ok) ? (
                <ul style={{ margin: '4px 0 0', paddingLeft: 18, fontSize: 12 }}>
                  {results
                    .filter(r => !r.ok)
                    .map(r => (
                      <li key={r.name}>
                        {r.name}：{r.note}
                      </li>
                    ))}
                </ul>
              ) : undefined
            }
          />
        )}
      </Space>
    </AppModal>

    {/* ppt/pptx 转换确认：用项目统一弹窗（见 docs/弹窗规范(AppModal).md） */}
    <AppModal
      title="该文件将先转换为 PDF"
      open={!!pendingConvert}
      dimension="auto"
      defaultSize={{ w: 520, h: 360 }}
      // 内容只有几行，放宽最小高度下限（组件注释里明确支持"内容很少的小弹窗"）
      minSize={{ w: 400, h: 180 }}
      onCancel={() => setPendingConvert(null)}
      footer={
        <AppModalFooter
          okText="转换并导入"
          onOk={() => {
            const f = pendingConvert;
            setPendingConvert(null);
            if (f) collect(f);
          }}
          onCancel={() => setPendingConvert(null)}
        />
      }
    >
      <div style={{ lineHeight: 1.8 }}>
        <b>{pendingConvert?.name}</b>
        <br />
        导入时会先通过文档转换服务（Gotenberg）转成 PDF，
        之后按 PDF 保存、解析与检索。
        <br />
        <Text type="secondary">原文件不保留。</Text>
      </div>
    </AppModal>
    </>
  );
};

export default BatchImportModal;
