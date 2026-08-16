import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Breadcrumb,
  Button,
  Card,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Skeleton,
  Space,
  Spin,
  Steps,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
  theme,
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  GlobalOutlined,
  InboxOutlined,
  ReloadOutlined,
  RestOutlined,
  RollbackOutlined,
  StopOutlined,
  ThunderboltOutlined,
  ApartmentOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  DocumentDetail,
  DocumentItem,
  DocumentStatus,
  KnowledgeBase,
  KnowledgeGraph,
  ParserLlmModelItem,
  buildDocumentGraph,
  cancelDocumentGraphBuild,
  deleteDocument,
  emptyTrash,
  getDocument,
  getKnowledgeGraph,
  getLlmModelList,
  ingestDocument,
  listDocuments,
  listKbs,
  listTrashDocuments,
  methodColor,
  methodLabel,
  purgeDocument,
  restoreDocument,
  testLlmModelByName,
  uploadDocument,
} from '../api/client';
import AppEmpty from '../components/AppEmpty';
import DocumentPreviewModal from '../components/DocumentPreviewModal';
import PageHeader from '../components/PageHeader';
import ParseConfigModal from '../components/ParseConfigModal';
import ChunkCompareView from '../components/ChunkCompareView';
import KnowledgeGraphTab from '../components/KnowledgeGraphTab';
import RenameDocumentModal from '../components/RenameDocumentModal';
import UrlImportModal from '../components/UrlImportModal';
import { useAuth } from '../auth/AuthContext';

const { Dragger } = Upload;
const { Text } = Typography;

const statusMeta: Record<DocumentStatus, { color: string; text: string }> = {
  uploaded: { color: 'default', text: '待解析' },
  parsing: { color: 'processing', text: '解析中' },
  parsed: { color: 'warning', text: '已解析' },
  ingested: { color: 'success', text: '已入库' },
  failed: { color: 'error', text: '失败' },
};

// 可触发解析的状态：待解析/已解析/失败/已入库（已入库=重新解析）
const parseableStatuses: DocumentStatus[] = ['uploaded', 'parsed', 'failed', 'ingested'];

/**
 * 文档状态筛选（M3 语义统一）：「未入库」= uploaded（待解析）+ parsed（已解析）
 * 两态，两者均可触发入库解析。筛选 value 用 unparsed（与 B 批后端契约对齐：
 * 后端接受 status=unparsed 映射两态，先过滤后分页）。
 */
type StatusFilter = 'all' | 'unparsed' | 'parsing' | 'ingested' | 'failed';
const statusFilterOptions: { label: string; value: StatusFilter }[] = [
  { label: '全部', value: 'all' },
  { label: '未入库', value: 'unparsed' },
  { label: '解析中', value: 'parsing' },
  { label: '已入库', value: 'ingested' },
  { label: '失败', value: 'failed' },
];

/** 前端筛选 value → 后端 status 参数：all 不传（=全部），其余原样透传 */
const toBackendStatus = (filter: StatusFilter): string | undefined =>
  filter === 'all' ? undefined : filter;

const formatSize = (bytes: number) => {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
};

/** 解析参数摘要（供解析方式列 Tooltip 展示） */
const parserConfigSummary = (
  config: Record<string, unknown> | undefined,
  method: string,
): string | null => {
  if (!config) return null;
  const parts: string[] = [];
  if (config.chunk_size != null) parts.push(`块大小 ${config.chunk_size}`);
  if (config.overlap != null) parts.push(`重叠 ${config.overlap}`);
  if (method === 'title' && config.split_level != null) parts.push(`标题层级 H${config.split_level}`);
  if (method === 'regex' && config.regex_pattern) parts.push(`正则 ${String(config.regex_pattern)}`);
  return parts.length > 0 ? parts.join(' / ') : null;
};

