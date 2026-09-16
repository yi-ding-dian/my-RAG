"""向量存储后端（可插拔抽象,当前实现 chroma,新增 milvus 服务化)

  VectorStore 内部委托给 VectorBackend 后端实现,按配置
  （配置文件 VECTOR_BACKEND=chroma|milvus,MILVUS_URI 为 milvus 地址）选择。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
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

from backend.config import CHROMA_DIR, settings  # noqa: E402

logger = logging.getLogger(__name__)

# 命中结果: (id, text, metadata, similarity)
Hit = Tuple[str, str, Dict, float]

# 全文检索字段配置（Milvus 侧）：
# - text 必须声明为显式字段才能挂分词器——analyzer 是字段级属性，动态字段配不了；
# - 中文必须显式指定 chinese 分词器：默认 standard 分词器会把整句当成一个 token
#   （实测「配置工具怎么创建工程」零命中），而且配错时**向量检索照常工作、只有
#   BM25 静默失效**，非常难发现；
# - max_length 用 Milvus VARCHAR 上限，单个切块远低于此（层级聚合的父块也只有几千字）。
TEXT_MAX_LENGTH = 65535
TEXT_ANALYZER_PARAMS = {"type": "chinese"}

# 显式 schema 的字段名：建表、检索、迁移脚本共用这一份，避免各处硬编码漂移
# （Milvus 的 search/query 都要按名字指定字段，写错了不会报"字段不存在"而是
#   报别的错，很难查）
VECTOR_FIELD = "vector"
TEXT_FIELD = "text"
SPARSE_FIELD = "sparse_vector"


class VectorBackend(ABC):
    """向量存储后端抽象

    接入新后端时逐条实现下面的契约，并在 tests/test_vector_backend.py 补一组
    同参数的用例——**契约靠测试固化，不靠口头约定**。

    统一契约：
    - 集合命名 kb_{kb_id}，余弦度量；
    - **过滤语义**：where 是等值字典，**严格等值匹配**（各后端不得自行放宽）。
      历史数据可能缺 doc_active 键，由**各后端在查询前补齐**（Chroma 见
      _ensure_doc_active；Milvus 侧无缺键历史数据，写入与迁移脚本都保证带键），
      而不是把过滤放宽成"缺键也算匹配"——后者会让软删过滤被绕过，回收站里的
      文档仍会被召回。写入侧另有保险：add 落库必带 doc_active；
    - 防御性行为：集合不存在/连接失败时 search 返回空、search_full_text 返回空、
      count 返回 0、delete_by_document 静默成功、update_metadata 返回 True——
      不让上游因为"库还没建好"而报错。
    """

    # 是否支持服务端全文检索（BM25）。False 时 search_full_text 一律返回空，
    # 调用方据此改用应用层索引或退化为纯向量（见 retrieval_service._bm25_candidates）。
    supports_full_text: bool = False

    @abstractmethod
    def ensure_collection(self, kb_id: str, dim: int) -> None:
        """确保集合存在且可用（写入前置）

        dim 为向量维度。后端可惰性建表（Chroma：首次写入时按需创建）或显式建表
        （Milvus：带全文检索字段，维度必须已知）。一次性工具（数据迁移脚本等）
        也走这个入口，不要直接碰后端的私有建表逻辑。
        """

    @abstractmethod
    def add(self, kb_id: str, doc_id: str, document_name: str,
            chunks: List[str], embeddings: List[List[float]],
            metadatas: List[Dict] | None = None) -> None:
        """向量入库（整文档批量）"""

    @abstractmethod
    def search(self, kb_id: str, query_embedding: List[float],
               top_k: int = 5, where: Optional[dict] = None) -> List[Hit]:
        """相似度检索，返回按相似度降序的命中列表"""

    def search_full_text(self, kb_id: str, query: str,
                         top_k: int = 5,
                         where: Optional[dict] = None) -> List[Hit]:
        """全文检索（BM25），入参为原始查询文本、返回格式与 search 一致。

        默认返回空列表 = 该后端不支持全文检索（supports_full_text=False），
        调用方据此改用应用层索引或退化为纯向量检索。
        """
        return []

    @abstractmethod
    def delete_by_document(self, kb_id: str, doc_id: str) -> None:
        """删除某文档的全部向量（重新入库/彻底删除时使用）"""

    @abstractmethod
    def update_metadata(self, kb_id: str, doc_id: str, **meta) -> bool:
        """更新某文档全部 chunk 的 metadata（软删/恢复时打 doc_active）"""

    @abstractmethod
    def count(self, kb_id: str) -> int:
        """collection 内向量总数"""

    @abstractmethod
    def get_embedding_dimension(self, kb_id: str) -> Optional[int]:
        """任意一条向量的维度；collection 不存在/为空 → None"""

    @abstractmethod
    def get_all(self, kb_id: str) -> List[Tuple[str, str, Dict]]:
        """拉取全部 (id, text, metadata)（数据导出/迁移用）"""

    @abstractmethod
    def drop_collection(self, kb_id: str) -> None:
        """删除知识库时级联删除整个集合"""


class ChromaVectorBackend(VectorBackend):
    """Chroma 嵌入式实现（PersistentClient 单实例，collection 名 kb_{kb_id}）"""

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

    def ensure_collection(self, kb_id: str, dim: int) -> None:
        """Chroma 为惰性建表：这里只触发一次 get_or_create

        dim 用不上——Chroma 的维度由首次写入的向量决定（也正因如此，换 embedding
        模型后维度冲突要靠 dim_check 的重建流程兜底）。
        """
        self._get_collection(kb_id)

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

        where: 可选 metadata 等值过滤（如 {"doc_active": True} 排除软删
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


def normalize_milvus_uri(uri: str) -> str:
    """MilvusClient 要求 URI 带 scheme(如 http://);用户可能只填 host:port
    ——缺 scheme 时补默认 http://(Milvus 默认监听 19530 gRPC,走 http 协议)"""
    if uri and not uri.startswith(("http://", "https://", "unix://", "tcp://")):
        return f"http://{uri}"
    return uri


class MilvusVectorBackend(VectorBackend):
    """Milvus 服务化实现（pymilvus 3.x MilvusClient 现代 API）

    与 ChromaVectorBackend 行为对齐（同一种"库=集合"契约）：
    - collection 名 kb_{kb_id}，COSINE 度量；注意 Milvus 3.x 的 search 返回里
      distance 字段**就是余弦相似度**（不再是 2.x 时代的"距离"，详见 search）；
    - schema（见 ensure_collection）：id/vector/text/sparse_vector 为显式字段，
      其中 text 挂中文分词器、sparse_vector 由 BM25 Function 自动生成；其余
      metadata（document_id/chunk_index/char_start/parent_text/doc_active…）
      走动态字段，查询 output_fields=['*'] 取回；
    - where 等值 dict 转 Milvus filter expr（如 doc_active == true；**字段缺失
      即不匹配**，与 Chroma 契约一致）；
    - 集合不存在/连接失败：search 空、search_full_text 空、count 0、delete 静默、
      update_metadata True（与 Chroma 防御性契约一致）。
    """

    # 全文检索由服务端 BM25 提供（中文分词 + 倒排索引随写入增量维护）
    supports_full_text = True

    def __init__(self, uri: str = ""):
        from pymilvus import MilvusClient
        self._uri = normalize_milvus_uri(uri or str(settings.MILVUS_URI))
        self._client = MilvusClient(uri=self._uri)

    def _collection_name(self, kb_id: str) -> str:
        return f"kb_{kb_id}"

    def ensure_collection(self, kb_id: str, dim: int) -> None:
        """建集合（显式 schema：向量字段 + 全文检索字段）

        字段布局：
        - id / vector：显式字段（与旧版一致）
        - text：显式字段并挂中文分词器，BM25 Function 据此自动生成稀疏向量
        - sparse_vector：稀疏向量，**写入时无需提供**，由 Function 从 text 生成
        - 其余 metadata（document_id/chunk_index/char_start/parent_text/doc_active…）
          继续走动态字段，写入侧代码不用动

        集合已存在时不再重建；若它是旧版 schema（text 在动态字段里、无稀疏向量），
        记一条警告提示迁移——那种集合上全文检索只能退化为纯向量（见 search_full_text）。
        """
        from pymilvus import DataType, Function, FunctionType
        name = self._collection_name(kb_id)
        if self._client.has_collection(name):
            fields = {f["name"] for f in
                      self._client.describe_collection(name).get("fields", [])}
            if SPARSE_FIELD not in fields:
                logger.warning(
                    "知识库 %s 为旧版集合（无全文检索字段），BM25 检索将退化为纯向量；"
                    "执行 scripts/migrate_milvus_full_text.py 可迁移", kb_id)
            return
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field(VECTOR_FIELD, DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field(TEXT_FIELD, DataType.VARCHAR, max_length=TEXT_MAX_LENGTH,
                         enable_analyzer=True,
                         analyzer_params=TEXT_ANALYZER_PARAMS)
        schema.add_field(SPARSE_FIELD, DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(name="bm25", function_type=FunctionType.BM25,
                                     input_field_names=[TEXT_FIELD],
                                     output_field_names=[SPARSE_FIELD]))
        index_params = self._client.prepare_index_params()
        index_params.add_index(field_name=VECTOR_FIELD, index_type="AUTOINDEX",
                               metric_type="COSINE")
        index_params.add_index(field_name=SPARSE_FIELD,
                               index_type="SPARSE_INVERTED_INDEX",
                               metric_type="BM25")
        self._client.create_collection(name, schema=schema,
                                       index_params=index_params)

    @staticmethod
    def _expr_where(where: Optional[dict]) -> str:
        """where 等值 dict → Milvus filter 表达式（布尔/int/float/str）"""
        if not where:
            return ""
        parts = []
        for k, v in where.items():
            if isinstance(v, bool):
                parts.append(f'{k} == {"true" if v else "false"}')
            elif isinstance(v, (int, float)):
                parts.append(f"{k} == {v}")
            else:
                s = str(v).replace('"', '\\"')
                parts.append(f'{k} == "{s}"')
        return " and ".join(parts)

    def add(self, kb_id: str, doc_id: str, document_name: str,
            chunks: List[str], embeddings: List[List[float]],
            metadatas: List[Dict] | None = None) -> None:
        if not chunks or not embeddings or len(chunks) != len(embeddings):
            raise ValueError("chunks 与 embeddings 长度不一致或为空")
        dim = len(embeddings[0])
        name = self._collection_name(kb_id)
        try:
            self.ensure_collection(kb_id, dim)
        except Exception as e:
            raise RuntimeError(f"Milvus 集合创建失败: {e}") from e
        if metadatas is None:
            metadatas = [
                {"document_id": doc_id, "document_name": document_name,
                 "chunk_index": i} for i in range(len(chunks))
            ]
        elif len(metadatas) != len(chunks):
            raise ValueError("metadatas 与 chunks 长度不一致")
        rows = []
        for i, (text, emb, m) in enumerate(zip(chunks, embeddings, metadatas)):
            rows.append({
                "id": f"{doc_id}_{i}",
                VECTOR_FIELD: emb,
                TEXT_FIELD: text,
                **m,
                "doc_active": m.get("doc_active", True),
            })
        try:
            self._client.upsert(collection_name=name, data=rows)
        except Exception as e:
            logger.warning("Milvus 向量入库失败: kb=%s doc=%s err=%s",
                           kb_id, doc_id, str(e)[:150])
            raise
        logger.info("Milvus 向量入库: kb=%s doc=%s chunks=%d",
                    kb_id, doc_id, len(chunks))

    def search(self, kb_id: str, query_embedding: List[float],
               top_k: int = 5, where: Optional[dict] = None) -> List[Hit]:
        name = self._collection_name(kb_id)
        try:
            if not self._client.has_collection(name):
                return []
            res = self._client.search(
                collection_name=name,
                data=[query_embedding],
                anns_field=VECTOR_FIELD,
                limit=max(1, top_k),
                filter=self._expr_where(where) or None,
                metric_type="COSINE",
                output_fields=["*"],
            )
        except Exception as e:
            logger.warning("Milvus 向量检索失败: kb=%s err=%s",
                           kb_id, str(e)[:150])
            return []
        hits: List[Hit] = []
        for row in (res[0] if res else []):
            ent = row.get("entity") or {}
            meta = {k: v for k, v in ent.items()
                    if k not in ("id", TEXT_FIELD, VECTOR_FIELD)}
            # Milvus 3.x：search 返回的 distance 字段**就是余弦相似度**（实测与
            # 手算逐位相同），不再是 2.x 时代的"距离"。若沿用 `1 - distance`，
            # 相似度会被整体反过来——最相关的排最后、最不相关的排最前，语义
            # 检索全面失效（表现为"答案就在库里却永远召不回"）。
            hits.append((row.get("id") or "", ent.get(TEXT_FIELD) or "", meta,
                         float(row.get("distance", 0.0))))
        hits.sort(key=lambda h: h[3], reverse=True)
        return hits

    def search_full_text(self, kb_id: str, query: str, top_k: int = 5,
                         where: Optional[dict] = None) -> List[Hit]:
        """BM25 全文检索（服务端分词 + 打分），入参是**原始查询文本**

        与 search 的差别：分词完全交给 Milvus 的 chinese analyzer（应用层不再
        持有分词器与全量索引），命中分数是 BM25 分而非余弦相似度。返回格式与
        search 一致，调用方可直接做 RRF 融合。
        集合缺失/为旧版 schema/连接异常一律返回空列表 → 调用方退化为纯向量。
        """
        name = self._collection_name(kb_id)
        try:
            if not self._client.has_collection(name):
                return []
            res = self._client.search(
                collection_name=name,
                data=[query],
                anns_field=SPARSE_FIELD,
                limit=max(1, top_k),
                filter=self._expr_where(where) or None,
                output_fields=["*"],
            )
        except Exception as e:
            logger.warning("Milvus 全文检索失败（本次退化为纯向量）: kb=%s err=%s",
                           kb_id, str(e)[:150])
            return []
        hits: List[Hit] = []
        for row in (res[0] if res else []):
            ent = row.get("entity") or {}
            meta = {k: v for k, v in ent.items()
                    if k not in ("id", TEXT_FIELD, VECTOR_FIELD, SPARSE_FIELD)}
            hits.append((row.get("id") or "", ent.get(TEXT_FIELD) or "", meta,
                         float(row.get("distance", 0.0))))
        hits.sort(key=lambda h: h[3], reverse=True)
        return hits

    def delete_by_document(self, kb_id: str, doc_id: str) -> None:
        name = self._collection_name(kb_id)
        try:
            if not self._client.has_collection(name):
                return
            self._client.delete(collection_name=name,
                                filter=f'document_id == "{doc_id}"')
            logger.info("Milvus 向量删除: kb=%s doc=%s", kb_id, doc_id)
        except Exception as e:
            logger.warning("Milvus 向量删除失败: kb=%s doc=%s err=%s",
                           kb_id, doc_id, str(e)[:150])

    def update_metadata(self, kb_id: str, doc_id: str, **meta) -> bool:
        """局部更新（partial_update：只提交主键 + 变更字段）

        不能走"整行取回再 upsert"—— query 不返回 vector 字段，而 upsert 是
        整行替换语义，缺 vector 会报 DataNotMatchException（Insert missed an
        field `vector`）。partial_update 由服务端按主键合并，只需变更字段，
        也省掉把大向量读回来再写回去的开销。
        """
        name = self._collection_name(kb_id)
        try:
            if not self._client.has_collection(name):
                return True
            # 只取主键：改写项全在 meta 里，其余字段由服务端保证不动
            rows = self._client.query(
                collection_name=name,
                filter=f'document_id == "{doc_id}"',
                output_fields=["id"],
                limit=16384,
            )
            if not rows:
                return True  # 无向量：无操作视为成功
            new_rows = [{"id": r["id"], **meta} for r in rows]
            self._client.upsert(collection_name=name, data=new_rows,
                                partial_update=True)
            logger.info("Milvus 向量 metadata 更新: kb=%s doc=%s blocks=%d",
                        kb_id, doc_id, len(new_rows))
            return True
        except Exception as e:
            logger.warning("Milvus 向量 metadata 更新失败: kb=%s doc=%s err=%s",
                           kb_id, doc_id, str(e)[:150])
            return False

    def count(self, kb_id: str) -> int:
        name = self._collection_name(kb_id)
        try:
            if not self._client.has_collection(name):
                return 0
            res = self._client.query(collection_name=name,
                                     filter="",
                                     output_fields=["count(*)"])
            return int(res[0]["count(*)"]) if res else 0
        except Exception:
            return 0

    def get_embedding_dimension(self, kb_id: str) -> Optional[int]:
        try:
            name = self._collection_name(kb_id)
            if not self._client.has_collection(name):
                return None
            desc = self._client.describe_collection(name)
            for f in desc.get("fields", []):
                params = f.get("params") or {}
                if params.get("dim"):
                    return int(params["dim"])
            return None
        except Exception as e:
            logger.warning("Milvus 维度检测失败: kb=%s err=%s", kb_id, e)
            return None

    def get_all(self, kb_id: str) -> List[Tuple[str, str, Dict]]:
        name = self._collection_name(kb_id)
        try:
            if not self._client.has_collection(name):
                return []
            out: List[Tuple[str, str, Dict]] = []
            offset = 0
            limit = 4096  # Milvus query 单次上限内的安全窗
            while True:
                rows = self._client.query(
                    collection_name=name, filter="",
                    output_fields=["*"], limit=limit, offset=offset)
                if not rows:
                    break
                for r in rows:
                    tid = r.get("id", "")
                    out.append((
                        tid,
                        r.get(TEXT_FIELD) or "",
                        {k: v for k, v in r.items()
                         if k not in ("id", TEXT_FIELD, VECTOR_FIELD)},
                    ))
                offset += len(rows)
                if len(rows) < limit:
                    break
            return out
        except Exception as e:
            logger.warning("Milvus 全量拉取失败: kb=%s err=%s", kb_id,
                           str(e)[:150])
            return []

    def drop_collection(self, kb_id: str) -> None:
        try:
            self._client.drop_collection(self._collection_name(kb_id))
            logger.info("Milvus 向量库删除: kb=%s", kb_id)
        except Exception as e:
            logger.warning("Milvus 向量库删除失败: kb=%s err=%s", kb_id, e)


class VectorStore:
    """向量存储门面：按配置选择后端，方法与 VectorBackend 契约一致。

    保留类名 VectorStore 兼容既有 import/get_vector_store() 调用，
    实现委托给 _backend；新增后端时无需改动上层调用方。
    """

    def __init__(self, backend: Optional[VectorBackend] = None,
                 backend_name: str = "", backend_uri: str = ""):
        self._backend = backend or _create_backend(
            settings.VECTOR_BACKEND, settings.MILVUS_URI)
        self._backend_name = backend_name or settings.VECTOR_BACKEND
        self._backend_uri = backend_uri or settings.MILVUS_URI

    @property
    def sync(self) -> VectorBackend:
        """同步视图：直接调用后端同步实现（仅供 asyncio.to_thread 场景，
        如 _purge_local 这类"纯同步部分放线程池"的调用，避免套娃）"""
        return self._backend

    @property
    def supports_full_text(self) -> bool:
        """当前后端是否支持服务端全文检索（同步属性，不必 await）

        调用方据此决定 BM25 候选从哪来：支持则用 search_full_text（服务端分词
        与倒排索引），不支持则改用应用层索引。这是**能力声明**，与
        search_full_text 返回空列表（可能只是这次没命中）语义不同。
        """
        return self._backend.supports_full_text

    # 后端实现均为同步（Chroma 嵌入式 / PyMilvus SDK），统一移到线程执行，
    # 避免阻塞事件循环（多 worker 前置修复：检索/入库不再占用主循环）
    async def ensure_collection(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.ensure_collection, *args, **kwargs)

    async def add(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.add, *args, **kwargs)

    async def search(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.search, *args, **kwargs)

    async def search_full_text(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.search_full_text, *args, **kwargs)

    async def delete_by_document(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.delete_by_document, *args, **kwargs)

    async def update_metadata(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.update_metadata, *args, **kwargs)

    async def count(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.count, *args, **kwargs)

    async def get_embedding_dimension(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.get_embedding_dimension, *args, **kwargs)

    async def get_all(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.get_all, *args, **kwargs)

    async def drop_collection(self, *args, **kwargs):
        return await asyncio.to_thread(self._backend.drop_collection, *args, **kwargs)


def _create_backend(name: str, uri: str = "") -> VectorBackend:
    """按配置创建后端实例（接入新后端在此注册）"""
    if name == "chroma":
        return ChromaVectorBackend()
    if name == "milvus":
        return MilvusVectorBackend(uri=uri or settings.MILVUS_URI)
    raise ValueError(
        f"未知向量后端: {name!r}（支持: chroma / milvus）")


_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """按当前配置档案 vector_store 段惰性创建 / 热切换后端

    配置档案切换 backend（前端保存后即时生效语义）后，首次调用本函数时
    按新后端重建门面（MilvusClient 实例复用连接开销低）；已持有旧实例的
    调用方继续用旧后端直到下一次获取(下一请求生效)。线程安全：uvicorn
    单进程模型下赋值原子;多进程场景各进程独立,随重启收敛。
    """
    global _vector_store
    from backend.config import get_active_config
    try:
        cfg = get_active_config().vector_store
        backend_name = cfg.backend
        uri = cfg.milvus_uri or settings.MILVUS_URI
    except Exception:
        # 档案不可用（初始化早期）→ env 兜底（与历史行为一致）
        backend_name, uri = settings.VECTOR_BACKEND, settings.MILVUS_URI
    if _vector_store is not None and (
            _vector_store._backend_name == backend_name
            and _vector_store._backend_uri == uri):
        return _vector_store
    _vector_store = VectorStore(
        backend=_create_backend(backend_name, uri),
        backend_name=backend_name, backend_uri=uri)
    return _vector_store
