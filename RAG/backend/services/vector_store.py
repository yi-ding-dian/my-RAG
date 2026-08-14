"""Chroma 向量库单例（嵌入式 PersistentClient）

- collection 名 kb_{kb_id}（多知识库隔离），hnsw:space=cosine
- metadata 只存 str/int/float/bool：{document_id, document_name, chunk_index}
- id = f"{doc_id}_{chunk_index}"
- 余弦距离转相似度: 1 - distance
- 检索文本存入 Chroma documents 字段（检索时一并返回，避免重新切块）
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

# 系统 libsqlite3 过旧（<3.35）时，用 pysqlite3-binary 自带的新版 sqlite 替换
# （chromadb 官方推荐的兼容方案；新系统/Docker slim 镜像无需此步）
try:
    import pysqlite3  # noqa: F401
    import sys
    sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
except ImportError:
    pass

import chromadb  # noqa: E402

from backend.config import CHROMA_DIR  # noqa: E402

logger = logging.getLogger(__name__)

# 命中结果: (id, text, metadata, similarity)
Hit = Tuple[str, str, Dict, float]


class VectorStore:

    def __init__(self):
        self._client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        # 已补齐 doc_active 键的 kb_id（历史数据兼容，见 _ensure_doc_active）
        self._doc_active_ensured: set = set()

    def _collection_name(self, kb_id: str) -> str:
        return f"kb_{kb_id}"

    def _get_collection(self, kb_id: str):
        name = self._collection_name(kb_id)
        try:
            return self._client.get_collection(name)
        except Exception:
            return self._client.get_or_create_collection(
                name=name,
                metadata={"hnsw:space": "cosine"},
            )

    def add(self, kb_id: str, doc_id: str, document_name: str,
            chunks: List[str], embeddings: List[List[float]],
            metadatas: List[Dict] | None = None):
        """入库：id=f"{doc_id}_{i}"，metadata 仅原始类型

        metadatas: 调用方传入的完整 metadata（每条含 document_id/document_name/
        chunk_index，parent_child 模式另含 char_start/char_end/parent_text/
        parent_chunk_index/retrieval_mode）；None 时构造默认三字段
        """
        if not chunks or not embeddings or len(chunks) != len(embeddings):
            raise ValueError("chunks 与 embeddings 长度不一致或为空")
        col = self._get_collection(kb_id)
        ids = [f"{doc_id}_{i}" for i in range(len(chunks))]
        if metadatas is None:
            metadatas = [
                {
                    "document_id": doc_id,
                    "document_name": document_name,
                    "chunk_index": i,
                }
                for i in range(len(chunks))
            ]
        elif len(metadatas) != len(chunks):
            raise ValueError("metadatas 与 chunks 长度不一致")
        # 软删除检索过滤标志：全部 chunk 显式带 doc_active（默认 True 活跃；
        # 重建向量走 add 时旧值保留——软删文档保持 False，历史无键补 True）
        metadatas = [{**m, "doc_active": m.get("doc_active", True)}
                     for m in metadatas]
        col.add(ids=ids, embeddings=embeddings, documents=chunks, metadatas=metadatas)
        logger.info("向量入库: kb=%s doc=%s chunks=%d", kb_id, doc_id, len(chunks))

    def _ensure_doc_active(self, kb_id: str):
        """历史数据兼容：给 collection 中缺 doc_active 键的 chunk 惰性补齐 True

        检索 where={'doc_active': True} 是简单等值过滤，只匹配显式有该键的记录；
        上线前入库的旧 chunk 无此键会被误排除。首次检索/全量拉取时补齐一次
        （幂等；失败也标记避免每次重试，与 BM25 失败态缓存思路一致）。
        """
        if kb_id in self._doc_active_ensured:
            return
        try:
            col = self._get_collection(kb_id)
            resp = col.get(include=["metadatas"])
            ids = resp.get("ids") or []
            metas = resp.get("metadatas") or []
            missing = [(ids[i], dict(metas[i] or {}))
                       for i in range(len(ids))
                       if "doc_active" not in (metas[i] or {})]
            if missing:
                col.update(ids=[m[0] for m in missing],
                           metadatas=[{**m[1], "doc_active": True}
                                      for m in missing])
                logger.info("向量 metadata 补齐 doc_active: kb=%s chunks=%d",
                            kb_id, len(missing))
        except Exception as e:
            logger.warning("doc_active 补齐失败: kb=%s err=%s",
                           kb_id, str(e)[:150])
        finally:
            self._doc_active_ensured.add(kb_id)

    def search(self, kb_id: str, query_embedding: List[float],
               top_k: int = 5, where: Optional[dict] = None) -> List[Hit]:
        """余弦相似度检索，返回按相似度降序的命中列表

        where: 可选 Chroma metadata 过滤（如 {"doc_active": True} 排除软删
        文档；默认 None=不过滤，向后兼容）
        """
        col = self._get_collection(kb_id)
        if col.count() == 0:
            return []
        self._ensure_doc_active(kb_id)
        try:
            kwargs: dict = {
                "query_embeddings": [query_embedding],
                "n_results": min(top_k, col.count()),
                "include": ["metadatas", "distances", "documents"],
            }
            if where is not None:
                kwargs["where"] = where
            resp = col.query(**kwargs)
        except Exception as e:
            logger.warning("向量检索失败: %s", e)
            return []
        ids = (resp.get("ids") or [[]])[0]
        docs = (resp.get("documents") or [[]])[0]
        metas = (resp.get("metadatas") or [[]])[0]
        dists = (resp.get("distances") or [[]])[0]
        hits: List[Hit] = []
        for i, cid in enumerate(ids):
            hits.append((cid, docs[i] or "", dict(metas[i] or {}), 1.0 - float(dists[i])))
        hits.sort(key=lambda h: h[3], reverse=True)
        return hits

    def delete_by_document(self, kb_id: str, doc_id: str):
        """删除某文档的全部向量（重新入库/删除文档时使用）"""
        try:
            col = self._client.get_collection(self._collection_name(kb_id))
        except Exception:
            return
        try:
            result = col.get(where={"document_id": doc_id}, include=[])
            ids = result.get("ids") or []
            if ids:
                col.delete(ids=ids)
                logger.info("向量删除: kb=%s doc=%s blocks=%d", kb_id, doc_id, len(ids))
        except Exception as e:
            logger.warning("向量删除失败: kb=%s doc=%s err=%s", kb_id, doc_id, e)

    def update_metadata(self, kb_id: str, doc_id: str, **meta) -> bool:
        """更新某文档全部 chunk 的 metadata（软删/恢复时打 doc_active 标志）

        Chroma update 整体替换 metadata，需先按 document_id 取回各 chunk 的
        完整旧 metadata 再合并写回（其余键保真）；返回 False 表示失败，
        调用方应中止软删/恢复以保持元数据与向量一致。
        """
        try:
            col = self._client.get_collection(self._collection_name(kb_id))
        except Exception:
            # collection 不存在 = 该知识库尚无向量（文档未入库），无操作视为成功
            return True
        try:
            result = col.get(where={"document_id": doc_id},
                             include=["metadatas"])
            ids = result.get("ids") or []
            metas = result.get("metadatas") or []
            if not ids:
                return True  # 无向量（未入库/向量已删）：无操作视为成功
            new_metas = []
            for m in metas:
                nm = dict(m or {})
                nm.update(meta)
                new_metas.append(nm)
            col.update(ids=ids, metadatas=new_metas)
            logger.info("向量 metadata 更新: kb=%s doc=%s blocks=%d keys=%s",
                        kb_id, doc_id, len(ids), list(meta))
            return True
        except Exception as e:
            logger.warning("向量 metadata 更新失败: kb=%s doc=%s err=%s",
                           kb_id, doc_id, str(e)[:150])
            return False

    def count(self, kb_id: str) -> int:
        try:
            col = self._client.get_collection(self._collection_name(kb_id))
            return col.count()
        except Exception:
            return 0

    def get_embedding_dimension(self, kb_id: str) -> Optional[int]:
        """collection 内任意一条向量的维度；collection 不存在/为空 → None

        维度冲突检测用（更换 embedding 模型后旧向量维度与新模型不符，
        写入/检索会报错——提前检测给出明确提示，而不是吞异常返回空）
        """
        try:
            col = self._client.get_collection(self._collection_name(kb_id))
        except Exception:
            return None
        if col.count() == 0:
            return None
        try:
            resp = col.get(limit=1, include=["embeddings"])
            embs = resp.get("embeddings")
            # 注意：Chroma 返回的 embeddings 是 numpy 二维数组，
            # 不能用 `or []` / `if embs` 做布尔判断（ndarray 多元素布尔报错），
            # 用 is None / len 判断
            if embs is None or len(embs) == 0:
                return None
            return len(embs[0])
        except Exception as e:
            logger.warning("向量维度检测失败: kb=%s err=%s", kb_id, e)
        return None

    def get_all(self, kb_id: str) -> List[Tuple[str, str, Dict]]:
        """拉取 collection 全部 (id, text, metadata)（BM25 索引构建/重建用）"""
        try:
            col = self._client.get_collection(self._collection_name(kb_id))
        except Exception:
            return []
        if col.count() == 0:
            return []
        self._ensure_doc_active(kb_id)
        try:
            resp = col.get(include=["documents", "metadatas"])
        except Exception as e:
            logger.warning("全量拉取失败: kb=%s err=%s", kb_id, e)
            return []
        ids = resp.get("ids") or []
        docs = resp.get("documents") or []
        metas = resp.get("metadatas") or []
        return [(ids[i], docs[i] or "", dict(metas[i] or {}))
                for i in range(len(ids))]

    def drop_collection(self, kb_id: str):
        """删除知识库时级联删除整个 collection"""
        try:
            self._client.delete_collection(self._collection_name(kb_id))
            logger.info("向量库删除: kb=%s", kb_id)
        except Exception as e:
            logger.warning("向量库删除失败: kb=%s err=%s", kb_id, e)


_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store
