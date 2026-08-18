import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  App as AntApp,
  Button,
  Card,
  Collapse,
  Input,
  Pagination,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  theme,
} from 'antd';
import {
  BookOutlined,
  DeleteOutlined,
  EditOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import {
  asApiError,
  Department,
  DocumentStatus,
  GlobalDocumentItem,
  deleteDocument,
  listDepartments,
  listGlobalDocuments,
  listKbs,
  methodColor,
  methodLabel,
} from '../api/client';
import AppEmpty from '../components/AppEmpty';
import PageHeader from '../components/PageHeader';
import RenameDocumentModal from '../components/RenameDocumentModal';
import { useAuth } from '../auth/AuthContext';

const { Text } = Typography;

/** 未分配部门的分组标识（与后端 /api/admin/documents 契约一致） */
const UNASSIGNED = '__unassigned__';

const statusMeta: Record<DocumentStatus, { color: string; text: string }> = {
  uploaded: { color: 'default', text: '待解析' },
  parsing: { color: 'processing', text: '解析中' },
  parsed: { color: 'warning', text: '已解析' },
  ingested: { color: 'success', text: '已入库' },
  failed: { color: 'error', text: '失败' },
  // Agentic 分块超限待确认：不算失败，橙色 Tag（与部门内文档页一致）
  pending_confirm: { color: 'orange', text: '待确认' },
};

/** 状态筛选（与部门内文档管理 M3 语义统一：「未入库」= uploaded+parsed） */
type StatusFilter = 'all' | 'unparsed' | 'parsing' | 'ingested' | 'failed';
const statusFilterOptions: { label: string; value: StatusFilter }[] = [
  { label: '全部', value: 'all' },
  { label: '未入库', value: 'unparsed' },
  { label: '解析中', value: 'parsing' },
  { label: '已入库', value: 'ingested' },
  { label: '失败', value: 'failed' },
];

const toBackendStatus = (filter: StatusFilter): string | undefined =>
  filter === 'all' ? undefined : filter;

const formatSize = (bytes: number) => {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
};

/** 解析参数摘要（供解析方式列 Tooltip 展示，与部门内文档页一致） */
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

/** 当前页内分组：部门 → 知识库（组名/顺序稳定：部门下拉顺序 + 未分配最后） */
interface KbGroup {
  kbId: string;
  kbName: string;
  docs: GlobalDocumentItem[];
}
interface DeptGroup {
  deptKey: string;
  deptName: string;
  kbs: KbGroup[];
}

/**
 * 全局文档管理页（super_admin 全量 / dept_admin 限本部门）：
 * 按部门分类查看知识库与文档，支持重命名与软删除。删除为「移入回收站（可恢复）」，
 * 回收站入口保留在各部门知识库内文档管理页（本期不做全局回收站）。
 * dept_admin：数据已由后端强制限定本部门，页面隐藏「部门」筛选下拉（其余筛选
 * 保留，如知识库/状态/关键字）；super_admin 保留全部筛选。
 * 分页方案：一次拉当前页（page/page_size，默认 50），当前页数据内按部门分组。
 */
const GlobalDocumentsPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { token } = theme.useToken();
  const { user } = useAuth();
  // dept_admin：本部门视图（后端强制 department_id，前端隐藏部门筛选）
  const isDeptAdmin = user?.role === 'dept_admin';

  // 筛选条件
  const [departments, setDepartments] = useState<Department[]>([]);
  const [departmentId, setDepartmentId] = useState<string | undefined>();
  const [kbId, setKbId] = useState<string | undefined>();
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [keywordInput, setKeywordInput] = useState('');
  const [keyword, setKeyword] = useState('');

  // 分页 + 数据
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [total, setTotal] = useState(0);
  const [items, setItems] = useState<GlobalDocumentItem[]>([]);
  const [loading, setLoading] = useState(false);

  // 重命名弹窗（复用部门内文档页的组件，kbId 取文档自身所属）
  const [renameDoc, setRenameDoc] = useState<GlobalDocumentItem | null>(null);
  // 部门折叠面板展开态（受控：数据变化时全部展开，符合「默认展开」）
  const [openKeys, setOpenKeys] = useState<string[]>([]);

  // 知识库下拉选项（随部门联动过滤，见 filteredKbOptions）
  const [kbOptions, setKbOptions] = useState<
    { value: string; label: string; department_id: string | null }[]
  >([]);

  // ---------- 数据加载 ----------

  const loadMeta = useCallback(async () => {
    try {
      const [deptRes, kbRes] = await Promise.all([
        listDepartments(),
        listKbs(),
      ]);
      setDepartments(deptRes.data);
      setKbOptions(kbRes.data.map(k => ({
        value: k.id,
        label: k.name,
        department_id: k.department_id ?? null,
      })));
    } catch {
      message.error('加载部门/知识库列表失败');
    }
  }, [message]);

  const load = useCallback(
    async (silent = false, p = page, ps = pageSize) => {
      if (!silent) setLoading(true);
      try {
        const res = await listGlobalDocuments({
          department_id: departmentId,
          kb_id: kbId,
          status: toBackendStatus(statusFilter),
          keyword: keyword || undefined,
          page: p,
          page_size: ps,
        });
        setItems(res.data.items);
        setTotal(res.data.total);
      } catch {
        if (!silent) message.error('加载全局文档列表失败');
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [departmentId, kbId, statusFilter, keyword, message, page, pageSize],
  );

  useEffect(() => {
    void loadMeta();
  }, [loadMeta]);

  // 任一筛选变化 → 回第 1 页重新请求
  useEffect(() => {
    setPage(1);
    void load(false, 1, pageSize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [departmentId, kbId, statusFilter, keyword]);

  // ---------- 分组（当前页数据内按 部门 → 知识库） ----------

  const groups = useMemo<DeptGroup[]>(() => {
    const deptNameById = new Map(departments.map(d => [d.id, d.name]));
    // 稳定顺序：部门下拉顺序（创建时间序）+ 未分配最后
    const order = [...departments.map(d => d.id), UNASSIGNED];
    const map = new Map<string, DeptGroup>();
    for (const key of order) {
      map.set(key, {
        deptKey: key,
        deptName: key === UNASSIGNED ? '未分配部门' : deptNameById.get(key) ?? '未知部门',
        kbs: [],
      });
    }
    for (const item of items) {
      const deptKey = item.department_id ?? UNASSIGNED;
      const group = map.get(deptKey) ?? {
        deptKey,
        deptName: item.department_name ?? '未分配部门',
        kbs: [],
      };
      if (!map.has(deptKey)) map.set(deptKey, group);
      let kbGroup = group.kbs.find(k => k.kbId === item.kb_id);
      if (!kbGroup) {
        kbGroup = { kbId: item.kb_id, kbName: item.kb_name, docs: [] };
        group.kbs.push(kbGroup);
      }
      kbGroup.docs.push(item);
    }
    return order
      .map(key => map.get(key)!)
      .filter(g => g.kbs.length > 0);
  }, [items, departments]);

  // 数据变化（筛选/翻页）后面板全部展开（defaultActiveKey 首次渲染后不再生效）
  useEffect(() => {
    setOpenKeys(groups.map(g => g.deptKey));
  }, [groups]);

  // ---------- 操作 ----------

  const handleDelete = async (doc: GlobalDocumentItem) => {
    try {
      await deleteDocument(doc.kb_id, doc.id);
      message.success('已移入回收站（可恢复）');
      void load(true);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  const columns: ColumnsType<GlobalDocumentItem> = [
    { title: '文件名', dataIndex: 'original_name', key: 'name', ellipsis: true, width: 240 },
    {
      title: '类型',
      dataIndex: 'file_type',
      key: 'file_type',
      width: 70,
      render: (v: string) => <Tag>{v === 'url' ? '网页' : v || '-'}</Tag>,
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 90,
      render: (v?: number) => (v == null ? '-' : formatSize(v)),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
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
        return (status === 'failed' || status === 'pending_confirm') && row.error ? (
          <Tooltip title={row.error}>{tag}</Tooltip>
        ) : (
          tag
        );
      },
    },
    {
      title: '切块数',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 80,
      render: (v: number, row) => (row.status === 'ingested' ? v : '-'),
    },
    {
      title: '解析方式',
      dataIndex: 'parser_id',
      key: 'parser_id',
      width: 120,
      render: (v: string | undefined, row) => {
        if (!v) return <Text type="secondary">-</Text>;
        const tag = <Tag color={methodColor(v)}>{methodLabel(v)}</Tag>;
        const summary = parserConfigSummary(row.parser_config, v);
        return summary ? <Tooltip title={summary}>{tag}</Tooltip> : tag;
      },
    },
    {
      title: '上传时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 170,
      render: (_, row) => (
        <Space size="small">
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => setRenameDoc(row)}
          >
            重命名
          </Button>
          <Popconfirm
            title={`移入回收站「${row.original_name}」？`}
            description="文档将不再参与检索，可在该知识库的回收站恢复；彻底删除请在回收站操作"
            onConfirm={() => handleDelete(row)}
            okText="移入回收站"
            cancelText="取消"
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  // 知识库下拉联动：选部门后仅显示该部门（含未分配）的知识库
  const filteredKbOptions = kbOptions.filter(k =>
    !departmentId
      ? true
      : departmentId === UNASSIGNED
        ? !k.department_id
        : k.department_id === departmentId,
  );

  const collapseItems = groups.map(g => {
    const count = g.kbs.reduce((s, k) => s + k.docs.length, 0);
    return {
      key: g.deptKey,
      label: (
        <Space size={8}>
          <Text strong>{g.deptName}</Text>
          <Tag color="blue">{count}</Tag>
        </Space>
      ),
      children: (
        <div>
          {g.kbs.map(kbGroup => (
            <div key={kbGroup.kbId} style={{ marginBottom: 16 }}>
              <div style={{ marginBottom: 8 }}>
                <Space size={6}>
                  <BookOutlined style={{ color: token.colorPrimary }} />
                  <Text strong>{kbGroup.kbName}</Text>
                  <Tag>{kbGroup.docs.length}</Tag>
                </Space>
              </div>
              <Table
                size="small"
                dataSource={kbGroup.docs}
                columns={columns}
                rowKey="id"
                pagination={false}
                scroll={{ x: 900 }}
                className="table-zebra"
              />
            </div>
          ))}
        </div>
      ),
    };
  });

  return (
    <div>
      <PageHeader
        title={isDeptAdmin ? '文档管理（本部门）' : '文档管理（全部部门）'}
        description={
          isDeptAdmin
            ? '本部门视图：查看本部门知识库的文档，可重命名或移入回收站（可在所属知识库回收站恢复）'
            : '超管跨部门视图：按部门查看所有知识库的文档，可重命名或移入回收站（可在所属知识库回收站恢复）'
        }
        extra={
          <>
            {!isDeptAdmin && (
              <Select
                value={departmentId}
                onChange={setDepartmentId}
                style={{ width: 180 }}
                allowClear
                placeholder="全部部门"
                options={[
                  ...departments.map(d => ({ value: d.id, label: d.name })),
                  { value: UNASSIGNED, label: '未分配部门' },
                ]}
              />
            )}
            <Select
              value={kbId}
              onChange={setKbId}
              style={{ width: 200 }}
              allowClear
              placeholder="全部知识库"
              options={filteredKbOptions.map(k => ({ value: k.value, label: k.label }))}
            />
            <Segmented
              size="middle"
              value={statusFilter}
              onChange={v => setStatusFilter(v as StatusFilter)}
              options={statusFilterOptions}
            />
            <Input.Search
              allowClear
              placeholder="搜索文件名"
              style={{ width: 200 }}
              value={keywordInput}
              onChange={e => setKeywordInput(e.target.value)}
              onSearch={v => {
                setKeyword(v.trim());
              }}
            />
            <Button icon={<ReloadOutlined />} onClick={() => void load(false, page, pageSize)}>
              刷新
            </Button>
          </>
        }
      />

      <Card>
        {loading && groups.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '40px 0' }}>
            <Spin tip="加载中..." />
          </div>
        ) : total === 0 ? (
          <AppEmpty
            title="暂无文档"
            description="当前筛选条件下没有文档，可调整部门/知识库/状态/关键字筛选"
          />
        ) : (
          <>
            <Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
              共 {total} 个文档{keyword ? `（关键字「${keyword}」）` : ''}，按部门分组展示当前页
            </Text>
            <Collapse
              items={collapseItems}
              activeKey={openKeys}
              onChange={keys => setOpenKeys(keys as string[])}
              size="small"
            />
            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <Pagination
                current={page}
                pageSize={pageSize}
                total={total}
                showSizeChanger
                pageSizeOptions={[20, 50, 100, 200]}
                showTotal={t => `共 ${t} 条`}
                onChange={(p, ps) => {
                  setPage(p);
                  setPageSize(ps);
                  void load(false, p, ps);
                }}
              />
            </div>
          </>
        )}
      </Card>

      {/* 重命名弹窗（复用部门内文档管理组件，kbId 取文档所属知识库；成功提示由弹窗自身展示） */}
      <RenameDocumentModal
        open={!!renameDoc}
        doc={renameDoc}
        kbId={renameDoc?.kb_id}
        onCancel={() => setRenameDoc(null)}
        onSuccess={() => { void load(true); }}
      />
    </div>
  );
};

export default GlobalDocumentsPage;
