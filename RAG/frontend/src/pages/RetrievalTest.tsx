/**
 * 检索测试/调试页：选知识库 → 输入问题 → 查看检索命中 chunk（文本/分数/来源/阈值对比）
 *
 * 布局参考 KnowFlow /knowledge/testing：左侧控制面板（知识库/问题/参数/快速尝试/历史），
 * 右侧结果区（统计 + 命中卡片列表 + 上下文 Modal）。信息密度高、专业调试工具风格。
 *
 * 参数语义（与后端契约一致，全可选）：
 * - top_k：null=跟随配置默认；数字=显式覆盖
 * - similarity_threshold：null=跟随配置；数字=覆盖（0-1），命中分数低于生效阈值标红
 * - enable_hybrid / enable_rerank：null=跟随配置 / true=强制开 / false=强制关（对比实验）；
 *   混合检索=BM25 关键词与向量 RRF 融合（关闭即纯向量），重排=rerank 服务重排
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Card, Col, Divider, Input, InputNumber, Modal, Row, Select,
  Skeleton, Slider, Space, Statistic, Tag, Tooltip, Typography, message, theme,
} from 'antd';
import {
  CloseOutlined, FileTextOutlined, HistoryOutlined, SearchOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import {
  KnowledgeBase, ServiceProfile, Source, getActiveProfile, getDocument,
  listKbs, retrieveChat,
} from '../api/client';
import AppEmpty from '../components/AppEmpty';
import MdImages from '../components/MdImages';
import PageHeader from '../components/PageHeader';

const { Text } = Typography;

/** 三态开关：null=跟随配置 / true=强制开 / false=强制关 */
type TriState = boolean | null;

/** 上下文前后文截取长度（字符） */
const CONTEXT_CHARS = 200;

/** 常用问题示例（快速尝试按钮） */
const QUICK_QUESTIONS = [
  '总结一下这个知识库的内容',
  '这个知识库主要包含哪些主题？',
  '系统支持哪些文档格式？',
  '数据是如何入库的？',
];

/** 本次页面会话内的检索历史 */
interface HistoryItem {
  query: string;
  hitCount: number;
  elapsedMs: number;
}

/** 检索结果快照 */
interface SearchResult {
  query: string;
  sources: Source[];
  elapsedMs: number;
}

/** 上下文 Modal 数据（前后文 + 命中块高亮） */
interface ContextData {
  source: Source;
  before: string;
  chunk: string;
  after: string;
  hasOffset: boolean;
}

/** 三态 Select：跟随配置 / 强制开 / 强制关 */
const TriStateSelect: React.FC<{
  value: TriState;
  onChange: (v: TriState) => void;
}> = ({ value, onChange }) => (
  <Select
    size="small"
    value={value === null ? 'follow' : value ? 'on' : 'off'}
    onChange={(v) => onChange(v === 'follow' ? null : v === 'on')}
    options={[
      { value: 'follow', label: '跟随配置' },
      { value: 'on', label: '强制开' },
      { value: 'off', label: '强制关' },
    ]}
    style={{ width: 120 }}
  />
);

