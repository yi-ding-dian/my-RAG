import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  App as AntApp,
  Breadcrumb,
  Button,
  Select,
  Typography,
} from 'antd';
import { ReloadOutlined, RestOutlined } from '@ant-design/icons';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  asApiError,
  DocumentItem,
  KnowledgeBase,
  cancelDocumentGraphBuild,
  cancelDocumentIngestion,
  deleteDocument,
  downloadDocument,
  ingestDocument,
  listDocuments,
  listKbs,
} from '../api/client';
import PageHeader from '../components/PageHeader';
import { useAuth } from '../auth/AuthContext';
import UploadArea from '../components/documents/UploadArea';
import BatchActionsBar, {
  StatusFilter,
  toBackendStatus,
} from '../components/documents/BatchActionsBar';
import DocumentTable, { TrashView, parseableStatuses } from '../components/documents/DocumentTable';
import DocumentModals, {
  useDetailModal,
  useGraphBuildModal,
  usePortraitModal,
} from '../components/documents/DocumentModals';

const { Text } = Typography;

const DocumentsPage: React.FC = () => {
  const { message, modal } = AntApp.useApp();
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
  // 批量解析状态
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [batchParsing, setBatchParsing] = useState(false);
  const [parseProgress, setParseProgress] = useState<{ done: number; total: number } | null>(null);

  // 解析配置弹窗
  const [parseDoc, setParseDoc] = useState<DocumentItem | null>(null);
  // 智能解析引导向导（独立模块：画像 → 切块方式 → 增强配置 → 确认解析，
  // 生成配置调现有 ingest；未解析文档也能分析）
  const [smartDoc, setSmartDoc] = useState<DocumentItem | null>(null);
  // QA 规范性检测失败确认框：已提示过的 doc_id 集合（防列表轮询重复弹窗；
  // 用户重新发起解析时清除该条目，再次失败可再次提示）
  const qaPromptedRef = useRef<Set<string>>(new Set());
  // Agentic 分块超限提示：已提示过的 doc_id 集合（防列表轮询重复弹窗；
  // 重新发起解析时清除该条目，再次失败可再次提示）
  const agenticPromptedRef = useRef<Set<string>>(new Set());
  // 上下文检索完整文档阈值超限提示：已提示过的 doc_id 集合（防列表轮询
  // 重复弹窗；重新发起解析时清除该条目，再次失败可再次提示）
  const ctxPromptedRef = useRef<Set<string>>(new Set());
  // 重命名弹窗（当前待重命名文档）
  const [renameDoc, setRenameDoc] = useState<DocumentItem | null>(null);
  // URL 网页导入弹窗
  const [urlImportOpen, setUrlImportOpen] = useState(false);
  // 批量导入并解析弹窗（一次多选 → 逐个上传+解析入库，智能/统一两种模式）
  const [batchImportOpen, setBatchImportOpen] = useState(false);
  // 在线预览弹窗（当前待预览文档）
  const [previewDoc, setPreviewDoc] = useState<DocumentItem | null>(null);
  // 回收站视图（软删除文档列表）
  const [trashView, setTrashView] = useState(false);
  // 状态筛选：全部/待解析/解析中/已入库/失败（前端过滤）
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  // 文件名/关键词过滤：keywordInput=输入框即时值，keyword=防抖生效值
  // （生效后触发重拉；列表接口无 keyword 参数，见 load 内注释）
  const [keywordInput, setKeywordInput] = useState('');
  const [keyword, setKeyword] = useState('');

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
  // keyword 为纯前端过滤（keyword 模式下 docs 是全量数据，见 load 内注释），
  // 与状态筛选叠加：先状态（幂等兜底）再文件名包含匹配（大小写不敏感）
  const visibleDocs = useMemo(() => {
    let list = docs;
    if (statusFilter === 'unparsed') {
      list = list.filter(d => d.status === 'uploaded' || d.status === 'parsed');
    }
    const kw = keyword.trim().toLowerCase();
    if (kw) {
      list = list.filter(d => (d.original_name || '').toLowerCase().includes(kw));
    }
    return list;
  }, [docs, statusFilter, keyword]);

  // 待解析文档数（仅"全部"筛选下有意义，用于列表上方引导提示；
  // 基于可见列表统计，keyword 过滤后与实际展示一致）
  const unparsedCount =
    statusFilter === 'all'
      ? visibleDocs.filter(d => d.status === 'uploaded' || d.status === 'parsed').length
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
        // keyword 纯前端过滤：列表接口（GET /kbs/{id}/documents）无 keyword
        // 参数，关键词非空时强制第 1 页 + page_size=200（后端上限）全量拉取，
        // 前端过滤后由 Table 前端分页展示（避免"搜索只作用于当前页"；
        // 超 200 文档的知识库搜索不完整，接口无 keyword 约束下的最优解）
        const res = await listDocuments(kbId, {
          page: keyword ? 1 : p,
          page_size: keyword ? 200 : ps,
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
    [kbId, message, statusFilter, keyword],
  );

  /** B2: 删除/解析/上传等变更操作后刷新：回第 1 页重拉（避免页码显示旧值错位） */
  const reloadFirstPage = useCallback(
    async (silent = false) => {
      setPage(1);
      await load(silent, 1, pageSize);
    },
    [load, pageSize],
  );

  // 自包含弹窗模块（图谱构建 / 文档画像 / 切块详情：状态机与数据加载在各自 hook 内）
  const graphModal = useGraphBuildModal(kbId, reloadFirstPage);
  const portraitModal = usePortraitModal(kbId);
  const detailModal = useDetailModal(kbId);

  useEffect(() => {
    loadKbs();
  }, [loadKbs]);

  // URL kb_id 参数变化时同步更新选中（如从知识库管理页跳转进入）
  useEffect(() => {
    if (urlKbId) setKbId(urlKbId);
  }, [urlKbId]);

  useEffect(() => {
    // 切换知识库/关键词生效时重置页码回第 1 页
    // （避免 Table 显示旧页码而数据是新库第 1 页）
    setPage(1);
    void load(false, 1, pageSize);
  }, [kbId, keyword, load, pageSize]);

  // 搜索框输入防抖 300ms 后生效（输入即时过滤，避免每击键重拉全量）
  useEffect(() => {
    const kw = keywordInput.trim();
    if (kw === keyword) return;
    const timer = window.setTimeout(() => setKeyword(kw), 300);
    return () => window.clearTimeout(timer);
  }, [keywordInput, keyword]);

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
          } catch (e: unknown) {
            message.error(asApiError(e).response?.data?.detail || '重新提交入库失败');
          }
        },
      });
    }
  }, [docs, kbId, modal, message, reloadFirstPage]);

  // Agentic 分块超限处理（两档）：Agentic 方式入库时后端校验解析文本长度
  // → 任务失败，error 经 doc.error 传递（提交前无法预知文本长度，由后端
  // 校验后带字数提示）。此处解析失败信息：
  // - "超过 5 万字" → 直接拒绝，弹错误提示引导换切块方式（不弹确认）
  // - "文档约 X.X 万字"（1 万~5 万字）→ 弹确认框询问是否继续：确认 →
  //   带 agentic_confirm=true 重新提交；取消 → 放弃本次提示
  useEffect(() => {
    if (!kbId) return;
    for (const doc of docs) {
      if (doc.status !== 'failed' || !doc.error) continue;
      // 超过 5 万字：不支持，保持错误提示换方式（不弹确认）
      if (doc.error.includes('超过 5 万字')) {
        if (agenticPromptedRef.current.has(doc.id)) continue;
        agenticPromptedRef.current.add(doc.id);
        message.error(
          `「${doc.original_name}」文档超过 5 万字，不支持 Agentic 分块，请换用其他切块方式`,
        );
        continue;
      }
      // 1 万~5 万字：弹确认框（字数从错误信息"文档约 X.X 万字"解析）
      const m = doc.error.match(/文档约\s*([\d.]+)\s*万字/);
      if (!m || agenticPromptedRef.current.has(doc.id)) continue;
      // 先标记再弹窗，避免 docs 重复变化导致连弹
      agenticPromptedRef.current.add(doc.id);
      modal.confirm({
        title: 'Agentic 分块成本较高',
        content: (
          <span>
            当前文档「{doc.original_name}」约 {m[1]} 万字，Agentic 分块成本较高，
            是否继续？
          </span>
        ),
        okText: '继续',
        cancelText: '取消',
        onOk: async () => {
          try {
            await ingestDocument(kbId!, doc.id, {
              method: 'agentic',
              agentic_confirm: true,
            });
            message.success(`已按 Agentic 方式重新提交「${doc.original_name}」入库`);
            void reloadFirstPage();
          } catch (e: unknown) {
            message.error(asApiError(e).response?.data?.detail || '重新提交入库失败');
          }
        },
      });
    }
  }, [docs, kbId, modal, message, reloadFirstPage]);

  // 上下文检索完整文档阈值超限提示：开启上下文检索增强入库时，解析文本
  // 超过系统配置阈值（默认 2 万字，超管在系统配置可改）→ 后端任务失败，
  // error 带"超过上下文检索完整文档阈值"提示（doc.error 已在状态列 tooltip
  // 展示，此处弹 message 强化，引导换用其他切块方式或关闭增强）
  useEffect(() => {
    if (!kbId) return;
    for (const doc of docs) {
      if (doc.status !== 'failed' || !doc.error) continue;
      if (!doc.error.includes('上下文检索') || !doc.error.includes('超过')) continue;
      if (ctxPromptedRef.current.has(doc.id)) continue;
      ctxPromptedRef.current.add(doc.id);
      message.error(`「${doc.original_name}」${doc.error}`);
    }
  }, [docs, kbId, message]);

  // ---------- 操作 ----------

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
      // 再次失败可再次弹确认框；Agentic 超限/上下文检索阈值超限提示记录同清
      // （再次失败可再提示）
      qaPromptedRef.current.delete(doc.id);
      agenticPromptedRef.current.delete(doc.id);
      ctxPromptedRef.current.delete(doc.id);
      try {
        // 不传配置 = 按文档已有 parser_config 解析，无配置的用后端默认
        await ingestDocument(kbId, doc.id);
      } catch (e: unknown) {
        failed.push(`${doc.original_name}（${asApiError(e).response?.data?.detail || '解析失败'}）`);
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

  const handleDelete = async (doc: DocumentItem) => {
    try {
      await deleteDocument(kbId!, doc.id);
      message.success('已移入回收站');
      await reloadFirstPage();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  /** 批量删除：循环复用现有软删接口（DELETE /documents/{id}，移入回收站，
   *  向量/存储保留可恢复）；串行逐个处理（与批量解析一致），失败不中断，
   *  汇总成功/失败数；解析中（parsing）行勾选框已禁用不会混入 */
  const handleBatchDelete = async () => {
    if (!kbId || selectedRowKeys.length === 0) return;
    const targets = docs.filter(d => selectedRowKeys.includes(d.id));
    const failed: string[] = [];
    for (const doc of targets) {
      try {
        await deleteDocument(kbId, doc.id);
      } catch (e: unknown) {
        failed.push(`${doc.original_name}（${asApiError(e).response?.data?.detail || '删除失败'}）`);
      }
    }
    setSelectedRowKeys([]);
    const ok = targets.length - failed.length;
    if (failed.length === 0) {
      message.success(`已删除 ${ok} 个文档（移入回收站，可恢复）`);
    } else {
      message.warning(`删除完成：成功 ${ok} 个，失败 ${failed.length} 个：${failed.join('、')}`);
    }
    await reloadFirstPage();
  };

  // 中断进行中的图谱构建（任务停止、恢复构建前状态、旧图谱保留）
  const handleCancelGraphBuild = async (doc: DocumentItem) => {
    try {
      await cancelDocumentGraphBuild(kbId!, doc.id);
      message.success('已发送中断请求，列表将自动刷新');
      void reloadFirstPage();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '中断请求失败');
    }
  };

  // 取消解析（仅 parsing 可取消）：接口置取消信号 → 任务尽快停止，文档
  // 回 failed（error="用户取消解析"），可重新发起解析；列表轮询自动刷新
  const handleCancelIngestion = async (doc: DocumentItem) => {
    try {
      await cancelDocumentIngestion(kbId!, doc.id);
      message.success('已发送取消请求，文档将回到失败状态，可重新解析');
      void reloadFirstPage();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '取消解析失败');
    }
  };

  // Agentic 分块超限待确认（pending_confirm）：直接带 agentic_confirm=true
  // 重提入库（复用 ingest 接口，后端跳过超限校验进入正常流转），不走
  // "失败→重解析→弹确认"的绕圈流程；失败（如超 5 万拒绝）提示原因
  const handleConfirmAgentic = async (doc: DocumentItem) => {
    try {
      await ingestDocument(kbId!, doc.id, {
        method: 'agentic',
        agentic_confirm: true,
      });
      message.success(`已按 Agentic 方式重新提交「${doc.original_name}」入库`);
      void reloadFirstPage();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '重新提交入库失败');
    }
  };

  /** 下载文档原始文件（GET /documents/{id}/download，can_access_kb）：
   *  fetch blob + 鉴权头 + Content-Disposition 文件名触发浏览器下载 */
  const handleDownload = async (doc: DocumentItem) => {
    try {
      await downloadDocument(kbId!, doc.id);
      message.success(`已开始下载「${doc.original_name}」`);
    } catch (e: unknown) {
      message.error(asApiError(e).message || '下载失败');
    }
  };

  // ---------- 更多菜单：文档下载 / 查看画像 ----------

  /** 更多下拉菜单项点击分发（菜单项由 DocumentTable 按状态/权限组装）：
   *  - graph：打开图谱构建弹窗（补建/重建，LLM 模型选择）
   *  - graph-cancel：中断构建（原操作列 Popconfirm 移入菜单后改 modal.confirm）
   *  - rename：打开重命名弹窗
   *  - download：下载文档原始文件
   *  - portrait：打开文档画像弹窗 */
  const handleMoreClick = useCallback(
    (key: string, row: DocumentItem) => {
      switch (key) {
        case 'graph':
          void graphModal.openGraphModal(row);
          break;
        case 'graph-cancel':
          // 原操作列「中断」Popconfirm 移入菜单后改 modal.confirm（菜单项不支持内嵌 Popconfirm）
          modal.confirm({
            title: '中断图谱构建？',
            content: '将停止本次构建，图谱保持中断前状态，可稍后重新构建',
            okText: '中断',
            cancelText: '取消',
            okButtonProps: { danger: true },
            onOk: () => handleCancelGraphBuild(row),
          });
          break;
        case 'rename':
          setRenameDoc(row);
          break;
        case 'download':
          void handleDownload(row);
          break;
        case 'portrait':
          void portraitModal.openPortrait(row);
          break;
      }
    },
    [modal, graphModal.openGraphModal, portraitModal.openPortrait, handleCancelGraphBuild, handleDownload],
  );

  // 解析/重新解析按钮：清除 QA 失败提示记录（再次失败可再次确认），打开解析配置弹窗
  const startParse = useCallback((doc: DocumentItem) => {
    qaPromptedRef.current.delete(doc.id);
    setParseDoc(doc);
  }, []);

  // 分页变化：keyword 模式为本地分页（不发请求）；否则服务端分页重新请求
  const handlePageChange = useCallback(
    (p: number, ps: number) => {
      setPage(p);
      setPageSize(ps);
      if (!keyword) void load(false, p, ps);
    },
    [keyword, load],
  );

  // 状态筛选变化：重置回第 1 页重新请求（P2-10 筛选下沉后端）
  const handleStatusFilterChange = useCallback(
    (v: StatusFilter) => {
      setStatusFilter(v);
      setPage(1);
      void load(false, 1, pageSize);
    },
    [load, pageSize],
  );

  const openTrash = () => {
    setTrashView(true);
  };

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
        <TrashView kbId={kbId} onBack={() => setTrashView(false)} onChanged={reloadFirstPage} />
      ) : (
        <>
          <BatchActionsBar
            kbId={kbId}
            canManage={canManage}
            statusFilter={statusFilter}
            onStatusFilterChange={handleStatusFilterChange}
            keywordInput={keywordInput}
            onKeywordInputChange={setKeywordInput}
            onKeywordSearch={v => setKeyword(v)}
            selectedCount={selectedRowKeys.length}
            batchParsing={batchParsing}
            parseProgress={parseProgress}
            onBatchParse={handleBatchParse}
            onBatchDelete={handleBatchDelete}
            unparsedCount={unparsedCount}
            showParsingHint={statusFilter === 'parsing'}
            showGuide={!!kbId && total === 0 && !loading}
          >
            {/* 上传条：点击/拖拽上传 + 批量导入并解析 + 从 URL 导入（自包含上传聚合逻辑） */}
            <UploadArea
              kbId={kbId}
              canManage={canManage}
              onOpenBatchImport={() => setBatchImportOpen(true)}
              onOpenUrlImport={() => setUrlImportOpen(true)}
              onUploaded={reloadFirstPage}
            />
            {/* 文档列表表格：列渲染/更多菜单/分页/勾选（可见列表由页面主组件计算） */}
            <DocumentTable
              docs={visibleDocs}
              loading={loading}
              page={page}
              pageSize={pageSize}
              total={total}
              keyword={keyword}
              canManage={canManage}
              selectedRowKeys={selectedRowKeys}
              onSelectionChange={setSelectedRowKeys}
              onPageChange={handlePageChange}
              totalIsEmpty={total === 0}
              onPreview={setPreviewDoc}
              onStartParse={startParse}
              onSmartParse={setSmartDoc}
              onConfirmAgentic={handleConfirmAgentic}
              onCancelIngestion={handleCancelIngestion}
              onDetail={doc => void detailModal.openDetail(doc)}
              onDelete={handleDelete}
              onMoreAction={handleMoreClick}
            />
          </BatchActionsBar>
        </>
      )}

      {/* 图谱构建弹窗 / 文档画像弹窗 / 切块详情弹窗（自包含状态机） */}
      {graphModal.node}
      {portraitModal.node}
      {detailModal.node}

      {/* 受控弹窗编排：在线预览 / 解析配置 / 智能向导 / 重命名 / 批量导入 / URL 导入 */}
      <DocumentModals
        kbId={kbId}
        previewDoc={previewDoc}
        onPreviewClose={() => setPreviewDoc(null)}
        parseDoc={parseDoc}
        onParseClose={() => setParseDoc(null)}
        smartDoc={smartDoc}
        onSmartClose={() => setSmartDoc(null)}
        renameDoc={renameDoc}
        onRenameClose={() => setRenameDoc(null)}
        urlImportOpen={urlImportOpen}
        onUrlImportClose={() => setUrlImportOpen(false)}
        batchImportOpen={batchImportOpen}
        onBatchImportClose={() => setBatchImportOpen(false)}
        onSuccess={() => { void reloadFirstPage(); }}
      />
    </div>
  );
};

export default DocumentsPage;
