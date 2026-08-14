import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  App as AntApp,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  CopyOutlined,
  LinkOutlined,
  PlusOutlined,
  ReloadOutlined,
  StopOutlined,
} from '@ant-design/icons';
import {
  ExtQuery,
  ExtQueryConfig,
  KnowledgeBase,
  createExtQuery,
  deleteExtQuery,
  extQueryLink,
  listDepartments,
  listExtQueries,
  listKbs,
  resetExtQueryToken,
  toggleExtQuery,
  updateExtQuery,
} from '../api/client';
import PageHeader from '../components/PageHeader';

const { TextArea } = Input;

/** 查询配置表单值（空 = 跟随全局，提交时转 null） */
interface ConfigFormValues {
  system_prompt?: string;
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
  top_k?: number | null;
  similarity_threshold?: number | null;
  enable_multi_turn?: boolean;
  history_rounds?: number | null;
}

interface FormValues {
  name: string;
  kb_ids: string[];
  config: ConfigFormValues;
}

/** 表单配置 → 提交载荷（空值转 null = 跟随全局；system_prompt 空串 = 默认模板） */
const configToPayload = (c: ConfigFormValues): ExtQueryConfig => ({
  system_prompt: c.system_prompt ?? '',
  temperature: c.temperature ?? null,
  top_p: c.top_p ?? null,
  max_tokens: c.max_tokens ?? null,
  top_k: c.top_k ?? null,
  similarity_threshold: c.similarity_threshold ?? null,
  enable_multi_turn: c.enable_multi_turn ?? true,
  history_rounds: c.history_rounds ?? null,
});

/** 默认配置表单值 */
const defaultConfigForm = (config: ExtQueryConfig = {}): ConfigFormValues => ({
  system_prompt: config.system_prompt ?? '',
  temperature: config.temperature ?? null,
  top_p: config.top_p ?? null,
  max_tokens: config.max_tokens ?? null,
  top_k: config.top_k ?? null,
  similarity_threshold: config.similarity_threshold ?? null,
  enable_multi_turn: config.enable_multi_turn ?? true,
  history_rounds: config.history_rounds ?? null,
});

/**
 * 外部查询管理（仅 super_admin）：将选定的知识库暴露为带 token 的查询链接，
 * 外部人员无需账号即可查询。链接 = 访问凭证，可复制分发/随时重置/停用。
 */
