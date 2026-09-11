import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Input,
  Result,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import {
  BookOutlined,
  SendOutlined,
} from '@ant-design/icons';

/**
 * 外部查询页（公开，无需登录）：/ext-query/:id?token=xxx
 *
 * - 独立简化页面（无侧栏/菜单/登录）：品牌标题 + 查询输入 + 流式回答 + 来源折叠
 * - 挂载时用 token 调 GET /api/ext/{id}/info 校验；401 → 「链接无效或已失效」
 * - 流式请求直接 fetch（不走 axios：外部请求必须携带 ext token 而非登录 JWT）
 * - 样式取舍：独立浅色卡片样式，不耦合主后台主题系统（外部用户无主题偏好）
 */
const ExtQueryPage: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token') ?? '';

  const [checking, setChecking] = useState(true);
  const [info, setInfo] = useState<{ name: string; kb_names: { name: string }[] } | null>(null);

  const [query, setQuery] = useState('');
  const [sending, setSending] = useState(false);
  const [answer, setAnswer] = useState('');
  const [sources, setSources] = useState<{ document_name: string; kb_name?: string }[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);

  // 会话 id：页面内生成一次（刷新即新会话）；多轮上下文由后端按 session_id 续接
  const sessionIdRef = useRef<string | null>(null);
  const getSessionId = (): string => {
    if (!sessionIdRef.current) {
      sessionIdRef.current =
        typeof crypto !== 'undefined' && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    return sessionIdRef.current;
  };

  // 挂载校验：无效链接（404/401 统一）→ 显示「链接无效或已失效」
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/ext/${encodeURIComponent(id ?? '')}/info?token=${encodeURIComponent(token)}`);
        if (!cancelled) {
          if (res.ok) {
            setInfo(await res.json());
          } else {
            setInfo(null); // 401/404 统一走无效链接
          }
        }
      } catch {
        if (!cancelled) setInfo(null);
      } finally {
        if (!cancelled) setChecking(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, token]);

  const handleSend = useCallback(async () => {
    const q = query.trim();
    if (!q || sending) return;
    setSending(true);
    setAnswer('');
    setSources([]);
    setStreamError(null);
    const body = { query: q, session_id: getSessionId() };

    try {
      const res = await fetch(`/api/ext/${encodeURIComponent(id ?? '')}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      if (res.status === 401 || res.status === 429) {
        let detail = res.status === 401 ? '链接无效或已失效' : '请求过于频繁，请稍后再试';
        try {
          const j = await res.json();
          if (j?.detail) detail = j.detail;
        } catch {
          // 非 JSON 响应体，保留默认提示
        }
        setStreamError(detail);
        if (res.status === 401) setInfo(null); // 失效链接回到无效态
        setSending(false);
        return;
      }
      if (!res.ok || !res.body) {
        setStreamError(`请求失败（HTTP ${res.status}）`);
        setSending(false);
        return;
      }

      // SSE 逐行解析（event:/data:，兼容 chunked 半行拆分）
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
          if (!raw || !eventType) return;
          let data: unknown = raw;
          try {
            data = JSON.parse(raw);
          } catch {
            // data 非 JSON（兼容形态）时原样使用
          }
          if (eventType === 'meta') {
            const list = Array.isArray(data)
              ? (data as { document_name: string }[])
              : ((data as { sources?: { document_name: string }[] })?.sources ?? []);
            setSources(prev => [...prev, ...list]);
          } else if (eventType === 'delta') {
            const text = typeof data === 'string' ? data : (data as { text?: string })?.text;
            if (text) setAnswer(prev => prev + text);
          } else if (eventType === 'error') {
            const msg =
              typeof data === 'string' ? data : (data as { message?: string })?.message ?? '生成出错';
            setStreamError(msg);
          }
        }
      };

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
      if (buffer.trim()) {
        buffer.split('\n').forEach(l => handleLine(l.replace(/\r$/, '')));
      }
    } catch {
      setStreamError('网络请求失败，请稍后再试');
    } finally {
      setSending(false);
    }
  }, [id, token, query, sending]);

  // 来源文档名去重（简化展示：仅文档名列表，不带溯源弹窗）
  const sourceNames = Array.from(
    new Map(sources.map(s => [s.document_name, s])).values(),
  );

  // 独立浅色样式（不耦合主主题系统）
  const pageStyle: React.CSSProperties = {
    minHeight: '100vh',
    background: 'linear-gradient(180deg, #eef2fb 0%, #f6f7fb 100%)',
    padding: '48px 16px 64px',
  };

  return (
    <div style={pageStyle}>
      <div style={{ maxWidth: 760, margin: '0 auto' }}>
        {/* 品牌头部 */}
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <div
            style={{
              width: 52,
              height: 52,
              margin: '0 auto 12px',
              borderRadius: 14,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 24,
              color: '#fff',
              background: 'linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%)',
              boxShadow: '0 8px 20px rgba(37, 99, 235, 0.25)',
            }}
          >
            <BookOutlined />
          </div>
          <Typography.Title level={3} style={{ marginBottom: 4, color: '#1e293b' }}>
            知识库智能查询
          </Typography.Title>
          {info && (
            <Typography.Text style={{ color: '#64748b' }}>
              {info.name}
              {info.kb_names.length > 0 && (
                <span style={{ marginLeft: 8 }}>
                  <Tag color="blue">{info.kb_names.map(k => k.name).join('、')}</Tag>
                </span>
              )}
            </Typography.Text>
          )}
        </div>

        {/* 校验态 / 无效链接 */}
        {checking ? (
          <Card style={{ textAlign: 'center', padding: '32px 0', borderRadius: 16 }}>
            <Spin tip="正在校验链接..." />
          </Card>
        ) : !info ? (
          <Card style={{ borderRadius: 16 }}>
            <Result
              status="warning"
              title="链接无效或已失效"
              subTitle="该查询链接不存在、已停用或访问令牌不正确，请联系管理员获取新的链接。"
            />
          </Card>
        ) : (
          <>
            {/* 查询卡片 */}
            <Card
              style={{ borderRadius: 16, boxShadow: '0 4px 16px rgba(15, 23, 42, 0.06)' }}
              styles={{ body: { padding: 16 } }}
            >
              <Space.Compact style={{ width: '100%' }}>
                <Input
                  size="large"
                  value={query}
                  onChange={e => setQuery(e.target.value)}
                  onPressEnter={() => handleSend()}
                  placeholder="请输入您想查询的问题..."
                  disabled={sending}
                  maxLength={500}
                />
                <Button
                  type="primary"
                  size="large"
                  icon={<SendOutlined />}
                  loading={sending}
                  onClick={() => handleSend()}
                  style={{ minWidth: 96 }}
                >
                  查询
                </Button>
              </Space.Compact>
            </Card>

            {/* 回答区 */}
            {(answer || streamError || sources.length > 0) && (
              <Card style={{ marginTop: 16, borderRadius: 16 }}>
                {streamError && (
                  <Alert
                    type="error"
                    showIcon
                    message={streamError}
                    style={{ marginBottom: answer ? 12 : 0 }}
                  />
                )}
                {answer && (
                  <Typography.Paragraph
                    style={{ whiteSpace: 'pre-wrap', marginBottom: 12, fontSize: 15, lineHeight: 1.8 }}
                  >
                    {answer}
                  </Typography.Paragraph>
                )}
                {sources.length > 0 && (
                  <Collapse
                    size="small"
                    items={[
                      {
                        key: 'refs',
                        label: `引用来源（${sourceNames.length} 个文档）`,
                        children: (
                          <ul style={{ margin: 0, paddingLeft: 20 }}>
                            {sourceNames.map(s => (
                              <li key={s.document_name}>
                                {s.document_name}
                                {s.kb_name && (
                                  <Typography.Text type="secondary">
                                    {' '}
                                    （{s.kb_name}）
                                  </Typography.Text>
                                )}
                              </li>
                            ))}
                          </ul>
                        ),
                      },
                    ]}
                  />
                )}
              </Card>
            )}
          </>
        )}
      </div>
    </div>
  );
};

export default ExtQueryPage;
