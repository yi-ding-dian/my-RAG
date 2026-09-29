/**
 * 对话 API（SSE 流式 + 检索 + 会话历史）。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 */
import api from './http';
import { authHeader, clearAuth } from '../auth/token';
import type {
  AgenticStatus,
  AgenticTrace,
  ChatSession,
  ChatSessionDetail,
  GenParams,
  RetrieveChatParams,
  Source,
  StreamCallbacks,
  StreamChatParams,
} from './types';

/**
 * SSE 流式对话：基于 fetch（axios 对 SSE 不友好）。
 * 使用 AbortController 支持"停止"按钮，返回 abort 函数。
 * 逐行解析 `event:` / `data:`，兼容 chunked 传输导致的半行拆分。
 */
export function streamChat(params: StreamChatParams, callbacks: StreamCallbacks): () => void {
  const controller = new AbortController();

  const parseData = (eventType: string, raw: string) => {
    let data: unknown = raw;
    try {
      data = JSON.parse(raw);
    } catch {
      // data 非 JSON（如纯文本 delta）时原样使用
    }
    switch (eventType) {
      case 'meta': {
        // 兼容两种形态：裸数组 [Source]（契约/mock）与 {"sources":[Source]}（真实后端）
        const sources = Array.isArray(data)
          ? (data as Source[])
          : ((data as { sources?: Source[] })?.sources ?? []);
        callbacks.onMeta?.(sources);
        break;
      }
      case 'reasoning': {
        // 推理模型思考增量（思考未关闭时才收到）：仅流式展示，后端不落盘。
        // total_ms 可能挂在本事件上——思考先于正文输出时，用户看到的第一段
        // 输出是思考，故「提问→首字」以思考首字为准
        const info = (data ?? {}) as { text?: string; total_ms?: number };
        if (info.text) callbacks.onReasoning?.(info.text, info.total_ms);
        break;
      }
      case 'delta':
        // 兼容两种形态：裸字符串（mock）与 {"text":"..."}（真实后端）
        // total_ms 只有首条 delta 带（后端首字埋点），后续增量没有
        if (typeof data === 'string') {
          callbacks.onDelta?.(data);
        } else {
          const info = (data ?? {}) as { text?: string; total_ms?: number };
          if (info.text) callbacks.onDelta?.(info.text, info.total_ms);
        }
        break;
      case 'prompt': {
        const info = (typeof data === 'object' && data !== null ? data : {}) as {
          prompt?: unknown[];
          retrieval_ms?: number;
          kg_ms?: number;
          rewrite_ms?: number;
          rewritten_query?: string | null;
          split_ms?: number;
          sub_queries?: string[];
        };
        callbacks.onPrompt?.({
          prompt: info.prompt ?? [],
          retrieval_ms: info.retrieval_ms,
          kg_ms: info.kg_ms,
          rewrite_ms: info.rewrite_ms,
          rewritten_query: info.rewritten_query,
          split_ms: info.split_ms,
          sub_queries: info.sub_queries,
        });
        break;
      }
      case 'agentic': {
        const info = (typeof data === 'object' && data !== null ? data : {}) as {
          original_query?: string;
          final_query?: string;
          trace?: AgenticTrace['trace'];
        };
        callbacks.onAgentic?.({
          original_query: info.original_query ?? '',
          final_query: info.final_query ?? '',
          trace: info.trace ?? [],
        });
        break;
      }
      case 'agentic_status': {
        const info = (typeof data === 'object' && data !== null ? data : {}) as {
          stage?: AgenticStatus['stage'];
          attempt?: number;
          query?: string;
        };
        callbacks.onAgenticStatus?.({
          stage: info.stage ?? 'retrieving',
          attempt: info.attempt,
          query: info.query,
        });
        break;
      }
      case 'done': {
        const info = (typeof data === 'object' && data !== null ? data : {}) as {
          session_id?: string;
          message_count?: number;
          gen_params?: GenParams;
        };
        callbacks.onDone?.({
          session_id: info.session_id ?? '',
          message_count: info.message_count ?? 0,
          gen_params: info.gen_params,
        });
        break;
      }
      case 'vision_error': {
        // 视觉模型不可用（未配置 / 服务不通 / 读图失败）：**与 error 分开**——
        // 用户该做的是删掉图改发纯文本、或找管理员，不是"稍后重试"。
        // 前端提示后要保留他已选好的图和输入的文字
        const msg =
          typeof data === 'string'
            ? data
            : ((data as { message?: string })?.message ?? '视觉模型当前无法使用，无法识图');
        callbacks.onVisionError?.(msg);
        break;
      }
      case 'error': {
        const msg =
          typeof data === 'string' ? data : ((data as { message?: string })?.message ?? '生成出错');
        callbacks.onError?.(msg);
        break;
      }
    }
  };

  (async () => {
    let res: Response;
    try {
      // fetch 不走 axios 拦截器，需手动携带认证头
      res = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeader() },
        // 兼容：契约字段 query；真实后端要求 message（同时发送，后端自行忽略多余字段）
        body: JSON.stringify({ ...params, message: params.query }),
        signal: controller.signal,
      });
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        callbacks.onError?.('已停止');
      } else {
        callbacks.onError?.('网络请求失败');
      }
      return;
    }

    // 401 统一处理（与 axios 拦截器一致）：清除本地认证并跳转登录
    if (res.status === 401) {
      clearAuth();
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
      callbacks.onError?.('登录已过期，请重新登录');
      return;
    }

    if (!res.ok || !res.body) {
      callbacks.onError?.(`请求失败（HTTP ${res.status}）`);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let eventType = '';

    const handleLine = (line: string) => {
      if (!line) return;
      if (line.startsWith('event:')) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        const raw = line.slice(5).trim();
        if (raw && eventType) parseData(eventType, raw);
      }
    };

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx = buffer.indexOf('\n');
        while (idx >= 0) {
          handleLine(buffer.slice(0, idx).replace(/\r$/, ''));
          buffer = buffer.slice(idx + 1);
          idx = buffer.indexOf('\n');
        }
      }
      // 处理流结束时残留的尾部
      if (buffer.trim()) {
        buffer.split('\n').forEach(l => handleLine(l.replace(/\r$/, '')));
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        callbacks.onError?.('流式响应中断');
      } else {
        callbacks.onError?.('已停止');
      }
    }
  })();

  return () => controller.abort();
}

