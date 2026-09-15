import React, { useEffect, useState } from 'react';
import AppModal from '../../../shared/components/common/AppModal';
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

/* 配置装配统一走 ingestDefaults（默认值来源在后端 /kbs/ingest-defaults）：
 *  - 智能模式：直接用后端给出的 plan.config —— 画像、引擎建议、切块参数是一起
 *    定好的，前端零加工
 *  - 统一模式：默认值查表 + 用户选的增强开关覆盖
 * 这里此前有两份硬编码参数表，且智能模式漏了"结构解析 → 层级聚合切块"那层联动，
 * 导致同一份 docx 在向导里给层级聚合、在批量智能里给父子分块。 */

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
    if (!['.txt', '.md', '.pdf', '.docx', '.doc'].includes(ext)) {
      message.warning(`不支持的文件类型：${file.name}（仅支持 .txt/.md/.pdf/.docx/.doc）`);
      return false;
    }
    if (files.some(f => f.name === file.name)) {
      message.warning(`已选择同名文件：${file.name}`);
      return false;
    }
    setFiles(prev => [...prev, file]);
    setFileList(prev => [
      ...prev,
      { uid: `${file.name}-${prev.length}`, name: file.name, status: 'done' as const },
    ]);
    return false; // 阻止 antd 自动上传
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
          // plan 为 null = 后端决策矩阵异常（画像仍照常返回）→ 退回最小配置
          config = plan?.config ?? { method: 'naive' };
          if (!plan) note = '画像未给出方案，已按默认配置入库';
        } catch {
          config = { method: 'naive' };
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
    <AppModal
      dimension="auto"
      defaultSize={{ w: 680, h: 520 }}
      rememberKey="batch-import"
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
              支持多选批量导入并解析（.txt/.md/.pdf/.docx/.doc，最多 100MB/个）
            </Text>
          </div>
          <Upload
            accept=".txt,.md,.pdf,.docx,.doc"
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
  );
};

export default BatchImportModal;
