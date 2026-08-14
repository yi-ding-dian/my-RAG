import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  App as AntApp, Card, Col, Row, Skeleton, Statistic, Table, Tag, Typography, Alert, Modal,
  Space, Tooltip, Button, Empty, Select, Checkbox, Form, Input, Segmented, List, Popconfirm,
} from 'antd';
import {
  DatabaseOutlined, DeleteOutlined, EyeOutlined, FileExcelOutlined, FileTextOutlined,
  ImportOutlined, MessageOutlined, PartitionOutlined, PlusOutlined, ReloadOutlined,
  StopOutlined, SyncOutlined, PlayCircleOutlined, UploadOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import * as XLSX from 'xlsx';
import {
  cancelRagasEvaluation, getStats, getRagasStatus, getRagasReport,
  getRetrievalQuality, listKbs, startRagasEvaluation, previewRagasSamples, ragasPrecheck,
  KnowledgeBase, RetrievalHitDoc, RetrievalQuality, RetrievalZeroHitDoc,
  Stats, RagasStatus, RagasTask, RagasReport, RagasSampleInput,
} from '../api/client';
import { useAuth } from '../auth/AuthContext';
import AppEmpty from '../components/AppEmpty';
import PageHeader from '../components/PageHeader';

const { Text } = Typography;

/** RAGAS 指标英文 → 中文映射（未知指标保持原名；Tooltip 显示英文原名） */
const metricLabelMap: Record<string, string> = {
  faithfulness: '忠实度',
  answer_relevancy: '答案相关性',
  context_precision: '上下文精确率',
  context_recall: '上下文召回率',
  answer_correctness: '答案正确性',
  answer_similarity: '答案相似度',
  hallucination: '幻觉率',
  noise_sensitivity: '噪声敏感度',
  coherence: '连贯性',
};

const metricLabel = (m: string): string => metricLabelMap[m] ?? m;

// RAGAS 任务状态文案/颜色
const statusConfig: Record<string, { color: string; label: string }> = {
  queued: { color: 'warning', label: '排队中' },
  pending: { color: 'default', label: '等待中' },
  running: { color: 'processing', label: '运行中' },
  completed: { color: 'success', label: '已完成' },
  failed: { color: 'error', label: '失败' },
};

const statusOf = (s: string) => statusConfig[s] || { color: 'default', label: s };

// 分数颜色（0.8 绿 / 0.5 黄 / 红）
const scoreColor = (v: number) => (v >= 0.8 ? '#52c41a' : v >= 0.5 ? '#faad14' : '#f5222d');

// RAGAS 6 个指标选项（值=英文名）；默认全部选中——手动测试集用户已填写
// 正确答案（ground_truth），6 个指标均可评分（可取消不需要的指标）
const ragasMetricOptions = [
  { label: '忠实度', value: 'faithfulness', desc: '答案是否忠于检索到的上下文，无虚构（不依赖标准答案）' },
  { label: '答案相关性', value: 'answer_relevancy', desc: '答案与问题的相关程度（不依赖标准答案）' },
  { label: '上下文精确率', value: 'context_precision', desc: '检索上下文是否包含有用信息（不依赖标准答案）' },
  { label: '上下文召回率', value: 'context_recall', desc: '检索上下文是否覆盖必要信息（需 ground_truth）' },
  { label: '答案正确性', value: 'answer_correctness', desc: '答案与参考答案的匹配程度（需 ground_truth）' },
  { label: '答案相似度', value: 'answer_similarity', desc: '答案与参考答案的语义相似度（需 ground_truth）' },
];
const DEFAULT_METRICS = ragasMetricOptions.map(o => o.value);

// 测试集表单每行的值类型
interface EvalSampleRow {
  question?: string;
  ground_truth?: string;
}

const AnalyticsPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { user } = useAuth();
  // 发起评估仅 super_admin / dept_admin（与后端权限一致）
  const isAdmin = user?.role === 'super_admin' || user?.role === 'dept_admin';
  const [stats, setStats] = useState<Stats | null>(null);
  const [ragas, setRagas] = useState<RagasStatus | null>(null);
  const [loading, setLoading] = useState(false);

  // 报告 Modal
  const [reportOpen, setReportOpen] = useState(false);
  const [reportTask, setReportTask] = useState<RagasTask | null>(null);
  const [report, setReport] = useState<RagasReport | null>(null);
  const [reportLoading, setReportLoading] = useState(false);

  // 发起评估 Modal（手动测试集）
  const [evalOpen, setEvalOpen] = useState(false);
  const [evalKbId, setEvalKbId] = useState<string | undefined>(undefined);
  const [evalMetrics, setEvalMetrics] = useState<string[]>(DEFAULT_METRICS);
  const [evalSubmitting, setEvalSubmitting] = useState(false);
  const [evalImporting, setEvalImporting] = useState(false);
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
  const ragasTableWrapRef = useRef<HTMLDivElement>(null);

  // 检索质量区块
  const [qKbs, setQKbs] = useState<KnowledgeBase[]>([]);
  const [qKbId, setQKbId] = useState<string | undefined>(undefined);
  const [quality, setQuality] = useState<RetrievalQuality | null>(null);
  const [qLoading, setQLoading] = useState(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [statsRes, ragasRes, kbsRes] = await Promise.all([
        getStats(),
        getRagasStatus(),
        listKbs(),
      ]);
      setStats(statsRes.data);
      setRagas(ragasRes.data);
      setQKbs(kbsRes.data);
    } catch {
      setStats(null);
      setRagas({ available: false, tasks: [], message: '自身统计接口异常' });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

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
      const row = ragasTableWrapRef.current?.querySelector<HTMLElement>(
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
    if (!evalKbId) {
      message.error('请先选择要评估的知识库');
      return;
    }
    setEvalImporting(true);
    try {
      const res = await previewRagasSamples({
        kb_id: evalKbId,
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
    if (!evalKbId) {
      message.error('请选择要评估的知识库');
      return;
    }
    if (evalMetrics.length === 0) {
      message.error('请至少选择一个评估指标');
      return;
    }
    let values: { samples?: EvalSampleRow[] };
    try {
      values = await evalForm.validateFields();
    } catch {
      return; // 表单校验错误已就地提示
    }
    const rows = (values.samples || [])
      .filter(r => r.question?.trim() && r.ground_truth?.trim())
      .map(r => ({ question: r.question!.trim(), ground_truth: r.ground_truth!.trim() }));
    if (rows.length === 0) {
      message.error('请至少填写一条有效测试样本（问题 + 正确答案）');
      return;
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
        kb_id: evalKbId,
        metrics: evalMetrics,
        top_k: 3,
        samples: rows,
      });
      message.success(`评估任务已创建：${res.data.name}（${res.data.sample_count} 条样本），运行中可查看进度`);
      // —— 魔法注入动画：测试集注入任务列表（0.6s）→ 新任务行高亮 + 滚动 ——
      setEvalAnim(true);              // 弹窗内测试集区域注入动画 + 魔法光晕
      setNewTaskId(res.data.task_id); // 任务列表端：新行高亮 + scrollIntoView
      await loadAll();                // 刷新任务列表（动画期间并行执行）
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
      loadAll();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '取消失败，请稍后重试');
    }
  };

  // 选择知识库 → 加载近 30 天检索质量统计
  const handleQualityKbChange = async (id: string) => {
    setQKbId(id);
    setQuality(null);
    setQLoading(true);
    try {
      const res = await getRetrievalQuality(id);
      setQuality(res.data);
    } catch {
      message.error('检索质量统计加载失败');
      setQuality(null);
    } finally {
      setQLoading(false);
    }
  };

  // 近 30 天整体命中率（有命中的检索占比，日粒度加权）
  const overallHitRate = quality && quality.total_retrievals > 0
    ? quality.daily.reduce((acc, d) => acc + d.retrievals * d.hit_rate, 0)
      / quality.total_retrievals
    : 0;

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

  const ragasColumns = [
    {
      title: '任务名称', dataIndex: 'name', key: 'name', ellipsis: true,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '知识库', dataIndex: 'kb_name', key: 'kb_name', width: 130, ellipsis: true,
      render: (v?: string) => v || <Text type="secondary">—</Text>,
    },
    {
      title: '样本来源', dataIndex: 'source', key: 'source', width: 100,
      render: (v?: string) => (v === 'chat'
        ? <Tag color="blue">会话问答</Tag>
        : v === 'logs' ? <Tag color="green">检索日志</Tag>
        : v === 'manual' ? <Tag color="purple">手动填写</Tag>
        : <Text type="secondary">—</Text>),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 110,
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
      title: '指标', dataIndex: 'metrics', key: 'metrics', width: 220,
      render: (ms: string[] | undefined) =>
        ms?.length ? ms.map(m => (
          <Tooltip key={m} title={metricLabelMap[m] ? m : undefined}>
            <Tag>{metricLabel(m)}</Tag>
          </Tooltip>
        )) : <Text type="secondary">—</Text>,
    },
    {
      title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 170,
      render: (v?: string) => v || '—',
    },
    {
      title: '完成时间', dataIndex: 'completed_at', key: 'completed_at', width: 170,
      render: (v?: string) => v || '—',
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

  // 检索质量：命中文档排行列（Top10）
  const hitDocColumns = [
    { title: '文档名', dataIndex: 'doc_name', key: 'doc_name', ellipsis: true },
    {
      title: '命中次数', dataIndex: 'hits', key: 'hits', width: 110,
      render: (v: number) => <Tag color="blue">{v}</Tag>,
    },
  ];

  // 检索质量：零命中文档列
  const zeroHitColumns = [
    { title: '文档名', dataIndex: 'doc_name', key: 'doc_name', ellipsis: true },
    {
      title: '切块数', dataIndex: 'chunks', key: 'chunks', width: 110,
      render: (v: number) => <Text type="secondary">{v}</Text>,
    },
  ];

  return (
    <div>
      <PageHeader title="统计分析" description="系统整体运行统计与 RAGAS 检索质量评估" />

      {/* 上半：自身统计 */}
      <Row gutter={[16, 16]} style={{ marginBottom: 24 }}>
        <Col xs={12} sm={6}>
          <Card size="small" className="stat-card stat-card--blue" styles={{ body: { padding: '18px 16px' } }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <Statistic
                title="知识库数量"
                value={stats?.kb_count ?? 0}
                valueStyle={{ fontSize: 24, fontWeight: 600 }}
              />
              <div className="stat-icon"><DatabaseOutlined /></div>
            </div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small" className="stat-card stat-card--green" styles={{ body: { padding: '18px 16px' } }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <Statistic
                title="文档数量"
                value={stats?.doc_count ?? 0}
                valueStyle={{ fontSize: 24, fontWeight: 600 }}
              />
              <div className="stat-icon"><FileTextOutlined /></div>
            </div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small" className="stat-card stat-card--purple" styles={{ body: { padding: '18px 16px' } }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <Statistic
                title="切块数量"
                value={stats?.chunk_count ?? 0}
                valueStyle={{ fontSize: 24, fontWeight: 600 }}
              />
              <div className="stat-icon"><PartitionOutlined /></div>
            </div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small" className="stat-card stat-card--orange" styles={{ body: { padding: '18px 16px' } }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <Statistic
                title="问答消息数"
                value={stats?.message_count ?? 0}
                valueStyle={{ fontSize: 24, fontWeight: 600 }}
              />
              <div className="stat-icon"><MessageOutlined /></div>
            </div>
          </Card>
        </Col>
      </Row>

      {/* 下半：RAGAS 评估区块 */}
      <Card
        title="RAGAS 评估系统"
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
            <Button icon={<ReloadOutlined />} onClick={loadAll} loading={loading}>刷新</Button>
          </Space>
        }
      >
        {loading ? (
          <Skeleton active paragraph={{ rows: 4 }} />
        ) : ragas?.available ? (
          <div ref={ragasTableWrapRef}>
            <Table<RagasTask>
              rowKey="id"
              size="small"
              dataSource={ragas.tasks || []}
              columns={ragasColumns}
              rowClassName={(record) => record.id === newTaskId ? 'ragas-row-highlight' : ''}
              pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 个任务` }}
              onRow={(record) => ({
                onClick: () => openReport(record),
                style: { cursor: 'pointer' },
              })}
              locale={{ emptyText: <AppEmpty title="RAGAS 已连接，但暂无评估任务" /> }}
            />
          </div>
        ) : (
          <Alert
            type="warning"
            showIcon
            message="RAGAS 评估系统不可用"
            description={
              <span>
                {ragas?.message || '无法连接 RAGAS 评估系统'}。
                系统自身统计不受影响，可在知识库管理/文档管理中继续使用；
                如需查看评估报告，请先启动 RAGAS 服务（端口 8090）。
              </span>
            }
          />
        )}
      </Card>

      {/* 检索质量区块（近 30 天检索日志：总次数/命中率/文档命中排行/零命中文档） */}
      <Card title="检索质量（近 30 天）" style={{ marginTop: 16 }}>
        <Space style={{ marginBottom: 16 }} wrap>
          <Text strong>知识库</Text>
          <Select
            value={qKbId}
            onChange={handleQualityKbChange}
            style={{ width: 260 }}
            placeholder="选择知识库"
            options={qKbs.map(k => ({ value: k.id, label: k.name }))}
            notFoundContent={<Empty description="暂无知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            基于检索日志（data/retrieval_logs）统计，保留 30 天
          </Text>
        </Space>
        {qLoading ? (
          <Skeleton active paragraph={{ rows: 6 }} />
        ) : !qKbId ? (
          <AppEmpty title="请先选择知识库" />
        ) : quality && quality.total_retrievals > 0 ? (
          <>
            {/* 概览统计 */}
            <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
              <Col xs={12} sm={6}>
                <Card size="small">
                  <Statistic title="总检索次数" value={quality.total_retrievals} />
                </Card>
              </Col>
              <Col xs={12} sm={6}>
                <Card size="small">
                  <Statistic title="平均命中 chunk 数" value={quality.avg_hits_per_retrieval} precision={2} />
                </Card>
              </Col>
              <Col xs={12} sm={6}>
                <Card size="small">
                  <Statistic
                    title="近 30 天命中率"
                    value={overallHitRate}
                    precision={2}
                    suffix="/ 1.0"
                    valueStyle={{ color: scoreColor(overallHitRate) }}
                  />
                </Card>
              </Col>
              <Col xs={12} sm={6}>
                <Card size="small">
                  <Statistic title="零命中文档数" value={quality.zero_hit_docs.length} />
                </Card>
              </Col>
            </Row>

            {/* 日粒度检索量 mini 柱状图（柱高=当日检索量，颜色=当日命中率；无检索为灰底） */}
            <Card
              size="small"
              title="每日检索量 / 命中率"
              style={{ marginBottom: 16 }}
              styles={{ body: { padding: '14px 16px 10px' } }}
            >
              <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 96 }}>
                {quality.daily.map(d => {
                  const max = Math.max(...quality.daily.map(x => x.retrievals), 1);
                  const h = d.retrievals > 0 ? Math.max(4, (d.retrievals / max) * 68) : 4;
                  const color = d.retrievals === 0 ? '#94a3b8'
                    : d.hit_rate >= 0.8 ? '#52c41a'
                    : d.hit_rate >= 0.5 ? '#faad14' : '#f5222d';
                  return (
                    <div
                      key={d.date}
                      style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3 }}
                    >
                      <Tooltip title={`${d.date}：${d.retrievals} 次检索，命中率 ${Math.round(d.hit_rate * 100)}%`}>
                        <div style={{ width: '72%', maxWidth: 18, height: h, borderRadius: 2, background: color }} />
                      </Tooltip>
                      <Text type="secondary" style={{ fontSize: 9, lineHeight: '12px', whiteSpace: 'nowrap' }}>
                        {d.date.slice(5)}
                      </Text>
                    </div>
                  );
                })}
              </div>
              <div style={{ marginTop: 8, display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  <span style={{ display: 'inline-block', width: 10, height: 10, background: '#52c41a', borderRadius: 2, marginRight: 4 }} />
                  命中率 ≥ 80%
                </Text>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  <span style={{ display: 'inline-block', width: 10, height: 10, background: '#faad14', borderRadius: 2, marginRight: 4 }} />
                  ≥ 50%
                </Text>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  <span style={{ display: 'inline-block', width: 10, height: 10, background: '#f5222d', borderRadius: 2, marginRight: 4 }} />
                  &lt; 50%
                </Text>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  <span style={{ display: 'inline-block', width: 10, height: 10, background: '#94a3b8', borderRadius: 2, marginRight: 4 }} />
                  无检索
                </Text>
              </div>
            </Card>

            {/* 命中文档排行 + 零命中文档 */}
            <Row gutter={16}>
              <Col xs={24} lg={12}>
                <Card size="small" title="Top 10 命中文档排行" styles={{ body: { padding: 0 } }}>
                  <Table<RetrievalHitDoc>
                    rowKey="doc_id"
                    size="small"
                    dataSource={quality.hit_docs}
                    columns={hitDocColumns}
                    pagination={false}
                    locale={{ emptyText: '暂无命中文档' }}
                  />
                </Card>
              </Col>
              <Col xs={24} lg={12}>
                <Card size="small" title="零命中文档" styles={{ body: { padding: 0 } }}>
                  <Table<RetrievalZeroHitDoc>
                    rowKey="doc_id"
                    size="small"
                    dataSource={quality.zero_hit_docs}
                    columns={zeroHitColumns}
                    pagination={false}
                    locale={{ emptyText: '无（所有已入库文档均被命中过）' }}
                  />
                  {quality.zero_hit_docs.length > 0 ? (
                    <Alert
                      style={{ margin: 12 }}
                      type="warning"
                      showIcon
                      message="该文档从未被检索命中，建议检查切块质量或内容相关性"
                    />
                  ) : null}
                </Card>
              </Col>
            </Row>
          </>
        ) : (
          <AppEmpty title="暂无检索记录" description="请先在聊天或检索测试页发起检索" />
        )}
      </Card>

      {/* 发起评估 Modal（手动测试集：问题 + 正确答案） */}
      <Modal
        title="发起 RAGAS 评估"
        open={evalOpen}
        onCancel={() => { if (evalAnim) return; setEvalOpen(false); }} // 注入动画期间禁止手动关闭
        onOk={handleStartEvaluation}
        okText="发起评估"
        confirmLoading={evalSubmitting}
        okButtonProps={{ disabled: evalValidCount === 0 || evalAnim }} // 无测试数据/注入动画中不可发起
        width={760}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={16}>
          <div>
            <Text strong>知识库</Text>
            <Select
              style={{ width: '100%', marginTop: 4 }}
              placeholder="选择要评估的知识库"
              value={evalKbId}
              onChange={setEvalKbId}
              options={qKbs.map(k => ({ value: k.id, label: `${k.name}（${k.chunk_count} 个切块）` }))}
              notFoundContent={<Empty description="暂无可用知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            />
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
          <div className={`eval-inject-wrap${evalAnim ? ' eval-inject-anim' : ''}`}>
            {/* 魔法注入光晕（纯 CSS 径向渐变扩散，aria-hidden 不干扰读屏） */}
            {evalAnim ? <div className="eval-glow" aria-hidden="true" /> : null}
            <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 8 }}>
              <Text strong>测试集（问题 + 正确答案）</Text>
              <Space>
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
      </Modal>

      {/* 测试集导入/导出 Modal（txt / Excel 格式选择；导出下载文件，导入文件/文本回填） */}
      <Modal
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
      </Modal>

      {/* 从聊天历史导入选择 Modal：勾选想要的预览样本 → 确定后追加到测试集 */}
      <Modal
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
      </Modal>

      {/* 任务报告 Modal */}
      <Modal
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
      </Modal>
    </div>
  );
};

export default AnalyticsPage;