const DocumentsPage: React.FC = () => {
  const { message, modal } = AntApp.useApp();
  const { token } = theme.useToken();
  const { user } = useAuth();
  const navigate = useNavigate();
  // 普通用户仅问答：上传/解析/删除仅 dept_admin 与 super_admin 可用
  const canManage = user?.role !== 'user';

  const [searchParams] = useSearchParams();
  const urlKbId = searchParams.get('kb_id') ?? undefined;

  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [kbId, setKbId] = useState<string | undefined>();
  const [docs, setDocs] = useState<DocumentItem[]>([]);
  const [loading, setLoading] = useState(false);
  // P2-10 服务端分页：当前页码/每页条数/总数（后端返回 total，Table 翻页重新请求）
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [total, setTotal] = useState(0);
  // 批量上传状态：uploading=本批进行中；total/done=第 done+1 个；current=当前文件名
  const [uploadState, setUploadState] = useState<{
    uploading: boolean;
    total: number;
    done: number;
    current: string;
  }>({ uploading: false, total: 0, done: 0, current: '' });
  // multiple 拖拽时 antd 逐个回调 beforeUpload，先聚合成批再统一并发上传
  const pendingFilesRef = useRef<File[]>([]);
  const uploadTimerRef = useRef<number | null>(null);
  // 批量解析状态
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchParsing, setBatchParsing] = useState(false);
  const [parseProgress, setParseProgress] = useState<{ done: number; total: number } | null>(null);

  const [detail, setDetail] = useState<DocumentItem | null>(null);
  const [detailData, setDetailData] = useState<DocumentDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailFullscreen, setDetailFullscreen] = useState(false);
  // 知识图谱 Tab 数据（null=未启用/加载失败/接口 404）
  const [graphData, setGraphData] = useState<KnowledgeGraph | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);

  // 解析配置弹窗
  const [parseDoc, setParseDoc] = useState<DocumentItem | null>(null);
  // QA 规范性检测失败确认框：已提示过的 doc_id 集合（防列表轮询重复弹窗；
  // 用户重新发起解析时清除该条目，再次失败可再次提示）
  const qaPromptedRef = useRef<Set<string>>(new Set());
  // Agentic 分块超限提示：已提示过的 doc_id 集合（防列表轮询重复弹窗；
  // 重新发起解析时清除该条目，再次失败可再次提示）
  const agenticPromptedRef = useRef<Set<string>>(new Set());
  // 重命名弹窗（当前待重命名文档）
  const [renameDoc, setRenameDoc] = useState<DocumentItem | null>(null);
  // URL 网页导入弹窗
  const [urlImportOpen, setUrlImportOpen] = useState(false);
  // 在线预览弹窗（当前待预览文档）
  const [previewDoc, setPreviewDoc] = useState<DocumentItem | null>(null);
  // 回收站视图（软删除文档列表）
  const [trashView, setTrashView] = useState(false);
  const [trashDocs, setTrashDocs] = useState<DocumentItem[]>([]);
  const [trashLoading, setTrashLoading] = useState(false);
  // 状态筛选：全部/待解析/解析中/已入库/失败（前端过滤）
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  const kbName = kbs.find(k => k.id === kbId)?.name;

  // 面包屑：知识库 / {kbName} / 文档管理（点击「知识库」返回知识库管理页）
  const breadcrumbItems = useMemo(() => {
    const items: { title: React.ReactNode }[] = [
      { title: <a onClick={() => navigate('/kbs')}>知识库</a> },
    ];
    if (kbName) items.push({ title: kbName });
    items.push({ title: '文档管理' });
    return items;
  }, [kbName, navigate]);

  // B2: 状态筛选已下沉后端（listDocuments 传 status），docs 即服务端过滤结果，
  // 不再本地 filter（避免"筛选只作用于当前页"的误导）
  // M3: unparsed 透传后端（status=unparsed 映射 uploaded+parsed 两态，
  // 服务端先过滤后分页，total 为过滤后数量）；此处过滤为幂等兜底
  // （docs 已是服务端 unparsed 结果，再滤一次结果一致）
  const visibleDocs = useMemo(() => {
    if (statusFilter === 'unparsed') {
      return docs.filter(d => d.status === 'uploaded' || d.status === 'parsed');
    }
    return docs;
  }, [docs, statusFilter]);

  // 待解析文档数（仅"全部"筛选下有意义，用于列表上方引导提示）
  const unparsedCount =
    statusFilter === 'all'
      ? docs.filter(d => d.status === 'uploaded' || d.status === 'parsed').length
      : 0;

  // ---------- 数据加载 ----------
  const loadKbs = useCallback(async () => {
    try {
      const res = await listKbs();
      setKbs(res.data);
      if (res.data.length > 0) {
        setKbId(prev => {
          // URL 带 kb_id 时优先作为初始选中
          if (urlKbId && res.data.some(k => k.id === urlKbId)) return urlKbId;
          return prev && res.data.some(k => k.id === prev) ? prev : res.data[0].id;
        });
      } else {
        setKbId(undefined);
      }
    } catch {
      message.error('加载知识库列表失败');
    }
  }, [message, urlKbId]);

  const load = useCallback(
    async (silent = false, p = 1, ps = 10) => {
      if (!kbId) {
        setDocs([]);
        setTotal(0);
        return;
      }
      if (!silent) setLoading(true);
      try {
        // P2-10 服务端分页：传参返回 {total, page, page_size, items}；
        // 兼容后端未分页的旧响应（裸数组）
        // B2: 状态筛选下沉后端（先过滤后分页，total 为过滤后数量），
        // 不再本地 filter 当前页数据；unparsed 由后端映射 uploaded+parsed 两态
        const res = await listDocuments(kbId, {
          page: p,
          page_size: ps,
          status: toBackendStatus(statusFilter),
        });
        const data = res.data;
        if (Array.isArray(data)) {
          setDocs(data);
          setTotal(data.length);
        } else {
          setDocs(data.items);
          setTotal(data.total);
        }
      } catch {
        if (!silent) message.error('加载文档列表失败');
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [kbId, message, statusFilter],
  );

  /** B2: 删除/解析/上传等变更操作后刷新：回第 1 页重拉（避免页码显示旧值错位） */
  const reloadFirstPage = useCallback(
    async (silent = false) => {
      setPage(1);
      await load(silent, 1, pageSize);
    },
    [load, pageSize],
  );

  useEffect(() => {
    loadKbs();
  }, [loadKbs]);

  // URL kb_id 参数变化时同步更新选中（如从知识库管理页跳转进入）
  useEffect(() => {
    if (urlKbId) setKbId(urlKbId);
  }, [urlKbId]);

  useEffect(() => {
    // 切换知识库时重置页码回第 1 页（避免 Table 显示旧页码而数据是新库第 1 页）
    setPage(1);
    void load(false, 1, pageSize);
  }, [kbId, load, pageSize]);

  // 状态轮询：存在 parsing（入库中）或 graph_status=building（图谱构建中）时
  // 每 2s 静默刷新，全终态自动停止（uploaded=待解析不会自动流转，无需轮询）
  useEffect(() => {
    if (!kbId) return;
    const hasPending = docs.some(
      d => d.status === 'parsing' || d.graph_status === 'building',
    );
    if (!hasPending) return;
    const timer = window.setInterval(() => {
      // P2-10: 轮询保持当前分页，避免覆盖用户所在页数据
      load(true, page, pageSize);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [kbId, docs, load, page, pageSize]);

  // QA 问答规范性检测失败确认：qa 方式入库时后端检测问答对占比（问答对/总
  // 段落）<50% → 任务失败，error 带检测详情（"问答对占比 x%（y 对 / z 段）"）；
  // 此处解析失败信息（现有错误传递机制：doc.status=failed + doc.error），
  // 弹确认框询问是否强制继续入库：确认 → 带 qa_force_continue=true 重新提交
  // （qa 方式，后端跳过规范性检测），取消 → 仅本次提示
  useEffect(() => {
    if (!kbId) return;
    const qaFailRe = /问答对占比\s*([\d.]+)%\s*（\s*(\d+)\s*对\s*\/\s*(\d+)\s*段）/;
    for (const doc of docs) {
      if (doc.status !== 'failed' || !doc.error) continue;
      const m = doc.error.match(qaFailRe);
      if (!m || qaPromptedRef.current.has(doc.id)) continue;
      // 先标记再弹窗，避免 docs 重复变化导致连弹
      qaPromptedRef.current.add(doc.id);
      modal.confirm({
        title: 'QA 问答格式检测未通过',
        content: (
          <span>
            该文档 QA 问答对占比 {m[1]}%（共 {m[2]} 对 / {m[3]} 段），不符合 QA
            文档规范（问答对占比 ≥50%），是否继续入库？
          </span>
        ),
        okText: '继续入库',
        cancelText: '取消',
        onOk: async () => {
          try {
            await ingestDocument(kbId!, doc.id, { method: 'qa', qa_force_continue: true });
            message.success(`已按 QA 问答方式重新提交「${doc.original_name}」入库`);
            void reloadFirstPage();
          } catch (e: any) {
            message.error(e.response?.data?.detail || '重新提交入库失败');
          }
        },
      });
    }
  }, [docs, kbId, modal, message, reloadFirstPage]);

  // Agentic 分块超限提示：Agentic 方式入库时后端校验解析文本 >1 万字 →
  // 任务失败，error 带"超过 1 万字"提示（成本太高，提交前无法预知文本
  // 长度，由后端校验后经 doc.error 传递）；此处弹错误提示引导换切块方式
  useEffect(() => {
    if (!kbId) return;
    for (const doc of docs) {
      if (doc.status !== 'failed' || !doc.error) continue;
      if (!doc.error.includes('超过 1 万字') || agenticPromptedRef.current.has(doc.id)) continue;
      agenticPromptedRef.current.add(doc.id);
      message.error(
        `「${doc.original_name}」文档超过 1 万字，Agentic 分块成本太高，请换用其他切块方式`,
      );
    }
  }, [docs, kbId, message]);

  // ---------- 操作 ----------

  /** 并发池：以 concurrency 上限执行 fn（fn 内部已捕获异常，不会中断整池） */
  const runPool = async <T,>(
    items: T[],
    concurrency: number,
    fn: (item: T) => Promise<void>,
  ) => {
    const queue = [...items];
    const workers = Array.from(
      { length: Math.min(concurrency, queue.length) },
      async () => {
        while (queue.length > 0) {
          const item = queue.shift()!;
          await fn(item);
        }
      },
    );
    await Promise.all(workers);
  };

  const flushUploads = async () => {
    if (uploadTimerRef.current) {
      window.clearTimeout(uploadTimerRef.current);
      uploadTimerRef.current = null;
    }
    const files = pendingFilesRef.current.splice(0);
    if (files.length === 0) return;
    if (!kbId) {
      // 上传期间切换了知识库：丢弃滞留文件，避免传到错误的知识库
      pendingFilesRef.current = [];
      return;
    }
    setUploadState({ uploading: true, total: files.length, done: 0, current: '' });
    const failed: string[] = [];
    await runPool(files, 3, async file => {
      setUploadState(s => ({ ...s, current: file.name }));
      try {
        await uploadDocument(kbId!, file);
      } catch (e: any) {
        failed.push(`${file.name}（${e.response?.data?.detail || '上传失败'}）`);
      }
      setUploadState(s => ({ ...s, done: s.done + 1 }));
    });
    setUploadState(s => ({ ...s, uploading: false, current: '' }));
    const ok = files.length - failed.length;
    if (failed.length === 0) {
      // 上传只上传不解析：由用户在文档列表手动选择解析方式后触发
      message.success(`批量上传完成：成功 ${ok} 个，请选择解析方式后点击解析`);
    } else {
      message.warning(`上传完成：成功 ${ok} 个，失败 ${failed.length} 个：${failed.join('、')}`);
    }
    // 上传后回第 1 页（新文档在列表顶部，避免停留在旧页码看不到新内容）
    await reloadFirstPage();
  };

  const handleUpload = (file: File) => {
    if (!kbId) {
      message.warning('请先选择知识库');
      return false;
    }
    // L4: 拖拽/点击上传的类型预校验（Dragger accept 仅过滤文件选择器，拖拽不拦截）
    const dot = file.name.lastIndexOf('.');
    const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : '';
    if (!['.txt', '.md', '.pdf', '.docx'].includes(ext)) {
      message.warning(`不支持的文件类型：${file.name}（仅支持 .txt/.md/.pdf/.docx）`);
      return false;
    }
    // multiple 时 antd 逐个回调 beforeUpload，先聚合成批（30ms 窗口）再统一并发上传
    pendingFilesRef.current.push(file);
    if (!uploadTimerRef.current) {
      uploadTimerRef.current = window.setTimeout(() => {
        void flushUploads();
      }, 30);
    }
    return false;
  };

  // ---------- 批量解析 ----------

  const handleBatchParse = () => {
    if (!kbId) return;
    const targets = docs.filter(
      d => selectedRowKeys.includes(d.id) && parseableStatuses.includes(d.status),
    );
    const skipped = selectedRowKeys.length - targets.length;
    if (targets.length === 0) {
      message.warning('请先勾选可解析的文档（待解析/已解析/失败/已入库）');
      return;
    }
    const hasIngested = targets.some(d => d.status === 'ingested');
    modal.confirm({
      title: hasIngested ? '批量解析（含重新解析）' : '批量解析',
      content: (
        <span>
          将对 {targets.length} 个文档发起解析
          {skipped > 0 ? `，自动跳过不可解析的 ${skipped} 个（解析中）` : ''}
          {hasIngested ? '，其中已入库文档将清除旧切块重新解析' : ''}
          。解析方式沿用各文档已有配置，无配置的用默认。
        </span>
      ),
      okText: '确认',
      cancelText: '取消',
      onOk: () => runBatchParse(targets),
    });
  };

  const runBatchParse = async (targets: DocumentItem[]) => {
    if (!kbId) return;
    setBatchParsing(true);
    setParseProgress({ done: 0, total: targets.length });
    const failed: string[] = [];
    for (let i = 0; i < targets.length; i++) {
      const doc = targets[i];
      // 批量重新解析（可能沿用已入库的 qa 配置）：清除 QA 失败提示记录，
      // 再次失败可再次弹确认框；Agentic 超限提示记录同清（再次失败可再提示）
      qaPromptedRef.current.delete(doc.id);
      agenticPromptedRef.current.delete(doc.id);
      try {
        // 不传配置 = 按文档已有 parser_config 解析，无配置的用后端默认
        await ingestDocument(kbId, doc.id);
      } catch (e: any) {
        failed.push(`${doc.original_name}（${e.response?.data?.detail || '解析失败'}）`);
      }
      setParseProgress({ done: i + 1, total: targets.length });
    }
    setBatchParsing(false);
    setParseProgress(null);
    setSelectedRowKeys([]);
    const ok = targets.length - failed.length;
    if (failed.length === 0) {
      message.success(`批量解析已发起：成功 ${ok} 个，后台解析中，列表将自动刷新`);
    } else {
      message.warning(`批量解析完成：成功 ${ok} 个，失败 ${failed.length} 个：${failed.join('、')}`);
    }
    await reloadFirstPage();
  };

  // 组件卸载时清理批量上传聚合定时器
  useEffect(() => {
    return () => {
      if (uploadTimerRef.current) window.clearTimeout(uploadTimerRef.current);
    };
  }, []);

  const handleDelete = async (doc: DocumentItem) => {
    try {
      await deleteDocument(kbId!, doc.id);
      message.success('已移入回收站');
      await reloadFirstPage();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '删除失败');
    }
  };

  // ---------- 图谱补建/重建（LLM 模型选择弹窗） ----------

  // 图谱构建弹窗状态：当前文档 / 模型列表 / 激活索引 / 选中模型 /
  // 连接测试中 / 已通过连接测试（通过才可确认构建）/ 确认构建中
  const [graphDoc, setGraphDoc] = useState<DocumentItem | null>(null);
  const [graphModels, setGraphModels] = useState<ParserLlmModelItem[]>([]);
  const [graphActiveIdx, setGraphActiveIdx] = useState(0);
  const [graphSelected, setGraphSelected] = useState<string>();
  const [graphTesting, setGraphTesting] = useState(false);
  const [graphTestedOk, setGraphTestedOk] = useState(false);
  const [graphConfirmLoading, setGraphConfirmLoading] = useState(false);

  /** 测试指定模型连接（通过 → 可确认构建；失败 → 提示重选，确认保持禁用） */
  const testGraphModel = useCallback(
    async (name: string) => {
      setGraphTesting(true);
      setGraphTestedOk(false);
      try {
        const res = await testLlmModelByName(name);
        if (res.data.ok) {
          message.success(`「${name}」连接正常（${res.data.latency_ms}ms）`);
          setGraphTestedOk(true);
        } else {
          message.error(`模型连接失败：${res.data.reason}，请重新选择`);
        }
      } catch (e: any) {
        message.error(
          `模型连接失败：${e.response?.data?.detail || '网络请求失败'}，请重新选择`,
        );
      } finally {
        setGraphTesting(false);
      }
    },
    [message],
  );

  /** 打开图谱构建弹窗：加载模型列表，默认文档已配置模型（标"当前使用"），
   * 否则默认当前激活模型；默认选中项同样先测连接，通过才可确认 */
  const openGraphModal = async (doc: DocumentItem) => {
    setGraphDoc(doc);
    setGraphSelected(undefined);
    setGraphTestedOk(false);
    setGraphTesting(true);
    try {
      const res = await getLlmModelList();
      const models = res.data.models ?? [];
      const activeIdx = res.data.active ?? 0;
      setGraphModels(models);
      setGraphActiveIdx(activeIdx);
      const docCfg = doc.parser_config?.parse_llm_model;
      const initial =
        typeof docCfg === 'string' && docCfg && models.some(m => m.name === docCfg)
          ? docCfg
          : models[activeIdx]?.name;
      setGraphSelected(initial);
      if (initial) {
        await testGraphModel(initial);
      }
    } catch {
      message.error('加载 LLM 模型列表失败');
    } finally {
      setGraphTesting(false);
    }
  };

  /** 切换模型：先测连接，通过才标记可确认（失败保持选中但确认禁用） */
  const handleGraphModelChange = (name: string) => {
    setGraphSelected(name);
    void testGraphModel(name);
  };

  /** 确认构建：以所选模型触发补建/重建（本次构建生效，不写回文档配置） */
  const handleGraphBuildConfirm = async () => {
    if (!kbId || !graphDoc) return;
    const isRebuild =
      graphDoc.graph_status === 'ready' || graphDoc.graph_status === 'failed';
    setGraphConfirmLoading(true);
    try {
      await buildDocumentGraph(kbId, graphDoc.id, { llm_model: graphSelected });
      message.success(`图谱${isRebuild ? '重建' : '补建'}任务已启动，完成后列表自动刷新`);
      setGraphDoc(null);
      void reloadFirstPage();
    } catch (e: any) {
      message.error(e.response?.data?.detail || `图谱${isRebuild ? '重建' : '补建'}失败`);
    } finally {
      setGraphConfirmLoading(false);
    }
  };

  /** 中断进行中的图谱构建（任务停止、恢复构建前状态、旧图谱保留） */
  const handleCancelGraphBuild = async (doc: DocumentItem) => {
    try {
      await cancelDocumentGraphBuild(kbId!, doc.id);
      message.success('已发送中断请求，列表将自动刷新');
      void reloadFirstPage();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '中断请求失败');
    }
  };

  // ---------- 回收站 ----------

  const loadTrash = useCallback(
    async (silent = false) => {
      if (!kbId) {
        setTrashDocs([]);
        return;
      }
      if (!silent) setTrashLoading(true);
      try {
        // 回收站不传分页参数，后端返回全量数组；类型兼容分页响应做收窄
        const res = await listTrashDocuments(kbId);
        const data = res.data;
        setTrashDocs(Array.isArray(data) ? data : data.items);
      } catch {
        if (!silent) message.error('加载回收站失败');
      } finally {
        if (!silent) setTrashLoading(false);
      }
    },
    [kbId, message],
  );

  const openTrash = () => {
    setTrashView(true);
    void loadTrash();
  };

  const handleRestore = async (doc: DocumentItem) => {
    try {
      await restoreDocument(kbId!, doc.id);
      message.success('文档已恢复');
      await loadTrash(true);
      await reloadFirstPage(true);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '恢复失败');
    }
  };

  const handlePurge = async (doc: DocumentItem) => {
    try {
      await purgeDocument(kbId!, doc.id);
      message.success('文档已彻底删除');
      await loadTrash(true);
      await reloadFirstPage(true);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '删除失败');
    }
  };

  const handleEmptyTrash = async () => {
    try {
      const res = await emptyTrash(kbId!);
      message.success(res.data?.message ?? '回收站已清空');
      await loadTrash(true);
      await reloadFirstPage(true);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '清空回收站失败');
    }
  };

  const handleDetail = async (doc: DocumentItem) => {
    setDetail(doc);
    setDetailData(null);
    setDetailLoading(true);
    try {
      const res = await getDocument(kbId!, doc.id);
      setDetailData(res.data);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '加载详情失败');
    } finally {
      setDetailLoading(false);
    }
    // 知识图谱 Tab 数据（按当前文档过滤；未启用 → 404 → graph=null 显示空状态引导）
    setGraphData(null);
    setGraphLoading(true);
    try {
      const res = await getKnowledgeGraph(kbId!, doc.id);
      setGraphData(res.data);
    } catch {
      setGraphData(null); // 404（该知识库暂无知识图谱）/ 网络错误 → 空状态
    } finally {
      setGraphLoading(false);
    }
  };

  const columns: ColumnsType<DocumentItem> = [
    {
      title: '文件名',
      dataIndex: 'original_name',
      key: 'name',
      ellipsis: true,
      width: 260,
      // 点击文件名即可打开预览弹窗（替代原「文档预览」按钮）
      render: (v: string, row) => (
        <Typography.Link onClick={() => setPreviewDoc(row)} title={v}>
          {v}
        </Typography.Link>
      ),
    },
    {
      title: '类型',
      dataIndex: 'file_type',
      key: 'file_type',
      width: 80,
      // URL 网页导入的文档 file_type 为 "url"，展示为"网页"
      render: (v: string) => <Tag>{v === 'url' ? '网页' : v || '-'}</Tag>,
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 100,
      // 真实后端列表接口暂未返回 size 字段时显示 '-'
      render: (v?: number) => (v == null ? '-' : formatSize(v)),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: DocumentStatus, row) => {
        const meta = statusMeta[status] ?? { color: 'default', text: status };
        const tag =
          status === 'parsing' ? (
            <Tag color={meta.color} icon={<Spin size="small" />}>
              {meta.text}
            </Tag>
          ) : (
            <Tag color={meta.color}>{meta.text}</Tag>
          );
        return status === 'failed' && row.error ? <Tooltip title={row.error}>{tag}</Tooltip> : tag;
      },
    },
    {
      title: '切块数',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 90,
      render: (v: number, row) => (row.status === 'ingested' ? v : '-'),
    },
    {
      title: '解析方式',
      dataIndex: 'parser_id',
      key: 'parser_id',
      width: 200,
      render: (v: string | undefined, row) => {
        if (!v) return <Text type="secondary">-</Text>;
        const tag = <Tag color={methodColor(v)}>{methodLabel(v)}</Tag>;
        const summary = parserConfigSummary(row.parser_config, v);
        const parseTag = summary ? <Tooltip title={summary}>{tag}</Tooltip> : tag;
        // 已构建知识图谱的文档：解析方式后追加紫色"图谱"标签（tooltip 说明）
        const graphTag =
          row.graph_status === 'ready' ? (
            <Tooltip title="已构建知识图谱">
              <Tag color="purple">图谱</Tag>
            </Tooltip>
          ) : null;
        return (
          <Space size={4}>
            {parseTag}
            {graphTag}
          </Space>
        );
      },
    },
    {
      title: '上传时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 400,
      render: (_, row) => (
        <Space size="small">
          {canManage && parseableStatuses.includes(row.status) &&
            (row.status === 'ingested' ? (
              <Popconfirm
                key="reparse"
                title={`重新解析「${row.original_name}」？`}
                description="将清除旧切块重新解析"
                onConfirm={() => {
                  // 重新发起解析：清除 QA 失败提示记录，再次失败可再次确认
                  qaPromptedRef.current.delete(row.id);
                  setParseDoc(row);
                }}
                okText="确认"
                cancelText="取消"
              >
                <Button size="small" type="primary" ghost icon={<ThunderboltOutlined />}>
                  解析
                </Button>
              </Popconfirm>
            ) : (
              <Button
                key="parse"
                size="small"
                type="primary"
                ghost
                icon={<ThunderboltOutlined />}
                onClick={() => {
                  qaPromptedRef.current.delete(row.id);
                  setParseDoc(row);
                }}
              >
                解析
              </Button>
            ))}
          {canManage && row.status === 'ingested' &&
            (row.graph_status === 'building' ? (
              <Popconfirm
                key="graph-cancel"
                title="中断图谱构建？"
                description="将停止本次构建，图谱保持中断前状态，可稍后重新构建"
                onConfirm={() => handleCancelGraphBuild(row)}
                okText="中断"
                cancelText="取消"
                okButtonProps={{ danger: true }}
              >
                <Button size="small" danger icon={<StopOutlined />}>
                  中断
                </Button>
              </Popconfirm>
            ) : (
              <Tooltip
                key="graph-build"
                title={
                  row.graph_status === 'failed'
                    ? `上次构建失败：${row.graph_error || '未知原因'}，点击重新构建`
                    : row.graph_status === 'ready'
                      ? '重新抽取实体-关系，覆盖旧图谱'
                      : '用现有切块抽取实体-关系构建图谱'
                }
              >
                <Button
                  size="small"
                  icon={<ApartmentOutlined />}
                  onClick={() => void openGraphModal(row)}
                >
                  {row.graph_status === 'ready' || row.graph_status === 'failed'
                    ? '重建图谱'
                    : '补建图谱'}
                </Button>
              </Tooltip>
            ))}
          <Button size="small" icon={<EyeOutlined />} onClick={() => handleDetail(row)}>
            切块详情
          </Button>
          {canManage && (
            <Button
              size="small"
              icon={<EditOutlined />}
              onClick={() => setRenameDoc(row)}
            >
              重命名
            </Button>
          )}
          {canManage && (
            <Popconfirm
              title={`移入回收站「${row.original_name}」？`}
              description="文档将不再参与检索，可在回收站恢复；彻底删除请在回收站操作"
              onConfirm={() => handleDelete(row)}
              okText="移入回收站"
              cancelText="取消"
            >
              <Button size="small" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        breadcrumb={<Breadcrumb items={breadcrumbItems} />}
        title="文档管理"
        description="上传、解析与入库管理，文档「已入库」后即可参与检索问答"
        extra={
          <>
            <Text strong>知识库</Text>
            <Select
              value={kbId}
              onChange={setKbId}
              style={{ width: 240 }}
              placeholder="选择知识库"
              options={kbs.map(k => ({ value: k.id, label: k.name }))}
            />
            {canManage && (
              <Button icon={<RestOutlined />} onClick={openTrash}>
                回收站
              </Button>
            )}
            <Button icon={<ReloadOutlined />} onClick={() => load(false, page, pageSize)}>
              刷新
            </Button>
          </>
        }
      />

      {trashView && canManage ? (
        <Card
          title="回收站"
          extra={
            <Space>
              {trashDocs.length > 0 && (
                <Popconfirm
                  title="清空回收站？"
                  description="回收站内全部文档将被彻底删除，不可恢复"
                  onConfirm={handleEmptyTrash}
                  okText="清空"
                  cancelText="取消"
                >
                  <Button danger icon={<DeleteOutlined />}>
                    清空回收站
                  </Button>
                </Popconfirm>
              )}
              <Button icon={<RollbackOutlined />} onClick={() => setTrashView(false)}>
                返回文档列表
              </Button>
            </Space>
          }
        >
          <Table
            dataSource={trashDocs}
            rowKey="id"
            loading={trashLoading}
            pagination={{ pageSize: 10 }}
            locale={{ emptyText: <AppEmpty title="回收站为空" description="已删除的文档会出现在这里，可在 30 天内恢复" /> }}
            scroll={{ x: 800 }}
            className="table-zebra"
            columns={[
              {
                title: '文件名',
                dataIndex: 'original_name',
                key: 'name',
                ellipsis: true,
                width: 260,
              },
              {
                title: '类型',
                dataIndex: 'file_type',
                key: 'file_type',
                width: 80,
                render: (v: string) => <Tag>{v === 'url' ? '网页' : v || '-'}</Tag>,
              },
              {
                title: '删除时间',
                dataIndex: 'deleted_at',
                key: 'deleted_at',
                width: 170,
                render: (v?: string | null) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
              },
              {
                title: '操作',
                key: 'actions',
                width: 200,
                render: (_, row) => (
                  <Space size="small">
                    <Button
                      size="small"
                      icon={<RollbackOutlined />}
                      onClick={() => handleRestore(row)}
                    >
                      恢复
                    </Button>
                    <Popconfirm
                      title={`彻底删除「${row.original_name}」？`}
                      description="将同时清除存储文件与向量，删除后不可恢复"
                      onConfirm={() => handlePurge(row)}
                      okText="彻底删除"
                      cancelText="取消"
                    >
                      <Button size="small" danger icon={<DeleteOutlined />}>
                        彻底删除
                      </Button>
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
        </Card>
      ) : (
      <>
      {/* 空状态引导：知识库尚无文档时展示构建知识的三步流程 */}
      {kbId && total === 0 && !loading && (
        <Card
          size="small"
          style={{ marginBottom: 16 }}
          title="开始构建您的知识库"
        >
          <Steps
            size="small"
            current={-1}
            responsive={false}
            items={[
              {
                title: '上传文件',
                description: '点击上方区域或拖拽文件上传，支持多选批量',
              },
              {
                title: '解析入库',
                description: '在上方列表勾选文档，点击「批量解析」',
              },
              {
                title: '开始问答',
                description: '等待状态变为「已入库」后，即可在问答页提问',
              },
            ]}
          />
        </Card>
      )}

      <Card
        title={
          <Space size={12}>
            <span>文档列表</span>
            <Segmented
              size="small"
              value={statusFilter}
              onChange={v => {
                // P2-10: 筛选变化重置回第 1 页重新请求
                setStatusFilter(v as StatusFilter);
                setPage(1);
                void load(false, 1, pageSize);
              }}
              options={statusFilterOptions}
            />
          </Space>
        }
        extra={
          canManage && (
            <Space>
              {selectedRowKeys.length > 0 && (
                <Text type="secondary">已选 {selectedRowKeys.length} 项</Text>
              )}
              <Button
                type="primary"
                ghost
                icon={<ThunderboltOutlined />}
                onClick={handleBatchParse}
                disabled={!kbId || selectedRowKeys.length === 0 || batchParsing}
                loading={batchParsing}
              >
                {batchParsing && parseProgress
                  ? `解析中 ${parseProgress.done}/${parseProgress.total}`
                  : '批量解析'}
              </Button>
            </Space>
          )
        }
      >
        {/* 上传条：收敛为列表卡片顶部的内嵌窄条（点击/拖拽上传 + 从 URL 导入） */}
        <div style={{ display: 'flex', alignItems: 'stretch', gap: 12, marginBottom: 12 }}>
          {canManage ? (
            <Dragger
              className="upload-zone upload-zone--inline"
              accept=".txt,.md,.pdf,.docx"
              multiple={true}
              showUploadList={false}
              beforeUpload={file => handleUpload(file)}
              disabled={uploadState.uploading || !kbId}
            >
              <div className="upload-inline__content">
                <InboxOutlined style={{ fontSize: 16, color: token.colorPrimary }} />
                <span>点击或拖拽文件到此处上传，支持 .txt/.md/.pdf/.docx</span>
              </div>
            </Dragger>
          ) : (
            <div className="upload-inline__denied">
              <InboxOutlined style={{ fontSize: 14 }} />
              <span>普通用户仅可查看与问答，如需上传请联系部门管理员</span>
            </div>
          )}
          {canManage && (
            <Button icon={<GlobalOutlined />} onClick={() => setUrlImportOpen(true)}>
              从 URL 导入
            </Button>
          )}
        </div>
        {uploadState.uploading && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              marginBottom: 12,
              color: token.colorTextSecondary,
              fontSize: 13,
            }}
          >
            <Spin size="small" />
            <span>
              上传中：第 {uploadState.done + 1}/{uploadState.total} 个
              {uploadState.current ? `（${uploadState.current}）` : ''}
            </span>
          </div>
        )}
        {unparsedCount > 0 && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={`有 ${unparsedCount} 个文档未入库，可勾选后批量解析`}
          />
        )}
        {statusFilter === 'parsing' && (
          <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
            解析中状态每 2 秒自动刷新，完成后自动更新
          </Text>
        )}
        <Table
          dataSource={visibleDocs}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={{
            // P2-10 服务端分页：翻页重新请求；total 来自后端
            current: page,
            pageSize,
            total,
            showTotal: (t: number) => `共 ${t} 条`,
            onChange: (p: number, ps: number) => {
              setPage(p);
              setPageSize(ps);
              void load(false, p, ps);
            },
          }}
          locale={{
            emptyText:
              total === 0 ? (
                <AppEmpty
                  title="暂无文档"
                  description="点击上方上传条上传文件，或从 URL 导入网页内容"
                />
              ) : (
                <AppEmpty
                  title="没有符合条件的文档"
                  description="当前筛选条件下暂无文档，可切换其他状态筛选"
                />
              ),
          }}
          scroll={{ x: 1100 }}
          className="table-zebra"
          rowSelection={
            canManage
              ? {
                  selectedRowKeys,
                  onChange: keys => setSelectedRowKeys(keys),
                  // 解析中（parsing）不可勾选；选中后状态变化的行提交时兜底跳过
                  getCheckboxProps: (row: DocumentItem) => ({
                    disabled: row.status === 'parsing',
                  }),
                }
              : undefined
          }
        />
      </Card>
      </>
      )}

      {/* 在线预览弹窗 */}
      <DocumentPreviewModal
        open={!!previewDoc}
        doc={previewDoc}
        kbId={kbId}
        onCancel={() => setPreviewDoc(null)}
      />

      {/* 解析配置弹窗 */}
      <ParseConfigModal
        open={!!parseDoc}
        doc={parseDoc}
        kbId={kbId}
        onCancel={() => setParseDoc(null)}
        onSuccess={() => { void reloadFirstPage(); }}
      />

      {/* 文档重命名弹窗 */}
      <RenameDocumentModal
        open={!!renameDoc}
        doc={renameDoc}
        kbId={kbId}
        onCancel={() => setRenameDoc(null)}
        onSuccess={() => { void reloadFirstPage(); }}
      />

      {/* 图谱构建弹窗：选择 LLM 模型（默认文档配置或激活模型），
          切换即测连接，通过才可确认构建（本次构建生效，对话模型不受影响） */}
      <Modal
        title="构建知识图谱"
        open={!!graphDoc}
        onCancel={() => setGraphDoc(null)}
        onOk={() => void handleGraphBuildConfirm()}
        confirmLoading={graphConfirmLoading}
        okText="确认构建"
        okButtonProps={{ disabled: !graphTestedOk || graphTesting }}
        cancelText="取消"
      >
        {graphDoc && (
          <>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              message={`将使用所选模型构建知识图谱（复用「${graphDoc.original_name}」现有切块），对话模型不受影响`}
            />
            <div style={{ marginBottom: 8 }}>
              <Text strong>图谱构建模型</Text>
            </div>
            <Select
              value={graphSelected}
              onChange={handleGraphModelChange}
              style={{ width: '100%' }}
              loading={graphTesting && !graphModels.length}
              disabled={graphConfirmLoading}
              options={graphModels.map((m, i) => {
                // 默认选中项（文档已配置模型或激活模型）标注"当前使用"
                const docCfg = graphDoc.parser_config?.parse_llm_model;
                const useDocCfg =
                  typeof docCfg === 'string' &&
                  docCfg &&
                  graphModels.some(x => x.name === docCfg);
                const isCurrent = useDocCfg ? m.name === docCfg : i === graphActiveIdx;
                return {
                  value: m.name,
                  label: `${m.name}${m.model && m.model !== m.name ? `（${m.model}）` : ''}${isCurrent ? ' — 当前使用' : ''}`,
                };
              })}
              placeholder="选择模型"
            />
            <div style={{ marginTop: 8, minHeight: 24 }}>
              {graphTesting ? (
                <Space size={4}>
                  <Spin size="small" />
                  <Text type="secondary">正在测试连接…</Text>
                </Space>
              ) : graphSelected && !graphTestedOk ? (
                <Text type="danger">模型连接未通过，请重新选择模型</Text>
              ) : null}
            </div>
          </>
        )}
      </Modal>

      {/* URL 网页导入弹窗 */}
      <UrlImportModal
        open={urlImportOpen}
        kbId={kbId}
        onCancel={() => setUrlImportOpen(false)}
        onSuccess={() => { void reloadFirstPage(); }}
      />

      {/* 切块对比视图：左栏切块列表 + 右栏原文高亮（点击双向联动，左右同页分页）。
          弹窗固定高度：头部（标题/关闭/放大按钮）与 ChunkCompareView 顶部工具条固定不动，
          滚动只发生在内容区内部（滚动条在弹窗内、贴内容区右边）；放大态加宽到 88vw × 88vh。
          滚动结构修复（见 index.css .chunk-detail-modal）：
          antd v5(rc-dialog) 在 .ant-modal 与 .ant-modal-content 之间插入 sentinel 中间层
          （高度 auto），导致 content 的 height:100% 百分比基准失效、body 的 flex:1 链断裂，
          内容超高时由全屏 .ant-modal-wrap 兜底滚动（滚动条贴浏览器右缘、头部滚走）。
          修复：className 挂到 .ant-modal 上，CSS 把它变成 flex 容器 + overflow:hidden，
          sentinel 层 flex:1 填满，content 高度随之确定，body 内部滚动恢复。 */}
      <Modal
        className="chunk-detail-modal"
        title={
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              paddingRight: 36,
            }}
          >
            <span>{detail ? `切块详情 - ${detail.original_name}` : '切块详情'}</span>
            <Tooltip title={detailFullscreen ? '还原' : '放大'}>
              <Button
                type="text"
                size="small"
                aria-label={detailFullscreen ? '还原窗口' : '放大窗口'}
                icon={detailFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
                onClick={() => setDetailFullscreen(v => !v)}
              />
            </Tooltip>
          </div>
        }
        open={!!detail}
        onCancel={() => setDetail(null)}
        footer={null}
        width={detailFullscreen ? '88vw' : 1150}
        // 高度用 min(固定vh, 视口高-120px) 兜底：小视口下 8vh+80vh+底部留白(padding 24px)
        // 也不会超过视口，全屏 .ant-modal-wrap 永不成为滚动容器
        style={
          detailFullscreen
            ? { top: '6vh', height: 'min(88vh, calc(100vh - 120px))' }
            : { top: '8vh', height: 'min(80vh, calc(100vh - 120px))' }
        }
        styles={{
          content: { display: 'flex', flexDirection: 'column', height: '100%' },
          header: { flexShrink: 0 },
          body: {
            padding: '16px 20px',
            flex: 1,
            minHeight: 0,
            overflow: 'auto',
            display: 'flex',
            flexDirection: 'column',
          },
        }}
      >
        {detailLoading ? (
          <Skeleton active paragraph={{ rows: 10 }} />
        ) : (
          <Tabs
            // 与日志页同款撑满规则（index.css .logs-page-tabs 链式 flex 撑满）
            className="logs-page-tabs"
            defaultActiveKey="chunks"
            items={[
              {
                key: 'chunks',
                label: '切块',
                children: (
                  <ChunkCompareView
                    // 弹窗固定高度，内容区撑满剩余高度（头部/工具条固定，仅左右内容区内部滚动）
                    fillHeight
                    // key 绑定文档 id：切换文档时重挂载，重置选中态
                    key={detailData?.id}
                    chunks={
                      detailData?.chunks?.map(c => ({
                        index: c.index,
                        text: c.text,
                        char_start: c.char_start,
                        char_end: c.char_end,
                        context: c.context,
                        label: c.label,
                      })) ??
                      detailData?.chunk_preview?.map((text, i) => ({ index: i, text })) ??
                      []
                    }
                    fullText={detailData?.full_text}
                  />
                ),
              },
              {
                key: 'graph',
                label: '知识图谱',
                children: (
                  <KnowledgeGraphTab
                    graph={graphData}
                    loading={graphLoading}
                    chunks={
                      detailData?.chunks?.map(c => ({ index: c.index, text: c.text })) ??
                      detailData?.chunk_preview?.map((text, i) => ({ index: i, text })) ??
                      []
                    }
                  />
                ),
              },
            ]}
          />
        )}
      </Modal>
    </div>
  );
};

export default DocumentsPage;