/**
 * 检索调试：真实后端返回裸数组 Source[]，契约约定 {sources: Source[]}，此处归一化为统一结构。
 * signal 可选：传入 AbortController.signal 支持「停止检索」取消（axios 抛 CanceledError，
 * 调用方以 err.code === 'ERR_CANCELED' 识别静默结束）。
 */
export const retrieveChat = async (data: RetrieveChatParams, signal?: AbortSignal) => {
  const res = await api.post<Source[] | { sources: Source[] }>('/chat/retrieve', {
    ...data,
    message: data.query,
  }, { signal });
  return { sources: Array.isArray(res.data) ? res.data : res.data.sources };
};

// ========== 聊天识图 API ==========

/**
 * 上传聊天图片 → 返回对象存储 key（把它塞进 StreamChatParams.images）。
 *
 * **为什么先上传再发 key，而不是 base64 直接进 /stream 请求体**：会话落盘在
 * data/chat/*.json，一张 2MB 的图 base64 后约 2.7MB，聊十轮该会话文件就 27MB
 * ——历史列表加载会卡死。存 key 则每条消息只多几十字节。
 *
 * 校验失败（格式不支持 / 超过 image_max_mb）后端返回 400，detail 是面向
 * 用户的中文提示，调用方直接展示即可。
 */
export const uploadChatImage = async (
  file: File,
): Promise<{ key: string; name: string }> => {
  const form = new FormData();
  form.append('file', file);
  // Content-Type 交给浏览器自己带（要含 multipart boundary，手写必然错）
  const res = await api.post<{ key: string; name: string }>(
    '/chat/upload-image', form, { headers: { 'Content-Type': undefined } },
  );
  return res.data;
};

/**
 * 视觉模型可用性探活（选图后立刻调，提前告知"识图用不了"）。
 *
 * 探活只证明服务活着，**不证明模型真能读图**（模型被卸载但 vLLM 进程还在时
 * /models 仍返回 200）——真正确认由发送时的 vision_error 事件兜底，两处
 * 文案一致。enabled=false 表示管理员关闭了聊天识图，前端不显示图片入口。
 */
