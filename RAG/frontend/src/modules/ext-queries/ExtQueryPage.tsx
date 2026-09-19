import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
import MdImages from '../../shared/components/common/MdImages';

/**
 * 引用来源（meta 事件下发的检索片段）
 *
 * 图片链接已由后端改写为本配置的专用端点（/api/ext/{id}/images/...?token=xxx），
 * 带 ext token 可直接加载——MdImages 的 withImageToken 只认内部图片代理前缀
 * （/api/files/images/），对 ext 链接原样返回，不会误加登录 JWT。
 */
interface ExtSource {
  document_name: string;
  kb_name?: string;
  text?: string;
  parent_text?: string | null;
}

/**
 * Markdown 图片语法探测（局部非全局正则：全局正则的 lastIndex 是共享可变
 * 状态，并发渲染下会互相改写导致匹配错乱）
 */
const HAS_IMAGE_RE = /!\[[^\]]*\]\([^)]*\)/;

/**
 * 兜底还原模型"简化"过的图片链接
 *
 * 实测：提示词已明确要求"图片链接必须原样保留"，模型仍会把
 * /api/ext/{config_id}/images/{doc_id}/{name} 压成 /api/ext/{doc_id}/{name}
 * （丢掉 config_id 与 images 段），浏览器里必然 404。流式回答无法在后端拦截
 * （delta 已逐块下发），故在渲染前统一还原；已是标准形式的链接不受影响。
 */
const normalizeAnswerImages = (text: string, configId: string): string =>
  text.replace(
    /\/api\/ext\/(?![^\s/?#]+\/images\/)([^\s/?#]+)\/([^\s/?#]+)/g,
    (_m, docId: string, name: string) =>
      `/api/ext/${configId}/images/${docId}/${name}`,
  );

/**
 * 外部查询页（公开，无需登录）：/ext-query/:id?token=xxx
 *
 * - 独立简化页面（无侧栏/菜单/登录）：品牌标题 + 查询输入 + 流式回答 + 来源折叠
 * - 挂载时用 token 调 GET /api/ext/{id}/info 校验；401 → 「链接无效或已失效」
 * - 流式请求直接 fetch（不走 axios：外部请求必须携带 ext token 而非登录 JWT）
 * - 样式取舍：独立浅色卡片样式，不耦合主后台主题系统（外部用户无主题偏好）
 * - 图片：回答正文与引用来源均经 MdImages 渲染（仅当配置开启图片时，
 *   后端才会下发 ext 图片链接；关闭时图片语法已在后端剥除）
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
  const [sources, setSources] = useState<ExtSource[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);
  // 引用来源默认收起时不渲染内容（父块可达上万字，展开才付出渲染开销）
  const [refsOpen, setRefsOpen] = useState(false);

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
    setRefsOpen(false);
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
              ? (data as ExtSource[])
              : ((data as { sources?: ExtSource[] })?.sources ?? []);
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

  // 回答里的图片链接兜底修复（模型可能简化路径）；仅 answer/id 变化时重算
  const renderedAnswer = useMemo(
    () => normalizeAnswerImages(answer, id ?? ''),
    [answer, id],
  );

  // 引用来源按文档分组（同一文档命中多个块时合并，各自独立展示图片）
  const sourceGroups = useMemo(() => {
    const groups = new Map<
      string,
      { document_name: string; kb_name?: string; texts: string[] }
    >();
    for (const s of sources) {
      const g = groups.get(s.document_name)
        ?? { document_name: s.document_name, kb_name: s.kb_name, texts: [] };
      // 优先用命中子块（聚焦命中位置、体量小，实测多数已含图）；
      // 子块不含图片语法时才回退父块——父块是完整章节、图更全但可能上万字
      const hit = s.text ?? '';
      const text = HAS_IMAGE_RE.test(hit) ? hit : (s.parent_text || hit);
      if (text.trim()) g.texts.push(text);
      groups.set(s.document_name, g);
    }
    return Array.from(groups.values());
  }, [sources]);

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
                    {/* 回答里的图片由模型原样输出（后端已改写为 ext 端点链接） */}
                    <MdImages text={renderedAnswer} maxWidth="100%" maxHeight={420} />
                  </Typography.Paragraph>
                )}
                {sources.length > 0 && (
                  <Collapse
                    size="small"
                    activeKey={refsOpen ? ['refs'] : []}
                    onChange={keys => setRefsOpen(keys.length > 0)}
                    items={[
                      {
                        key: 'refs',
                        label: `引用来源（${sourceGroups.length} 个文档）`,
                        // 收起时不渲染内容：父块可达上万字，展开才付出渲染开销
                        children: refsOpen ? (
                          <div style={{ maxHeight: 460, overflowY: 'auto' }}>
                            {sourceGroups.map(g => (
                              <div key={g.document_name} style={{ marginBottom: 14 }}>
                                <Typography.Text strong style={{ fontSize: 13 }}>
                                  {g.document_name}
                                </Typography.Text>
                                {g.kb_name && (
                                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                                    {' '}（{g.kb_name}）
                                  </Typography.Text>
                                )}
                                {g.texts.map((t, i) => (
                                  <div
                                    key={i}
                                    style={{
                                      marginTop: 6,
                                      padding: '8px 10px',
                                      borderRadius: 8,
                                      background: '#f8fafc',
                                      border: '1px solid #eef2f7',
                                      fontSize: 13,
                                      lineHeight: 1.7,
                                      whiteSpace: 'pre-wrap',
                                      wordBreak: 'break-word',
                                    }}
                                  >
                                    <MdImages text={t} maxWidth="100%" maxHeight={300} />
                                  </div>
                                ))}
                              </div>
                            ))}
                          </div>
                        ) : null,
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
