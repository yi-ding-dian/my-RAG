import React, { useState } from 'react';
import { App as AntApp, Card, Tabs } from 'antd';
import { useAuth } from '../../auth/AuthContext';
import PageHeader from '../../components/layout/PageHeader';
import AuditTab from './AuditTab';
import LogFilesTab from './LogFilesTab';
import LiveLogTab from './LiveLogTab';

/**
 * 日志查看页（super_admin 全量 / dept_admin 仅操作审计）：
 * - Tab1 操作审计：样式/筛选/分页与 Users.tsx 审计 Tab 一致；时间范围默认最近
 *   7 天；5s 自动轮询刷新当前页（「自动刷新」开关默认开）；按天删除审计记录仅
 *   super_admin 可见（dept_admin 只读，数据已由后端限本部门人员）
 * - Tab2 系统日志文件（仅 super_admin）：按天日志文件表格（日期/文件/大小/
 *   日志条数/修改时间/操作），行数大文件为采样估算（前端标「约」）；前端分页
 *   + 合计占用 + 清空全部；点击文件名查看该天内容
 * - Tab3 实时系统日志（仅 super_admin）：tail 字节游标增量 + 5s 轮询；倒序最新
 *   在上；关键字/级别过滤；条数记录（分级别小计）；按「时间空档」切分输出段，
 *   点击时间段只看该区间日志（缓冲不够时调后端按时间段查询）
 */
const LogsPage: React.FC = () => {
  const app = AntApp.useApp();
  const { user } = useAuth();
  const [tab, setTab] = useState('audit');
  // 系统日志相关 Tab 仅 super_admin（dept_admin 无运行日志查看权限，接口 403）
  const isSuperAdmin = user?.role === 'super_admin';

  // 页面固定撑满视口（Content 上下 padding 24×2，与 Chat.tsx 同款布局）：
  // 页头/Tab/筛选栏固定不动，内容区在内部滚动（flex 链见 index.css .logs-page-tabs）
  return (
    <div
      style={{
        height: 'calc(100vh - 48px)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      <PageHeader
        title="日志查看"
        description={
          isSuperAdmin
            ? '超管实时查看：操作审计与系统运行日志（5 秒自动轮询，默认最近 7 天）'
            : '本部门人员操作审计（5 秒自动轮询，默认最近 7 天）'
        }
        style={{ flexShrink: 0 }}
      />
      <Card
        style={{
          flex: 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
        styles={{
          body: {
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            // 页头与 Card 之间已有间距，body 顶部不再叠默认 24px（两块空白叠加
            // 会让 Tab 上方空出一大条）
            padding: '8px 24px 16px',
          },
        }}
      >
        <Tabs
          className="page-tabs"
          activeKey={tab}
          onChange={setTab}
          style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
          items={
            isSuperAdmin
              ? [
                  { key: 'audit', label: '操作审计', children: <AuditTab app={app} /> },
                  // active：非当前 Tab 暂停轮询（切回时自动续拉），避免后台空转
                  { key: 'files', label: '系统日志文件', children: <LogFilesTab app={app} active={tab === 'files'} /> },
                  { key: 'live', label: '实时系统日志', children: <LiveLogTab app={app} active={tab === 'live'} /> },
                ]
              : [
                  { key: 'audit', label: '操作审计', children: <AuditTab app={app} /> },
                ]
          }
        />
      </Card>
    </div>
  );
};

export default LogsPage;