export const getVisionStatus = async (): Promise<{
  enabled: boolean;
  available: boolean;
  reason: string;
  /** 单次最多几张图（后端 chat.image_max_count，前端做同款拦截） */
  max_count: number;
  /** 单张大小上限 MB（后端 chat.image_max_mb） */
  max_mb: number;
}> => {
  const res = await api.get<{
    enabled: boolean; available: boolean; reason: string;
    max_count: number; max_mb: number;
  }>('/chat/vision-status');
  return res.data;
};

/**
 * 图片 key → 浏览器可加载的代理 URL（形如
 * `chat_images/{user_id}/{name}` → `/api/files/chat-images/{user_id}/{name}`）。
 *
 * 渲染时还需经 withImageToken 追加 JWT（<img> 带不了 header）。key 不合法
 * 时返回空串——调用方据此跳过渲染，不至于把脏数据直接拼进 src。
 */
export const chatImageUrl = (key: string): string => {
  const parts = (key || '').split('/');
  if (parts.length !== 3 || parts[0] !== 'chat_images') return '';
  return `/api/files/chat-images/${parts[1]}/${parts[2]}`;
};

// ========== 会话历史 API ==========

/** 会话列表（kbId 省略 = 不过滤返回全部；问答页用全局一份列表，会话自带 kb_ids） */
export const listSessions = (kbId?: string) =>
  api.get<ChatSession[]>('/chat/history',
    { params: kbId ? { kb_id: kbId } : undefined });

/**
 * 会话详情。includeDeleted=true（仅超管生效）时，会话已被用户删除则回退读
 * 归档目录——「用户反馈」页回溯点踩现场用：点踩者常顺手把会话删了。
 */
export const getSession = (sessionId: string, includeDeleted = false) =>
  api.get<ChatSessionDetail>(`/chat/history/${sessionId}`, {
    params: includeDeleted ? { include_deleted: true } : undefined,
  });

export const deleteSession = (sessionId: string) =>
  api.delete(`/chat/history/${sessionId}`);

/**
 * 批量删除会话（列表页「管理」多选删除）。
 * 一次请求删多条：后端逐条校验归属，越权/不存在的自动跳过，返回 {deleted, skipped}
 * 供调用方如实提示——skipped 不为 0 时不能谎报"全部删除成功"。
 */
export const batchDeleteSessions = (sessionIds: string[]) =>
  api.post<{ deleted: number; skipped: number }>('/chat/history/batch-delete', {
    session_ids: sessionIds,
  });

export const renameSession = (sessionId: string, title: string) =>
  api.post<{ message: string; title: string }>(`/chat/history/${sessionId}/rename`, { title });

/**
 * 导出会话为 Markdown：fetch 拿 blob 后创建下载链接（不裸传 token，
 * Authorization 头由 authHeader() 携带；文件名取自 Content-Disposition）。
 */
export const exportSession = async (sessionId: string): Promise<void> => {
  const res = await fetch(`/api/chat/history/${sessionId}/export`, {
    headers: authHeader(),
  });
  if (res.status === 401) {
    clearAuth();
    if (!window.location.pathname.startsWith('/login')) {
      window.location.href = '/login';
    }
    throw new Error('登录已过期，请重新登录');
  }
  if (!res.ok) throw new Error(`导出失败（HTTP ${res.status}）`);
  const blob = await res.blob();
  // 优先解析 RFC 5987 filename*=UTF-8''，其次普通 filename
  const cd = res.headers.get('Content-Disposition') || '';
  let filename = `会话_${sessionId}.md`;
  const m1 = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (m1) {
    filename = decodeURIComponent(m1[1]);
  } else {
    const m2 = cd.match(/filename="?([^";]+)"?/i);
    if (m2) filename = m2[1];
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
};

/** 提交回答反馈（👍👎 + 可选纠正原因；任何登录用户） */
export const submitFeedback = (data: {
  rating: 'up' | 'down';
  kb_id?: string;
  session_id?: string;
  msg_idx?: number;
  reason?: string;
}) => api.post('/chat/feedback', data);
