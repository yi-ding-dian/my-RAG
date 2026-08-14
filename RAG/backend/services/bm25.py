"""BM25 全文检索索引（纯 Python 实现，jieba 中文分词，无外部服务/数据库）

设计要点：
- tokenize：jieba 分词（中文按词、英文小写整词），过滤单字符/纯标点/基础停用词
- 打分：BM25Okapi 风格（k1=1.5, b=0.75），IDF 平滑 ln(1 + (N-df+0.5)/(df+0.5))
- 本模块只做纯算法：给定文本列表构造索引，search(query_tokens, top_k) 返回
  (doc_idx, score) 降序；索引按 kb 惰性构建与缓存见 retrieval_service
  （collection 计数变化即重建 + ingestion 成功后 invalidate）
- 复杂度：倒排 postings 只遍历含命中词的文档（O(term × df)），
  企业单库几千 chunk 全量打分无压力；上万 chunk 时建议分批/提示用户
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Dict, List, Tuple

import jieba

logger = logging.getLogger(__name__)

# 基础停用词表（中英文高频虚词/无意义字词，按需扩充）
_STOP_WORDS = frozenset({
    # 中文
    "的", "了", "和", "是", "在", "有", "我", "你", "他", "她", "它", "这", "那",
    "就", "都", "而", "及", "与", "着", "或", "又", "也", "还", "为", "对", "于",
    "从", "把", "被", "让", "向", "以", "之", "其", "此", "一个", "一种", "我们",
    "你们", "他们", "没有", "什么", "怎么", "为什么", "如何", "以及", "或者",
    "因为", "所以", "如果", "但是", "然后", "虽然", "并且", "因此", "可以", "需要",
    # 英文
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "being", "this", "that", "it",
    "as", "at", "by", "from", "not", "but", "do", "does", "did", "have",
    "has", "had", "will", "would", "can", "could", "should", "may", "might",
    "about", "into", "over", "after", "before", "up", "down", "out", "off",
    "than", "then", "so", "also", "if", "else", "what", "which", "who", "when",
    "where", "how", "why", "just", "very", "more", "most", "some", "any",
})


def tokenize(text: str) -> List[str]:
    """中文 + 英文分词：jieba 切词，英文小写，过滤单字符/纯标点/停用词"""
    tokens: List[str] = []
    for tok in jieba.cut(text or ""):
        t = tok.strip().lower()
        if not t or t in _STOP_WORDS:
            continue
        # 过滤纯标点/空白/控制字符（如 "。"、","、" "、"1."）
        if not any(ch.isalnum() for ch in t):
            continue
        # 过滤单字符（中文单字/英文单字母，均为高频噪音）
        if len(t) < 2:
            continue
        tokens.append(t)
    return tokens


class BM25Index:
    """BM25 索引（纯算法）：构造一次可多次打分，线程不安全但检索场景单线程"""

    K1 = 1.5   # 词频饱和参数
    B = 0.75   # 文档长度归一化参数

    def __init__(self, texts: List[str]):
        if not texts:
            raise ValueError("BM25 索引不能为空文本列表")
        self._tokenized: List[List[str]] = [tokenize(t) for t in texts]
        self._doc_lens: List[int] = [len(d) for d in self._tokenized]
        self._avgdl = (sum(self._doc_lens) / len(self._doc_lens)
                       if self._doc_lens else 0.0)
        # 文档频率（每篇文档内词去重计数）与倒排 postings
        df: Counter[str] = Counter()
        postings: Dict[str, List[int]] = {}
        for i, doc in enumerate(self._tokenized):
            for t in set(doc):
                df[t] += 1
                postings.setdefault(t, []).append(i)
        self._df: Dict[str, int] = dict(df)
        self._postings: Dict[str, List[int]] = postings
        # IDF 平滑（rank_bm25 风格，避免低频词权重爆炸）
        n = len(texts)
        self._idf: Dict[str, float] = {
            t: math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
            for t in self._df
        }

    @property
    def size(self) -> int:
        """文档数（与构造时文本列表等长）"""
        return len(self._tokenized)

    def scores(self, query_tokens: List[str]) -> List[float]:
        """按 query 词累加 BM25 得分，返回与构造时文本顺序一致的分数列表"""
        n = len(self._tokenized)
        if n == 0 or not query_tokens:
            return [0.0] * n
        scores = [0.0] * n
        for term in set(query_tokens):
            idf = self._idf.get(term)
            if idf is None:
                continue  # 词典外的词（停用/罕见）无贡献
            for doc_idx in self._postings[term]:
                doc = self._tokenized[doc_idx]
                tf = doc.count(term)
                dl = self._doc_lens[doc_idx]
                denom = tf + self.K1 * (1 - self.B + self.B * dl / self._avgdl)
                scores[doc_idx] += idf * tf * (self.K1 + 1) / denom
        return scores

    def search(self, query_tokens: List[str],
               top_k: int = 5) -> List[Tuple[int, float]]:
        """BM25 检索，返回 (doc_idx, score) 按分数降序，最多 top_k 条（0 分不返回）"""
        scores = self.scores(query_tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out: List[Tuple[int, float]] = []
        for i in ranked:
            if scores[i] <= 0:
                break  # 降序排列，后续均为 0
            out.append((i, scores[i]))
            if len(out) >= top_k:
                break
        return out
