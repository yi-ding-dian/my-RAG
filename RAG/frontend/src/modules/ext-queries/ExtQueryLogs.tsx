import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  App as AntApp,
  Breadcrumb,
  Button,
  DatePicker,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import type { Dayjs } from 'dayjs';import {
  ExtQuery,
  ExtQueryLog,
  listExtQueries,
  listExtQueryLogs,
} from '../../shared/api/client';
import PageHeader from '../../shared/components/layout/PageHeader';

const { RangePicker } = DatePicker;
const TIME_FMT = 'YYYY-MM-DD HH:mm:ss';

/**
 * 表体高度：撑满视口剩余空间
 *
 * 260 ≈ 页头与筛选区 + 分页器(40) + 底部留白(24) + 表头。实测 720 视口下
 * 表体顶边在 195，取 260 时表体 460、分页器正好贴底且不产生页面滚动。
 * 配合下面的 min-height 规则使用——antd 的 scroll.y 只写 max-height，
 * 数据少时表体仍按内容收缩，分页器会悬在半空、下面留一大片空白。
 */
const TABLE_BODY_HEIGHT = 'max(360px, calc(100vh - 260px))';

/** 接入方式 → 展示文案与颜色 */
const SOURCE_META: Record<string, { text: string; color: string }> = {
  chat: { text: '对外网页', color: 'blue' },
  query: { text: 'MCP / Agent', color: 'purple' },
};

/**
 * 外部查询记录（仅 super_admin）：全部外部链接的访问日志
 *
 * 外部人员无需账号，因此 IP 是追溯来源的唯一线索——表里单列展示，
 * 也支持按 IP 模糊筛选定位某个来源的全部访问。
 */
const ExtQueryLogs: React.FC = () => {
  const navigate = useNavigate();
  const { message } = AntApp.useApp();

  const [rows, setRows] = useState<ExtQueryLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [loading, setLoading] = useState(false);
  const [links, setLinks] = useState<ExtQuery[]>([]);

  // 筛选条件（改动后需点「查询」才生效，避免每敲一个字符就请求一次）
  const [linkId, setLinkId] = useState<string | undefined>(undefined);
  const [ip, setIp] = useState('');
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  // 已生效的筛选（点查询时从上面拷贝过来）
  const [applied, setApplied] = useState<{
    linkId?: string; ip?: string; start?: string; end?: string;
  }>({});

  const load = useCallback(async (
    p = page, ps = pageSize, cond = applied,
  ) => {
    setLoading(true);
    try {
      const res = await listExtQueryLogs({
        config_id: cond.linkId,
        ip: cond.ip || undefined,
        start: cond.start,
        end: cond.end,
        page: p,
        page_size: ps,
      });
      setRows(res.data.items);
      setTotal(res.data.total);
    } catch {
      message.error('加载外部查询记录失败');
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, applied, message]);

  useEffect(() => {
    load(1, pageSize, {});
    // 链接下拉用于筛选：只取一次
    listExtQueries()
      .then(res => setLinks(res.data))
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSearch = () => {
    const cond = {
      linkId,
      ip: ip.trim(),
      start: range?.[0] ? range[0].startOf('day').format(TIME_FMT) : undefined,
      end: range?.[1] ? range[1].endOf('day').format(TIME_FMT) : undefined,
    };
    setApplied(cond);
    setPage(1);
    load(1, pageSize, cond);
  };

  const handleReset = () => {
    setLinkId(undefined);
    setIp('');
    setRange(null);
    setApplied({});
    setPage(1);
    load(1, pageSize, {});
  };

  const linkOptions = useMemo(
    () => links.map(l => ({ value: l.id, label: l.name })),
    [links],
  );

  const columns = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 175,
      render: (t: string) => <Typography.Text style={{ fontSize: 13 }}>{t}</Typography.Text>,
    },
    {
      title: '链接名称',
      dataIndex: 'config_name',
      width: 180,
      render: (name: string, row: ExtQueryLog) => (
        <Tooltip title="查看该链接的全部访问记录">
          <Typography.Link
            onClick={() => navigate(`/ext-queries/logs/${row.config_id}`)}
          >
            {name || '(已删除的链接)'}
          </Typography.Link>
        </Tooltip>
      ),
    },
    {
      title: '查询问题',
      dataIndex: 'query',
      render: (q: string) => (
        <Typography.Text ellipsis={{ tooltip: q }} style={{ maxWidth: 420 }}>
          {q}
        </Typography.Text>
      ),
    },
    {
      title: '命中',
      dataIndex: 'hit_count',
      width: 80,
      align: 'center' as const,
      render: (n: number) =>
        n > 0 ? <Tag color="green">{n}</Tag> : <Tag>0</Tag>,
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
      width: 140,
      render: (v: string) => (
        <Typography.Text code style={{ fontSize: 12 }}>
          {v || '-'}
        </Typography.Text>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="外部查询记录"
        description="外部链接的访问日志（含来源 IP 与接入方式）；记录保留 90 天，超期自动清理"
        breadcrumb={
          <Breadcrumb
            items={[
              { title: <Link to="/ext-queries">外部查询</Link> },
              { title: '外部查询记录' },
            ]}
          />
        }
        extra={
          <Button icon={<ReloadOutlined />} onClick={() => load()}>
            刷新
          </Button>
        }
      />

      {/* 筛选区 */}
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          allowClear
          placeholder="按链接筛选"
          style={{ width: 200 }}
          value={linkId}
          onChange={v => setLinkId(v)}
          options={linkOptions}
          optionFilterProp="label"
        />
        <RangePicker
          showTime={{ format: 'HH:mm' }}
          format="YYYY-MM-DD HH:mm"
          value={range}
          onChange={v => setRange(v as [Dayjs | null, Dayjs | null] | null)}
          placeholder={['开始时间', '结束时间']}
        />
        <Input
          allowClear
          placeholder="按来源 IP 筛选（支持片段）"
          style={{ width: 220 }}
          value={ip}
          onChange={e => setIp(e.target.value)}
          onPressEnter={handleSearch}
          prefix={<SearchOutlined style={{ color: '#bfbfbf' }} />}
        />
        <Button type="primary" onClick={handleSearch}>
          查询
        </Button>
        <Button onClick={handleReset}>重置</Button>
      </Space>

      {/* 表体补 min-height：见 TABLE_BODY_HEIGHT 注释 */}
      <style>{`
        .ext-logs-table .ant-table-body {
          min-height: ${TABLE_BODY_HEIGHT};
        }
      `}</style>

      <Table
        className="ext-logs-table"
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={rows}
        columns={columns}
        // 数据多时表头固定、表体内部滚动；数据少时由上面的 min-height 撑满
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
        locale={{ emptyText: '暂无查询记录' }}
      />
    </div>
  );
};

export default ExtQueryLogs;
