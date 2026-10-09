/**
 * 用户管理 Tab：用户表格（含行内改部门/启停/删除）+ 三个弹窗的编排。
 *
 * 从 Users.tsx 抽出来。数据（users/departments）与加载函数由容器持有并传入——
 * departments 既服务本 Tab 的"改部门"下拉，也是部门 Tab 的数据源，两处必须同源。
 *
 * 三个弹窗（编辑用户 / 批量建号 / 查看画像）都由本 Tab 编排：
 * 它们的触发按钮就在这张表的工具栏与操作列里，就近管理最内聚。
 */
import { useState } from 'react';
import {
  App as AntApp, Button, Card, Popconfirm, Select, Space, Switch, Table,
  Tag, Tooltip, Typography,
} from 'antd';
import {
  DeleteOutlined, EditOutlined, PlusOutlined, ProfileOutlined,
  ReloadOutlined, UserAddOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';

import {
  asApiError, deleteUser, updateUser, Department, User,
} from '../../../shared/api/client';
import { useResizableColumns } from '../../../shared/hooks/useResizableColumns';
import LeftPagination from '../../../shared/components/layout/LeftPagination';
import ResizableTitle from '../../../shared/components/common/ResizableTitle';
import { UserAvatar } from '../../chat/components/MessageList';
import BatchCreateUsersModal from './BatchCreateUsersModal';
import { UserEditModal } from './UserEditModal';
import { UserMemoryModal } from './UserMemoryModal';
import { deptOptions, roleMeta } from '../shared';

const { Text } = Typography;

export interface UsersTabProps {
  users: User[];
  /** 部门列表：行内"改部门"下拉与编辑弹窗的候选（与部门 Tab 同源） */
  departments: Department[];
  loading: boolean;
  isDeptAdmin: boolean;
  me: User | null;
  /** 用户变更后刷新 */
  onRefresh: () => Promise<void>;
  /** 一并刷新部门（"刷新"按钮与原行为一致会同时刷两张表；批量建号可能建部门） */
  onRefreshDepartments: () => Promise<void>;
  /** 编辑的是自己时刷新会话用户 */
  onRefreshMe: () => Promise<void>;
}

export const UsersTab = ({
  users, departments, loading, isDeptAdmin, me,
  onRefresh, onRefreshDepartments, onRefreshMe,
}: UsersTabProps) => {
  const { message } = AntApp.useApp();
  // 列宽拖拽（用户表）：拖拽后的列宽存 colWidths，scroll.x 动态对齐列宽和
  const { colWidths: uColW, handleResize: uResize, tableWidth: uTableW } = useResizableColumns<User>();

  // 前端分页（用户列表一次拉全量，本地分页；分页条固定左下角不随表格滚动）
  const [userPage, setUserPage] = useState(1);
  const [userPageSize, setUserPageSize] = useState(10);
  // 行内更新中的用户（禁行内控件防连点）
  const [updatingId, setUpdatingId] = useState<string | null>(null);

  // ---------- 弹窗编排 ----------
  const [userModalOpen, setUserModalOpen] = useState(false);
  const [batchModalOpen, setBatchModalOpen] = useState(false);
  const [editingUser, setEditingUser] = useState<User | null>(null);
  const [memoryModalOpen, setMemoryModalOpen] = useState(false);
  const [memoryViewUser, setMemoryViewUser] = useState<User | null>(null);

  const openCreateUser = () => {
    setEditingUser(null);
    setUserModalOpen(true);
  };

  const openEditUser = (u: User) => {
    setEditingUser(u);
    setUserModalOpen(true);
  };

  const openMemoryView = (row: User) => {
    setMemoryViewUser(row);
    setMemoryModalOpen(true);
  };

  // ---------- 行内操作 ----------
  const handleChangeDepartment = async (row: User, deptId: string) => {
    if (deptId === (row.department_id ?? '')) return;
    setUpdatingId(row.id);
    try {
      await updateUser(row.id, { department_id: deptId || null });
      message.success('部门已更新');
      await onRefresh();
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
      await onRefresh();
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
      await onRefresh();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  const userColumns: ColumnsType<User> = [
    {
      // 头像列：有头像走鉴权代理 URL（query token），无头像/加载失败回退默认 SVG
      title: '头像',
      key: 'avatar',
      width: 64,
      align: 'center' as const,
      render: (_, row) => <UserAvatar userId={row.id} avatarKey={row.avatar} />,
    },
    {
      title: '用户名', dataIndex: 'username', key: 'username',
      width: uColW.username ?? 150,
      onHeaderCell: () => ({ width: uColW.username ?? 150, onResize: uResize('username'), title: '用户名' }),
    },
    {
      title: '显示名',
      dataIndex: 'display_name',
      key: 'display_name',
      width: uColW.display_name ?? 150,
      onHeaderCell: () => ({ width: uColW.display_name ?? 150, onResize: uResize('display_name'), title: '显示名' }),
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: uColW.role ?? 120,
      onHeaderCell: () => ({ width: uColW.role ?? 120, onResize: uResize('role'), title: '角色' }),
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
      width: uColW.created_at ?? 170,
      onHeaderCell: () => ({ width: uColW.created_at ?? 170, onResize: uResize('created_at'), title: '创建时间' }),
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

  return (
    <>
      <Card
        title={isDeptAdmin
          ? `本部门成员（${me?.department_name || '未分配'}）`
          : '用户列表'}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => { onRefresh(); onRefreshDepartments(); }}>
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
        style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}
        styles={{ body: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } }}
      >
        {/* Table 区：flex:1 撑满剩余，内部滚动，分页条固定左下不动 */}
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        <Table
          dataSource={users.slice((userPage - 1) * userPageSize, userPage * userPageSize)}
          columns={userColumns}
          rowKey="id"
          loading={loading}
          pagination={false}
          components={{ header: { cell: ResizableTitle } }}
          scroll={{ x: uTableW(userColumns) }}
          sticky
          size="middle"
          className="table-zebra"
          style={{ flex: 1, minHeight: 0 }}
        />
        </div>
        {/* 分页条：统一 LeftPagination（固定左下） */}
        <LeftPagination
          page={userPage}
          pageSize={userPageSize}
          total={users.length}
          onChange={(p, ps) => { setUserPage(p); setUserPageSize(ps); }}
        />
      </Card>

      {/* 新建/编辑用户弹窗（表单回填由弹窗自己按 editingUser 完成） */}
      <UserEditModal
        open={userModalOpen}
        editingUser={editingUser}
        departments={departments}
        isDeptAdmin={isDeptAdmin}
        me={me}
        onClose={() => setUserModalOpen(false)}
        onSaved={onRefresh}
        onRefreshMe={onRefreshMe}
      />

      {/* 批量建号弹窗（复用现有 createUser/createDepartment API，不新增后端接口） */}
      <BatchCreateUsersModal
        open={batchModalOpen}
        departments={departments}
        onCancel={() => setBatchModalOpen(false)}
        onSuccess={() => onRefresh()}
      />

      {/* 查看用户画像（只读；编辑仅本人可在个人设置页操作） */}
      <UserMemoryModal
        open={memoryModalOpen}
        user={memoryViewUser}
        onClose={() => setMemoryModalOpen(false)}
      />
    </>
  );
};
