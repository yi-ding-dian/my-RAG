/**
 * 用户与部门管理页（super_admin / dept_admin 可访问，路由层已守卫）。
 * super_admin：全量管理（部门选择器/全部门户/部门 CRUD）；
 * dept_admin：仅本部门成员（部门固定、角色限 user/dept_admin、部门只读
 * 名称/描述编辑，无创建/删除部门）。
 *
 * 本文件只剩容器职责：数据（用户/部门）+ 加载 + 页头 + Tabs。
 * 两个 Tab 的实现与各自的弹窗见 components/。
 * （审计日志不在此页——它属于「日志查看」模块：/logs 的操作审计 Tab）
 */
import { useCallback, useEffect, useState } from 'react';
import { App as AntApp, Tabs } from 'antd';

import { listDepartments, listUsers, Department, User } from '../../shared/api/client';
import { useAuth } from '../../shared/auth/AuthContext';
import PageHeader from '../../shared/components/layout/PageHeader';
import { DepartmentsTab } from './components/DepartmentsTab';
import { UsersTab } from './components/UsersTab';

const UsersPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { user: me, refreshUser } = useAuth();
  const isDeptAdmin = me?.role === 'dept_admin';

  // ---------- 数据（两个 Tab 共用：departments 也服务用户 Tab 的"改部门"下拉） ----------
  const [users, setUsers] = useState<User[]>([]);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [deptsLoading, setDeptsLoading] = useState(false);

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

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <PageHeader
        title={isDeptAdmin ? '部门成员管理' : '用户管理'}
        description={isDeptAdmin
          ? '管理本部门成员账号与部门信息（数据与其他部门隔离）'
          : '管理用户账号与部门归属（超级管理员全量）'}
        style={{ flexShrink: 0 }}
      />
      <div style={{ flex: 1, minHeight: 0 }}>
      <Tabs
        defaultActiveKey="users"
        className="page-tabs"
        style={{ height: '100%' }}
        items={[
          {
            key: 'users',
            label: isDeptAdmin ? '本部门成员' : '用户管理',
            children: (
              <UsersTab
                users={users}
                departments={departments}
                loading={usersLoading}
                isDeptAdmin={isDeptAdmin}
                me={me}
                onRefresh={loadUsers}
                onRefreshDepartments={loadDepartments}
                onRefreshMe={refreshUser}
              />
            ),
          },
          {
            key: 'departments',
            label: '部门管理',
            children: (
              <DepartmentsTab
                departments={departments}
                loading={deptsLoading}
                isDeptAdmin={isDeptAdmin}
                onRefresh={loadDepartments}
                onRefreshUsers={loadUsers}
              />
            ),
          },
        ]}
      />
      </div>
    </div>
  );
};

export default UsersPage;
