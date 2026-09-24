/**
 * RAGAS 评测详情页（/analytics/ragas）：统计分析概览页点击「RAGAS 评测」摘要卡下钻。
 * - 完整评估任务表（发起/取消/查看报告/导入导出测试集全部能力，从原 Analytics 单页搬入）
 * - 运行中任务 8s 轮询状态（原逻辑搬入）；分页固定左下 + 表格内部滚动（统一布局规范）
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Breadcrumb } from 'antd';
import { useNavigate } from 'react-router-dom';
import AppModal from '../../shared/components/common/AppModal';
import {
  App as AntApp, Card, Col, Row, Statistic, Table, Tag, Typography, Alert,
  Space, Tooltip, Button, Empty, Select, Checkbox, Form, Input, InputNumber,
  Segmented, List, Popconfirm, Radio, Skeleton,
} from 'antd';
import {
  DeleteOutlined, EyeOutlined, FileExcelOutlined, FileTextOutlined,
  ImportOutlined, PlusOutlined, ReloadOutlined, RobotOutlined, SaveOutlined,
  StopOutlined, SyncOutlined, PlayCircleOutlined, UploadOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import * as XLSX from 'xlsx';
import {
  cancelRagasEvaluation, getRagasStatus, getRagasReport,
  listKbs, startRagasEvaluation, previewRagasSamples, ragasPrecheck,
  listRagasDatasets, createRagasDataset, deleteRagasDataset, getLlmModelList,
  generateRagasQuestions, listDocuments,
  KnowledgeBase, RagasStatus, RagasTask, RagasReport, RagasSampleInput,
  RagasDataset, ParserLlmModelItem, RagasGeneratedSample, DocumentItem,
} from '../../shared/api/client';
import { useAuth } from '../../shared/auth/AuthContext';
import AppEmpty from '../../shared/components/common/AppEmpty';
import PageLayout from '../../shared/components/layout/PageLayout';
import TableSectionLayout from '../../shared/components/layout/TableSectionLayout';
import ResizableTitle from '../../shared/components/common/ResizableTitle';
import { useResizableColumns } from '../../shared/hooks/useResizableColumns';
import {
  metricLabel, metricLabelMap, statusOf, scoreColor,
  ragasMetricOptions, DEFAULT_METRICS, EvalSampleRow,
} from './analytics-shared';

const { Text } = Typography;

/** 任务的平均分（各指标算术平均）；没有分数快照时返回 null */
const avgScore = (scores?: Record<string, number>): number | null => {
  if (!scores) return null;
  const vals = Object.values(scores).filter(v => typeof v === 'number');
  if (vals.length === 0) return null;
  return vals.reduce((a, b) => a + b, 0) / vals.length;
};

const AnalyticsRagasDetailPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { user } = useAuth();
  const navigate = useNavigate();
  // 发起评估仅 super_admin / dept_admin（与后端权限一致）
  const isAdmin = user?.role === 'super_admin' || user?.role === 'dept_admin';

  // 列宽拖拽（评测任务表）：拖拽后的列宽存 colWidths，scroll.x 动态对齐列宽和
  const { colWidths, handleResize, tableWidth } = useResizableColumns<RagasTask>();
  const [ragas, setRagas] = useState<RagasStatus | null>(null);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(false);

  // 任务列表分页（统一 LeftPagination：分页左下固定；表格内部滚动）
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const tasks = ragas?.tasks || [];
  const pageTasks = useMemo(
    () => tasks.slice((page - 1) * pageSize, page * pageSize),
    [tasks, page, pageSize],
  );
  // 刷新/轮询后总条数变化导致当前页越界 → 自动回到最后一页（antd 非受控分页同样行为）
  useEffect(() => {
    const max = Math.max(1, Math.ceil(tasks.length / pageSize));
    if (page > max) setPage(max);
  }, [tasks.length, page, pageSize]);

  // 报告 Modal
  const [reportOpen, setReportOpen] = useState(false);
  const [reportTask, setReportTask] = useState<RagasTask | null>(null);
  const [report, setReport] = useState<RagasReport | null>(null);
  const [reportLoading, setReportLoading] = useState(false);

  // 发起评估 Modal（手动测试集）
  const [evalOpen, setEvalOpen] = useState(false);
  /** 评估用的知识库（**可多选**）：多库问答的问题评估时也走多库链路，
   *  否则评的不是用户实际用的那条 */
  const [evalKbIds, setEvalKbIds] = useState<string[]>([]);
  const [evalMetrics, setEvalMetrics] = useState<string[]>(DEFAULT_METRICS);
  const [evalSubmitting, setEvalSubmitting] = useState(false);
  const [evalImporting, setEvalImporting] = useState(false);
  // 本地评估集（可反复重跑的固定题集）：选中后用它重跑，分数才可与历史对比
  const [evalDatasets, setEvalDatasets] = useState<RagasDataset[]>([]);
  const [evalDatasetId, setEvalDatasetId] = useState<string | undefined>(undefined);
  const [datasetMgrOpen, setDatasetMgrOpen] = useState(false);
  // 「另存为评估集」小弹窗：把当前测试集沉淀成可复用的题集
  const [saveDsOpen, setSaveDsOpen] = useState(false);
  const [saveDsName, setSaveDsName] = useState('');
  // 「新建评估集」（评估集管理弹窗入口）：不经过发起评估，直接建题集
  const [newDsOpen, setNewDsOpen] = useState(false);
  const [newDsName, setNewDsName] = useState('');
  const [newDsKbIds, setNewDsKbIds] = useState<string[]>([]);
  const [newDsText, setNewDsText] = useState('');
  const [newDsSubmitting, setNewDsSubmitting] = useState(false);
  // AI 出题（合成测试集）：生成的是**草稿**，审核通过才存成评估集
  const [genOpen, setGenOpen] = useState(false);
  const [genKbId, setGenKbId] = useState<string | undefined>(undefined);
  const [genCount, setGenCount] = useState(10);
  const [genLoading, setGenLoading] = useState(false);
  const [genSamples, setGenSamples] = useState<RagasGeneratedSample[]>([]);
  const [genChecked, setGenChecked] = useState<boolean[]>([]);
  const [genName, setGenName] = useState('');
  const [genSaving, setGenSaving] = useState(false);
  // 取材范围：选中的文档（空 = 全库）；只列已入库的（软删的块后端会排除）
  const [genDocs, setGenDocs] = useState<DocumentItem[]>([]);
  const [genDocIds, setGenDocIds] = useState<string[]>([]);
  // 评估用的**评委模型**（数据源 = 当前档案的 LLM 模型列表）：
  // 换评委分数不可比，所以默认跟随"当前使用"的那个，不选即维持现状
  const [llmModelList, setLlmModelList] = useState<ParserLlmModelItem[]>([]);
  /** 当前激活模型在列表里的下标（下拉里标"当前使用"） */
  const [llmActiveIdx, setLlmActiveIdx] = useState(0);
  const [evalModel, setEvalModel] = useState<string | undefined>(undefined);
  /** answer 来源：dataset=题集里的参考答案（快，测检索）；generate=系统实时生成（慢，测端到端） */
  const [answerSource, setAnswerSource] = useState<'dataset' | 'generate'>('dataset');
  const [evalForm] = Form.useForm<{ samples: EvalSampleRow[] }>();
  // 测试集有效行数（问题 + 正确答案均非空）：为 0 时禁止发起评估
  const evalSamples = Form.useWatch('samples', evalForm);
  const evalValidCount = useMemo(
    () => (evalSamples || []).filter((r: EvalSampleRow) =>
      r?.question?.trim() && r?.ground_truth?.trim()).length,
    [evalSamples],
  );
  // 测试集文本导入/导出 Modal
  const [textModalOpen, setTextModalOpen] = useState(false);
  const [evalText, setEvalText] = useState('');
  // 导入/导出格式（txt 文本 / Excel）；txt 走文本区与文件，Excel 走文件
  const [ioFormat, setIoFormat] = useState<'txt' | 'excel'>('txt');
  // 文件导入解析结果提示（有效/跳过/无效行明细），展示在弹窗内
  const [fileParseResult, setFileParseResult] = useState<{ ok: number; skipped: number; invalid: string[] } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // 从聊天历史导入选择 Modal（勾选预览样本后追加）
  const [chatPickOpen, setChatPickOpen] = useState(false);
  const [chatPickList, setChatPickList] = useState<RagasSampleInput[]>([]);
  const [chatPickSelected, setChatPickSelected] = useState<string[]>([]);
  const pollTimerRef = useRef<number | null>(null);

  // 发起评估成功"魔法注入"动画：测试集飞向任务列表（注入 0.6s + 光晕）→ 新任务行高亮
  const [evalAnim, setEvalAnim] = useState(false);
  const [newTaskId, setNewTaskId] = useState<string | null>(null);
  const evalAnimTimerRef = useRef<number | null>(null);

  const loadRagas = useCallback(async () => {
    setLoading(true);
    try {
      const [ragasRes, kbsRes] = await Promise.all([
        getRagasStatus(),
        listKbs(),
      ]);
      setRagas(ragasRes.data);
      setKbs(kbsRes.data);
      // 评估集列表失败不影响任务列表（静默降级为空，发起评估里等同"没有评估集"）
      listRagasDatasets()
        .then(r => setEvalDatasets(r.data.datasets || []))
        .catch(() => setEvalDatasets([]));
      // 评委模型候选（当前档案的 LLM 模型列表）；失败则下拉为空 = 只能跟随激活模型
      getLlmModelList()
        .then(r => {
          setLlmModelList(r.data.models || []);
          setLlmActiveIdx(r.data.active ?? 0);
        })
        .catch(() => setLlmModelList([]));
    } catch {
      setRagas({ available: false, tasks: [], message: '自身统计接口异常' });
      setKbs([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRagas();
  }, [loadRagas]);

  // 任务进度轮询：发起评估后每 8s 刷新任务列表，直到无运行中任务
  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    pollTimerRef.current = window.setInterval(async () => {
      try {
        const res = await getRagasStatus();
        setRagas(res.data);
        const active = (res.data.tasks || []).some(t =>
          t.status === 'queued' || t.status === 'pending' || t.status === 'running');
        if (!active) stopPolling();
      } catch {
        // 轮询失败静默，下个周期重试
      }
    }, 8000);
  }, [stopPolling]);

  useEffect(() => {
    stopPolling();
    return () => {
      if (evalAnimTimerRef.current !== null) {
        window.clearTimeout(evalAnimTimerRef.current);
        evalAnimTimerRef.current = null;
      }
    };
  }, [stopPolling]);

  // 新任务行高亮：列表刷新后滚动到新行（AntD rowKey 渲染 tr[data-row-key]），
  // 高亮 class 挂 2.6s（CSS 呼吸动画 1.5s 渐退）后移除
  useEffect(() => {
    if (!newTaskId) return;
    const scrollTimer = window.setTimeout(() => {
      // 页面级查找（页面内表格唯一；scrollIntoView 就近滚动到表格滚动容器）
      const row = document.querySelector<HTMLElement>(
        `tr[data-row-key="${newTaskId}"]`);
      row?.scrollIntoView({ block: 'nearest' });
    }, 120);
    const clearTimer = window.setTimeout(() => setNewTaskId(null), 2600);
    return () => {
      window.clearTimeout(scrollTimer);
      window.clearTimeout(clearTimer);
    };
  }, [newTaskId, ragas]);

  // 打开发起评估 Modal：重置测试集表单为一行空行
  const openEvalModal = () => {
    evalForm.resetFields();
    evalForm.setFieldsValue({ samples: [{ question: '', ground_truth: '' }] });
    setEvalOpen(true);
  };

  // 从聊天历史导入：调 preview 采样 → 弹选择弹窗（勾选想要的样本后追加）
  const handleImportFromChat = async () => {
    if (evalKbIds.length === 0) {
      message.error('请先选择要评估的知识库');
      return;
    }
    setEvalImporting(true);
    try {
      const res = await previewRagasSamples({
        // 采样接口按单个库读会话历史：多库时取第一个
        kb_id: evalKbIds[0],
        sample_source: 'chat',
        sample_count: 20,
        preview: true,
      });
      const list = (res.data.samples || [])
        .map(s => ({
          question: s.question ?? '',
          ground_truth: s.ground_truth ?? s.answer ?? '',
        }))
        .filter(s => s.question.trim() && s.ground_truth.trim());
      if (list.length === 0) {
        message.warning('该知识库近 30 天无聊天问答记录，可手动填写测试集');
        return;
      }
      setChatPickList(list);
      setChatPickSelected(list.map(s => s.question)); // 默认全选
      setChatPickOpen(true);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '导入失败');
    } finally {
      setEvalImporting(false);
    }
  };

  // 选择弹窗确定：勾选的样本追加到当前测试集（相同问题不重复添加）
  const handleChatPickConfirm = () => {
    const selected = new Set(chatPickSelected);
    const current = (evalForm.getFieldValue('samples') as EvalSampleRow[] | undefined) || [];
    const existing = new Set(current.map(r => r?.question?.trim()).filter(Boolean));
    const merged = [...current];
    let added = 0;
    let skipped = 0;
    for (const s of chatPickList) {
      if (!selected.has(s.question)) continue;
      const q = s.question.trim();
      if (existing.has(q)) { skipped++; continue; } // 同问题已存在 → 跳过
      existing.add(q);
      merged.push({ question: q, ground_truth: (s.ground_truth ?? '').trim() });
      added++;
    }
    if (added === 0) {
      message.warning('未勾选任何样本（或全部与现有测试集重复），未追加');
      return;
    }
    evalForm.setFieldsValue({ samples: merged });
    setChatPickOpen(false);
    message.success(
      `已追加 ${added} 条聊天样本${skipped ? `（跳过 ${skipped} 条重复问题）` : ''}，可编辑后发起评估`);
  };

  // 当前测试集有效行（问题 + 正确答案均非空，已 trim）
  const evalValidRows = (): EvalSampleRow[] => {
    const rows = (evalForm.getFieldValue('samples') as EvalSampleRow[] | undefined) || [];
    return rows
      .filter(r => r?.question?.trim() && r?.ground_truth?.trim())
      .map(r => ({ question: r.question!.trim(), ground_truth: r.ground_truth!.trim() }));
  };

  // 当前测试集 → 文本（每行一条：问题【Tab】正确答案；首行 # 注释说明，
  // 导入时自动忽略——txt 导出/导入闭环）
  const exportSamplesText = (): string => {
    const rows = evalValidRows();
    if (rows.length === 0) return '';
    return `# 每行一条：问题【Tab 键】正确答案（# 开头为注释行，导入时忽略）\n`
      + rows.map(r => `${r.question}\t${r.ground_truth}`).join('\n');
  };

  // 浏览器下载文件（用户自行选择保存位置/文件夹）
  const downloadBlob = (blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const evalFileStamp = () => dayjs().format('YYYYMMDD-HHmmss');

  // 打开测试集导入/导出 Modal：默认带出当前测试集文本
  const openTextModal = () => {
    setEvalText(exportSamplesText());
    setFileParseResult(null);
    setTextModalOpen(true);
  };

  // 导出（按所选格式）：txt 回填文本区 + 下载 .txt；Excel 生成 .xlsx 下载
  const handleExportByFormat = () => {
    const rows = evalValidRows();
    if (rows.length === 0) {
      message.warning('当前测试集为空，无可导出内容');
      return;
    }
    if (ioFormat === 'excel') {
      // 生成 Excel（列：问题/正确答案，首行表头——导入时跳过）
      const ws = XLSX.utils.json_to_sheet(
        rows.map(r => ({ 问题: r.question, 正确答案: r.ground_truth })));
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, '测试集');
      XLSX.writeFile(wb, `测试集_${evalFileStamp()}.xlsx`);
      message.success(`已导出 Excel（${rows.length} 条），请选择保存位置`);
      return;
    }
    const text = exportSamplesText();
    setEvalText(text);
    downloadBlob(new Blob([text], { type: 'text/plain;charset=utf-8' }),
      `测试集_${evalFileStamp()}.txt`);
    message.success(`已导出 txt（${rows.length} 条），请选择保存位置`);
  };

  // 解析 txt 文本 → 样本（每行：问题【Tab】答案；# 注释/空行忽略；
  // 无效行计数并记录行号+片段，最多 100 条）
  const parseTxtSamples = (text: string):
    { rows: EvalSampleRow[]; skipped: number; invalid: string[] } => {
    const rows: EvalSampleRow[] = [];
    let skipped = 0;
    const invalid: string[] = [];
    const lines = text.split('\n');
    for (let i = 0; i < lines.length && rows.length < 100; i++) {
      const line = lines[i].replace(/\r$/, '');
      const t = line.trim();
      if (!t || t.startsWith('#')) continue; // 空行 / # 注释行忽略
      const idx = line.indexOf('\t');
      if (idx <= 0) { skipped++; invalid.push(`第 ${i + 1} 行：${t.slice(0, 30)}`); continue; }
      const question = line.slice(0, idx).trim();
      const groundTruth = line.slice(idx + 1).trim();
      if (!question || !groundTruth) { skipped++; invalid.push(`第 ${i + 1} 行：${t.slice(0, 30)}`); continue; }
      rows.push({ question, ground_truth: groundTruth });
    }
    return { rows, skipped, invalid };
  };

  // 解析 Excel/csv 文件 → 样本（第一列问题、第二列正确答案；首行表头跳过，
  // 最多 100 条；xlsx 库统一处理含引号转义等 csv 细节）
  const parseExcelSamples = (buf: ArrayBuffer):
    { rows: EvalSampleRow[]; skipped: number; invalid: string[] } => {
    const wb = XLSX.read(buf, { type: 'array' });
    const ws = wb.Sheets[wb.SheetNames[0]];
    if (!ws) return { rows: [], skipped: 0, invalid: ['文件无工作表'] };
    const grid = XLSX.utils.sheet_to_json<unknown[]>(ws, { header: 1, defval: '' });
    const rows: EvalSampleRow[] = [];
    let skipped = 0;
    const invalid: string[] = [];
    for (let i = 1; i < grid.length && rows.length < 100; i++) { // 跳过首行表头
      const cell = grid[i] || [];
      const question = String(cell[0] ?? '').trim();
      const groundTruth = String(cell[1] ?? '').trim();
      if (!question && !groundTruth) continue; // 空行忽略
      if (!question || !groundTruth) {
        skipped++;
        invalid.push(`第 ${i + 1} 行：${(question || groundTruth).slice(0, 30)}`);
        continue;
      }
      rows.push({ question, ground_truth: groundTruth });
    }
    return { rows, skipped, invalid };
  };

  // 导入结果回填测试集（覆盖）+ 提示（有效/跳过；无效行明细展示在弹窗内）
  const applyImportResult = (
    rows: EvalSampleRow[], skipped: number, invalid: string[], closeModal: boolean) => {
    if (rows.length === 0) {
      message.error(invalid.length
        ? `未能解析出有效测试样本（${invalid[0]}）`
        : '未能解析出有效测试样本，请检查文件内容格式');
      return;
    }
    evalForm.setFieldsValue({ samples: rows });
    setFileParseResult({ ok: rows.length, skipped, invalid });
    if (closeModal) setTextModalOpen(false);
    message.success(
      `已导入 ${rows.length} 条测试样本（覆盖当前测试集）${skipped ? `，跳过 ${skipped} 条无效内容` : ''}`);
  };

  // 导入：解析文本框内容（txt：每行一条：问题【Tab】正确答案）覆盖回填
  const handleImportText = () => {
    const { rows, skipped, invalid } = parseTxtSamples(evalText);
    applyImportResult(rows, skipped, invalid, true);
  };

  // 导入：文件选择器按所选格式解析（txt 读文本；Excel 读 .xlsx/.csv）覆盖回填
  const handleFileImport = (file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = ioFormat === 'excel'
        ? parseExcelSamples(reader.result as ArrayBuffer)
        : parseTxtSamples(String(reader.result ?? ''));
      applyImportResult(result.rows, result.skipped, result.invalid, false);
    };
    reader.onerror = () => message.error('文件读取失败');
    if (ioFormat === 'excel') reader.readAsArrayBuffer(file);
    else reader.readAsText(file, 'utf-8');
  };

  // 文件选择器触发（按所选格式限定文件类型）；选择后清空 value 允许重选同文件
  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    handleFileImport(file);
  };

  // 发起评估提交（手动测试集：问题 + 正确答案，answer 由后端自动=ground_truth）
  const handleStartEvaluation = async () => {
    // 评估集模式：知识库以评估集绑定的为准，既不必选库、也不校验下方测试集
    if (!evalDatasetId && evalKbIds.length === 0) {
      message.error('请选择要评估的知识库');
      return;
    }
    if (evalMetrics.length === 0) {
      message.error('请至少选择一个评估指标');
      return;
    }
    let rows: RagasSampleInput[] = [];
    if (!evalDatasetId) {
      let values: { samples?: EvalSampleRow[] };
      try {
        values = await evalForm.validateFields();
      } catch {
        return; // 表单校验错误已就地提示
      }
      rows = (values.samples || [])
        .filter(r => r.question?.trim() && r.ground_truth?.trim())
        .map(r => ({ question: r.question!.trim(), ground_truth: r.ground_truth!.trim() }));
      if (rows.length === 0) {
        message.error('请至少填写一条有效测试样本（问题 + 正确答案）');
        return;
      }
    }
    setEvalSubmitting(true);
    try {
      // 发起前检测 LLM/Embedding 可用性（任一不可用 → 阻止发起并说明原因）；
      // precheck 接口自身异常（网络/5xx）不阻断——后端发起时会给出真实错误
      let precheck = null;
      try {
        precheck = await ragasPrecheck();
      } catch {
        precheck = null;
      }
      if (precheck) {
        const failures: string[] = [];
        if (!precheck.data.llm.available) {
          failures.push(`LLM 服务不可用（${precheck.data.llm.reason || '无响应'}）`);
        }
        if (!precheck.data.embedding.available) {
          failures.push(`Embedding 服务不可用（${precheck.data.embedding.reason || '无响应'}）`);
        }
        if (failures.length > 0) {
          message.error(`${failures.join('；')}，无法发起评估`, 6);
          return;
        }
      }
      const res = await startRagasEvaluation({
        ...(evalDatasetId
          // 评估集重跑：知识库后端忽略（以评估集绑定的为准，避免题集与库不配套）
          ? { kb_ids: evalKbIds, dataset_id: evalDatasetId }
          : { kb_ids: evalKbIds, samples: rows }),
        metrics: evalMetrics,
        top_k: 3,
        // 评委模型：不选 = 跟随当前激活模型（现状行为）
        llm_model: evalModel,
        // 回答来源：dataset=题集参考答案 / generate=走真实链路实时生成
        answer_source: answerSource,
      });
      message.success(`评估任务已创建：${res.data.name}（${res.data.sample_count} 条样本），运行中可查看进度`);
      // —— 魔法注入动画：测试集注入任务列表（0.6s）→ 新任务行高亮 + 滚动 ——
      setEvalAnim(true);              // 弹窗内测试集区域注入动画 + 魔法光晕
      setNewTaskId(res.data.task_id); // 任务列表端：新行高亮 + scrollIntoView
      await loadRagas();              // 刷新任务列表（动画期间并行执行）
      startPolling();
      // 动画结束后关闭弹窗并重置表单（动画期间 Modal 保持打开、禁止重复提交）
      if (evalAnimTimerRef.current !== null) window.clearTimeout(evalAnimTimerRef.current);
      evalAnimTimerRef.current = window.setTimeout(() => {
        setEvalAnim(false);
        setEvalOpen(false);
        evalForm.resetFields();
      }, 700);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '发起评估失败，请确认 RAGAS 服务已启动（端口 8090）');
    } finally {
      setEvalSubmitting(false);
    }
  };

  // 解析「问题<TAB>答案」文本（与「文本导入」同口径，也兼容 | 分隔；答案可留空——
  // 配合「实时生成」模式时不需要标准答案）
  const parseDatasetText = (text: string): RagasSampleInput[] => {
    const rows: RagasSampleInput[] = [];
    for (const line of text.split('\n')) {
      const t = line.trim();
      if (!t) continue;
      const parts = t.split(/\t|\s*\|\s*/);
      const q = (parts[0] || '').trim();
      if (!q) continue;
      rows.push({ question: q, ground_truth: (parts[1] || '').trim() });
    }
    return rows;
  };

  // 直接新建评估集（不走发起评估）：题集可以先把问题攒起来，之后再跑
  const handleCreateDataset = async () => {
    const name = newDsName.trim();
    if (!name) {
      message.error('请填写评估集名称');
      return;
    }
    if (newDsKbIds.length === 0) {
      message.error('请选择知识库');
      return;
    }
    const rows = parseDatasetText(newDsText);
    if (rows.length === 0) {
      message.error('请至少填一条样本，每行格式：问题 | 参考答案（答案可留空）');
      return;
    }
    setNewDsSubmitting(true);
    try {
      const res = await createRagasDataset({
        kb_id: newDsKbIds[0], name, samples: rows, source: 'manual',
      });
      setEvalDatasets(prev => [res.data, ...prev]);
      setNewDsOpen(false);
      setNewDsName('');
      setNewDsText('');
      setNewDsKbIds([]);
      message.success(`评估集「${name}」已创建（${rows.length} 条样本）`);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '新建评估集失败');
    } finally {
      setNewDsSubmitting(false);
    }
  };

  // ===== AI 出题（合成测试集）：给"还没人问过、也没人写题"的新库用 =====

  const resetGen = () => {
    setGenSamples([]);
    setGenChecked([]);
    setGenName('');
  };

  // 换知识库 → 拉它的文档列表供"取材范围"多选（只列已入库的；
  // 软删的文档块会被后端直接排除，列出来反而误导）
  const handleGenKbChange = async (kbId: string | undefined) => {
    setGenKbId(kbId);
    setGenDocIds([]);
    setGenDocs([]);
    if (!kbId) return;
    try {
      const res = await listDocuments(kbId);
      const arr = Array.isArray(res.data) ? res.data : (res.data.items || []);
      setGenDocs(arr.filter(d => d.status === 'ingested'));
    } catch {
      setGenDocs([]);
    }
  };

  const handleGenerateQuestions = async () => {
    if (!genKbId) {
      message.error('请选择知识库');
      return;
    }
    setGenLoading(true);
    try {
      const res = await generateRagasQuestions(genKbId, genCount, genDocIds);
      const samples = res.data.samples || [];
      setGenSamples(samples);
      setGenChecked(samples.map(() => true));  // 默认全选，用户再取消不要的
      if (samples.length === 0) {
        message.warning(
          `抽了 ${res.data.picked} 个片段都没能出题（内容太少或模型判断不适合出题），`
          + '换个知识库或调大条数再试', 6);
      } else {
        message.success(`已生成 ${samples.length} 条草稿，请审核后采用`);
      }
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || 'AI 出题失败（检查 LLM 服务与知识库是否有切块）', 6);
    } finally {
      setGenLoading(false);
    }
  };

  // 采用勾选的草稿 → 建成正式评估集
  const handleAdoptGenerated = async () => {
    const picked = genSamples.filter((_, i) => genChecked[i]);
    if (picked.length === 0) {
      message.error('请至少勾选一条样本');
      return;
    }
    const name = genName.trim();
    if (!name) {
      message.error('请填写评估集名称');
      return;
    }
    if (!genKbId) return;
    setGenSaving(true);
    try {
      const res = await createRagasDataset({
        kb_id: genKbId, name, source: 'ai',
        samples: picked.map(s => ({
          question: s.question, ground_truth: s.ground_truth,
        })),
      });
      setEvalDatasets(prev => [res.data, ...prev]);
      setGenOpen(false);
      resetGen();
      message.success(`评估集「${name}」已创建（${picked.length} 条）`);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '保存评估集失败');
    } finally {
      setGenSaving(false);
    }
  };

  // 删除评估集（已跑过的任务记录不受影响，仍可在任务列表回看）
  const handleDeleteDataset = async (d: RagasDataset) => {
    try {
      await deleteRagasDataset(d.id);
      message.success('评估集已删除');
      if (evalDatasetId === d.id) setEvalDatasetId(undefined);
      setEvalDatasets(prev => prev.filter(x => x.id !== d.id));
    } catch {
      message.error('删除评估集失败');
    }
  };

  // 把当前填写/导入的测试集另存为评估集，之后可反复重跑同一份题集做分数对比
  const handleSaveAsDataset = async () => {
    const name = saveDsName.trim();
    if (!name) {
      message.error('请填写评估集名称');
      return;
    }
    if (evalKbIds.length === 0) {
      message.error('请先选择知识库');
      return;
    }
    let values: { samples?: EvalSampleRow[] };
    try {
      values = await evalForm.validateFields();
    } catch {
      return;
    }
    const rows = (values.samples || [])
      .filter(r => r.question?.trim() && r.ground_truth?.trim())
      .map(r => ({ question: r.question!.trim(), ground_truth: r.ground_truth!.trim() }));
    if (rows.length === 0) {
      message.error('请先填写至少一条有效测试样本（问题 + 正确答案）');
      return;
    }
    try {
      const res = await createRagasDataset({
        // 评估集绑定单个知识库：多库时取第一个作为归属库
        kb_id: evalKbIds[0], name, samples: rows, source: 'manual',
      });
      setEvalDatasets(prev => [res.data, ...prev]);
      setSaveDsOpen(false);
      setSaveDsName('');
      message.success(`评估集「${name}」已保存（${rows.length} 条样本），以后可一键重跑对比`);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '保存评估集失败');
    }
  };

  // 打开任务报告
  const openReport = async (task: RagasTask) => {
    setReportTask(task);
    setReportOpen(true);
    setReportLoading(true);
    setReport(null);
    try {
      const res = await getRagasReport(task.id);
      setReport(res.data);
    } catch {
      setReport(null);
    } finally {
      setReportLoading(false);
    }
  };

  // 取消按钮显隐（与后端权限一致）：仅运行中/排队中任务，且为发起人本人或
  // super_admin（dept_admin 本人发起同样命中 user_id 比对；本部门其他用户
  // 发起的任务后端放行、前端从简不显示——权限由后端兜底校验）
  const canCancelTask = (t: RagasTask) =>
    (t.status === 'running' || t.status === 'queued') &&
    (t.user_id === user?.id || user?.role === 'super_admin');

  // 取消评估任务（Popconfirm 确认后调用；成功刷新列表，失败显示后端中文错误）
  const handleCancelTask = async (task: RagasTask) => {
    try {
      await cancelRagasEvaluation(task.id);
      message.success('评估任务已取消');
      loadRagas();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '取消失败，请稍后重试');
    }
  };

  const renderAggregate = () => {
    if (!report) return null;
    const scores = report.aggregate?.scores || {};
    const entries = Object.entries(scores);
    if (entries.length === 0) {
      return <AppEmpty title="该任务无聚合评分数据" />;
    }
    return (
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        {entries.map(([k, v]) => (
          <Col key={k} xs={12} sm={8} md={6} lg={4}>
            <Card size="small">
              <Tooltip title={metricLabelMap[k] ? k : undefined}>
                <Statistic
                  title={metricLabel(k)}
                  value={v}
                  precision={4}
                  suffix="/ 1.0"
                  valueStyle={{ color: scoreColor(v) }}
                />
              </Tooltip>
            </Card>
          </Col>
        ))}
      </Row>
    );
  };

  // 明细表：指标列从 aggregate.scores 动态生成（缺失显示 —）
  const detailColumns = [
    { title: '#', dataIndex: 'idx', key: 'idx', width: 44 },
    {
      title: '问题', dataIndex: 'question', key: 'question', ellipsis: true, width: 220,
      render: (v: string) => <Tooltip title={v}>{v.length > 60 ? v.slice(0, 60) + '...' : v}</Tooltip>,
    },
    {
      title: '答案', dataIndex: 'answer', key: 'answer', ellipsis: true, width: 220,
      render: (v: string) => <Tooltip title={v}>{v.length > 60 ? v.slice(0, 60) + '...' : v}</Tooltip>,
    },
    ...Object.keys(report?.aggregate?.scores || {}).map(m => ({
      title: (
        <Tooltip title={metricLabelMap[m] ? m : undefined}>
          <span>{metricLabel(m)}</span>
        </Tooltip>
      ),
      dataIndex: ['scores', m],
      key: m,
      width: 110,
      render: (v: number | null | undefined) =>
        v != null ? (
          <Tag color={v >= 0.8 ? 'success' : v >= 0.5 ? 'warning' : 'error'}>{v.toFixed(4)}</Tag>
        ) : <Text type="secondary">—</Text>,
    })),
  ];

  // 同一评估集 + **同一评委模型** + **同一回答来源**的"上一次"平均分：按这三者分组，
  // 组内按创建时间升序，每个任务取它前一个的分数。
  // 为什么三项都要进分组键：换评委换的是评分标准、换 answer 来源换的是被测对象
  // （题集答案→测检索 / 实时生成→测端到端），任一不同分数都不可直接比——
  // 混在一起算差值会把"测法变了"误读成"质量变了"，那正是评估集要避免的事
  const prevScoreByTask = useMemo(() => {
    const byKey = new Map<string, RagasTask[]>();
    for (const t of ragas?.tasks || []) {
      const ds = t.local_dataset_id;
      if (!ds) continue;
      const key = `${ds}|${t.eval_model || ''}|${t.answer_source || 'dataset'}`;
      const arr = byKey.get(key);
      if (arr) arr.push(t);
      else byKey.set(key, [t]);
    }
    const out = new Map<string, number>();
    for (const arr of byKey.values()) {
      const sorted = [...arr].sort(
        (a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
      for (let i = 1; i < sorted.length; i++) {
        const prev = avgScore(sorted[i - 1].scores);
        if (prev !== null) out.set(sorted[i].id, prev);
      }
    }
    return out;
  }, [ragas]);

  const ragasColumns = [
    {
      title: '任务名称', dataIndex: 'name', key: 'name', ellipsis: true,
      width: colWidths.name ?? 260,
      onHeaderCell: () => ({ width: colWidths.name ?? 260, onResize: handleResize('name'), title: '任务名称' }),
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '知识库', dataIndex: 'kb_name', key: 'kb_name', ellipsis: true,
      width: colWidths.kb_name ?? 130,
      onHeaderCell: () => ({ width: colWidths.kb_name ?? 130, onResize: handleResize('kb_name'), title: '知识库' }),
      render: (v?: string) => v || <Text type="secondary">—</Text>,
    },
    {
      title: '样本来源', dataIndex: 'source', key: 'source',
      width: colWidths.source ?? 100,
      onHeaderCell: () => ({ width: colWidths.source ?? 100, onResize: handleResize('source'), title: '样本来源' }),
      render: (v?: string) => (v === 'chat'
        ? <Tag color="blue">会话问答</Tag>
        : v === 'logs' ? <Tag color="green">检索日志</Tag>
        : v === 'manual' ? <Tag color="purple">手动填写</Tag>
        : v === 'dataset' ? <Tag color="orange">评估集重跑</Tag>
        : <Text type="secondary">—</Text>),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status',
      width: colWidths.status ?? 110,
      onHeaderCell: () => ({ width: colWidths.status ?? 110, onResize: handleResize('status'), title: '状态' }),
      render: (s: string) => {
        const cfg = statusOf(s);
        return (
          <Tag color={cfg.color}>
            {s === 'running' ? <SyncOutlined spin /> : null} {cfg.label}
          </Tag>
        );
      },
    },
    {
      title: '指标', dataIndex: 'metrics', key: 'metrics',
      width: colWidths.metrics ?? 220,
      onHeaderCell: () => ({ width: colWidths.metrics ?? 220, onResize: handleResize('metrics'), title: '指标' }),
      render: (ms: string[] | undefined) =>
        ms?.length ? ms.map(m => (
          <Tooltip key={m} title={metricLabelMap[m] ? m : undefined}>
            <Tag>{metricLabel(m)}</Tag>
          </Tooltip>
        )) : <Text type="secondary">—</Text>,
    },
    {
      title: '创建时间', dataIndex: 'created_at', key: 'created_at',
      width: colWidths.created_at ?? 170,
      onHeaderCell: () => ({ width: colWidths.created_at ?? 170, onResize: handleResize('created_at'), title: '创建时间' }),
      render: (v?: string) => v || '—',
    },
    {
      title: '完成时间', dataIndex: 'completed_at', key: 'completed_at',
      width: colWidths.completed_at ?? 170,
      onHeaderCell: () => ({ width: colWidths.completed_at ?? 170, onResize: handleResize('completed_at'), title: '完成时间' }),
      render: (v?: string) => v || '—',
    },
    {
      // 平均分 + 与"同一评估集上一次"的对比标签。
      // 这一列才是评估集的价值兑现处：改了检索/切块/提示词后，有无进步一眼可见
      title: '分数', key: 'score', width: 160,
      render: (_: unknown, t: RagasTask) => {
        const cur = avgScore(t.scores);
        // 悬停显示本次评委模型：换过评委的任务，分数不与其它任务直接比
        const tip = t.eval_model ? `评委模型：${t.eval_model}` : undefined;
        if (cur === null) {
          return <Text type="secondary" title={tip}>—</Text>;
        }
        const prev = prevScoreByTask.get(t.id);
        const diff = prev === undefined ? null : cur - prev;
        return (
          <Space size={4} title={tip}>
            <span style={{ color: scoreColor(cur), fontWeight: 600 }}>{cur.toFixed(4)}</span>
            {diff !== null && (
              <Tag color={diff >= 0 ? 'green' : 'red'} style={{ marginInlineEnd: 0 }}>
                {diff >= 0 ? '↑' : '↓'}{Math.abs(diff).toFixed(3)}
              </Tag>
            )}
          </Space>
        );
      },
    },
    {
      // 行尾操作："查看报告"常显；"取消"仅运行中/排队中且有权限时显示
      // （Popconfirm 确认，点击不触发整行报告跳转）
      title: '操作', key: 'action', width: 170,
      render: (_: unknown, record: RagasTask) => (
        <>
          <Button
            size="small"
            type="link"
            icon={<EyeOutlined />}
            style={{ padding: 0, fontSize: 12 }}
            onClick={e => {
              e.stopPropagation();
              openReport(record);
            }}
          >
            查看报告
          </Button>
          {canCancelTask(record) ? (
            <Popconfirm
              title="取消该评估任务？"
              description="取消后任务停止运行，本次评估结果将不可用"
              okText="确认取消"
              okButtonProps={{ danger: true }}
              cancelText="保留"
              onConfirm={() => handleCancelTask(record)}
            >
              <Button
                size="small"
                type="link"
                danger
                icon={<StopOutlined />}
                style={{ padding: 0, fontSize: 12, marginLeft: 8 }}
                onClick={e => e.stopPropagation()}
              >
                取消
              </Button>
            </Popconfirm>
          ) : null}
        </>
      ),
    },
  ];

  const renderTableOrUnavailable = () => {
    if (loading && tasks.length === 0) {
      return (
        <Card style={{ flex: 1, minHeight: 0 }}>
          <Skeleton active paragraph={{ rows: 4 }} />
        </Card>
      );
    }
    if (!ragas?.available) {
      return (
        <Card style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
          <Alert
            type="warning"
            showIcon
            message="RAGAS 评测系统不可用"
            description={
              <span>
                {ragas?.message || '无法连接 RAGAS 评测系统'}。
                可在知识库管理/文档管理中继续使用；
                如需查看评估报告，请先启动 RAGAS 服务（端口 8090）。
              </span>
            }
          />
        </Card>
      );
    }
    return (
      <TableSectionLayout
        total={tasks.length}
        page={page}
        pageSize={pageSize}
        onPageChange={(p, ps) => { setPage(p); setPageSize(ps); }}
        toolbar={
          <Text type="secondary" style={{ fontSize: 12 }}>
            RAGAS 服务已连接，任务运行中每 8 秒自动刷新进度
          </Text>
        }
        emptyOrLoading={undefined}
      >
        <Table<RagasTask>
          rowKey="id"
          size="small"
          dataSource={pageTasks}
          columns={ragasColumns}
          rowClassName={(record) => record.id === newTaskId ? 'ragas-row-highlight' : ''}
          pagination={false}
          sticky
          className="table-zebra"
          components={{ header: { cell: ResizableTitle } }}
          scroll={{ x: tableWidth(ragasColumns) }}
          onRow={(record) => ({
            onClick: () => openReport(record),
            style: { cursor: 'pointer' },
          })}
          locale={{ emptyText: <AppEmpty title="RAGAS 已连接，但暂无评估任务" /> }}
        />
      </TableSectionLayout>
    );
  };

  return (
    <PageLayout
      title="RAGAS 评测"
      description="评估任务管理：发起评测、取消任务、查看逐样本报告（发起仅管理员）"
      breadcrumb={
        <Breadcrumb
          items={[
            { title: <a onClick={() => navigate('/analytics')}>统计分析</a> },
            { title: 'RAGAS 评测' },
          ]}
        />
      }
      extra={
        <Space>
          {isAdmin ? (
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              onClick={openEvalModal}
            >
              发起评估
            </Button>
          ) : null}
          <Button icon={<ReloadOutlined />} onClick={loadRagas} loading={loading}>刷新</Button>
        </Space>
      }
    >
      {renderTableOrUnavailable()}

      {/* 发起评估 Modal（手动测试集：问题 + 正确答案） */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 760, h: 520 }}
        rememberKey="ana-1"
        title="发起 RAGAS 评估"
        open={evalOpen}
        onCancel={() => { if (evalAnim) return; setEvalOpen(false); }} // 注入动画期间禁止手动关闭
        onOk={handleStartEvaluation}
        okText="发起评估"
        confirmLoading={evalSubmitting}
        okButtonProps={{ disabled: (evalDatasetId ? false : evalValidCount === 0) || evalAnim }} // 评估集模式样本来自题集，不再要求测试集有内容
        width={760}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={16}>
          <div>
            <Text strong>知识库</Text>
            <Select
              style={{ width: '100%', marginTop: 4 }}
              mode="multiple"
              maxTagCount="responsive"
              placeholder="选择要评估的知识库（可多选）"
              value={evalKbIds}
              onChange={(v: string[]) => setEvalKbIds(v)}
              options={kbs.map(k => ({ value: k.id, label: `${k.name}（${k.chunk_count} 个切块）` }))}
              notFoundContent={<Empty description="暂无可用知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            />
          </div>
          <div>
            <Space style={{ width: '100%', justifyContent: 'space-between' }}>
              <Text strong>评估集（可选）</Text>
              <Button size="small" type="link" onClick={() => setDatasetMgrOpen(true)}>
                管理评估集
              </Button>
            </Space>
            <Select
              style={{ width: '100%', marginTop: 4 }}
              placeholder="不使用评估集（改用下方测试集）"
              allowClear
              value={evalDatasetId}
              onChange={(v: string | undefined) => setEvalDatasetId(v)}
              options={evalDatasets.map(d => ({
                value: d.id,
                label: `${d.name}（${d.samples?.length ?? 0} 条 · ${d.kb_name}）`,
              }))}
              notFoundContent={<Empty description="暂无评估集（发起评估后可把测试集另存）"
                                         image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {evalDatasetId
                ? '将用该题集重跑（知识库以其绑定的为准），分数可与历史任务对比'
                : '固定的一份题集反复重跑，分数才可比——这是判断「改动到底变好没有」的前提'}
            </Text>
          </div>
          <div>
            <Text strong>评估模型（评委）</Text>
            <Select
              style={{ width: '100%', marginTop: 4 }}
              placeholder="跟随当前使用的模型"
              allowClear
              value={evalModel}
              onChange={(v: string | undefined) => setEvalModel(v)}
              options={llmModelList.map((m, i) => ({
                value: m.name,
                label: `${m.name}${i === llmActiveIdx ? '（当前使用）' : ''}`,
              }))}
              notFoundContent={<Empty description="当前档案没有配 LLM 模型"
                                         image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              它是给回答打分的<strong>评委</strong>，不是被评估的对象。换评委等于换评分标准，
              分数不能和别的模型跑出来的比——任务列表里只在同一评委之间显示 ↑↓ 对比
            </Text>
          </div>
          <div>
            <Text strong>回答来源</Text>
            <div style={{ marginTop: 4 }}>
              <Radio.Group
                value={answerSource}
                onChange={e => setAnswerSource(e.target.value)}
              >
                <Radio value="dataset">题集里的参考答案</Radio>
                <Radio value="generate">系统实时生成</Radio>
              </Radio.Group>
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {answerSource === 'generate'
                ? '对每道题走一遍真实问答链路（检索→组装提示词→生成），评的是端到端质量。'
                  + '更慢——每条样本一次完整问答——但分数就等于用户实际体验到的水平'
                : '直接用题集里填的参考答案当回答，快；实际评的是"检索出的上下文够不够答这道题"'}
            </Text>
          </div>
          <div>
            <Text strong>评估指标</Text>
            <div style={{ marginTop: 4 }}>
              <Checkbox.Group
                value={evalMetrics}
                onChange={(v) => setEvalMetrics(v as string[])}
                options={ragasMetricOptions.map(o => ({
                  value: o.value,
                  label: <Tooltip key={o.value} title={o.desc}><span>{o.label}</span></Tooltip>,
                }))}
              />
            </div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              已填写正确答案，默认全选全部 6 个指标均可评分（可取消不需要的指标）
            </Text>
          </div>
          {/* 选了评估集就隐藏测试集表单（样本来自题集，避免两处来源混淆）；
              用 display 隐藏而非条件渲染——Form.List 需保持挂载才能正常 resetFields */}
          <div
            className={`eval-inject-wrap${evalAnim ? ' eval-inject-anim' : ''}`}
            style={evalDatasetId ? { display: 'none' } : undefined}
          >
            {/* 魔法注入光晕（纯 CSS 径向渐变扩散，aria-hidden 不干扰读屏） */}
            {evalAnim ? <div className="eval-glow" aria-hidden="true" /> : null}
            <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 8 }}>
              <Text strong>测试集（问题 + 正确答案）</Text>
              <Space>
                <Button
                  size="small"
                  icon={<SaveOutlined />}
                  onClick={() => setSaveDsOpen(true)}
                  title="把当前测试集存成评估集，以后可反复重跑对比分数"
                >
                  另存为评估集
                </Button>
                <Button
                  size="small"
                  icon={<ImportOutlined />}
                  onClick={handleImportFromChat}
                  loading={evalImporting}
                >
                  从聊天历史导入
                </Button>
                <Button
                  size="small"
                  icon={<FileTextOutlined />}
                  onClick={openTextModal}
                >
                  文本导入/导出
                </Button>
              </Space>
            </Space>
            {evalValidCount === 0 && (
              <Alert
                type="warning"
                showIcon
                message="暂无测试数据，无法发起评估"
                description="请从聊天历史导入、文本导入或手动填写测试集（问题 + 正确答案）后再发起"
                style={{ marginBottom: 8 }}
              />
            )}
            <Form form={evalForm} component={false}>
              <Form.List name="samples">
                {(fields, { add, remove }) => (
                  <div
                    style={{
                      maxHeight: 400, // 最多约 10 行，超出滚动
                      overflowY: 'auto',
                      border: '1px solid #f0f0f0',
                      borderRadius: 6,
                      padding: '8px 8px 12px',
                    }}
                  >
                    {fields.map((field, index) => (
                      <Row key={field.key} gutter={8} align="top" style={{ marginBottom: 8 }}>
                        <Col flex="auto">
                          <Form.Item
                            name={[field.name, 'question']}
                            rules={[{ required: true, whitespace: true, message: '请输入测试问题' }]}
                            style={{ marginBottom: 0 }}
                          >
                            <Input placeholder={`问题 ${index + 1}（必填）`} />
                          </Form.Item>
                        </Col>
                        <Col flex="auto">
                          <Form.Item
                            name={[field.name, 'ground_truth']}
                            rules={[{ required: true, whitespace: true, message: '请输入正确答案' }]}
                            style={{ marginBottom: 0 }}
                          >
                            <Input placeholder={`正确答案 ${index + 1}（必填）`} />
                          </Form.Item>
                        </Col>
                        {fields.length > 1 ? (
                          <Col flex="none">
                            <Button
                              type="text"
                              danger
                              icon={<DeleteOutlined />}
                              onClick={() => remove(field.name)}
                            />
                          </Col>
                        ) : null}
                      </Row>
                    ))}
                    <Space>
                      <Button
                        type="dashed"
                        icon={<PlusOutlined />}
                        disabled={fields.length >= 100}
                        onClick={() => add({ question: '', ground_truth: '' })}
                      >
                        添加一行
                      </Button>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {fields.length}/100 行（最多 100 行）
                      </Text>
                    </Space>
                  </div>
                )}
              </Form.List>
            </Form>
          </div>
          <Alert
            type="info"
            showIcon
            message="请填写测试问题与正确答案（仅您知道准确答案）"
            description="系统将自动检索知识库内容作为上下文进行评分；您填写的答案将同时作为
              参考答案（ground_truth）与回答（answer）参与评估。评估使用知识库当前活跃
              LLM 配置作为评分模型。"
          />
        </Space>
      </AppModal>

      {/* 测试集导入/导出 Modal（txt / Excel 格式选择；导出下载文件，导入文件/文本回填） */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 680, h: 480 }}
        rememberKey="ana-2"
        title="测试集导入/导出"
        open={textModalOpen}
        onCancel={() => setTextModalOpen(false)}
        width={680}
        footer={[
          <Button
            key="export"
            icon={ioFormat === 'excel' ? <FileExcelOutlined /> : <FileTextOutlined />}
            onClick={handleExportByFormat}
          >
            导出文件（{ioFormat === 'excel' ? 'Excel' : 'txt'}）
          </Button>,
          <Button key="file" icon={<UploadOutlined />} onClick={() => fileInputRef.current?.click()}>
            导入文件（{ioFormat === 'excel' ? 'Excel/csv' : 'txt'}）
          </Button>,
          ...(ioFormat === 'txt' ? [(
            <Button
              key="import"
              type="primary"
              icon={<ImportOutlined />}
              onClick={handleImportText}
            >
              从下方文本导入（覆盖测试集）
            </Button>
          )] : []),
        ]}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Space wrap>
            <Text strong>格式</Text>
            <Segmented
              options={[{ label: 'txt 文本', value: 'txt' }, { label: 'Excel(xlsx/csv)', value: 'excel' }]}
              value={ioFormat}
              onChange={(v) => setIoFormat(v as 'txt' | 'excel')}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {ioFormat === 'excel'
                ? 'Excel：导出两列（问题/正确答案，首行表头）；导入解析第一列为问题、第二列为正确答案'
                : 'txt：每行一条：问题【Tab 键】正确答案'}
            </Text>
          </Space>
          {ioFormat === 'txt' ? (
            <Alert
              type="info"
              showIcon
              message="txt 格式：每行一条：问题【Tab 键】正确答案"
              description={
                <span style={{ fontSize: 12 }}>
                  先导出当前测试集（自动下载文件）→ 在外部按格式编辑 → 再导入回填（覆盖）。
                  <br />空行与 <Text code>#</Text> 注释行忽略；问题或答案为空的行跳过；最多 100 条。
                  <br />
                  <Text code>在线式软地线和 IEC104 接地线配置有什么区别？&nbsp;&nbsp;两者不可同时配置</Text>
                </span>
              }
            />
          ) : (
            <Alert
              type="info"
              showIcon
              message="Excel 格式：两列（问题 / 正确答案）"
              description={
                <span style={{ fontSize: 12 }}>
                  导出生成 .xlsx（首行表头，Excel/WPS 均可打开）；导入支持 .xlsx/.csv
                  文件（首行表头跳过，其余每行取第一列为问题、第二列为正确答案）。
                  <br />问题或答案为空的单元格所在行跳过；最多 100 条。
                </span>
              }
            />
          )}
          {fileParseResult ? (
            <Alert
              type={fileParseResult.invalid.length ? 'warning' : 'success'}
              showIcon
              message={`导入结果：有效 ${fileParseResult.ok} 条${fileParseResult.skipped ? `，跳过 ${fileParseResult.skipped} 条无效内容` : ''}（已覆盖当前测试集）`}
              description={fileParseResult.invalid.length > 0 ? (
                <span style={{ fontSize: 12 }}>
                  无效内容（最多显示前 3 条）：<br />
                  {fileParseResult.invalid.slice(0, 3).map((s, i) => (
                    <span key={i}>{s}<br /></span>
                  ))}
                </span>
              ) : undefined}
            />
          ) : null}
          <Input.TextArea
            value={evalText}
            onChange={(e) => setEvalText(e.target.value)}
            rows={12}
            placeholder={'问题1\t正确答案1\n问题2\t正确答案2'}
            style={{ fontFamily: 'monospace', fontSize: 13 }}
          />
          <input
            ref={fileInputRef}
            type="file"
            accept={ioFormat === 'excel' ? '.xlsx,.xls,.csv' : '.txt'}
            style={{ display: 'none' }}
            onChange={onFileChange}
          />
        </Space>
      </AppModal>

      {/* 从聊天历史导入选择 Modal：勾选想要的预览样本 → 确定后追加到测试集 */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 680, h: 480 }}
        rememberKey="ana-3"
        title={`从聊天历史导入（预览 ${chatPickList.length} 条）`}
        open={chatPickOpen}
        onCancel={() => setChatPickOpen(false)}
        onOk={handleChatPickConfirm}
        okText={`追加勾选的 ${chatPickSelected.length} 条`}
        cancelText="取消"
        width={680}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Alert
            type="info"
            showIcon
            message="将追加到当前测试集"
            description="勾选想要的问题后点确定：追加到测试集表单末尾（相同问题不重复添加）；点取消则不改动当前测试集。"
          />
          <Space>
            <Button size="small" onClick={() => setChatPickSelected(chatPickList.map(s => s.question))}>
              全选
            </Button>
            <Button size="small" onClick={() => setChatPickSelected([])}>
              全不选
            </Button>
            <Text type="secondary" style={{ fontSize: 12 }}>
              已选 {chatPickSelected.length} / {chatPickList.length} 条
            </Text>
          </Space>
          <div style={{ maxHeight: 320, overflowY: 'auto', border: '1px solid #f0f0f0', borderRadius: 6, padding: '4px 8px' }}>
            <Checkbox.Group
              style={{ width: '100%' }}
              value={chatPickSelected}
              onChange={(v) => setChatPickSelected(v as string[])}
            >
              <List
                size="small"
                dataSource={chatPickList}
                renderItem={(s, i) => (
                  <List.Item style={{ padding: '6px 4px' }}>
                    <Checkbox value={s.question} style={{ width: '100%' }}>
                      <Tooltip title={s.question}>
                        <span
                          style={{
                            display: 'inline-block', maxWidth: 470,
                            overflow: 'hidden', textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap', verticalAlign: 'bottom',
                          }}
                        >
                          {i + 1}. {s.question}
                        </span>
                      </Tooltip>
                      <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                        会话问答
                      </Text>
                    </Checkbox>
                  </List.Item>
                )}
              />
            </Checkbox.Group>
          </div>
        </Space>
      </AppModal>

      {/* 任务报告 Modal */}
      {/* 评估集管理：已保存的题集列表，可一键用它评估、也可删除 */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 720, h: 460 }}
        rememberKey="ana-dataset-mgr"
        title="评估集管理"
        open={datasetMgrOpen}
        onCancel={() => setDatasetMgrOpen(false)}
        footer={[
          <Button
            key="ai"
            icon={<RobotOutlined />}
            onClick={() => setGenOpen(true)}
          >
            AI 生成题集
          </Button>,
          <Button
            key="new"
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setNewDsOpen(true)}
          >
            新建评估集
          </Button>,
          <Button key="close" onClick={() => setDatasetMgrOpen(false)}>关闭</Button>,
        ]}
        width={720}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          评估集是一份固定的题集（问题 + 正确答案）。反复用同一份题集重跑，分数才有可比性
          ——改了切块 / 检索 / 提示词之后跑一次，就知道是变好还是变差。
        </Text>
        <List
          rowKey="id"
          style={{ marginTop: 12 }}
          dataSource={evalDatasets}
          locale={{ emptyText: <Empty description="暂无评估集（在发起评估弹窗里可把测试集另存为评估集）"
                                           image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
          renderItem={d => (
            <List.Item
              actions={[
                <Button
                  key="run"
                  type="link"
                  size="small"
                  icon={<PlayCircleOutlined />}
                  onClick={() => {
                    setEvalDatasetId(d.id);
                    setEvalKbIds([d.kb_id]);
                    setDatasetMgrOpen(false);
                    setEvalOpen(true);  // 直接打开发起弹窗，评估集已选好
                  }}
                >
                  用它评估
                </Button>,
                <Popconfirm key="del" title="删除该评估集？" onConfirm={() => handleDeleteDataset(d)}>
                  <Button type="link" size="small" danger icon={<DeleteOutlined />}>删除</Button>
                </Popconfirm>,
              ]}
            >
              <List.Item.Meta
                title={d.name}
                description={`${d.kb_name} · ${d.samples?.length ?? 0} 条样本 · 更新于 ${dayjs(d.updated_at).format('YYYY-MM-DD HH:mm')}`}
              />
            </List.Item>
          )}
        />
      </AppModal>

      {/* 新建评估集：不经过发起评估，先把题集攒起来（之后可随时"用它评估"） */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 560, h: 540 }}
        rememberKey="ana-dataset-new"
        title="新建评估集"
        open={newDsOpen}
        onCancel={() => setNewDsOpen(false)}
        onOk={handleCreateDataset}
        okText="创建"
        cancelText="取消"
        confirmLoading={newDsSubmitting}
        width={560}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <div>
            <Text strong>知识库</Text>
            <Select
              style={{ width: '100%', marginTop: 4 }}
              placeholder="选择题集归属的知识库"
              value={newDsKbIds[0]}
              onChange={(v: string) => setNewDsKbIds([v])}
              options={kbs.map(k => ({ value: k.id, label: k.name }))}
              notFoundContent={<Empty description="暂无可用知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            />
          </div>
          <div>
            <Text strong>名称</Text>
            <Input
              style={{ marginTop: 4 }}
              placeholder="评估集名称（1~50 字）"
              maxLength={50}
              value={newDsName}
              onChange={e => setNewDsName(e.target.value)}
            />
          </div>
          <div>
            <Space style={{ width: '100%', justifyContent: 'space-between' }}>
              <Text strong>样本</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>每行一条：问题 | 参考答案</Text>
            </Space>
            <Input.TextArea
              style={{ marginTop: 4 }}
              rows={9}
              placeholder={'这份文档主要讲什么？ | 讲的是……\n第二个问题？ |'}
              value={newDsText}
              onChange={e => setNewDsText(e.target.value)}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              共 {parseDatasetText(newDsText).length} 条。答案可以留空——配合「系统实时生成」
              跑评估时不需要标准答案（那种模式测的是端到端质量）
            </Text>
          </div>
        </Space>
      </AppModal>

      {/* AI 出题：草稿 → 人工审核 → 采用（给还没人问过、也没人写题的新库用） */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 780, h: 640 }}
        rememberKey="ana-dataset-ai"
        title="AI 生成题集（草稿，需审核）"
        open={genOpen}
        onCancel={() => { setGenOpen(false); resetGen(); }}
        footer={genSamples.length === 0 ? [
          <Button key="cancel" onClick={() => { setGenOpen(false); resetGen(); }}>取消</Button>,
          <Button key="gen" type="primary" loading={genLoading} onClick={handleGenerateQuestions}>
            开始生成
          </Button>,
        ] : [
          <Button key="regen" onClick={resetGen}>重新生成</Button>,
          <Button key="cancel2" onClick={() => { setGenOpen(false); resetGen(); }}>取消</Button>,
          <Button key="adopt" type="primary" loading={genSaving} onClick={handleAdoptGenerated}>
            采用选中的 {genChecked.filter(Boolean).length} 条
          </Button>,
        ]}
        width={780}
      >
        {genSamples.length === 0 ? (
          <Space direction="vertical" style={{ width: '100%' }} size={14}>
            <Alert
              type="info"
              showIcon
              message="AI 读知识库的切块，自己出题 + 写参考答案"
              description="生成结果是**草稿**：你可以逐条改、删、勾选，确认后才存成评估集。
                参考答案是基于原文生成的，但仍建议对着「来源」核对一遍——AI 出的题和答案不能直接拿来就用。"
            />
            <div>
              <Text strong>知识库</Text>
              <Select
                style={{ width: '100%', marginTop: 4 }}
                placeholder="选择要出题的知识库"
                value={genKbId}
                onChange={handleGenKbChange}
                options={kbs.map(k => ({
                  value: k.id,
                  label: `${k.name}（${k.chunk_count} 个切块）`,
                }))}
                notFoundContent={<Empty description="暂无可用知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
              />
            </div>
            {genKbId && (
              <div>
                <Text strong>取材文档</Text>
                <Select
                  style={{ width: '100%', marginTop: 4 }}
                  mode="multiple"
                  maxTagCount="responsive"
                  allowClear
                  placeholder="不选 = 全库（按文档分散抽样）"
                  value={genDocIds}
                  onChange={(v: string[]) => setGenDocIds(v)}
                  // 用**原始文件名**：与出题来源显示的块 metadata.document_name 是同一个名字。
                  // 若这里用 name（内部存储名 xxx.docx），下拉里选的与来源里看到的对不上
                  options={genDocs.map(d => ({
                    value: d.id, label: d.original_name || d.name,
                  }))}
                  notFoundContent={<Empty description="该知识库暂无已入库文档"
                                             image={Empty.PRESENTED_IMAGE_SIMPLE} />}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  只从选中的文档取材；不选则整库。AI 读的是**切块**（被检索的正是它），
                  不是原始文档——这样才能保证出的题"检索得到"，评估才公平。
                  已删除的文档一律不参与
                </Text>
              </div>
            )}
            <div>
              <Text strong>生成条数</Text>
              <div style={{ marginTop: 4 }}>
                <InputNumber
                  min={1}
                  max={20}
                  value={genCount}
                  onChange={v => setGenCount(Number(v) || 10)}
                />
                <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                  1~20 条；出题会分散到不同文档取材，每条一次 LLM 调用，越多越慢
                </Text>
              </div>
            </div>
          </Space>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size={10}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              共 {genSamples.length} 条草稿，已默认全选。可直接编辑问题和参考答案，
              取消勾选不要的；「来源」是出题用的原文位置，方便核对答案准不准。
            </Text>
            <div style={{ maxHeight: 370, overflowY: 'auto' }}>
              {genSamples.map((s, i) => (
                <div
                  key={i}
                  style={{
                    padding: '8px 10px', marginBottom: 8,
                    border: '1px solid #f0f0f0', borderRadius: 8,
                  }}
                >
                  <Space align="start" style={{ width: '100%' }}>
                    <Checkbox
                      checked={genChecked[i]}
                      onChange={e => {
                        const next = [...genChecked];
                        next[i] = e.target.checked;
                        setGenChecked(next);
                      }}
                      style={{ marginTop: 6 }}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <Input
                        value={s.question}
                        placeholder="问题"
                        onChange={e => {
                          const next = [...genSamples];
                          next[i] = { ...s, question: e.target.value };
                          setGenSamples(next);
                        }}
                      />
                      <Input.TextArea
                        style={{ marginTop: 6 }}
                        rows={2}
                        value={s.ground_truth}
                        placeholder="参考答案"
                        onChange={e => {
                          const next = [...genSamples];
                          next[i] = { ...s, ground_truth: e.target.value };
                          setGenSamples(next);
                        }}
                      />
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        来源：{s.source_doc_name
                          ? `${s.source_doc_name} · 块 ${s.source_chunk_index ?? '?'}`
                          : (s.source_doc
                            ? `${s.source_doc.slice(0, 12)}… · 块 ${s.source_chunk_index ?? '?'}`
                            : '（后端未能从块元数据取到）')}
                      </Text>
                    </div>
                  </Space>
                </div>
              ))}
            </div>
            <div>
              <Text strong>存入哪个评估集</Text>
              <Input
                style={{ marginTop: 4 }}
                placeholder="评估集名称（1~50 字）"
                maxLength={50}
                value={genName}
                onChange={e => setGenName(e.target.value)}
              />
            </div>
          </Space>
        )}
      </AppModal>

      {/* 另存为评估集：把当前测试集沉淀成可复用题集 */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 440, h: 280 }}
        rememberKey="ana-dataset-save"
        title="另存为评估集"
        open={saveDsOpen}
        onCancel={() => setSaveDsOpen(false)}
        onOk={handleSaveAsDataset}
        okText="保存"
        cancelText="取消"
        width={440}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          保存后可在「评估集」下拉里选中它重跑；同一份题集的历次分数可以直接对比。
        </Text>
        <Input
          style={{ marginTop: 12 }}
          placeholder="评估集名称（1~50 字）"
          maxLength={50}
          value={saveDsName}
          onChange={e => setSaveDsName(e.target.value)}
          onPressEnter={handleSaveAsDataset}
          autoFocus
        />
      </AppModal>

      <AppModal
        dimension="resizable"
        defaultSize={{ w: 960, h: 600 }}
        rememberKey="ana-4"
        title={
          <Space>
            <span>评估报告</span>
            {reportTask ? <Tag color={statusOf(reportTask.status).color}>{statusOf(reportTask.status).label}</Tag> : null}
          </Space>
        }
        open={reportOpen}
        onCancel={() => setReportOpen(false)}
        footer={null}
        width={960}
      >
        {reportLoading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : report ? (
          <>
            {renderAggregate()}
            <Card
              title={`逐样本明细 (${report.results?.length ?? 0} 条)`}
              size="small"
              styles={{ body: { padding: 0 } }}
            >
              <Table
                rowKey="idx"
                size="small"
                dataSource={(report.results || []).map((r, i) => ({ ...r, idx: i + 1 }))}
                columns={detailColumns}
                scroll={{ x: 'max-content' }}
                pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
              />
            </Card>
          </>
        ) : (
          <AppEmpty title="报告加载失败" description="RAGAS 任务可能已删除或尚未完成" />
        )}
      </AppModal>
    </PageLayout>
  );
};

export default AnalyticsRagasDetailPage;
