/**
 * 部门管理 Tab：部门表格 + 新建/编辑/删除。
 *
 * 从 Users.tsx 抽出来。数据（departments）与加载函数由容器持有并传入——
 * 它同时也服务用户 Tab 的"改部门"下拉，两处必须同源。
 */
import { useState } from 'react';
import {
  App as AntApp, Button, Card, Popconfirm, Space, Table, Tooltip, Typography,
} from 'antd';
import { DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';

import {
  asApiError, deleteDepartment, Department,
} from '../../../shared/api/client';
import { useResizableColumns } from '../../../shared/hooks/useResizableColumns';
import LeftPagination from '../../../shared/components/layout/LeftPagination';
import ResizableTitle from '../../../shared/components/common/ResizableTitle';
import { DeptEditModal } from './DeptEditModal';

const { Text } = Typography;

export interface DepartmentsTabProps {
  departments: Department[];
  loading: boolean;
  isDeptAdmin: boolean;
  /** 部门变更后刷新 */
  onRefresh: () => Promise<void>;
  /** 删除部门后一并刷新用户（用户可能归属该部门） */
  onRefreshUsers: () => Promise<void>;
}

export const DepartmentsTab = ({
  departments, loading, isDeptAdmin, onRefresh, onRefreshUsers,
}: DepartmentsTabProps) => {
  const { message } = AntApp.useApp();
  // 列宽拖拽（部门表）
  const { colWidths: dColW, handleResize: dResize, tableWidth: dTableW } = useResizableColumns<Department>();

  // 前端分页（部门列表一次拉全量，本地分页）
  const [deptPage, setDeptPage] = useState(1);
  const [deptPageSize, setDeptPageSize] = useState(10);

  // ---------- 弹窗编排 ----------
  const [deptModalOpen, setDeptModalOpen] = useState(false);
  const [editingDept, setEditingDept] = useState<Department | null>(null);

  const openCreateDept = () => {
    setEditingDept(null);
    setDeptModalOpen(true);
  };

  const openEditDept = (d: Department) => {
    setEditingDept(d);
    setDeptModalOpen(true);
  };

  const handleDeleteDept = async (d: Department) => {
    try {
      await deleteDepartment(d.id);
      message.success(`部门「${d.name}」已删除`);
      await Promise.all([onRefresh(), onRefreshUsers()]);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  const deptColumns: ColumnsType<Department> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: dColW.name ?? 200,
      onHeaderCell: () => ({ width: dColW.name ?? 200, onResize: dResize('name'), title: '名称' }),
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '描述', dataIndex: 'description', key: 'description', ellipsis: true,
      width: dColW.description ?? 300,
      onHeaderCell: () => ({ width: dColW.description ?? 300, onResize: dResize('description'), title: '描述' }),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: dColW.created_at ?? 170,
      onHeaderCell: () => ({ width: dColW.created_at ?? 170, onResize: dResize('created_at'), title: '创建时间' }),
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
    <>
      <Card
        title={isDeptAdmin ? '本部门信息' : '部门列表'}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={onRefresh}>
              刷新
            </Button>
            {!isDeptAdmin && (
              <Button type="primary" icon={<PlusOutlined />} onClick={openCreateDept}>
                新建部门
              </Button>
            )}
          </Space>
        }
        style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, margin: 0 }}
        styles={{ body: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } }}
      >
        {/* Table 区：flex:1 撑满剩余，内部滚动，分页条固定左下不动 */}
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        <Table
          dataSource={departments.slice((deptPage - 1) * deptPageSize, deptPage * deptPageSize)}
          columns={deptColumns}
          rowKey="id"
          loading={loading}
          pagination={false}
          components={{ header: { cell: ResizableTitle } }}
          scroll={{ x: dTableW(deptColumns) }}
          sticky
          size="middle"
          className="table-zebra"
          style={{ flex: 1, minHeight: 0 }}
        />
        </div>
        {/* 分页条：统一 LeftPagination（固定左下） */}
        <LeftPagination
          page={deptPage}
          pageSize={deptPageSize}
          total={departments.length}
          onChange={(p, ps) => { setDeptPage(p); setDeptPageSize(ps); }}
        />
      </Card>

      {/* 新建/编辑部门弹窗（表单回填由弹窗自己按 editingDept 完成） */}
      <DeptEditModal
        open={deptModalOpen}
        editingDept={editingDept}
        onClose={() => setDeptModalOpen(false)}
        onSaved={onRefresh}
      />
    </>
  );
};
