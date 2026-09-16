/**
 * 引用来源的召回分数口径（四处展示复用：聊天页「引用来源」面板、气泡下引用列表、
 * 「请求详情」的引用片段区块与完整提示词里的 `[引用 N]` 标注、反馈页回溯抽屉）。
 *
 * 分两个数展示，主次分明：
 * - **主**（列表排序依据，即 `score`）：rerank 生效时是 relevance_score，混合检索
 *   无 rerank 时是 RRF 融合分。列表就是按它降序排的，展示它才能让"数字递减"与
 *   "排位"一致。
 * - **辅**（`vector_score`）：原始 cos 相似度（0~1），与「相似度阈值」同量纲、
 *   横向可比，是排查"这块凭什么进候选"的判据——融合排名高而向量排名低 = 靠
 *   BM25 上位。BM25 单独命中无此分。
 *
 * 为什么不一律只展示 vector_score：会出现"相似度 0.697 排在 0.752 前面"的
 * 错乱观感——向量分与最终排位本就无关（排位归 rerank/RRF 管），用户会以为
 * 排序坏了。口径由后端 `score_type` 显式下发，前端不猜（score 量纲随配置变：
 * RRF 只有 0.001~0.033，径直标成"相关度"会显示 0.016 这种假低分）。
 *
 * 检索测试页（RetrievalTest）不适用：那里刻意同时展示两种分并标注了来由。
 */
import type { Source } from '../api/client';

/** 图谱引用判据：id 前缀 `kg:` 是后端约定（knowledge_graph_service），
 *  document_name 兜底历史数据 */
export const isKgSource = (s: Source): boolean =>
  !!s.id?.startsWith('kg:') || s.document_name === '知识图谱';

/** 召回分数标签：三种来源（向量命中 / BM25 单独命中 / 图谱）口径统一 */
export const scoreBadge = (s: Source): string => {
  if (isKgSource(s)) return '知识图谱';
  // 排序依据分（rerank 相关度 / RRF 融合分）作主、原始向量分作辅。
  // score_type 缺失（老会话快照）时退回原口径只显示向量分，不猜量纲。
  if (s.score_type === 'rerank' || s.score_type === 'rrf') {
    const label = s.score_type === 'rerank' ? '相关度' : '融合分';
    const main = `${label} ${s.score.toFixed(3)}`;
    // BM25 单独命中的块无向量分（但仍被 rerank/RRF 打了主分，照常展示）
    return s.vector_score == null
      ? `${main} · 关键词命中`
      : `${main} · 向量 ${s.vector_score.toFixed(3)}`;
  }
  if (s.vector_score == null) return '关键词命中';
  return `相似度 ${s.vector_score.toFixed(3)}`;
};
