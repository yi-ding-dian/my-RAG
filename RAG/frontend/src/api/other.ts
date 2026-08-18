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
  LogFileInfo,
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
 */
export const tailSystemLogs = (date?: string, offset = 0, limit = 200) =>
  api.get<LogTailResult>('/logs/tail', { params: { date, offset, limit } });

export const listLogFiles = () => api.get<{ files: LogFileInfo[] }>('/logs/files');

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

// ========== 外部查询 API（仅 super_admin；token 为访问凭证，内网管理端返回明文） ==========

export const listExtQueries = () => api.get<ExtQuery[]>('/ext-queries');

export const createExtQuery = (data: ExtQueryCreateInput) =>
  api.post<ExtQuery>('/ext-queries', data);

export const updateExtQuery = (id: string, data: ExtQueryUpdateInput) =>
  api.put<ExtQuery>(`/ext-queries/${id}`, data);

export const resetExtQueryToken = (id: string) =>
  api.post<{ token: string; message: string }>(`/ext-queries/${id}/reset-token`);

export const toggleExtQuery = (id: string) =>
  api.post<ExtQuery>(`/ext-queries/${id}/toggle`);

export const deleteExtQuery = (id: string) =>
  api.delete<{ message: string }>(`/ext-queries/${id}`);

/** 外部查询分享链接（token 即访问凭证，仅超管可见/复制） */
export const extQueryLink = (id: string, token: string): string =>
  `${window.location.origin}/ext-query/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}`;
