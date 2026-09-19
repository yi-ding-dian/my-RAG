/**
 * 统计 / RAGAS / 审计 / 运行日志 / 外部查询 API。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 */
import api from './http';
import { authHeader, clearAuth } from '../auth/token';
import type {
  AuditLogPage,
  AuditLogQuery,
  AuditActionOption,
  ExtQuery,
  ExtQueryCreateInput,
  ExtQueryUpdateInput,
  ExtQueryLogPage,
  ExtQueryOverview,
  LogFileInfo,
  LogHealth,
  LogOverview,
  LogRangeResult,
  LogSegmentsResult,
  LogTailResult,
  RagasEvaluationPreview,
  RagasEvaluationRequest,
  RagasEvaluationResult,
  RagasPrecheck,
  RagasReport,
  RagasStatus,
  RetrievalQuality,
  Stats,
} from './types';

// ========== 系统统计 API（阶段2） ==========

export const getStats = () => api.get<Stats>('/stats');

export const getRagasStatus = () => api.get<RagasStatus>('/stats/ragas');

export const startRagasEvaluation = (body: RagasEvaluationRequest) =>
  api.post<RagasEvaluationResult>('/stats/ragas/evaluations', body);

export const previewRagasSamples = (body: RagasEvaluationRequest) =>
  api.post<RagasEvaluationPreview>('/stats/ragas/evaluations', body);

export const ragasPrecheck = () =>
  api.get<RagasPrecheck>('/stats/ragas/precheck');

// ========== 检索质量统计（近 30 天） ==========

export const getRetrievalQuality = (kbId: string) =>
  api.get<RetrievalQuality>('/stats/quality', { params: { kb_id: kbId } });

/** 用户回答反馈汇总（仅超管）：总数/好评/差评 + 最近反馈 */
export interface ChatFeedbackStats {
  total: number;
  up: number;
  down: number;
  recent: Array<{
    id: string;
    user_id: string;
    kb_id: string | null;
    session_id: string | null;
    msg_idx: number;
    rating: string;
    reason: string;
    created_at: string;
  }>;
}

export const getChatFeedbackStats = () =>
  api.get<ChatFeedbackStats>('/stats/chat-feedback');

/** 反馈记录项（分页接口；用户名/部门名/知识库名由后端 join 回填，缺失为空串/null） */
export interface ChatFeedbackLogItem {
  id: string;
  user_id: string;
  username: string;
  display_name: string;
  /** 无部门为 null；部门名缺失（无部门/部门已删）为空串 */
  department_id: string | null;
  department_name: string;
  kb_id: string | null;
  kb_name: string | null;
  session_id: string | null;
  /** 被反馈消息在会话 messages 数组中的下标（回溯定位用） */
  msg_idx: number;
  rating: string;
  reason: string;
  created_at: string;
}

export interface ChatFeedbackLogPage {
  total: number;
  page: number;
  page_size: number;
  items: ChatFeedbackLogItem[];
}

export interface ChatFeedbackLogQuery {
  page?: number;
  page_size?: number;
  rating?: 'up' | 'down';
  /** 用户名模糊搜索 */
  username?: string;
  /** 部门 ID 过滤 */
  department_id?: string;
  /** 关键词（模糊匹配点踩原因） */
  keyword?: string;
  /** 起始日期 YYYY-MM-DD */
  date_from?: string;
  /** 结束日期 YYYY-MM-DD（含当天） */
  date_to?: string;
}

/** 聊天反馈分页查询（仅超管）；筛选条件全可选、多条件为 AND */
export const listChatFeedbackLogs = (params: ChatFeedbackLogQuery) =>
  api.get<ChatFeedbackLogPage>('/stats/chat-feedback/logs', { params });

export const getRagasReport = (taskId: string) =>
  api.get<RagasReport>(`/stats/ragas/tasks/${taskId}`);

/** 取消 RAGAS 评估任务（发起人本人 / super_admin / dept_admin 本部门可取消） */
export const cancelRagasEvaluation = (taskId: string) =>
  api.post<{ message: string }>(`/stats/ragas/evaluations/${taskId}/cancel`);

