/**
 * RAGAS 评测专用常量、类型与纯函数（RagasDetail 及其拆出的组件共用）。
 *
 * 原先常量散在 `analytics-shared.tsx`（那个文件 9 个导出有 5 个只有 RAGAS 用，
 * 与"公共资源"名不副实），纯函数则散在 RagasDetail 里（2 个在模块级、
 * 4 个埋在组件内部）。按领域归拢到本文件。
 *
 * **不在本文件的**：`statusOf` 键虽取自 RAGAS 任务状态（queued/running/…），
 * 但概览页 Analytics.tsx 也在用，属跨领域，留在 `../shared`。
 *
 * 本文件无 JSX，故扩展名用 .ts（对照 `documents/ingestDefaults.ts`）。
 */
import dayjs from 'dayjs';
import * as XLSX from 'xlsx';

import type { RagasSampleInput } from '../../../shared/api/client';

/* ────────────── 常量与类型 ────────────── */

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

/** 测试集解析结果（txt / Excel 两条路径返回同一形状） */
export interface ParseSamplesResult {
  rows: EvalSampleRow[];
  skipped: number;
  invalid: string[];
}

/* ────────────── 纯函数（不碰组件状态，可独立复用） ────────────── */

/** 任务的平均分（各指标算术平均）；没有分数快照时返回 null */
export const avgScore = (scores?: Record<string, number>): number | null => {
  if (!scores) return null;
  const vals = Object.values(scores).filter(v => typeof v === 'number');
  if (vals.length === 0) return null;
  return vals.reduce((a, b) => a + b, 0) / vals.length;
};

/** 浏览器下载文件（用户自行选择保存位置/文件夹） */
export const downloadBlob = (blob: Blob, filename: string) => {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
};

/** 导出文件名的时间戳（形如 20261009-093000） */
export const evalFileStamp = () => dayjs().format('YYYYMMDD-HHmmss');

/** 解析 txt 文本 → 样本（每行：问题【Tab】答案；# 注释/空行忽略；
 *  无效行计数并记录行号+片段，最多 100 条） */
export const parseTxtSamples = (text: string): ParseSamplesResult => {
  const rows: EvalSampleRow[] = [];
  let skipped = 0;
  const invalid: string[] = [];
  const lines = text.split('\n');
  for (let i = 0; i < lines.length && rows.length < 100; i++) {
    const line = lines[i].replace(/\r$/, '');
    const t = line.trim();
    if (!t || t.startsWith('#')) continue; // 空行 / # 注释行忽略
    const idx = line.indexOf('\t');
    if (idx <= 0) { skipped++; invalid.push(`第 ${i + 1} 行：${t.slice(0, 30)}`); continue; }
    const question = line.slice(0, idx).trim();
    const groundTruth = line.slice(idx + 1).trim();
    if (!question || !groundTruth) { skipped++; invalid.push(`第 ${i + 1} 行：${t.slice(0, 30)}`); continue; }
    rows.push({ question, ground_truth: groundTruth });
  }
  return { rows, skipped, invalid };
};

/** 解析 Excel/csv 文件 → 样本（第一列问题、第二列正确答案；首行表头跳过，
 *  最多 100 条；xlsx 库统一处理含引号转义等 csv 细节） */
export const parseExcelSamples = (buf: ArrayBuffer): ParseSamplesResult => {
  const wb = XLSX.read(buf, { type: 'array' });
  const ws = wb.Sheets[wb.SheetNames[0]];
  if (!ws) return { rows: [], skipped: 0, invalid: ['文件无工作表'] };
  const grid = XLSX.utils.sheet_to_json<unknown[]>(ws, { header: 1, defval: '' });
  const rows: EvalSampleRow[] = [];
  let skipped = 0;
  const invalid: string[] = [];
  for (let i = 1; i < grid.length && rows.length < 100; i++) { // 跳过首行表头
    const cell = grid[i] || [];
    const question = String(cell[0] ?? '').trim();
    const groundTruth = String(cell[1] ?? '').trim();
    if (!question && !groundTruth) continue; // 空行忽略
    if (!question || !groundTruth) {
      skipped++;
      invalid.push(`第 ${i + 1} 行：${(question || groundTruth).slice(0, 30)}`);
      continue;
    }
    rows.push({ question, ground_truth: groundTruth });
  }
  return { rows, skipped, invalid };
};

/** 解析「问题<TAB>答案」文本（与「文本导入」同口径，也兼容 | 分隔；答案可留空——
 *  配合「实时生成」模式时不需要标准答案） */
export const parseDatasetText = (text: string): RagasSampleInput[] => {
  const rows: RagasSampleInput[] = [];
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    const parts = t.split(/\t|\s*\|\s*/);
    const q = (parts[0] || '').trim();
    if (!q) continue;
    rows.push({ question: q, ground_truth: (parts[1] || '').trim() });
  }
  return rows;
};
