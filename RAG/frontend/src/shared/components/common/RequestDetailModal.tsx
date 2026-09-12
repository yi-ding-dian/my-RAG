/**
 * 请求详情弹窗：检索问题 / Agentic 检索决策 / 耗时 / 完整提示词。
 *
 * 原写死在聊天页 MessageList 内部，「用户反馈」页的会话回溯抽屉要展示同款
 * 详情，复制一份必然发散 → 抽为公共组件，两处复用同一实现。
 *
 * 数据全部来自 assistant 消息自身字段（prompt / retrieval_ms / kg_ms /
 * total_ms / agentic），后端落盘时随消息保存，故历史会话与归档回溯同样可看。
 */
import React from 'react';
import { Button, Tag, theme } from 'antd';
import { FileTextOutlined } from '@ant-design/icons';
import type { ChatMessage, GenParams, Source } from '../../api/client';
import AppModal from './AppModal';
import MdImages from './MdImages';
import { renderTableBlocks } from './MarkdownTable';
import { cleanAnswerText } from '../../utils/cleanMarkdown';

/** 回答内图片最大宽度（与聊天页气泡一致） */
const ANSWER_IMAGE_MAX_WIDTH = 'min(480px, 100%)';

/** 毫秒可读化：<1s 显示毫秒，≥1s 同时显示秒（"总耗时"展示用） */
const formatMs = (ms: number): string =>
  ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒（${Math.round(ms)} ms）` : `${Math.round(ms)} ms`;

/**
 * 提示词里的引用块小标题（`[引用 3]（来源：xxx.xlsx）`）染色显示。
 * 完整提示词动辄上万字，引用块靠这个小标题分隔——染成紫色便于一眼定位到
 * 第几段引用，与正文的黑/灰拉开区分。
 */
const renderPromptText = (text: string): React.ReactNode[] => {
  const re = /\[引用\s*\d+\]（来源：[^）]*）/g;
  const parts: React.ReactNode[] = [];
  let last = 0;
  let k = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      parts.push(
        <MdImages key={`t${k}`} text={text.slice(last, m.index)}
                  maxWidth={ANSWER_IMAGE_MAX_WIDTH} />,
      );
    }
    parts.push(
      <span key={`r${k++}`} style={{ color: '#722ed1', fontWeight: 600 }}>
        {m[0]}
      </span>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    parts.push(
      <MdImages key={`t${k}`} text={text.slice(last)}
                maxWidth={ANSWER_IMAGE_MAX_WIDTH} />,
    );
  }
  return parts;
};

/**
 * 提示词内容渲染：清洗 markdown 结构符号 → 表格块渲染 → 图片引用转真实图片
 *
 * 等价于聊天页 renderContent(content, undefined, undefined)：无 sources 时
 * 引用标拆分会整段原样返回，故此处只保留清洗/表格/图片三段，不搬引用标逻辑
 * （那部分与聊天页气泡渲染强耦合，搬过来只会引来重复维护）。
 */
const renderPromptContent = (content: string): React.ReactNode =>
  renderTableBlocks(cleanAnswerText(content.trim())).map((b, bi) =>
    typeof b === 'string'
      ? <React.Fragment key={`m${bi}`}>{renderPromptText(b)}</React.Fragment>
      : <React.Fragment key={`t${bi}`}>{b}</React.Fragment>,
  );

/**
 * 完整提示词逐条渲染：
 * - 第一条 system → "System（系统提示）"
 * - 最后一条 user → "User（当前问题）"
 * - 中间条目按 role 标注"历史 · user / assistant"
 */
const renderPromptEntries = (
  prompt: unknown,
  token: ReturnType<typeof theme.useToken>['token'],
): React.ReactNode => {
  if (!Array.isArray(prompt)) {
    return <div style={{ fontSize: 12, color: token.colorTextTertiary }}>（无提示词数据）</div>;
  }
  return prompt.map((entry, i) => {
    const msg = entry as { role?: string; content?: string };
    const role = msg?.role ?? '';
    const content = msg?.content ?? '';
    let title: string;
    if (i === 0 && role === 'system') {
      title = 'System（系统提示）';
    } else if (i === prompt.length - 1 && role === 'user') {
      title = 'User（当前问题）';
    } else if (role === 'user') {
      title = '历史 · user';
    } else if (role === 'assistant') {
      title = '历史 · assistant';
    } else {
      title = `消息 ${i + 1}`;
    }
    return (
      <div key={i} style={{ marginBottom: 10 }}>
        <div
          style={{
            fontSize: 12,
            fontWeight: 600,
            color: token.colorTextSecondary,
            marginBottom: 2,
          }}
        >
          {title}
        </div>
        <div
          style={{
            fontSize: 12,
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            color: token.colorText,
            background: token.colorFillTertiary,
            padding: '8px 10px',
            borderRadius: 6,
          }}
        >
          {content ? renderPromptContent(content) : '（空）'}
        </div>
      </div>
    );
  });
};

/** 思考模式展示文案 */
const THINKING_LABELS: Record<string, string> = {
  disabled: '关闭思考（disabled）',
  enabled_low: '开启思考 · 低强度',
  enabled_high: '开启思考 · 高强度',
  enabled_max: '开启思考 · 最高强度',
};

/** 布尔开关展示（非布尔值显示 —，避免 undefined 渲染成空白） */
const onOff = (v: unknown): string => (v === true ? '开' : v === false ? '关' : '—');

/**
 * 生成参数展示（详情最末区块）：本次问答**实际生效**的配置快照。
 *
 * 存在的意义是事后追溯——"这条回答当时是怎么跑出来的"。温度偏高时标红：
 * 实测 temperature=1.3 时同一问题有约 25% 概率拒答，0.3 时 8/8 全对。
 */
const GenParamsView: React.FC<{
  params?: GenParams;
  token: ReturnType<typeof theme.useToken>['token'];
}> = ({ params, token }) => {
  if (!params || Object.keys(params).length === 0) {
    return (
      <div style={{ fontSize: 12, color: token.colorTextTertiary }}>
        该条未记录生成参数——早于该功能上线的历史会话。
      </div>
    );
  }
  const r = params.retrieval || {};
  const retrievalLine = (
    <>
      检索：top_k {r.top_k ?? '—'} ｜ 相似度阈值 {r.similarity_threshold ?? '不限'} ｜ 混合检索{' '}
      {onOff(r.enable_hybrid)} ｜ 重排 {onOff(r.enable_rerank)}
    </>
  );
  // 未调用模型（检索无命中）：模型/采样字段本就为空，单独说明而非显示一排"—"
  if (!params.model) {
    return (
      <div style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}>
        <div>本次未调用模型——检索无命中，直接返回固定提示。</div>
        <div>{retrievalLine}</div>
      </div>
    );
  }
  const tempHot = typeof params.temperature === 'number' && params.temperature >= 1.0;
  return (
    <div style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}>
      <div>模型：{params.model}</div>
      <div>
        温度：
        <span style={tempHot ? { color: '#f5222d', fontWeight: 600 } : undefined}>
          {params.temperature ?? '未设置（用模型默认）'}
        </span>
        {tempHot && <span style={{ color: '#f5222d' }}>（偏高，输出易摇摆）</span>}
        {' ｜ '}top_p：{params.top_p ?? '未设置'}
        {' ｜ '}最大输出：{params.max_tokens ?? '未设置'}
      </div>
      <div>
        思考模式：{THINKING_LABELS[params.thinking_mode ?? ''] ?? params.thinking_mode ?? '—'}
      </div>
      <div>
        多轮对话：{onOff(params.enable_multi_turn)}
        {params.enable_multi_turn ? `（历史 ${params.history_rounds ?? '—'} 轮）` : ''}
        {' ｜ '}知识图谱增强：{onOff(params.kg_enhance)}
        {' ｜ '}Agentic 检索：{onOff(params.agentic_enabled)}
      </div>
      <div>{retrievalLine}</div>
    </div>
  );
};

export interface RequestDetailModalProps {
  /** 目标 assistant 消息（null = 不渲染弹窗） */
  message: ChatMessage | null;
  /** 检索问题：该回答对应的用户提问（调用方从消息数组向前取最近一条 user） */
  question: string;
  onClose: () => void;
  /** 该条回答的引用片段。超管回溯时传入（气泡下只列了文档名，片段正文在此展开）；
   *  聊天页气泡下已有「引用来源」入口，不重复传 */
  sources?: Source[];
  /** 该条反馈。仅超管回溯时传入（聊天页是自己点的，无展示意义） */
  feedback?: { rating: string; reason?: string } | null;
}

const RequestDetailModal: React.FC<RequestDetailModalProps> = ({
  message,
  question,
  onClose,
  sources,
  feedback,
}) => {
  const { token } = theme.useToken();
  if (!message) return null;
  // agentic 为空对象（默认关闭时后端落 {}）时整块不渲染——原实现只画一个空标题
  const hasAgentic = !!message.agentic
    && (!!message.agentic.original_query || (message.agentic.trace?.length ?? 0) > 0);
  const promptEntries = Array.isArray(message.prompt) ? message.prompt : [];
  // 引用的文档名去重：同一份文档常被多个块命中（或重复上传多份副本），
  // 逐个列会刷屏。取 message.sources（回答自带）而非 props.sources——
  // 后者仅回溯时传入，而"这份回答引用了哪些文档"两处都该看得到
  const citedDocs = [...new Set(
    (message.sources || []).map(s => s.document_name || s.document_id))].filter(Boolean);
  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 760, h: 520 }}
      rememberKey="detail"
      open
      width={760}
      title={
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          <FileTextOutlined style={{ color: 'var(--brand-primary, #2563eb)' }} />
          请求详情
        </span>
      }
      footer={[
        <Button key="close" onClick={onClose}>
          关闭
        </Button>,
      ]}
      onCancel={onClose}
      styles={{ body: { maxHeight: '70vh', overflowY: 'auto', paddingTop: 8 } }}
      destroyOnClose
    >
      {/* flexShrink: 0 —— Modal body 内层是 flex column 容器，内容总高超出时
          子元素会被 flex 压缩（实测「回复」框被压到 16px、正文只剩一线）。
          整块包一层不可压缩的容器，超出部分交给 body 的 overflow 滚动 */}
      <div style={{ flexShrink: 0 }}>
      {/* 回答时间（历史数据可能缺失，缺则整行不显示） */}
      {message.created_at && (
        <div style={{ marginBottom: 10, fontSize: 13, color: token.colorTextSecondary }}>
          回答时间：{message.created_at}
        </div>
      )}
      {/* 检索问题：该条回答对应的用户原问题（当前链路无查询改写） */}
      <div style={{ marginBottom: 14 }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>检索问题</div>
        <div
          style={{
            fontSize: 13,
            color: token.colorTextSecondary,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            background: token.colorFillTertiary,
            padding: '8px 12px',
            borderRadius: 6,
          }}
        >
          {question || '（无）'}
        </div>
      </div>
      {/* 用户反馈（仅超管回溯传入；聊天页是自己点的，无展示意义） */}
      {feedback && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>用户反馈</div>
          <div style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}>
            {feedback.rating === 'up'
              ? <Tag color="green" style={{ marginRight: 6 }}>👍 点赞</Tag>
              : <Tag color="red" style={{ marginRight: 6 }}>👎 点踩</Tag>}
            {feedback.reason
              ? (
                <span style={{ color: feedback.rating === 'down' ? '#f5222d' : undefined }}>
                  {feedback.reason}
                </span>
              )
              : '（用户未填写原因）'}
          </div>
        </div>
      )}
      {/* Agentic 检索决策轨迹（改写查询/分档分数/尝试次数；关闭时后端落 {}，整块不渲染） */}
      {hasAgentic && message.agentic && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>Agentic 检索决策</div>
          {message.agentic.original_query !== message.agentic.final_query && (
            <div style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}>
              查询改写：{message.agentic.original_query} → {message.agentic.final_query}
            </div>
          )}
          {(message.agentic.trace ?? []).map(t => (
            <div
              key={t.attempt}
              style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}
            >
              第 {t.attempt} 轮{t.from_rewrite ? '（改写后）' : '（原始查询）'}：相似度{' '}
              {t.best_score != null ? t.best_score.toFixed(3) : '—'}，命中 {t.sources_count} 条
              {t.rewrite_failed ? '（改写失败，原查询重试）' : ''}
            </div>
          ))}
        </div>
      )}
      {/* 耗时统计（后端统计召回/图谱构建，前端计算提问→首字总耗时）
          判类型而非判 !== undefined：后端 None 序列化成 null，历史会话与归档
          里未采集的耗时是 null，判 undefined 会渲染出 "null ms" / "0 ms" */}
      <div style={{ marginBottom: 14 }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>耗时</div>
        <div style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}>
          召回耗时：{typeof message.retrieval_ms === 'number' ? `${message.retrieval_ms} ms` : '—'}
          {typeof message.kg_ms === 'number' && ` ｜ 图谱构建：${message.kg_ms} ms`}
        </div>
        <div style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}>
          总耗时（提问→首字）：{typeof message.total_ms === 'number' ? formatMs(message.total_ms) : '—'}
        </div>
      </div>
      {/* 引用片段（仅回溯传入）：该轮检索到的片段正文——超管判断"AI 为什么这么答"靠它 */}
      {sources && sources.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>引用片段（{sources.length}）</div>
          {sources.map((s, i) => (
            <div key={i} style={{ marginBottom: 8 }}>
              <div style={{ fontSize: 12, color: token.colorTextSecondary, marginBottom: 2 }}>
                [{i + 1}] {s.document_name || s.document_id} · 块 {s.chunk_index} · 相似度{' '}
                {(s.score * 100).toFixed(0)}%
              </div>
              <div
                style={{
                  fontSize: 12,
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  color: token.colorText,
                  background: token.colorFillTertiary,
                  padding: '8px 10px',
                  borderRadius: 6,
                  maxHeight: 180,
                  overflowY: 'auto',
                }}
              >
                {s.text || '（无片段文本）'}
              </div>
            </div>
          ))}
        </div>
      )}
      {/* 完整提示词：整块打包发给 AI 的 messages 数组（可读格式逐条展示） */}
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        完整提示词{promptEntries.length > 0 ? `（${promptEntries.length} 条消息）` : ''}
      </div>
      {promptEntries.length > 0
        ? renderPromptEntries(message.prompt, token)
        : (
          <div style={{ fontSize: 12, color: token.colorTextTertiary }}>
            该条未记录提示词——早于该功能上线的历史会话，或未调用模型的回复（如无命中检索）。
            引用片段与回答内容仍可在回溯抽屉中查看。
          </div>
        )}
      {/* 回复正文：追溯时不用关掉弹窗就能看到"AI 最终答了什么"——
          上面的完整提示词只说明"喂进去什么"，回答正文才是产出结果 */}
      <div style={{ fontWeight: 600, marginBottom: 4, marginTop: 16 }}>回复</div>
      <div
        style={{
          fontSize: 13,
          lineHeight: 1.7,
          wordBreak: 'break-word',
          color: token.colorText,
          background: token.colorFillTertiary,
          padding: '8px 10px',
          borderRadius: 6,
          maxHeight: 320,
          overflowY: 'auto',
        }}
      >
        {message.content ? renderPromptContent(message.content) : '（空回答）'}
      </div>
      {/* 回复里引用的文档：只列文档名（去重）。片段正文看上面的「引用片段」 */}
      {citedDocs.length > 0 && (
        <>
          <div style={{ fontWeight: 600, marginBottom: 4, marginTop: 16 }}>
            回复里引用的文档（{citedDocs.length}）
          </div>
          <div style={{ fontSize: 13, lineHeight: '22px', color: token.colorTextSecondary }}>
            {citedDocs.map((name, i) => <div key={i}>· {name}</div>)}
          </div>
        </>
      )}
      {/* 生成参数放最末：先看现场（问题/回答/引用），再回头看"当时是怎么跑的" */}
      <div style={{ fontWeight: 600, marginBottom: 4, marginTop: 16 }}>生成参数</div>
      <GenParamsView params={message.gen_params} token={token} />
      </div>
    </AppModal>
  );
};

export default RequestDetailModal;
