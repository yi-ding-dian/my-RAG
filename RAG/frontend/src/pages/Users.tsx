import React, { useCallback, useEffect, useState } from 'react';
import {
  App as AntApp,
  Button,
  Card,
  DatePicker,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ProfileOutlined,
  ReloadOutlined,
  SearchOutlined,
  UserAddOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs, { type Dayjs } from 'dayjs';
import {
  asApiError,
  AuditActionOption,
  AuditLog,
  AuditLogQuery,
  Department,
  User,
  UserCreateInput,
  UserMemory,
  UserMemoryItem,
  UserUpdateInput,
  createDepartment,
  createUser,
  deleteDepartment,
  deleteUser,
  getUserMemory,
  listAuditActions,
  listAuditLogs,
  listDepartments,
  listUsers,
  updateDepartment,
  updateUser,
} from '../api/client';
import { useAuth } from '../auth/AuthContext';
import BatchCreateUsersModal from '../components/BatchCreateUsersModal';
import { UserAvatar } from '../components/MessageList';
import PageHeader from '../components/PageHeader';

const { Text } = Typography;

const roleMeta: Record<User['role'], { color: string; text: string }> = {
  super_admin: { color: 'red', text: '超级管理员' },
  dept_admin: { color: 'blue', text: '部门管理员' },
  user: { color: 'default', text: '普通用户' },
};

/** 部门选择项：空值表示未分配 */
const deptOptions = (departments: Department[]) => [
  { value: '', label: '未分配' },
  ...departments.map(d => ({ value: d.id, label: d.name })),
];

/** 操作类型中文映射（与后端 /api/audit/actions 同源，前端常量兜底/展示） */
const actionLabelMap: Record<string, string> = {
  'auth.login': '登录',
  'auth.change-password': '修改密码',
  'user.create': '创建用户',
  'user.update': '更新用户',
  'user.delete': '删除用户',
  'dept.create': '创建部门',
  'dept.update': '更新部门',
  'dept.delete': '删除部门',
  'kb.create': '创建知识库',
  'kb.update': '更新知识库',
  'kb.delete': '删除知识库',
  'kb.tags-update': '更新知识库标签',
  'kb.rebuild-vectors': '重建向量',
  'doc.upload': '上传文档',
  'doc.rename': '重命名文档',
  'doc.from-url': '网页导入',
  'doc.ingest': '解析文档',
  'doc.delete': '删除文档',
  'doc.purge': '彻底删除文档',
  'doc.restore': '恢复文档',
  'doc.trash-empty': '清空回收站',
  'chat.delete': '删除会话',
  'chat.export': '导出会话',
  'settings.create': '创建配置档案',
  'settings.update': '修改配置',
  'settings.delete': '删除配置档案',
  'settings.activate': '激活配置档案',
  'settings.test-connections': '测试连接',
};

const targetTypeLabelMap: Record<string, string> = {
  user: '用户',
  dept: '部门',
  kb: '知识库',
  doc: '文档',
  chat: '会话',
  config: '配置',
};

/** 审计详情 JSON 美化（解析失败保持原样） */
const prettyDetail = (raw: string | null | undefined): string => {
  if (!raw) return '无';
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
};

/**
 * 用户与部门管理页（super_admin / dept_admin 可访问，路由层已守卫）。
 * super_admin：全量管理（部门选择器/全部门户/部门 CRUD/审计日志）；
 * dept_admin：仅本部门成员（部门固定、角色限 user/dept_admin、部门只读
 * 名称/描述编辑，无创建/删除部门与审计日志）。
 */
const UsersPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { user: me, refreshUser } = useAuth();
  const isDeptAdmin = me?.role === 'dept_admin';

  // ---------- 数据 ----------
  const [users, setUsers] = useState<User[]>([]);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [deptsLoading, setDeptsLoading] = useState(false);
  // 行内更新中的用户（禁行内控件防连点）
  const [updatingId, setUpdatingId] = useState<string | null>(null);

  const loadUsers = useCallback(async () => {
    setUsersLoading(true);
    try {
      const res = await listUsers();
      setUsers(res.data);
    } catch {
      message.error('加载用户列表失败');
    } finally {
      setUsersLoading(false);
    }
  }, [message]);

  const loadDepartments = useCallback(async () => {
    setDeptsLoading(true);
    try {
      const res = await listDepartments();
      setDepartments(res.data);
    } catch {
      message.error('加载部门列表失败');
    } finally {
      setDeptsLoading(false);
    }
  }, [message]);

  useEffect(() => {
    loadUsers();
    loadDepartments();
  }, [loadUsers, loadDepartments]);

  // ---------- 用户：新建/编辑 ----------
  const [userModalOpen, setUserModalOpen] = useState(false);
  // 批量建号弹窗（仅 super_admin 可见）
  const [batchModalOpen, setBatchModalOpen] = useState(false);
  const [editingUser, setEditingUser] = useState<User | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [userForm] = Form.useForm();

  const openCreateUser = () => {
    setEditingUser(null);
    userForm.resetFields();
    userForm.setFieldsValue({ role: 'user' });
    setUserModalOpen(true);
  };

  const openEditUser = (u: User) => {
    setEditingUser(u);
    userForm.resetFields();
    userForm.setFieldsValue({
      display_name: u.display_name,
      role: u.role,
      // 部门管理员不可调整成员部门：不注册该字段（提交时也不携带）
      ...(isDeptAdmin ? {} : { department_id: u.department_id ?? '' }),
      status: u.status,
      password: '',
    });
    setUserModalOpen(true);
  };

  const handleUserSubmit = async () => {
    let values: {
      username?: string;
      password?: string;
      display_name: string;
      role: User['role'];
      department_id: string;
      status?: 'active' | 'disabled';
    };
    try {
      values = await userForm.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      if (editingUser) {
        // 部分更新：传哪个改哪个；密码留空则不重置
        const data: UserUpdateInput = {
          display_name: values.display_name,
          role: values.role,
          status: values.status,
        };
        // 部门管理员不可调整成员部门（后端亦会拒绝跨部门变更）
        if (!isDeptAdmin) data.department_id = values.department_id || null;
        if (values.password) data.password = values.password;
        await updateUser(editingUser.id, data);
        message.success('用户已更新');
        // 编辑的是自己时同步刷新当前会话用户
        if (editingUser.id === me?.id) await refreshUser();
      } else {
        const data: UserCreateInput = {
          username: values.username!,
          password: values.password!,
          display_name: values.display_name,
          role: values.role,
        };
        // 部门管理员创建的用户强制归属本部门（后端同样强制覆盖）
        data.department_id = isDeptAdmin
          ? (me?.department_id ?? null)
          : (values.department_id || null);
        await createUser(data);
        message.success('用户创建成功');
      }
      setUserModalOpen(false);
      await loadUsers();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || (editingUser ? '更新失败' : '创建失败'));
    } finally {
      setSubmitting(false);
    }
  };

  // ---------- 用户：行内改部门/状态 + 删除 ----------
  const handleChangeDepartment = async (row: User, deptId: string) => {
    if (deptId === (row.department_id ?? '')) return;
    setUpdatingId(row.id);
    try {
      await updateUser(row.id, { department_id: deptId || null });
      message.success('部门已更新');
      await loadUsers();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '更新部门失败');
    } finally {
      setUpdatingId(null);
    }
  };

  const handleToggleStatus = async (row: User, checked: boolean) => {
    setUpdatingId(row.id);
    try {
      await updateUser(row.id, { status: checked ? 'active' : 'disabled' });
      message.success(checked ? '已启用' : '已禁用');
      await loadUsers();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '更新状态失败');
    } finally {
      setUpdatingId(null);
    }
  };

  const handleDeleteUser = async (row: User) => {
    try {
      await deleteUser(row.id);
      message.success(`用户「${row.display_name}」已删除`);
      await loadUsers();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  // ---------- 部门：新建/编辑/删除 ----------
  const [deptModalOpen, setDeptModalOpen] = useState(false);
  const [editingDept, setEditingDept] = useState<Department | null>(null);
  const [deptForm] = Form.useForm();

  const openCreateDept = () => {
    setEditingDept(null);
    deptForm.resetFields();
    setDeptModalOpen(true);
  };

  const openEditDept = (d: Department) => {
    setEditingDept(d);
    deptForm.resetFields();
    deptForm.setFieldsValue({ name: d.name, description: d.description || '' });
    setDeptModalOpen(true);
  };

  const handleDeptSubmit = async () => {
    let values: { name: string; description?: string };
    try {
      values = await deptForm.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      if (editingDept) {
        await updateDepartment(editingDept.id, values);
        message.success('部门已更新');
      } else {
        await createDepartment(values);
        message.success('部门创建成功');
      }
      setDeptModalOpen(false);
      await loadDepartments();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || (editingDept ? '更新失败' : '创建失败'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDeleteDept = async (d: Department) => {
    try {
      await deleteDepartment(d.id);
      message.success(`部门「${d.name}」已删除`);
      await Promise.all([loadDepartments(), loadUsers()]);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  // ---------- 用户画像只读查看（管理员：本人可编辑，管理员只读） ----------
  const [memoryModalOpen, setMemoryModalOpen] = useState(false);
  const [memoryView, setMemoryView] = useState<UserMemory | null>(null);
  const [memoryViewLoading, setMemoryViewLoading] = useState(false);
  const [memoryViewUser, setMemoryViewUser] = useState<User | null>(null);

  const openMemoryView = async (row: User) => {
    setMemoryViewUser(row);
    setMemoryView(null);
    setMemoryModalOpen(true);
    setMemoryViewLoading(true);
    try {
      const res = await getUserMemory(row.id);
      setMemoryView(res.data);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '加载用户画像失败');
      setMemoryModalOpen(false);
    } finally {
      setMemoryViewLoading(false);
    }
  };

  // ---------- 审计日志（仅 super_admin，路由层已守卫） ----------
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditPage, setAuditPage] = useState(1);
  const [auditPageSize, setAuditPageSize] = useState(10);
  const [actionOptions, setActionOptions] = useState<AuditActionOption[]>([]);
  const [auditFilters, setAuditFilters] = useState<{
    action?: string;
    username?: string;
    timeRange?: [Dayjs, Dayjs] | null;
  }>({});

  useEffect(() => {
    // 操作类型下拉（/api/audit/actions，一次加载；仅 super_admin 可见审计）
    if (me?.role !== 'super_admin') return;
    listAuditActions()
      .then(res => setActionOptions(res.data.actions))
      .catch(() => message.error('加载操作类型列表失败'));
  }, [message, me?.role]);

  const loadAuditLogs = useCallback(async () => {
    setAuditLoading(true);
    try {
      const params: AuditLogQuery = {
        page: auditPage,
        page_size: auditPageSize,
        action: auditFilters.action || undefined,
        username: auditFilters.username || undefined,
      };
      if (auditFilters.timeRange?.[0] && auditFilters.timeRange[1]) {
        params.start_time = auditFilters.timeRange[0].format('YYYY-MM-DD HH:mm:ss');
        params.end_time = auditFilters.timeRange[1].format('YYYY-MM-DD HH:mm:ss');
      }
      const res = await listAuditLogs(params);
      setAuditLogs(res.data.items);
      setAuditTotal(res.data.total);
    } catch {
      message.error('加载审计日志失败');
    } finally {
      setAuditLoading(false);
    }
  }, [message, auditPage, auditPageSize, auditFilters]);

  // 筛选条件/页码/每页条数变化时自动重新加载（仅 super_admin 可见审计）
  useEffect(() => {
    if (me?.role !== 'super_admin') return;
    loadAuditLogs();
  }, [loadAuditLogs, me?.role]);

  const handleAuditSearch = () => {
    setAuditPage(1); // 筛选后回到第一页（loadAuditLogs 由 useEffect 驱动）
  };

  const handleAuditReset = () => {
    setAuditFilters({});
    setAuditPage(1);
  };

  const auditColumns: ColumnsType<AuditLog> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (v: string) => dayjs(v).format('YYYY-MM-DD HH:mm:ss'),
    },
    {
      title: '用户',
      dataIndex: 'username',
      key: 'username',
      width: 130,
      render: (v: string) => (v ? <Text strong>{v}</Text> : <Text type="secondary">未认证</Text>),
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: 110,
      render: (role: string) => {
        const meta = roleMeta[role as User['role']];
        return meta ? <Tag color={meta.color}>{meta.text}</Tag> : <Tag>{role || '-'}</Tag>;
      },
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: 150,
      render: (a: string) => <Tag color="blue">{actionLabelMap[a] ?? a}</Tag>,
    },
    {
      title: '目标',
      key: 'target',
      width: 200,
      ellipsis: true,
      render: (_, row) => {
        const type = targetTypeLabelMap[row.target_type ?? ''] ?? row.target_type ?? '';
        return row.target_name
          ? `${type ? `${type} · ` : ''}${row.target_name}`
          : (type || '-');
      },
    },
    {
      title: '详情',
      key: 'detail',
      ellipsis: true,
      render: (_, row) => (row.detail ? row.detail.slice(0, 80) : '-'),
    },
    { title: 'IP', dataIndex: 'ip', key: 'ip', width: 140, render: (v: string) => v || '-' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (s: string) =>
        s === 'success' ? <Tag color="green">成功</Tag> : <Tag color="red">失败</Tag>,
    },
  ];

  // ---------- 列定义 ----------
  const userColumns: ColumnsType<User> = [
    {
      // 头像列：有头像走鉴权代理 URL（query token），无头像/加载失败回退默认 SVG
      title: '头像',
      key: 'avatar',
      width: 64,
      align: 'center' as const,
      render: (_, row) => <UserAvatar userId={row.id} avatarKey={row.avatar} />,
    },
    { title: '用户名', dataIndex: 'username', key: 'username', width: 150 },
    {
      title: '显示名',
      dataIndex: 'display_name',
      key: 'display_name',
      width: 150,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: 120,
      render: (role: User['role']) => <Tag color={roleMeta[role].color}>{roleMeta[role].text}</Tag>,
    },
    {
      title: '部门',
      key: 'department',
      width: 160,
      render: (_, row) => (isDeptAdmin
        ? (
          <Text type="secondary">{row.department_name || '未分配'}</Text>
        ) : (
          <Select
            size="small"
            style={{ width: 140 }}
            value={row.department_id ?? ''}
            disabled={updatingId === row.id}
            options={deptOptions(departments)}
            onChange={v => handleChangeDepartment(row, v)}
          />
        )),
    },
    {
      title: '状态',
      key: 'status',
      width: 110,
      render: (_, row) => {
        const switchDisabled = updatingId === row.id || (isDeptAdmin && row.id === me?.id);
        const switchNode = (
          <Switch
            size="small"
            checked={row.status === 'active'}
            checkedChildren="启用"
            unCheckedChildren="禁用"
            disabled={switchDisabled}
            onChange={checked => {
              // 启用：直接执行并轻提示；禁用：走下方 Popconfirm 二次确认
              if (checked) void handleToggleStatus(row, true);
            }}
          />
        );
        // 禁用需二次确认（误触会封禁账号，企业验收反馈）；启用无需确认
        if (row.status === 'active') {
          return (
            <Popconfirm
              title={`确认禁用用户「${row.display_name}」？`}
              description="禁用后该用户将无法登录，但账号与数据保留"
              okText="确认禁用"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              disabled={switchDisabled}
              onConfirm={() => handleToggleStatus(row, false)}
            >
              {switchNode}
            </Popconfirm>
          );
        }
        return switchNode;
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 170,
      render: (_, row) => {
        const isSelf = row.id === me?.id;
        return (
          <Space>
            <Tooltip title="查看画像（只读）">
              <Button
                size="small"
                icon={<ProfileOutlined />}
                onClick={() => openMemoryView(row)}
              />
            </Tooltip>
            <Tooltip title="编辑">
              <Button size="small" icon={<EditOutlined />} onClick={() => openEditUser(row)} />
            </Tooltip>
            {isSelf ? (
              <Tooltip title="不能删除当前登录账号">
                <Button size="small" danger icon={<DeleteOutlined />} disabled />
              </Tooltip>
            ) : (
              <Popconfirm
                title={`确定删除用户「${row.display_name}」？`}
                okButtonProps={{ danger: true }}
                onConfirm={() => handleDeleteUser(row)}
              >
                <Button size="small" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            )}
          </Space>
        );
      },
    },
  ];

  const deptColumns: ColumnsType<Department> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: 200,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    { title: '描述', dataIndex: 'description', key: 'description', ellipsis: true },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      render: (_, row) => (
        <Space>
          <Tooltip title="编辑">
            <Button size="small" icon={<EditOutlined />} onClick={() => openEditDept(row)} />
          </Tooltip>
          {!isDeptAdmin && (
            <Popconfirm
              title={`确定删除部门「${row.name}」？`}
              description="部门下存在用户或知识库时无法删除"
              okButtonProps={{ danger: true }}
              onConfirm={() => handleDeleteDept(row)}
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
        title={isDeptAdmin ? '部门成员管理' : '用户管理'}
        description={isDeptAdmin
          ? '管理本部门成员账号与部门信息（数据与其他部门隔离）'
          : '管理用户账号、部门归属与操作审计日志（超级管理员全量）'}
      />
      <Tabs
        defaultActiveKey="users"
        items={[
          {
            key: 'users',
            label: isDeptAdmin ? '本部门成员' : '用户管理',
            children: (
              <Card
                title={isDeptAdmin
                  ? `本部门成员（${me?.department_name || '未分配'}）`
                  : '用户列表'}
                extra={
                  <Space>
                    <Button icon={<ReloadOutlined />} onClick={() => { loadUsers(); loadDepartments(); }}>
                      刷新
                    </Button>
                    {me?.role === 'super_admin' && (
                      <Button icon={<UserAddOutlined />} onClick={() => setBatchModalOpen(true)}>
                        批量建号
                      </Button>
                    )}
                    <Button type="primary" icon={<PlusOutlined />} onClick={openCreateUser}>
                      新建用户
                    </Button>
                  </Space>
                }
              >
                <Table
                  dataSource={users}
                  columns={userColumns}
                  rowKey="id"
                  loading={usersLoading}
                  pagination={{ pageSize: 10 }}
                  scroll={{ x: 980 }}
                  className="table-zebra"
                />
              </Card>
            ),
          },
          {
            key: 'departments',
            label: '部门管理',
            children: (
              <Card
                title={isDeptAdmin ? '本部门信息' : '部门列表'}
                extra={
                  <Space>
                    <Button icon={<ReloadOutlined />} onClick={loadDepartments}>
                      刷新
                    </Button>
                    {!isDeptAdmin && (
                      <Button type="primary" icon={<PlusOutlined />} onClick={openCreateDept}>
                        新建部门
                      </Button>
                    )}
                  </Space>
                }
              >
                <Table
                  dataSource={departments}
                  columns={deptColumns}
                  rowKey="id"
                  loading={deptsLoading}
                  pagination={{ pageSize: 10 }}
                  className="table-zebra"
                />
              </Card>
            ),
          },
          // 审计日志仅超级管理员可见（dept_admin 无审计权限）
          ...(me?.role === 'super_admin' ? [{
            key: 'audit',
            label: '审计日志',
            children: (
              <Card
                title="审计操作日志"
                extra={
                  <Button
                    icon={<ReloadOutlined />}
                    onClick={() => { loadAuditLogs(); listAuditActions()
                      .then(res => setActionOptions(res.data.actions))
                      .catch(() => undefined); }}
                  >
                    刷新
                  </Button>
                }
              >
                <Space wrap style={{ marginBottom: 16 }}>
                  <Select
                    placeholder="操作类型"
                    allowClear
                    showSearch
                    style={{ width: 200 }}
                    value={auditFilters.action}
                    options={actionOptions.map(o => ({
                      value: o.action,
                      label: `${o.label}（${o.action}）`,
                    }))}
                    onChange={v => setAuditFilters(f => ({ ...f, action: v }))}
                  />
                  <Input
                    placeholder="用户名"
                    allowClear
                    style={{ width: 140 }}
                    value={auditFilters.username}
                    onChange={e => setAuditFilters(f => ({ ...f, username: e.target.value }))}
                    onPressEnter={handleAuditSearch}
                  />
                  <DatePicker.RangePicker
                    showTime
                    style={{ width: 340 }}
                    value={auditFilters.timeRange}
                    onChange={v => setAuditFilters(f => ({
                      ...f,
                      timeRange: v as [Dayjs, Dayjs] | null,
                    }))}
                  />
                  <Button type="primary" icon={<SearchOutlined />} onClick={handleAuditSearch}>
                    搜索
                  </Button>
                  <Button onClick={handleAuditReset}>重置</Button>
                </Space>
                <Table
                  dataSource={auditLogs}
                  columns={auditColumns}
                  rowKey="id"
                  loading={auditLoading}
                  scroll={{ x: 1100 }}
                  className="table-zebra"
                  expandable={{
                    expandedRowRender: row => (
                      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                        {prettyDetail(row.detail)}
                      </pre>
                    ),
                    rowExpandable: row => !!row.detail,
                  }}
                  pagination={{
                    current: auditPage,
                    pageSize: auditPageSize,
                    total: auditTotal,
                    showSizeChanger: true,
                    showTotal: t => `共 ${t} 条`,
                    onChange: (p, ps) => {
                      setAuditPage(p);
                      setAuditPageSize(ps);
                    },
                  }}
                />
              </Card>
            ),
          }] : []),
        ]}
      />

      {/* 新建/编辑用户弹窗 */}
      <Modal
        title={editingUser ? `编辑用户 - ${editingUser.username}` : '新建用户'}
        open={userModalOpen}
        onOk={handleUserSubmit}
        onCancel={() => setUserModalOpen(false)}
        confirmLoading={submitting}
        okText="保存"
        cancelText="取消"
        width={520}
      >
        <Form form={userForm} layout="vertical" size="small">
          {!editingUser && (
            <Form.Item
              name="username"
              label="用户名"
              rules={[{ required: true, message: '请输入用户名' }]}
            >
              <Input placeholder="登录名（唯一）" maxLength={64} />
            </Form.Item>
          )}
          <Form.Item
            name="display_name"
            label="显示名"
            rules={[{ required: true, message: '请输入显示名' }]}
          >
            <Input placeholder="例如：张三" maxLength={64} />
          </Form.Item>
          <Form.Item
            name="role"
            label="角色"
            rules={[{ required: true, message: '请选择角色' }]}
          >
            <Select
              // 部门管理员仅可授予 user/dept_admin；编辑自己时不可改动角色（后端 400）
              disabled={isDeptAdmin && editingUser?.id === me?.id}
              options={isDeptAdmin
                ? [
                  { value: 'dept_admin', label: '部门管理员' },
                  { value: 'user', label: '普通用户' },
                ]
                : [
                  { value: 'super_admin', label: '超级管理员' },
                  { value: 'dept_admin', label: '部门管理员' },
                  { value: 'user', label: '普通用户' },
                ]}
            />
          </Form.Item>
          {!isDeptAdmin && (
            <Form.Item name="department_id" label="所属部门" tooltip="未分配则留空">
              <Select options={deptOptions(departments)} placeholder="请选择部门（可留空）" />
            </Form.Item>
          )}
          {editingUser ? (
            <Form.Item
              name="status"
              label="状态"
              tooltip="禁用后该用户无法登录与访问"
              rules={[{ required: true, message: '请选择状态' }]}
            >
              <Select
                // 部门管理员不可禁用自己（后端 400）
                disabled={isDeptAdmin && editingUser.id === me?.id}
                options={[
                  { value: 'active', label: '启用' },
                  { value: 'disabled', label: '禁用' },
                ]}
              />
            </Form.Item>
          ) : (
            <Form.Item
              name="password"
              label="初始密码"
              tooltip="新用户首次登录时须使用该密码，建议提醒其登录后修改"
              rules={[
                { required: true, message: '请输入初始密码' },
                { min: 6, message: '初始密码至少 6 位' },
              ]}
            >
              <Input.Password placeholder="至少 6 位" maxLength={128} />
            </Form.Item>
          )}
          {editingUser && (
            <Form.Item
              name="password"
              label="重置密码"
              tooltip="留空则不修改密码"
              rules={[{ min: 6, message: '密码至少 6 位' }]}
            >
              <Input.Password placeholder="留空则不修改密码，重置须至少 6 位" maxLength={128} />
            </Form.Item>
          )}
        </Form>
      </Modal>

      {/* 新建/编辑部门弹窗 */}
      <Modal
        title={editingDept ? `编辑部门 - ${editingDept.name}` : '新建部门'}
        open={deptModalOpen}
        onOk={handleDeptSubmit}
        onCancel={() => setDeptModalOpen(false)}
        confirmLoading={submitting}
        okText="保存"
        cancelText="取消"
      >
        <Form form={deptForm} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入部门名称' }]}>
            <Input placeholder="例如：研发部" maxLength={64} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea placeholder="选填，简单描述该部门" maxLength={200} rows={3} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 批量建号弹窗（复用现有 createUser/createDepartment API，不新增后端接口） */}
      <BatchCreateUsersModal
        open={batchModalOpen}
        departments={departments}
        onCancel={() => setBatchModalOpen(false)}
        onSuccess={() => loadUsers()}
      />

      {/* 查看用户画像（只读；编辑仅本人可在个人设置页操作） */}
      <Modal
        title={`用户画像 - ${memoryViewUser?.username ?? ''}`}
        open={memoryModalOpen}
        onCancel={() => setMemoryModalOpen(false)}
        footer={
          <Button onClick={() => setMemoryModalOpen(false)}>关闭</Button>
        }
        width={560}
      >
        {memoryViewLoading ? (
          <div style={{ padding: 24, textAlign: 'center' }}>
            <Text type="secondary">加载中…</Text>
          </div>
        ) : memoryView ? (
          <div>
            <Space style={{ marginBottom: 12 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                个性化记忆：
              </Text>
              <Tag color={memoryView.memory_enabled ? 'success' : 'default'}>
                {memoryView.memory_enabled ? '已开启' : '已关闭'}
              </Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {memoryView.updated_at
                  ? `最近更新 ${dayjs(memoryView.updated_at).format('YYYY-MM-DD HH:mm:ss')}`
                  : '暂无更新记录'}
              </Text>
            </Space>
            {memoryView.items.length > 0 ? (
              <List
                size="small"
                dataSource={memoryView.items}
                rowKey="id"
                renderItem={(item: UserMemoryItem) => (
                  <List.Item>
                    <Space wrap>
                      <Tag color={item.type === 'profile' ? 'blue' : 'purple'}>
                        {item.type === 'profile' ? '画像' : '偏好'}
                      </Tag>
                      <Text>{item.content}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        把握度 {Math.round(item.confidence * 100)}%
                      </Text>
                    </Space>
                  </List.Item>
                )}
              />
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无画像条目"
              />
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              管理员仅可查看（只读）；编辑/删除需用户本人登录个人设置页操作
            </Text>
          </div>
        ) : null}
      </Modal>
    </div>
  );
};

export default UsersPage;
