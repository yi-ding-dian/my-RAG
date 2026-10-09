/**
 * RAGAS 评测详情页（/analytics/ragas）：统计分析概览页点击「RAGAS 评测」摘要卡下钻。
 * - 完整评估任务表（发起/取消/查看报告/导入导出测试集全部能力，从原 Analytics 单页搬入）
 * - 运行中任务 8s 轮询状态（原逻辑搬入）；分页固定左下 + 表格内部滚动（统一布局规范）
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Breadcrumb } from 'antd';
import { useNavigate } from 'react-router-dom';
import AppModal from '../../../shared/components/common/AppModal';
import {
  App as AntApp, Card, Col, Row, Table, Typography, Alert,
  Space, Tooltip, Button, Empty, Select, Checkbox, Form, Input,
  Radio, Skeleton,
} from 'antd';
import {
  DatabaseOutlined, DeleteOutlined,
  FileTextOutlined, ImportOutlined, PlusOutlined, ReloadOutlined,
  SaveOutlined,
  PlayCircleOutlined,
} from '@ant-design/icons';
import {
  getRagasStatus,
  listKbs, startRagasEvaluation, previewRagasSamples, ragasPrecheck,
  listRagasDatasets, getLlmModelList,
  KnowledgeBase, RagasStatus, RagasTask, RagasSampleInput,
  RagasDataset, ParserLlmModelItem,
} from '../../../shared/api/client';
import { useAuth } from '../../../shared/auth/AuthContext';
import AppEmpty from '../../../shared/components/common/AppEmpty';
import PageLayout from '../../../shared/components/layout/PageLayout';
import TableSectionLayout from '../../../shared/components/layout/TableSectionLayout';
import ResizableTitle from '../../../shared/components/common/ResizableTitle';
import {
  ragasMetricOptions, DEFAULT_METRICS, EvalSampleRow,
} from './ragas-shared';
import { useRagasColumns } from './useRagasColumns';
import { RagasReportModal } from './components/RagasReportModal';
import { GenerateQuestionsModal } from './components/GenerateQuestionsModal';
import { NewDatasetModal } from './components/NewDatasetModal';
import { ChatSamplePickerModal } from './components/ChatSamplePickerModal';
import { DatasetManagerModal } from './components/DatasetManagerModal';
import { SampleTextImportModal } from './components/SampleTextImportModal';
import { SaveAsDatasetModal } from './components/SaveAsDatasetModal';

const { Text } = Typography;

const AnalyticsRagasDetailPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { user } = useAuth();
  const navigate = useNavigate();
  // 发起评估仅 super_admin / dept_admin（与后端权限一致）
  const isAdmin = user?.role === 'super_admin' || user?.role === 'dept_admin';
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
  // 「新建评估集」（评估集管理弹窗入口）：不经过发起评估，直接建题集
  const [newDsOpen, setNewDsOpen] = useState(false);
  // AI 出题（合成测试集）：生成的是**草稿**，审核通过才存成评估集
  const [genOpen, setGenOpen] = useState(false);
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
  // 打开那一刻的测试集快照（供弹窗带出文本 / 导出用）。
  // **必须存快照，不能写成 samples={evalValidRows()}**：后者在渲染期访问 evalForm，
  // 而表单只在「发起评估」弹窗内挂载——弹窗关着时 antd 会报
  // "Instance created by useForm is not connected to any Form element"
  const [textModalSamples, setTextModalSamples] = useState<EvalSampleRow[]>([]);
  // 从聊天历史导入选择 Modal（勾选预览样本后追加）
  const [chatPickOpen, setChatPickOpen] = useState(false);
  const [chatPickList, setChatPickList] = useState<RagasSampleInput[]>([]);
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
      setChatPickOpen(true);  // 默认全选由 ChatSamplePickerModal 打开时处理
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '导入失败');
    } finally {
      setEvalImporting(false);
    }
  };

  // 选择弹窗确定：勾选的样本追加到当前测试集（相同问题不重复添加）
  const handleChatPickConfirm = (selectedQuestions: string[]) => {
    const selected = new Set(selectedQuestions);
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

  // 打开测试集导入/导出 Modal：先把当前测试集存成快照
  const openTextModal = () => {
    setTextModalSamples(evalValidRows());
    setTextModalOpen(true);
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

  // 打开任务报告（报告数据由弹窗自己拉，这里只管"开哪个任务的"）
  const openReport = (task: RagasTask) => {
    setReportTask(task);
    setReportOpen(true);
  };
  const { columns: ragasColumns, tableWidth } = useRagasColumns({
    tasks,
    onOpenReport: openReport,
    onRefresh: loadRagas,
  });

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
          {/* 评估集管理提到顶部：原先只藏在「发起评估」弹窗里，不进弹窗够不着，
              而"管题集"和"跑评估"是两件事，不该绑在一起 */}
          {isAdmin ? (
            <Button
              icon={<DatabaseOutlined />}
              onClick={() => setDatasetMgrOpen(true)}
            >
              评估集管理
            </Button>
          ) : null}
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

      <SampleTextImportModal
        open={textModalOpen}
        samples={textModalSamples}
        onClose={() => setTextModalOpen(false)}
        onImport={rows => evalForm.setFieldsValue({ samples: rows })}
      />

      <ChatSamplePickerModal
        open={chatPickOpen}
        list={chatPickList}
        onClose={() => setChatPickOpen(false)}
        onPick={handleChatPickConfirm}
      />

      <DatasetManagerModal
        open={datasetMgrOpen}
        datasets={evalDatasets}
        onClose={() => setDatasetMgrOpen(false)}
        onDeleted={id => {
          if (evalDatasetId === id) setEvalDatasetId(undefined);
          setEvalDatasets(prev => prev.filter(x => x.id !== id));
        }}
        onUse={d => {
          setEvalDatasetId(d.id);
          setEvalKbIds([d.kb_id]);
          setDatasetMgrOpen(false);
          setEvalOpen(true);
        }}
        onOpenGenerate={() => setGenOpen(true)}
        onOpenNewDataset={() => setNewDsOpen(true)}
      />

      <NewDatasetModal
        open={newDsOpen}
        kbs={kbs}
        onClose={() => setNewDsOpen(false)}
        onCreated={ds => setEvalDatasets(prev => [ds, ...prev])}
      />

      <GenerateQuestionsModal
        open={genOpen}
        kbs={kbs}
        onClose={() => setGenOpen(false)}
        onCreated={ds => setEvalDatasets(prev => [ds, ...prev])}
      />

      <SaveAsDatasetModal
        open={saveDsOpen}
        kbIds={evalKbIds}
        getSamples={async () => {
          const values = await evalForm.validateFields();
          return (values.samples || [])
            .filter(r => r.question?.trim() && r.ground_truth?.trim())
            .map(r => ({ question: r.question!.trim(), ground_truth: r.ground_truth!.trim() }));
        }}
        onClose={() => setSaveDsOpen(false)}
        onCreated={ds => setEvalDatasets(prev => [ds, ...prev])}
      />

      <RagasReportModal
        open={reportOpen}
        task={reportTask}
        onClose={() => setReportOpen(false)}
      />
    </PageLayout>
  );
};

export default AnalyticsRagasDetailPage;
