import React, { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  App as AntApp,
  Breadcrumb,
  Button,
  Card,
  Descriptions,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import {
  ExtQuery,
  ExtQueryLog,
  listExtQueries,
  listExtQueryLogs,
} from '../../shared/api/client';
import PageHeader from '../../shared/components/layout/PageHeader';
import { expiryColor, expiryText } from './expiry';

/** 接入方式 → 展示文案与颜色 */
const SOURCE_META: Record<string, { text: string; color: string }> = {
  chat: { text: '对外网页', color: 'blue' },
  query: { text: 'MCP / Agent', color: 'purple' },
};

/**
 * 表体高度：撑满视口剩余空间
 *
 * 270 ≈ 记录页的 260 + 链接信息卡的额外高度（实测表体顶边比记录页低 7px）。
 * 与记录页同理，需配合下面的 min-height 规则——antd 的 scroll.y 只写
 * max-height，数据少时撑不满。
 */
const TABLE_BODY_HEIGHT = 'max(320px, calc(100vh - 270px))';

/**
 * 单个外部链接的访问明细（从记录页点链接名进入）
 *
 * 面包屑带上链接名称，便于从记录总表下钻后仍知道自己在看哪条链接；
 * 链接配置可能已被删除——此时记录仍在（config_name 冗余存储），
 * 顶部信息区降级为"已删除，仅保留记录"。
 */
const ExtQueryLogDetail: React.FC = () => {
  const { configId } = useParams<{ configId: string }>();
  const { message } = AntApp.useApp();

  const [link, setLink] = useState<ExtQuery | null>(null);
  const [linkLoaded, setLinkLoaded] = useState(false);
  const [rows, setRows] = useState<ExtQueryLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (p = page, ps = pageSize) => {
    if (!configId) return;
    setLoading(true);
    try {
      const res = await listExtQueryLogs({
        config_id: configId, page: p, page_size: ps,
      });
      setRows(res.data.items);
      setTotal(res.data.total);
    } catch {
      message.error('加载访问记录失败');
    } finally {
      setLoading(false);
    }
  }, [configId, page, pageSize, message]);

  useEffect(() => {
    load(1, pageSize);
    // 链接信息：从列表里找（无需单条详情接口）；找不到 = 配置已删除
    listExtQueries()
      .then(res => {
        setLink(res.data.find(l => l.id === configId) ?? null);
        setLinkLoaded(true);
      })
      .catch(() => setLinkLoaded(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configId]);

  const linkName = link?.name
    ?? rows[0]?.config_name
    ?? (linkLoaded ? '(已删除的链接)' : '加载中...');

  const columns = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 180,
      render: (t: string) => <Typography.Text style={{ fontSize: 13 }}>{t}</Typography.Text>,
    },
    {
      title: '查询问题',
      dataIndex: 'query',
      render: (q: string) => (
        <Typography.Text ellipsis={{ tooltip: q }} style={{ maxWidth: 460 }}>
          {q}
        </Typography.Text>
      ),
    },
    {
      title: '命中',
      dataIndex: 'hit_count',
      width: 80,
      align: 'center' as const,
      render: (n: number) => (n > 0 ? <Tag color="green">{n}</Tag> : <Tag>0</Tag>),
    },
    {
      title: '接入方式',
      dataIndex: 'source',
      width: 120,
      render: (s: string) => {
        const meta = SOURCE_META[s] ?? { text: s, color: 'default' };
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: '来源 IP',
      dataIndex: 'client_ip',
      width: 150,
      render: (v: string) => (
        <Typography.Text code style={{ fontSize: 12 }}>{v || '-'}</Typography.Text>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="链接访问明细"
        breadcrumb={
          <Breadcrumb
            items={[
              { title: <Link to="/ext-queries">外部查询</Link> },
              { title: <Link to="/ext-queries/logs">外部查询记录</Link> },
              { title: linkName },
            ]}
          />
        }
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => load()}>
              刷新
            </Button>
          </Space>
        }
      />

      <Card size="small" style={{ marginBottom: 12 }}>
        <Descriptions size="small" column={4} items={[
          {
            key: 'name',
            label: '链接名称',
            children: linkName,
          },
          {
            key: 'enabled',
            label: '状态',
            children: link
              ? (link.enabled
                ? <Tag color="green">启用</Tag>
                : <Tag color="default">已停用</Tag>)
              : <Tag>配置已删除</Tag>,
          },
          {
            key: 'expires',
            label: '有效期',
            children: link ? (
              <Tag color={expiryColor(link.expires_at)}>
                {expiryText(link.expires_at)}
              </Tag>
            ) : '-',
          },
          {
            key: 'kbs',
            label: '暴露知识库',
            children: link
              ? (link.kb_names ?? []).map(k => <Tag key={k.id}>{k.name}</Tag>)
              : '-',
          },
        ]} />
      </Card>

      {/* 表体补 min-height：见 TABLE_BODY_HEIGHT 注释 */}
      <style>{`
        .ext-log-detail-table .ant-table-body {
          min-height: ${TABLE_BODY_HEIGHT};
        }
      `}</style>

      <Table
        className="ext-log-detail-table"
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={rows}
        columns={columns}
        scroll={{ y: TABLE_BODY_HEIGHT }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          showTotal: t => `共 ${t} 条`,
          onChange: (p, ps) => {
            setPage(p);
            setPageSize(ps);
            load(p, ps);
          },
        }}
        locale={{ emptyText: '该链接暂无访问记录' }}
      />
    </div>
  );
};

export default ExtQueryLogDetail;
