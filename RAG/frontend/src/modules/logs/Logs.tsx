import React, { useEffect, useState } from 'react';
import { App as AntApp, Badge, Card, Tabs } from 'antd';
import { useAuth } from '../../shared/auth/AuthContext';
import PageHeader from '../../shared/components/layout/PageHeader';
import {
  LOG_HEALTH_POLL_INTERVAL,
  LOG_HEALTH_REFRESH_EVENT,
  getLogHealth,
} from '../../shared/api/client';
import type { LogHealth } from '../../shared/api/client';
import AuditTab from './AuditTab';
import LogFilesTab from './LogFilesTab';
import LiveLogTab from './LiveLogTab';
import OverviewTab from './OverviewTab';

/**
 * 日志查看页（super_admin 全量 / dept_admin 仅操作审计）：
 * - Tab1 总览（仅 super_admin）：近 N 天分级别统计 + 堆叠柱趋势图 + 红绿灯
 *   （只看**系统级**故障，用户级失败不点灯）+ 最近系统级故障列表；60s 轮询
 * - Tab2 操作审计：样式/筛选/分页与 Users.tsx 审计 Tab 一致；时间范围默认最近
 *   7 天；5s 自动轮询刷新当前页（「自动刷新」开关默认开）；按天删除审计记录仅
 *   super_admin 可见（dept_admin 只读，数据已由后端限本部门人员）
 * - Tab3 系统日志文件（仅 super_admin）：按天日志文件表格（日期/文件/大小/
 *   日志条数/修改时间/操作），行数大文件为采样估算（前端标「约」）；前端分页
 *   + 合计占用 + 清空全部；点击文件名查看该天内容
 * - Tab4 实时系统日志（仅 super_admin）：tail 字节游标增量 + 5s 轮询；倒序最新
 *   在上；关键字/级别过滤；条数记录（分级别小计）；按「时间空档」切分输出段，
 *   点击时间段只看该区间日志（缓冲不够时调后端按时间段查询）
 */
const LogsPage: React.FC = () => {
  const app = AntApp.useApp();
  const { user } = useAuth();
  // 系统日志相关 Tab 仅 super_admin（dept_admin 无运行日志查看权限，接口 403）
  const isSuperAdmin = user?.role === 'super_admin';
  // null = 还没手动切过；默认落「总览」（菜单红点点进来先看健康度），
  // dept_admin 没有总览 Tab 则落「操作审计」。用派生值而非 useState 初值：
  // user 是异步加载的，初值渲染时还拿不到角色
  const [tab, setTab] = useState<string | null>(null);
  const activeTab = tab ?? (isSuperAdmin ? 'overview' : 'audit');
  // 总览点柱子 → 跳到「实时系统日志」并定位该天。每次跳转都是新对象，
  // 保证连点同一天也能再次触发（LiveLogTab 按引用变化响应）
  const [jumpDate, setJumpDate] = useState<{ date: string } | null>(null);
  const jumpToLive = (date: string) => {
    setJumpDate({ date });
    setTab('live');
  };

  // 「总览」Tab 标签上的红绿灯（与左侧菜单那个同源同色）。本页独立取一份：
  // 菜单那份在 App.tsx、跨路由传下来太重；只在日志页挂载期间轮询，切走即停。
  // 确认故障后总览会广播 LOG_HEALTH_REFRESH_EVENT，这里立即重拉（不等 60s）
  const [health, setHealth] = useState<LogHealth | null>(null);
  useEffect(() => {
    if (!isSuperAdmin) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await getLogHealth();
        if (!cancelled) setHealth(res.data);
      } catch {
        // 静默：灯拿不到不影响日志页其他功能
      }
    };
    void tick();
    const timer = setInterval(tick, LOG_HEALTH_POLL_INTERVAL);
    window.addEventListener(LOG_HEALTH_REFRESH_EVENT, tick);
    return () => {
      cancelled = true;
      clearInterval(timer);
      window.removeEventListener(LOG_HEALTH_REFRESH_EVENT, tick);
    };
  }, [isSuperAdmin]);

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
          activeKey={activeTab}
          onChange={setTab}
          style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
          items={
            isSuperAdmin
              ? [
                  // active：非当前 Tab 暂停轮询（切回时自动续拉），避免后台空转
                  {
                    key: 'overview',
                    // 标签带红绿灯：与左侧菜单那个同源同色，页面内一眼可见健康度
                    label: (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                        总览
                        {health && (
                          <Badge status={health.level === 'red' ? 'error' : 'success'} />
                        )}
                      </span>
                    ),
                    children: <OverviewTab app={app} active={activeTab === 'overview'} onJumpToDate={jumpToLive} />,
                  },
                  { key: 'audit', label: '操作审计', children: <AuditTab app={app} /> },
                  { key: 'files', label: '系统日志文件', children: <LogFilesTab app={app} active={activeTab === 'files'} /> },
                  { key: 'live', label: '实时系统日志', children: <LiveLogTab app={app} active={activeTab === 'live'} jumpDate={jumpDate} /> },
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
