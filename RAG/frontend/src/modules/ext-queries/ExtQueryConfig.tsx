import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  App as AntApp,
  Breadcrumb,
  Button,
  Dropdown,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  ClockCircleOutlined,
  CopyOutlined,
  LinkOutlined,
  PlusOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons';
import {
  asApiError,
  ExtQuery,
  KnowledgeBase,
  createExtQuery,
  deleteExtQuery,
  extQueryLink,
  getExtQueryToken,
  listDepartments,
  listExtQueries,
  listKbs,
  renewExtQuery,
  resetExtQueryToken,
  toggleExtQuery,
  updateExtQuery,
} from '../../shared/api/client';
import AppModal from '../../shared/components/common/AppModal';
import PageHeader from '../../shared/components/layout/PageHeader';
import ResizableTitle from '../../shared/components/common/ResizableTitle';
import { useResizableColumns } from '../../shared/hooks/useResizableColumns';
import ExtQueryForm, { type ExtQueryFormValues } from './components/ExtQueryForm';
import { expiryColor, expiryText, RENEW_PRESETS } from './expiry';

/**
 * 表体高度：撑满视口剩余空间（数据少时不留大片空白，数据多时表头固定、内部滚动）
 *
 * 180 ≈ 面包屑 + 页头 + 上下留白 + 表头。本页无筛选区与分页器，
 * 比记录页偏移小。配合下面的 min-height 规则——antd 的 scroll.y 只写
 * max-height，数据少时表体按内容收缩，撑不满。
 */
const TABLE_BODY_HEIGHT = 'max(360px, calc(100vh - 180px))';

/**
 * 外部查询配置（仅 super_admin）：把选定知识库暴露为带令牌的查询链接，
 * 外部人员无需账号即可查询。链接 = 访问凭证，可复制分发 / 重置 / 停用 / 续期。
 */