const ExtQueriesPage: React.FC = () => {
  const { message } = AntApp.useApp();

  const [items, setItems] = useState<ExtQuery[]>([]);
  const [loading, setLoading] = useState(false);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [deptName, setDeptName] = useState<Record<string, string>>({});

  // 新建/编辑弹窗
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ExtQuery | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<FormValues>();

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

  // 知识库下拉选项：库名 + （部门名 / 全局）
  const kbOptions = useMemo(
    () =>
      kbs.map(k => ({
        value: k.id,
        label: `${k.name}（${k.department_id ? deptName[k.department_id] ?? '未知部门' : '全局'}）`,
      })),
    [kbs, deptName],
  );

  const kbNameOf = useCallback(
    (item: ExtQuery) =>
      (item.kb_names ?? []).map(
        k => `${k.name}${k.department_id ? `（${deptName[k.department_id] ?? '未知部门'}）` : '（全局）'}`,
      ),
    [deptName],
  );

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ config: defaultConfigForm() });
    setModalOpen(true);
  };

  const openEdit = (item: ExtQuery) => {
    setEditing(item);
    form.setFieldsValue({
      name: item.name,
      kb_ids: item.kb_ids,
      config: defaultConfigForm(item.config),
    });
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

  const handleSubmit = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      const payload = {
        name: values.name.trim(),
        kb_ids: values.kb_ids,
        config: configToPayload(values.config),
      };
      if (editing) {
        await updateExtQuery(editing.id, payload);
        message.success('外部查询已更新');
        setModalOpen(false);
        await load();
        // 编辑不展示链接（token 不变，原链接继续有效）
      } else {
        const res = await createExtQuery(payload);
        setModalOpen(false);
        await load();
        setLinkModal({
          title: '外部查询链接已生成',
          link: extQueryLink(res.data.id, res.data.token),
        });
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || (editing ? '更新失败' : '创建失败'));
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
    } catch (e: any) {
      message.error(e.response?.data?.detail || '重置令牌失败');
    }
  };

  const handleToggle = async (item: ExtQuery) => {
    try {
      const res = await toggleExtQuery(item.id);
      message.success(res.data.enabled ? '已启用' : '已停用（链接立即失效）');
      await load();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '操作失败');
    }
  };

  const handleDelete = async (item: ExtQuery) => {
    try {
      await deleteExtQuery(item.id);
      message.success(`外部查询「${item.name}」已删除`);
      await load();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '删除失败');
    }
  };

  const columns = [
    {
      title: '名称',
      dataIndex: 'name',
      render: (name: string) => (
        <Typography.Text strong>{name}</Typography.Text>
      ),
    },
    {
      title: '暴露的知识库',
      dataIndex: 'kb_ids',
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
      width: 90,
      render: (enabled: boolean) =>
        enabled ? <Tag color="green">启用</Tag> : <Tag color="default">已停用</Tag>,
    },
    {
      title: '链接',
      dataIndex: 'token',
      width: 130,
      render: (_: unknown, item: ExtQuery) => (
        <Space size={4}>
          <Typography.Text code ellipsis style={{ maxWidth: 90, fontSize: 12 }}>
            /ext-query/{item.id.slice(0, 8)}…
          </Typography.Text>
          <Tooltip title={item.enabled ? '复制分享链接' : '已停用，无法查询'}>
            <Button
              type="text"
              size="small"
              icon={<CopyOutlined />}
              disabled={!item.enabled}
              onClick={() => copyLink(extQueryLink(item.id, item.token))}
            />
          </Tooltip>
        </Space>
      ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 170,
      render: (t: string) => <Typography.Text type="secondary">{t}</Typography.Text>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 230,
      render: (_: unknown, item: ExtQuery) => (
        <Space size={0}>
          <Button type="link" size="small" onClick={() => openEdit(item)}>
            编辑
          </Button>
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
        title="外部查询"
        description="将知识库开放给外部人员查询（无需系统账号）：选择暴露的知识库并配置查询参数后生成带令牌的链接，外部人员打开链接即可提问。链接即访问凭证，请妥善保管；泄露可随时重置或停用。"
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

      <Table
        rowKey="id"
        loading={loading}
        dataSource={items}
        columns={columns}
        pagination={false}
        locale={{ emptyText: '暂无外部查询配置，点击右上角「新建外部查询」创建' }}
      />

      {/* 新建 / 编辑弹窗 */}
      <Modal
        title={editing ? '编辑外部查询' : '新建外部查询'}
        open={modalOpen}
        onOk={handleSubmit}
        onCancel={() => setModalOpen(false)}
        confirmLoading={submitting}
        okText={editing ? '保存' : '生成链接'}
        cancelText="取消"
        width={640}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, whitespace: true, message: '请输入名称' }]}
          >
            <Input placeholder="例如：产品知识对外查询" maxLength={50} showCount />
          </Form.Item>
          <Form.Item
            name="kb_ids"
            label="暴露的知识库（多选，1-10 个）"
            rules={[{ required: true, message: '请至少选择一个知识库' }]}
          >
            <Select
              mode="multiple"
              placeholder="选择要对外的知识库（跨部门可见）"
              options={kbOptions}
              optionFilterProp="label"
              maxTagCount={5}
            />
          </Form.Item>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            查询参数（留空 = 跟随全局配置）
          </Typography.Text>
          <Form.Item name={['config', 'system_prompt']} label="系统提示词" style={{ marginTop: 8 }}>
            <TextArea
              rows={3}
              placeholder="留空使用默认提示词；可含 {knowledge} 占位符（检索原文逐字注入）或 {refs}（带来源标注的引用内容）"
            />
          </Form.Item>
          <Space size={16} wrap>
            <Form.Item name={['config', 'temperature']} label="温度（0-2）">
              <InputNumber min={0} max={2} step={0.1} style={{ width: 140 }} placeholder="跟随全局" />
            </Form.Item>
            <Form.Item name={['config', 'top_p']} label="Top P（0-1）">
              <InputNumber min={0} max={1} step={0.05} style={{ width: 140 }} placeholder="跟随全局" />
            </Form.Item>
            <Form.Item name={['config', 'top_k']} label="检索条数（1-20）">
              <InputNumber min={1} max={20} step={1} style={{ width: 140 }} placeholder="跟随全局" />
            </Form.Item>
            <Form.Item name={['config', 'similarity_threshold']} label="相似度阈值（0-1）">
              <InputNumber min={0} max={1} step={0.05} style={{ width: 140 }} placeholder="跟随全局" />
            </Form.Item>
            <Form.Item name={['config', 'max_tokens']} label="最大输出 Token">
              <InputNumber min={1} max={16384} step={128} style={{ width: 140 }} placeholder="跟随全局" />
            </Form.Item>
            <Form.Item name={['config', 'history_rounds']} label="历史轮数（1-20）">
              <InputNumber min={1} max={20} step={1} style={{ width: 140 }} placeholder="跟随全局" />
            </Form.Item>
            <Form.Item
              name={['config', 'enable_multi_turn']}
              label="多轮对话"
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      {/* 链接展示（创建/重置后）：含访问凭证，仅展示一次 */}
      <Modal
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
      </Modal>
    </div>
  );
};

export default ExtQueriesPage;
