import { App as AntApp, List, Space, Tag, Typography } from 'antd';
import dayjs from 'dayjs';
import type { LogLine, LogSegment } from '../../shared/api/client';

const { Text } = Typography;

/** 系统日志保留行数上限（超出丢最旧） */
export const MAX_LOG_LINES = 500;
/** 每次 tail 拉取行数（与后端默认一致） */
export const TAIL_LIMIT = 200;
/** 轮询间隔（毫秒） */
export const POLL_INTERVAL = 5000;
/** 日志/审计默认查看天数（最近 7 天） */
export const VIEW_DAYS = 7;
/** 时间段摘要刷新频率：每 N 个轮询周期一次（切段要扫日志文件，降频避免浪费） */
export const SEGMENT_POLL_EVERY = 3;
/** 按时间段查询的最大返回行数（超出后端只回该区间最后 N 行） */
export const RANGE_LIMIT = 500;
/** 文件详情视图一次拉取行数（同后端 tail limit 上限） */
export const DETAIL_LIMIT = 2000;

/** 日志空档切分选项（相邻两行间隔超过该值 → 视为新的输出段） */
export const GAP_OPTIONS = [
  { value: 30, label: '30 秒空档' },
  { value: 60, label: '1 分钟空档' },
  { value: 300, label: '5 分钟空档' },
];

/** 角色 → 颜色/文案（与 Users.tsx 一致） */
export const roleMeta: Record<string, { color: string; text: string }> = {
  super_admin: { color: 'red', text: '超级管理员' },
  dept_admin: { color: 'blue', text: '部门管理员' },
  user: { color: 'default', text: '普通用户' },
};

/** 目标类型 → 中文（与 Users.tsx 一致） */
export const targetTypeLabelMap: Record<string, string> = {
  user: '用户',
  dept: '部门',
  kb: '知识库',
  doc: '文档',
  chat: '会话',
  config: '配置',
};

/** 日志级别 → Tag 颜色（INFO 蓝 / WARNING 橙 / ERROR 红 / DEBUG 灰，非标准行默认灰） */
export const levelColor = (level: string | null): string => {
  switch (level) {
    case 'INFO':
      return 'blue';
    case 'WARNING':
      return 'orange';
    case 'ERROR':
      return 'red';
    case 'DEBUG':
      return 'default';
    default:
      return 'default';
  }
};

export const LEVEL_OPTIONS = ['INFO', 'WARNING', 'ERROR', 'DEBUG'].map(l => ({
  value: l,
  label: l,
}));

/** 文件大小格式化（B/KB/MB） */
export const formatBytes = (bytes: number) => {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
};

/** 秒级时间戳 → HH:mm:ss（时间段展示用；缺省显示占位符） */
export const shortTs = (ts: string | null): string => (ts ? ts.slice(11, 19) : '--:--:--');

/** 行是否落在某个时间段内（秒级前缀比较，含起止边界） */
export const inSegment = (line: LogLine, seg: LogSegment): boolean => {
  if (!line.ts) return false;
  const ts = line.ts.slice(0, 19);
  return (!seg.start_ts || ts >= seg.start_ts) && (!seg.end_ts || ts <= seg.end_ts);
};

/** 单行日志渲染（实时流/时间段视图/文件详情共用） */
export const renderLogItem = (item: LogLine) => (
  <List.Item style={{ padding: '4px 0' }}>
    <Space size={10} align="start" style={{ width: '100%' }}>
      <Tag
        color={levelColor(item.level)}
        style={{ minWidth: 68, textAlign: 'center', marginInlineEnd: 0 }}
      >
        {item.level ?? 'LOG'}
      </Tag>
      <Text type="secondary" style={{ whiteSpace: 'nowrap', fontSize: 12 }}>
        {item.ts ?? ''}
      </Text>
      <Text style={{ wordBreak: 'break-all', fontSize: 13 }}>{item.message}</Text>
    </Space>
  </List.Item>
);

/** 最近 7 天日期（YYYY-MM-DD，旧→新，供日期下拉） */
export const recentDates = (): string[] => {
  const out: string[] = [];
  for (let i = VIEW_DAYS - 1; i >= 0; i--) {
    out.push(dayjs().subtract(i, 'day').format('YYYY-MM-DD'));
  }
  return out;
};

/** 统一的 App.useApp 实例类型（message/modal） */
export type AppInstance = ReturnType<typeof AntApp.useApp>;
