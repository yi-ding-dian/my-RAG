/**
 * 统计分析公共资源（概览页 Analytics.tsx 与 3 个详情页共用）：
 * - RAGAS 常量：指标中文映射/状态文案/分数颜色/评测指标选项/测试集行类型
 * - FeedbackSummary：用户反馈汇总小卡（概览摘要卡内展示）
 *
 * 由 Analytics 单页改造"概览卡片 + 点击下钻详情页"时拆分，逻辑纯搬移不改行为。
 */
import React, { useEffect, useState } from 'react';
import { Skeleton, Space, Typography, theme } from 'antd';
import type { ChatFeedbackStats } from '../../shared/api/other';
import { getChatFeedbackStats } from '../../shared/api/client';

const { Text } = Typography;

/** RAGAS 指标英文 → 中文映射（未知指标保持原名；Tooltip 显示英文原名） */
export const metricLabelMap: Record<string, string> = {
  faithfulness: '忠实度',
  answer_relevancy: '答案相关性',
  context_precision: '上下文精确率',
  context_recall: '上下文召回率',
  answer_correctness: '答案正确性',
  answer_similarity: '答案相似度',
  hallucination: '幻觉率',
  noise_sensitivity: '噪声敏感度',
  coherence: '连贯性',
};

export const metricLabel = (m: string): string => metricLabelMap[m] ?? m;

// RAGAS 任务状态文案/颜色
export const statusConfig: Record<string, { color: string; label: string }> = {
  queued: { color: 'warning', label: '排队中' },
  pending: { color: 'default', label: '等待中' },
  running: { color: 'processing', label: '运行中' },
  completed: { color: 'success', label: '已完成' },
  failed: { color: 'error', label: '失败' },
};

export const statusOf = (s: string) => statusConfig[s] || { color: 'default', label: s };

// 分数颜色（0.8 绿 / 0.5 黄 / 红）
export const scoreColor = (v: number) => (v >= 0.8 ? '#52c41a' : v >= 0.5 ? '#faad14' : '#f5222d');

// RAGAS 6 个指标选项（值=英文名）；默认全部选中——手动测试集用户已填写
// 正确答案（ground_truth），6 个指标均可评分（可取消不需要的指标）
export const ragasMetricOptions = [
  { label: '忠实度', value: 'faithfulness', desc: '答案是否忠于检索到的上下文，无虚构（不依赖标准答案）' },
  { label: '答案相关性', value: 'answer_relevancy', desc: '答案与问题的相关程度（不依赖标准答案）' },
  { label: '上下文精确率', value: 'context_precision', desc: '检索上下文是否包含有用信息（不依赖标准答案）' },
  { label: '上下文召回率', value: 'context_recall', desc: '检索上下文是否覆盖必要信息（需 ground_truth）' },
  { label: '答案正确性', value: 'answer_correctness', desc: '答案与参考答案的匹配程度（需 ground_truth）' },
  { label: '答案相似度', value: 'answer_similarity', desc: '答案与参考答案的语义相似度（需 ground_truth）' },
];
export const DEFAULT_METRICS = ragasMetricOptions.map(o => o.value);

// 测试集表单每行的值类型
export interface EvalSampleRow {
  question?: string;
  ground_truth?: string;
}

/** 用户反馈汇总小卡：总数/好评/差评 + 最近反馈（原因）列表。
 *  maxRecent 可选：限制"最近反馈"展示条数（概览摘要卡传小值保一屏，缺省=全部，与原行为一致） */
export const FeedbackSummary: React.FC<{ maxRecent?: number }> = ({ maxRecent }) => {
  const { token } = theme.useToken();
  const [stats, setStats] = useState<ChatFeedbackStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    getChatFeedbackStats()
      .then(res => {
        if (!cancelled) setStats(res.data);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return <Skeleton active paragraph={{ rows: 3 }} />;
  if (error || !stats) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        反馈数据加载失败或暂无权限查看
      </Text>
    );
  }
  const rate = stats.total > 0 ? Math.round((stats.up / stats.total) * 100) : 0;
  const recent = maxRecent ? stats.recent.slice(0, maxRecent) : stats.recent;
  return (
    <div>
      <Space size={24} wrap>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>总反馈</Text>
          <div style={{ fontSize: 20, fontWeight: 600 }}>{stats.total}</div>
        </div>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>好评</Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: '#52c41a' }}>{stats.up}</div>
        </div>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>差评</Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: '#ff4d4f' }}>{stats.down}</div>
        </div>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>好评率</Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: token.colorPrimary }}>
            {rate}%
          </div>
        </div>
      </Space>
      {recent.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            最近反馈{maxRecent ? `（最新 ${maxRecent} 条）` : ''}
          </Text>
          {recent.map(r => (
            <div
              key={r.id}
              style={{
                padding: '6px 0',
                borderBottom: `1px solid ${token.colorBorderSecondary}`,
                fontSize: 12,
              }}
            >
              <span
                style={{
                  color: r.rating === 'up' ? '#52c41a' : '#ff4d4f',
                  marginRight: 6,
                }}
              >
                {r.rating === 'up' ? '👍' : '👎'}
              </span>
              {r.reason || <Text type="secondary">（无补充说明）</Text>}
              <Text type="secondary" style={{ marginLeft: 8, fontSize: 11 }}>
                {r.created_at}
              </Text>
            </div>
          ))}
        </div>
      )}
      {stats.total === 0 && (
        <Text type="secondary" style={{ fontSize: 12 }}>暂无用户反馈</Text>
      )}
    </div>
  );
};