// ========== 审计操作日志 API（仅 super_admin） ==========

export const listAuditLogs = (params?: AuditLogQuery) =>
  api.get<AuditLogPage>('/audit/logs', { params });

export const listAuditActions = () =>
  api.get<{ actions: AuditActionOption[] }>('/audit/actions');

/** 按天删除审计记录（created_at 前缀匹配，删除前二次确认） */
export const deleteAuditLogsByDate = (date: string) =>
  api.delete<{ message: string; deleted: number }>('/audit/logs', { params: { date } });

// ========== 系统运行日志 tail API（仅 super_admin） ==========

/**
 * 读系统运行日志（按天 + 字节游标增量）：date 缺省=今天（YYYY-MM-DD）；
 * offset < 0 = 尾部模式取最近 limit 行（首次加载/切换日期用）；
 * offset 超文件大小后端自动归位尾部；文件不存在返回空。
 * hideHttp=true 时后端跳过第三方 HTTP 正常噪音行（httpx 2xx/3xx 回显、
 * 连接池提示；4xx/5xx 保留），只影响返回内容，日志文件始终全量落盘。
 */
export const tailSystemLogs = (date?: string, offset = 0, limit = 200, hideHttp = false) =>
  api.get<LogTailResult>('/logs/tail', {
    params: { date, offset, limit, hide_http: hideHttp },
  });

export const listLogFiles = () => api.get<{ files: LogFileInfo[] }>('/logs/files');

/**
 * 系统日志总览（「总览」Tab）：近 N 天分级别计数 + 系统级故障数 + 红绿灯 +
 * 最近系统级故障列表。每天统计后端按 (路径, mtime, 大小) 缓存，轮询成本低。
 */
export const getLogOverview = (days = 7, windowMinutes = 60) =>
  api.get<LogOverview>('/logs/overview', {
    params: { days, window_minutes: windowMinutes },
  });

/**
 * 红绿灯状态（轻量，菜单红点轮询与外部监控用）：red = 窗口内有系统级故障
 * （`system.` 域：LLM 崩溃/依赖连不上等），green = 正常。用户级失败不点灯。
 */
export const getLogHealth = (windowMinutes = 60) =>
  api.get<LogHealth>('/logs/health', { params: { window_minutes: windowMinutes } });

/** 菜单红绿灯轮询间隔（毫秒）：日志统计扫的是文件，比页面内轮询放慢些 */
export const LOG_HEALTH_POLL_INTERVAL = 60000;

/**
 * 灯色已变化的广播事件：总览页确认故障（ACK）后派发，让菜单红绿灯**立即**重拉，
 * 不必干等一个 60s 轮询周期（否则用户刚点完确认、菜单还红着，会以为没生效）。
 */
export const LOG_HEALTH_REFRESH_EVENT = 'log-health-refresh';

/**
 * 确认系统级故障已解决（**按条** ACK 消警，可附处理备注）：传故障 id 列表
 * （overview 的 recent_faults[].id），全部确认完（未确认数为 0）红灯才灭；
 * 之后新产生的故障是新 id、会重新亮灯。备注随确认存下，overview 会带回来。
 */
export const ackLogFaults = (ids: string[], note = '', windowMinutes = 60) =>
  api.post<LogHealth>('/logs/ack', { ids, note }, {
    params: { window_minutes: windowMinutes },
  });

/**
 * 日志时间段切分（时间段导航）：相邻两行间隔超 gapSeconds 视为新输出段，
 * 返回每段起止时间/条数/级别小计。后端只扫描文件尾部（大文件保护），
 * truncated=true 表示更早内容未统计。hideHttp 同 tailSystemLogs（噪音行
 * 仍参与段边界划分，但不计入条数与级别小计，整段皆噪音的段不返回）。
 */
export const listLogSegments = (date?: string, gapSeconds = 60, maxSegments = 200,
                               hideHttp = false) =>
  api.get<LogSegmentsResult>('/logs/segments', {
    params: {
      date, gap_seconds: gapSeconds, max_segments: maxSegments, hide_http: hideHttp,
    },
  });

