/**
 * RAGAS 评测专用常量与类型（RagasDetail 及其拆出的组件共用）。
 *
 * 原先混在 `analytics-shared.tsx` 里——那个文件 9 个导出有 5 个只有 RAGAS 用
 * （就是本文件这批），名不副实。按领域拆开后本文件归 RAGAS。
 *
 * **不在本文件的**：`statusOf` 虽然键是 RAGAS 任务状态（queued/running/...），
 * 但概览页 Analytics.tsx 也在用，属跨领域，留在 `../shared`。
 */

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
