/**
 * 对话 API（SSE 流式 + 检索 + 会话历史）。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 */
import api from './http';
import { authHeader, clearAuth } from '../auth/token';
import type {
  ChatMessage,
  ChatSession,
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
      case 'delta':
        // 兼容两种形态：裸字符串（mock）与 {"text":"..."}（真实后端）
        if (typeof data === 'string') {
          callbacks.onDelta?.(data);
        } else {
          const text = (data as { text?: string })?.text;
          if (text) callbacks.onDelta?.(text);
        }
        break;
      case 'done': {
        const info = (typeof data === 'object' && data !== null ? data : {}) as {
          session_id?: string;
          message_count?: number;
        };
        callbacks.onDone?.({ session_id: info.session_id ?? '', message_count: info.message_count ?? 0 });
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

// ========== 会话历史 API ==========

export const listSessions = (kbId: string) =>
  api.get<ChatSession[]>('/chat/history', { params: { kb_id: kbId } });

export const getSession = (sessionId: string) =>
  api.get<{ messages: ChatMessage[] }>(`/chat/history/${sessionId}`);

export const deleteSession = (sessionId: string) =>
  api.delete(`/chat/history/${sessionId}`);

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