const ExtQueryConfig: React.FC = () => {
  const { message } = AntApp.useApp();

  // 列宽拖拽：拖拽后的列宽存 colWidths（按列 key），scroll.x 动态对齐列宽和
  const { colWidths, handleResize, tableWidth } = useResizableColumns<ExtQuery>();

  const [items, setItems] = useState<ExtQuery[]>([]);
  const [loading, setLoading] = useState(false);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [deptName, setDeptName] = useState<Record<string, string>>({});

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ExtQuery | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 创建/重置后展示链接（含新 token）
  const [linkModal, setLinkModal] = useState<{ title: string; link: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listExtQueries();
      setItems(res.data);
    } catch {
      message.error('加载外部查询列表失败');
    } finally {
      setLoading(false);
    }
  }, [message]);

  // 数据源：全部门知识库（超管视角）+ 部门名映射
  const loadOptions = useCallback(async () => {
    try {
      const [kbRes, deptRes] = await Promise.all([listKbs(), listDepartments()]);
      setKbs(kbRes.data);
      const map: Record<string, string> = {};
      deptRes.data.forEach(d => {
        map[d.id] = d.name;
      });
      setDeptName(map);
    } catch {
      // 选项加载失败不阻塞页面（下拉留空可刷新重试）
    }
  }, []);

  useEffect(() => {
    load();
    loadOptions();
  }, [load, loadOptions]);

  const kbNameOf = useCallback(
    (item: ExtQuery) =>
      (item.kb_names ?? []).map(
        k => `${k.name}${k.department_id ? `（${deptName[k.department_id] ?? '未知部门'}）` : '（全局）'}`,
      ),
    [deptName],
  );

  const openCreate = () => {
    setEditing(null);
    setModalOpen(true);
  };

  const openEdit = (item: ExtQuery) => {
    setEditing(item);
    setModalOpen(true);
  };

  const copyLink = async (link: string) => {
    try {
      await navigator.clipboard.writeText(link);
      message.success('链接已复制');
    } catch {
      message.error('复制失败，请手动复制');
    }
  };

  const handleSubmit = async (values: ExtQueryFormValues) => {
    setSubmitting(true);
    try {
      if (editing) {
        await updateExtQuery(editing.id, values);
        message.success('外部查询已更新');
        setModalOpen(false);
        await load();
        // 编辑不展示链接（token 不变，原链接继续有效）
      } else {
        const res = await createExtQuery(values);
        setModalOpen(false);
        await load();
        setLinkModal({
          title: '外部查询链接已生成',
          link: extQueryLink(res.data.id, res.data.token),
        });
      }
    } catch (e: unknown) {
      message.error(
        asApiError(e).response?.data?.detail || (editing ? '更新失败' : '创建失败'),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleResetToken = async (item: ExtQuery) => {
    try {
      const res = await resetExtQueryToken(item.id);
      await load();
      setLinkModal({
        title: '访问令牌已重置（旧链接已失效）',
        link: extQueryLink(item.id, res.data.token),
      });
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '重置令牌失败');
    }
  };

  const handleToggle = async (item: ExtQuery) => {
    try {
      const res = await toggleExtQuery(item.id);
      message.success(res.data.enabled ? '已启用' : '已停用（链接立即失效）');
      await load();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '操作失败');
    }
  };

  const handleRenew = async (item: ExtQuery, days: number) => {
    try {
      const res = await renewExtQuery(item.id, days);
      message.success(`已续期 ${days} 天，到期时间：${res.data.expires_at}`);
      await load();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '续期失败');
    }
  };

  const handleDelete = async (item: ExtQuery) => {
    try {
      await deleteExtQuery(item.id);
      message.success(`外部查询「${item.name}」已删除`);
      await load();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  const columns = [
    {
      title: '名称',
      dataIndex: 'name',
      width: colWidths.name ?? 200,
      onHeaderCell: () => ({ width: colWidths.name ?? 200, onResize: handleResize('name'), title: '名称' }),
      render: (name: string) => <Typography.Text strong>{name}</Typography.Text>,
    },
    {
      title: '暴露的知识库',
      dataIndex: 'kb_ids',
      width: colWidths.kb_ids ?? 240,
      onHeaderCell: () => ({ width: colWidths.kb_ids ?? 240, onResize: handleResize('kb_ids'), title: '暴露的知识库' }),
      render: (_: unknown, item: ExtQuery) => (
        <Space size={[4, 4]} wrap>
          {kbNameOf(item).map(n => (
            <Tag key={n}>{n}</Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      width: colWidths.enabled ?? 90,
      onHeaderCell: () => ({ width: colWidths.enabled ?? 90, onResize: handleResize('enabled'), title: '状态' }),
      render: (enabled: boolean, item: ExtQuery) => {
        if (!enabled) return <Tag color="default">已停用</Tag>;
        // 启用中但已过期：状态本身没停用，但外部实际访问不到——必须显式提示
        if (expiryColor(item.expires_at) === 'red') {
          return <Tag color="red">已过期</Tag>;
        }
        return <Tag color="green">启用</Tag>;
      },
    },
    {
      title: '有效期',
      dataIndex: 'expires_at',
      width: colWidths.expires_at ?? 150,
      onHeaderCell: () => ({ width: colWidths.expires_at ?? 150, onResize: handleResize('expires_at'), title: '有效期' }),
      render: (v: string | null) => (
        <Tooltip title={v ? `到期时间：${v}` : '永久有效'}>
          <Tag color={expiryColor(v)}>{expiryText(v)}</Tag>
        </Tooltip>
      ),
    },
    {
      title: '链接',
      dataIndex: 'token',
      // 列宽 210：需容纳「/ext-query/+id 前 8 位」共 19 个等宽字符（12px 下约 137px）
      // + 复制按钮 + 单元格 padding；原先 130 装不下，连前缀都被截成 /ext-quer...
      width: colWidths.token ?? 210,
      onHeaderCell: () => ({ width: colWidths.token ?? 210, onResize: handleResize('token'), title: '链接' }),
      render: (_: unknown, item: ExtQuery) => (
        <Space size={4}>
          {/* 悬浮展示完整路径；令牌是访问凭证，不在此明文展示，复制走右侧按钮 */}
          <Tooltip
            title={
              <>
                <div>/ext-query/{item.id}</div>
                <div style={{ opacity: 0.8 }}>含令牌的完整链接请点右侧按钮复制</div>
              </>
            }
          >
            <Typography.Text code ellipsis style={{ maxWidth: 145, fontSize: 12 }}>
              /ext-query/{item.id.slice(0, 8)}
            </Typography.Text>
          </Tooltip>
          <Tooltip title={!item.enabled ? '已停用，无法查询' : '复制分享链接'}>
            <Button
              type="text"
              size="small"
              icon={<CopyOutlined />}
              disabled={!item.enabled}
              onClick={async () => {
                try {
                  // 列表只回传打码 token：复制时走独立接口取完整凭证（带审计）
                  const res = await getExtQueryToken(item.id);
                  await copyLink(extQueryLink(item.id, res.data.token));
                } catch {
                  message.error('获取访问令牌失败，请重试');
                }
              }}
            />
          </Tooltip>
        </Space>
      ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: colWidths.updated_at ?? 165,
      onHeaderCell: () => ({ width: colWidths.updated_at ?? 165, onResize: handleResize('updated_at'), title: '更新时间' }),
      render: (t: string) => <Typography.Text type="secondary">{t}</Typography.Text>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 290,
      render: (_: unknown, item: ExtQuery) => (
        <Space size={0}>
          <Button type="link" size="small" onClick={() => openEdit(item)}>
            编辑
          </Button>
          <Dropdown
            menu={{
              items: RENEW_PRESETS.map(d => ({
                key: String(d),
                label: `续期 ${d} 天`,
                onClick: () => handleRenew(item, d),
              })),
            }}
            trigger={['click']}
          >
            <Button type="link" size="small" icon={<ClockCircleOutlined />}>
              续期
            </Button>
          </Dropdown>
          <Popconfirm
            title="重置访问令牌"
            description="旧链接将立即失效，并生成新链接。确定重置？"
            okText="重置"
            cancelText="取消"
            onConfirm={() => handleResetToken(item)}
          >
            <Button type="link" size="small">重置令牌</Button>
          </Popconfirm>
          <Button
            type="link"
            size="small"
            danger={item.enabled}
            icon={item.enabled ? <StopOutlined /> : undefined}
            onClick={() => handleToggle(item)}
          >
            {item.enabled ? '停用' : '启用'}
          </Button>
          <Popconfirm
            title="删除外部查询"
            description="删除后链接立即失效，无法恢复。确定删除？"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => handleDelete(item)}
          >
            <Button type="link" size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="外部查询配置"
        description="选择暴露的知识库并配置查询参数，生成带令牌的链接；链接即访问凭证，可随时重置或停用"
        breadcrumb={
          <Breadcrumb
            items={[
              { title: <Link to="/ext-queries">外部查询</Link> },
              { title: '外部查询配置' },
            ]}
          />
        }
        extra={
          <>
            <Button icon={<ReloadOutlined />} onClick={() => { load(); loadOptions(); }}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新建外部查询
            </Button>
          </>
        }
      />

      {/* 表体补 min-height：见 TABLE_BODY_HEIGHT 注释 */}
      <style>{`
        .ext-config-table .ant-table-body {
          min-height: ${TABLE_BODY_HEIGHT};
        }
      `}</style>

      <Table
        className="ext-config-table"
        rowKey="id"
        loading={loading}
        dataSource={items}
        columns={columns}
        pagination={false}
        components={{ header: { cell: ResizableTitle } }}
        scroll={{ x: tableWidth(columns), y: TABLE_BODY_HEIGHT }}
        locale={{ emptyText: '暂无外部查询配置，点击右上角「新建外部查询」创建' }}
      />

      <ExtQueryForm
        open={modalOpen}
        editing={editing}
        kbs={kbs}
        deptName={deptName}
        submitting={submitting}
        onSubmit={handleSubmit}
        onCancel={() => setModalOpen(false)}
      />

      {/* 链接展示（创建/重置后）：含访问凭证，仅展示一次 */}
      <AppModal
        dimension="auto"
        defaultSize={{ w: 680, h: 420 }}
        rememberKey="extq-link"
        title={linkModal?.title}
        open={!!linkModal}
        onCancel={() => setLinkModal(null)}
        footer={
          <Button
            type="primary"
            icon={<LinkOutlined />}
            onClick={() => linkModal && copyLink(linkModal.link)}
          >
            复制链接
          </Button>
        }
      >
        <Typography.Paragraph type="warning" style={{ marginBottom: 8 }}>
          链接内含访问令牌，凭此链接即可查询，请妥善保管，仅发给需要的外部人员。
        </Typography.Paragraph>
        <Typography.Paragraph
          copyable={{ tooltips: ['复制', '已复制'], text: linkModal?.link }}
          code
          style={{ wordBreak: 'break-all', marginBottom: 0 }}
        >
          {linkModal?.link}
        </Typography.Paragraph>
      </AppModal>
    </div>
  );
};

export default ExtQueryConfig;