const RetrievalTestPage: React.FC = () => {
  const [messageApi, contextHolder] = message.useMessage();
  const { token } = theme.useToken();

  // 知识库（多选：多库对比检索，每库独立检索后合并按分数降序取全局 top_k）
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [kbsLoadFailed, setKbsLoadFailed] = useState(false);
  const [kbIds, setKbIds] = useState<string[]>([]);

  // 问题与检索状态
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);

  // 「停止」按钮：AbortController 取消进行中的检索请求（axios CanceledError 静默结束）
  const abortRef = useRef<AbortController | null>(null);

  // 调试参数（null=跟随配置）
  const [topK, setTopK] = useState<number | null>(null);
  const [threshold, setThreshold] = useState<number | null>(null);
  const [hybrid, setHybrid] = useState<TriState>(null);
  const [rerank, setRerank] = useState<TriState>(null);

  // 活跃配置（展示配置默认值）
  const [profile, setProfile] = useState<ServiceProfile | null>(null);

  // 上下文 Modal
  const [contextOpen, setContextOpen] = useState(false);
  const [contextLoading, setContextLoading] = useState(false);
  const [context, setContext] = useState<ContextData | null>(null);

  // 生效阈值：用户覆盖优先，否则配置默认
  const defaultThreshold = profile?.retrieval?.similarity_threshold ?? 0;
  const effectiveThreshold = threshold !== null ? threshold : defaultThreshold;
  const defaultTopK = profile?.retrieval?.top_k ?? 5;
  // 配置默认的混合检索/重排状态（"跟随配置"时的实际行为）
  const defaultHybrid = profile?.retrieval?.enable_hybrid ?? true;
  const defaultRerank = profile?.retrieval?.rerank?.enabled ?? false;
  // 本次检索的 rerank 是否实际生效（强制开 或 跟随配置且配置默认开）
  const isRerankMode = rerank === true || (rerank === null && defaultRerank);

  // 选中知识库中暂无文档的库（doc_count 由列表接口返回，0=尚未上传任何文档）
  const selectedEmptyKbs = kbIds
    .map(id => kbs.find(k => k.id === id))
    .filter((kb): kb is KnowledgeBase => !!kb && kb.doc_count === 0);
  // 所有选中库都为空时禁用检索，引导先去文档管理上传解析
  const allSelectedEmpty = kbIds.length > 0 && selectedEmptyKbs.length === kbIds.length;

  // 加载知识库列表 + 活跃配置（失败明确提示，并与"确实无知识库"区分）
  useEffect(() => {
    listKbs().then((r) => {
      setKbsLoadFailed(false);
      setKbs(r.data);
    }).catch(() => {
      setKbsLoadFailed(true);
      setKbs([]);
      messageApi.error('知识库列表加载失败，请刷新页面重试');
    });
    getActiveProfile().then((r) => setProfile(r.data)).catch(() => undefined);
  }, []);

  /** 执行检索（q 传入时用 q，否则用输入框当前值）；多库时后端按 kb_ids 对比检索 */
  const doSearch = useCallback(async (q?: string) => {
    const query = (q ?? question).trim();
    if (kbIds.length === 0) {
      messageApi.warning('请至少选择一个知识库');
      return;
    }
    if (allSelectedEmpty) {
      messageApi.warning('所选知识库暂无文档，请先在文档管理上传并解析');
      return;
    }
    if (!query) {
      messageApi.warning('请输入测试问题');
      return;
    }
    // 取消上一次未完成的检索（loading 期间按钮已禁用，此处兜底防快速连点）
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    const t0 = performance.now();
    try {
      const { sources } = await retrieveChat({
        kb_ids: kbIds,
        query,
        top_k: topK ?? undefined,
        enable_hybrid: hybrid ?? undefined,
        enable_rerank: rerank ?? undefined,
        similarity_threshold: threshold ?? undefined,
      }, controller.signal);
      const elapsedMs = performance.now() - t0;
      setResult({ query, sources, elapsedMs });
      // 追加历史（保留最近 20 条，点击可回填重新检索）
      setHistory((prev) =>
        [...prev, { query, hitCount: sources.length, elapsedMs }].slice(-20));
    } catch (err) {
      // 用户点击「停止」：axios 抛 CanceledError，静默结束不弹错误
      if ((err as { code?: string })?.code === 'ERR_CANCELED') return;
      const detail = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      messageApi.error(`检索失败：${detail || '未知错误'}`);
    } finally {
      setLoading(false);
      if (abortRef.current === controller) abortRef.current = null;
    }
  }, [kbIds, question, topK, hybrid, rerank, threshold, messageApi, allSelectedEmpty]);

  /** 「停止」：中止进行中的检索请求 */
  const stopSearch = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // 组件卸载时中止未完成的检索
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  /** 打开上下文 Modal：优先用命中块的 char_start/char_end 截取前后文；缺失降级展示全文 */
  const openContext = async (s: Source) => {
    setContextOpen(true);
    setContextLoading(true);
    setContext(null);
    try {
      let before = '';
      let chunk = s.text;
      let after = '';
      let hasOffset = false;
      // 多库检索时以 Source 自带的 kb_id 定位文档；缺失时回退首个选中库
      const targetKb = s.kb_id || kbIds[0];
      if (targetKb) {
        const doc = await getDocument(targetKb, s.document_id);
        const full = doc.data?.full_text || '';
        let cs = s.char_start ?? -1;
        let ce = s.char_end ?? -1;
        // Source 无偏移时尝试从文档 chunks 元数据按块序号补取
        if ((cs < 0 || ce < 0) && Array.isArray(doc.data?.chunks)) {
          const c = doc.data.chunks.find((x) => x.index === s.chunk_index);
          if (c && typeof c.char_start === 'number' && typeof c.char_end === 'number') {
            cs = c.char_start;
            ce = c.char_end;
          }
        }
        if (full && cs >= 0 && ce > cs && cs < full.length) {
          before = full.slice(Math.max(0, cs - CONTEXT_CHARS), cs);
          chunk = full.slice(cs, Math.min(ce, full.length));
          after = full.slice(Math.min(ce, full.length),
                             Math.min(full.length, ce + CONTEXT_CHARS));
          hasOffset = true;
        }
      }
      setContext({ source: s, before, chunk, after, hasOffset });
    } catch {
      // 获取全文失败：降级展示命中文本
      setContext({ source: s, before: '', chunk: s.text, after: '', hasOffset: false });
    } finally {
      setContextLoading(false);
    }
  };

  // 命中平均分（无命中为 null）
  const avgScore = result && result.sources.length > 0
    ? result.sources.reduce((a, s) => a + s.score, 0) / result.sources.length
    : null;

  /** 单个命中卡片：文档名 + 块序号 + 分数（rerank 模式显示重排分、阈值不适用不标红）+ 3 行文本预览 + 查看上下文 */
  const renderSourceCard = (s: Source, idx: number) => {
    // rerank 模式按重排分排序，相似度阈值不适用 → 不标红
    const isLow = !isRerankMode && s.score < effectiveThreshold;
    return (
      <div
        key={s.id}
        style={{
          border: `1px solid ${token.colorBorderSecondary}`,
          borderLeft: `3px solid ${isLow ? '#f5222d' : token.colorPrimary}`,
          borderRadius: 8,
          padding: '10px 12px',
          marginBottom: 10,
          background: token.colorBgContainer,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>#{idx + 1}</Text>
          <Tooltip title={s.document_id}>
            <Tag color="blue" icon={<FileTextOutlined />} style={{ maxWidth: 200 }}>
              {s.document_name || '未知文档'}
            </Tag>
          </Tooltip>
          {/* 多库对比检索：命中来源库名（kb_name 后端填充，缺失时按 kb_id 查列表兜底） */}
          <Tooltip title={s.kb_name || (s.kb_id ? kbs.find(k => k.id === s.kb_id)?.name : undefined)}>
            <Tag color="geekblue" style={{ maxWidth: 140 }}>
              {s.kb_name || kbs.find(k => k.id === s.kb_id)?.name || s.kb_id?.slice(0, 8) || '未知库'}
            </Tag>
          </Tooltip>
          <Text type="secondary" style={{ fontSize: 12 }}>块 {s.chunk_index}</Text>
          {/* 混合检索调试信息：原始向量分（BM25 单独命中的块无向量分） */}
          {s.vector_score != null ? (
            <Tooltip title="原始向量检索分数（混合模式调试用）">
              <Text type="secondary" style={{ fontSize: 11 }}>
                向量分 {s.vector_score.toFixed(4)}
              </Text>
            </Tooltip>
          ) : (
            <Tag color="purple" style={{ fontSize: 11, lineHeight: '16px' }}>
              仅关键词命中
            </Tag>
          )}
          <span style={{ flex: 1 }} />
          <Tooltip title={
            isRerankMode
              ? 'rerank 服务重排分数（相似度阈值不适用，不标红）'
              : `融合分数（生效阈值 ${effectiveThreshold.toFixed(4)}，低于阈值标红）`
          }>
            <Tag color={isLow ? 'red' : 'green'} style={{ marginInlineEnd: 0 }}>
              {isRerankMode ? `重排分 ${s.score.toFixed(4)}` : `融合分 ${s.score.toFixed(4)}`}
            </Tag>
          </Tooltip>
          <Button size="small" type="link" onClick={() => openContext(s)}>
            查看上下文
          </Button>
        </div>
        <Typography.Paragraph
          ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
          style={{ margin: 0, fontSize: 13, color: token.colorTextSecondary }}
        >
          {/* markdown 图片引用渲染为真实图片（缩略图 120px，同 ChunkCompareView 左栏） */}
          <MdImages text={s.text} maxWidth={120} />
        </Typography.Paragraph>
      </div>
    );
  };

  /** 参数行包装（label 左、控件右，紧凑密度） */
  const paramRow = (label: string, control: React.ReactNode, hint?: string) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
      <Text type="secondary" style={{ fontSize: 12, width: 84, flexShrink: 0 }}>
        {label}
      </Text>
      {control}
      {hint ? (
        <Text type="secondary" style={{ fontSize: 11 }}>{hint}</Text>
      ) : null}
    </div>
  );

  return (
    <div>
      {contextHolder}
      <PageHeader
        title="检索测试"
        description="调试知识库检索效果：调整 Top K / 相似度阈值 / 混合检索与重排开关，对比命中结果差异。"
      />

      <Row gutter={16} style={{ marginTop: 16 }}>
        {/* ==================== 左侧控制面板 ==================== */}
        <Col flex="300px">
          <Card size="small" title="检索设置" styles={{ body: { paddingTop: 12 } }}>
            <Text type="secondary" style={{ fontSize: 12 }}>知识库（可多选）</Text>
            <Select
              mode="multiple"
              showSearch
              size="small"
              style={{ width: '100%', marginTop: 4 }}
              placeholder="选择知识库(可多选)"
              maxTagCount={2}
              optionFilterProp="label"
              value={kbIds}
              onChange={setKbIds}
              options={kbs.map((kb) => ({
                value: kb.id,
                label: `${kb.name}（${kb.doc_count} 文档）`,
              }))}
              notFoundContent={kbs.length === 0
                ? (kbsLoadFailed ? '知识库列表加载失败，请刷新页面重试' : '暂无知识库，请先在知识库管理创建')
                : '未找到匹配知识库'}
            />
            {selectedEmptyKbs.length > 0 && (
              <Alert
                type="warning"
                showIcon
                style={{ marginTop: 8 }}
                message={
                  allSelectedEmpty
                    ? '该知识库暂无文档，请先在文档管理上传并解析'
                    : `部分选中知识库暂无文档：${selectedEmptyKbs.map(k => k.name).join('、')}`
                }
              />
            )}

            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 12 }}>
              测试问题
            </Text>
            <Input.TextArea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="输入测试问题，Enter 检索（Shift+Enter 换行）"
              autoSize={{ minRows: 3, maxRows: 6 }}
              onPressEnter={(e) => {
                if (e.shiftKey) return;
                e.preventDefault();
                doSearch();
              }}
              style={{ marginTop: 4, fontSize: 13 }}
            />
            <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
              <Button
                type="primary"
                icon={<SearchOutlined />}
                loading={loading}
                disabled={allSelectedEmpty}
                onClick={() => doSearch()}
                style={{ flex: 1 }}
              >
                检索
              </Button>
              {/* 检索进行中显示「停止」：AbortController 取消请求（M5） */}
              {loading && (
                <Button danger icon={<CloseOutlined />} onClick={stopSearch}>
                  停止
                </Button>
              )}
            </div>

            <Divider style={{ margin: '14px 0 10px' }} />
            {paramRow('Top K', (
              <InputNumber
                size="small"
                min={1}
                max={50}
                precision={0}
                placeholder={`跟随配置（${defaultTopK}）`}
                value={topK}
                onChange={setTopK}
                style={{ width: 120 }}
              />
            ), '1-50')}

            <div style={{ marginTop: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center' }}>
                <Text type="secondary" style={{ fontSize: 12, width: 84, flexShrink: 0 }}>
                  相似度阈值
                </Text>
                <Slider
                  min={0}
                  max={1}
                  step={0.01}
                  value={threshold ?? defaultThreshold}
                  onChange={setThreshold}
                  // M5: tooltip 以百分比展示（内部值保持 0-1，请求参数不变）
                  tooltip={{ formatter: (v) => `${Math.round((v ?? 0) * 100)}%` }}
                  style={{ flex: 1, margin: '0 4px 0 0' }}
                />
                {threshold !== null ? (
                  <Button type="link" size="small" onClick={() => setThreshold(null)}
                          style={{ padding: 0, fontSize: 12 }}>
                    恢复默认
                  </Button>
                ) : (
                  <Text type="secondary" style={{ fontSize: 11, width: 56, textAlign: 'right' }}>
                    默认
                  </Text>
                )}
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: -4 }}>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  配置默认 {Math.round(defaultThreshold * 100)}%
                </Text>
                <Text strong style={{ fontSize: 11, color: token.colorPrimary }}>
                  生效 {Math.round(effectiveThreshold * 100)}%
                </Text>
              </div>
            </div>

            {paramRow('混合检索', <TriStateSelect value={hybrid} onChange={setHybrid} />,
                      defaultHybrid ? '默认开' : '默认关')}
            {paramRow('重排', <TriStateSelect value={rerank} onChange={setRerank} />,
                      defaultRerank ? '默认开' : '默认关')}
            <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 8 }}>
              混合检索：BM25 关键词与向量 RRF 融合，关闭即纯向量；
              重排：rerank 服务重排（需在系统配置中开启并配置服务地址）。用于对比实验。
            </Text>
          </Card>

          <Card size="small" title="快速尝试" style={{ marginTop: 12 }}
                styles={{ body: { paddingTop: 8, paddingBottom: 8 } }}>
            {QUICK_QUESTIONS.map((q) => (
              <Button
                key={q}
                size="small"
                block
                icon={<ThunderboltOutlined />}
                disabled={allSelectedEmpty}
                onClick={() => {
                  setQuestion(q);
                  doSearch(q);
                }}
                style={{ textAlign: 'left', marginTop: 6 }}
              >
                <span style={{
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  display: 'block', fontSize: 12,
                }}>
                  {q}
                </span>
              </Button>
            ))}
          </Card>

          <Card size="small" title="检索历史（本次会话）" style={{ marginTop: 12 }}
                styles={{ body: { paddingTop: 8, paddingBottom: 8 } }}>
            {history.length === 0 ? (
              <Text type="secondary" style={{ fontSize: 12 }}>暂无检索记录</Text>
            ) : (
              <div style={{ maxHeight: 260, overflow: 'auto' }}>
                {history.map((h, i) => (
                  <Tooltip key={i} title={`点击重新检索：${h.query}`}>
                    <div
                      onClick={() => {
                        setQuestion(h.query);
                        doSearch(h.query);
                      }}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 6,
                        padding: '5px 6px', borderRadius: 6, cursor: 'pointer',
                        borderBottom: `1px solid ${token.colorBorderSecondary}`,
                      }}
                    >
                      <HistoryOutlined style={{ fontSize: 12, color: token.colorTextTertiary }} />
                      <span style={{
                        flex: 1, fontSize: 12, color: token.colorText,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}>
                        {h.query}
                      </span>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {h.elapsedMs.toFixed(0)}ms
                      </Text>
                      <Tag color={h.hitCount > 0 ? 'green' : 'default'}
                           style={{ fontSize: 11, lineHeight: '16px', marginInlineEnd: 0 }}>
                        {h.hitCount} 命中
                      </Tag>
                    </div>
                  </Tooltip>
                ))}
              </div>
            )}
          </Card>
        </Col>

        {/* ==================== 右侧结果区 ==================== */}
        <Col flex="1" style={{ minWidth: 0 }}>
          <Card
            size="small"
            title={result ? `检索结果：${result.query}` : '检索结果'}
            styles={{ body: { paddingTop: 12 } }}
          >
            {loading ? (
              <Skeleton active paragraph={{ rows: 6 }} />
            ) : !result ? (
              /* 未检索引导 */
              <AppEmpty title="开始检索" description="输入问题测试知识库检索效果" />
            ) : result.sources.length === 0 ? (
              /* 已检索但无命中（rerank 模式按重排分排序，阈值不适用） */
              <AppEmpty
                title="未找到相关内容"
                description={
                  isRerankMode ? (
                    <span>
                      重排模式按重排分排序，相似度阈值不适用
                      <br />
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        可关闭重排或调整检索参数后重试
                      </Text>
                    </span>
                  ) : (
                    <span>
                      全部低于阈值 {effectiveThreshold.toFixed(4)}
                      <br />
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        可调低相似度阈值或关闭阈值过滤后重试
                      </Text>
                    </span>
                  )
                }
              />
            ) : (
              <>
                {/* 顶部统计：命中数 / 平均分 / 用时 / 生效阈值 */}
                <div style={{
                  display: 'flex', gap: 32, padding: '10px 16px', marginBottom: 14,
                  background: token.colorFillQuaternary, borderRadius: 8,
                }}>
                  <Statistic title="命中数" value={result.sources.length} />
                  <Tooltip title={isRerankMode
                    ? '重排模式下为 rerank 服务重排分数'
                    : '混合模式下为 RRF 融合分，纯向量模式下为向量相似度'}>
                    <div>
                      <Statistic
                        title="平均分"
                        value={avgScore ?? 0}
                        precision={4}
                        valueStyle={{ color: (avgScore ?? 0) >= 0.8 ? '#52c41a' : token.colorPrimary }}
                      />
                    </div>
                  </Tooltip>
                  <Statistic title="用时" value={result.elapsedMs} precision={0} suffix="ms" />
                  <Tooltip title={isRerankMode
                    ? '重排模式按重排分排序，相似度阈值不适用'
                    : '低于阈值的命中在卡片中标红'}>
                    <Statistic
                      title="生效阈值"
                      value={effectiveThreshold}
                      precision={4}
                      valueStyle={{ color: effectiveThreshold > 0 ? '#fa8c16' : undefined }}
                    />
                  </Tooltip>
                </div>

                {/* 命中列表 */}
                <div style={{ maxHeight: 'calc(100vh - 320px)', overflow: 'auto' }}>
                  {result.sources.map((s, i) => renderSourceCard(s, i))}
                </div>
              </>
            )}
          </Card>
        </Col>
      </Row>

      {/* ==================== 上下文 Modal ==================== */}
      <Modal
        title="命中上下文"
        open={contextOpen}
        onCancel={() => setContextOpen(false)}
        footer={null}
        width={760}
      >
        {contextLoading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : context ? (
          <>
            <Space size={8} style={{ marginBottom: 12 }}>
              <Tag color="blue" icon={<FileTextOutlined />}>{context.source.document_name || '未知文档'}</Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>块 {context.source.chunk_index}</Text>
              <Tag color={context.source.score < effectiveThreshold ? 'red' : 'green'}>
                {context.source.score.toFixed(4)}
              </Tag>
              {context.source.vector_score != null ? (
                <Tooltip title="原始向量检索分数">
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    向量 {context.source.vector_score.toFixed(4)}
                  </Text>
                </Tooltip>
              ) : (
                <Tag color="purple" style={{ fontSize: 11, lineHeight: '16px' }}>
                  仅关键词命中
                </Tag>
              )}
              {context.source.kb_id ? (
                <Text type="secondary" style={{ fontSize: 12 }}>kb: {context.source.kb_id.slice(0, 8)}</Text>
              ) : null}
            </Space>
            {context.hasOffset ? (
              <div
                style={{
                  background: token.colorFillQuaternary, border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 8,
                  padding: '12px 16px', whiteSpace: 'pre-wrap', fontSize: 13,
                  lineHeight: 1.9, maxHeight: 420, overflow: 'auto',
                }}
              >
                <Text type="secondary"><MdImages text={context.before} /></Text>
                <mark
                  style={{
                    background: 'rgba(var(--brand-primary-rgb, 37, 99, 235), 0.15)', color: 'inherit',
                    padding: '0 2px', borderRadius: 3,
                  }}
                >
                  <MdImages text={context.chunk} />
                </mark>
                <Text type="secondary"><MdImages text={context.after} /></Text>
              </div>
            ) : (
              <div>
                <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
                  历史数据无块偏移信息，展示命中文本全文
                </Text>
                <div
                  style={{
                    background: token.colorFillQuaternary, border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 8,
                    padding: '12px 16px', whiteSpace: 'pre-wrap', fontSize: 13,
                    lineHeight: 1.9, maxHeight: 420, overflow: 'auto',
                  }}
                >
                  <MdImages text={context.chunk} />
                </div>
              </div>
            )}
            {context.source.parent_text ? (
              <div style={{ marginTop: 12 }}>
                <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
                  父块全文（parent_child 检索模式上下文）
                </Text>
                <div
                  style={{
                    background: token.colorPrimaryBg, border: `1px solid ${token.colorPrimaryBorder}`, borderRadius: 8,
                    padding: '12px 16px', whiteSpace: 'pre-wrap', fontSize: 12,
                    lineHeight: 1.8, maxHeight: 220, overflow: 'auto', color: token.colorTextSecondary,
                  }}
                >
                  <MdImages text={context.source.parent_text} />
                </div>
              </div>
            ) : null}
          </>
        ) : null}
      </Modal>
    </div>
  );
};

export default RetrievalTestPage;