/**
 * 按时间段查询日志行（时间戳秒级前缀比较，含起止边界）；超 limit 取区间最后
 * limit 行，total 为区间命中总数、truncated 表示被 limit 截断（供前端提示）。
 * hideHttp 同 tailSystemLogs（total 与行内容口径一致）。
 */
export const queryLogRange = (
  date: string | undefined, start: string, end: string, limit = 500, hideHttp = false,
) => api.get<LogRangeResult>('/logs/range', {
  params: { date, start, end, limit, hide_http: hideHttp },
});

/** 删除指定天日志文件（不存在静默成功） */
export const deleteLogFile = (date: string) =>
  api.delete<{ message: string; deleted: number }>('/logs/files', { params: { date } });

/** 清空所有运行日志（今天文件截断继续写入，其余天删除） */
export const deleteAllLogFiles = () =>
  api.delete<{ message: string; deleted: number }>('/logs/files');

/**
 * 下载指定天日志文件（仅 super_admin）：fetch 带鉴权头取字节流返回 Blob，
 * 调用方拼文件名（kb-YYYY-MM-DD.log）触发下载；非 2xx 抛后端中文错误。
 */
export const downloadLogFile = async (date: string): Promise<Blob> => {
  const res = await fetch(`/api/logs/files/download?date=${encodeURIComponent(date)}`, {
    headers: authHeader(),
  });
  if (res.status === 401) {
    // 与 axios 拦截器一致：登录过期统一跳转
    clearAuth();
    if (!window.location.pathname.startsWith('/login')) {
      window.location.href = '/login';
    }
    throw new Error('登录已过期，请重新登录');
  }
  if (!res.ok) {
    let detail = `下载失败（HTTP ${res.status}）`;
    try {
      const j = await res.json();
      if (j?.detail) detail = j.detail;
    } catch {
      // 非 JSON 响应体，保留默认提示
    }
    throw new Error(detail);
  }
  return res.blob();
};

// ========== 外部查询 API（仅 super_admin；token 为访问凭证，列表仅回传打码值） ==========

export const listExtQueries = () => api.get<ExtQuery[]>('/ext-queries');

/** 取完整 token（独立接口，带审计）：复制分发链接时临时取回，列表不回传明文 */
export const getExtQueryToken = (id: string) =>
  api.get<{ token: string; id: string }>(`/ext-queries/${id}/token`);

export const createExtQuery = (data: ExtQueryCreateInput) =>
  api.post<ExtQuery>('/ext-queries', data);

export const updateExtQuery = (id: string, data: ExtQueryUpdateInput) =>
  api.put<ExtQuery>(`/ext-queries/${id}`, data);

export const resetExtQueryToken = (id: string) =>
  api.post<{ token: string; message: string }>(`/ext-queries/${id}/reset-token`);

export const toggleExtQuery = (id: string) =>
  api.post<ExtQuery>(`/ext-queries/${id}/toggle`);

/** 续期：从 max(现在, 原到期时间) 顺延 N 天（未过期时不损失剩余天数），token 不变 */
export const renewExtQuery = (id: string, days: number) =>
  api.post<ExtQuery>(`/ext-queries/${id}/renew`, { days });

/** 总览统计（链接维度 + 记录维度），总览页卡片用 */
export const getExtQueryOverview = () =>
  api.get<ExtQueryOverview>('/ext-queries/overview');

/** 外部查询记录（按链接 / IP / 时间段筛选，时间倒序分页） */
export const listExtQueryLogs = (params: {
  config_id?: string;
  ip?: string;
  start?: string;
  end?: string;
  page?: number;
  page_size?: number;
}) => api.get<ExtQueryLogPage>('/ext-queries/logs', { params });

export const deleteExtQuery = (id: string) =>
  api.delete<{ message: string }>(`/ext-queries/${id}`);

/** 外部查询分享链接（token 即访问凭证，仅超管可见/复制） */
export const extQueryLink = (id: string, token: string): string =>
  `${window.location.origin}/ext-query/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}`;
