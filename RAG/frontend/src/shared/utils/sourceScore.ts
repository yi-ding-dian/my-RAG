/**
 * 引用来源的召回分数口径（四处展示复用：聊天页「引用来源」面板、气泡下引用列表、
 * 「请求详情」的引用片段区块与完整提示词里的 `[引用 N]` 标注、反馈页回溯抽屉）。
 *
 * 统一用 **vector_score** 而非 score：score 在混合检索下是 RRF 融合分
 * （1/(60+rank+1)，0.001~0.033 量级——按"相似度"展示会得到 0.006 这种
 * 看着像坏掉的假低分），rerank 生效时又变成 relevance_score，量纲随配置变；
 * vector_score 是原始 cos 相似度（0~1），与「相似度阈值」同量纲、横向可比，
 * 也是排查"这块凭什么进候选"的判据——融合排名高而向量排名低 = 靠 BM25 上位。
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
  if (s.vector_score == null) return '关键词命中';
  return `相似度 ${s.vector_score.toFixed(3)}`;
};
