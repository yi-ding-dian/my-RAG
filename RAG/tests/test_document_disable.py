"""文档启用/禁用检索测试

禁用 = 不参与召回（向量的 doc_active 过滤 + BM25 索引失效 + 知识图谱路径过滤），
但文档保留：可正常查看、下载、重新解析。

两条最容易踩坏的不变量：
- **重新解析**：向量是先删后加的全量重建，不显式带 doc_active 会被默认值洗白
- **出回收站**：恢复接口若无条件置 doc_active=True，同样会洗掉"禁用"

另：回收站优先级高于启用——站内点"启用"只记状态，不出站不参与检索。
"""
from __future__ import annotations

from conftest import create_kb, upload_and_ingest, wait_for_status


def _doc(client, kb_id, doc_id, headers):
    r = client.get(f"/api/kbs/{kb_id}/documents/{doc_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _retrieve_doc_ids(client, kb_id, headers, query="Python 是什么语言？"):
    """检索命中的文档 ID 集合（判定"是否被召回"的唯一标准）"""
    r = client.post("/api/chat/retrieve",
                    json={"kb_id": kb_id, "query": query}, headers=headers)
    assert r.status_code == 200, r.text
    return {s["document_id"] for s in r.json()["sources"]}


class TestDocumentDisable:
    def test_disable_then_enable_affects_retrieval(self, client, admin_headers,
                                                    mock_embedding):
        """禁用后不再被召回，启用后恢复"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        kb_id, doc_id = kb["id"], doc["id"]
        assert doc_id in _retrieve_doc_ids(client, kb_id, admin_headers), "基线应能召回"

        r = client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/disable",
                        headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False
        assert doc_id not in _retrieve_doc_ids(client, kb_id, admin_headers)

        r = client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/enable",
                        headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True
        assert doc_id in _retrieve_doc_ids(client, kb_id, admin_headers)

    def test_disable_survives_reparse(self, client, admin_headers, mock_embedding):
        """禁用状态经"重新解析"仍在（向量全量重建最易把它洗白）"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        kb_id, doc_id = kb["id"], doc["id"]
        client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/disable",
                    headers=admin_headers)

        r = client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/ingest",
                        json={}, headers=admin_headers)
        assert r.status_code == 200, r.text
        wait_for_status(client, kb_id, doc_id, headers=admin_headers)

        assert _doc(client, kb_id, doc_id, admin_headers)["enabled"] is False
        assert doc_id not in _retrieve_doc_ids(client, kb_id, admin_headers), \
            "重新解析后仍应保持禁用"

    def test_disable_survives_trash_restore(self, client, admin_headers,
                                            mock_embedding):
        """禁用 → 回收站 → 恢复：仍是禁用（恢复不该把禁用一起洗掉）"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        kb_id, doc_id = kb["id"], doc["id"]
        client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/disable",
                    headers=admin_headers)
        assert client.delete(f"/api/kbs/{kb_id}/documents/{doc_id}",
                             headers=admin_headers).status_code == 200

        r = client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/restore",
                        headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False
        assert doc_id not in _retrieve_doc_ids(client, kb_id, admin_headers), \
            "禁用的文档出回收站后仍不该被召回"

    def test_enable_in_trash_waits_for_restore(self, client, admin_headers,
                                               mock_embedding):
        """回收站内点"启用"只记状态：不出回收站就不参与检索"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        kb_id, doc_id = kb["id"], doc["id"]
        assert client.delete(f"/api/kbs/{kb_id}/documents/{doc_id}",
                             headers=admin_headers).status_code == 200

        r = client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/enable",
                        headers=admin_headers)
        assert r.status_code == 200 and r.json()["enabled"] is True
        assert doc_id not in _retrieve_doc_ids(client, kb_id, admin_headers), \
            "回收站优先级更高：站内启用不得绕过回收站语义"

        assert client.post(f"/api/kbs/{kb_id}/documents/{doc_id}/restore",
                           headers=admin_headers).status_code == 200
        assert doc_id in _retrieve_doc_ids(client, kb_id, admin_headers)

    def test_disable_requires_manage(self, client, admin_headers, user_headers,
                                     mock_embedding):
        """普通用户无权禁用（can_manage_kb）"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        r = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/disable",
                        headers=user_headers)
        assert r.status_code in (403, 404), r.text


def test_filter_graph_by_active_docs():
    """图谱过滤：只保留引用了活跃文档的实体与关系（纯函数，不起图谱链路）

    这条兜住"禁用只禁了一半"——向量与 BM25 已排除，图谱若不筛，被禁文档的
    实体照样会以「知识图谱」引用冒进回答。
    """
    from backend.services.knowledge_graph_service import filter_graph_by_active_docs

    graph = {
        "entities": [
            {"id": "e1", "name": "只引 d1", "count": 1,
             "chunk_refs": [{"doc_id": "d1", "chunk_index": 0}]},
            {"id": "e2", "name": "只引 d2", "count": 1,
             "chunk_refs": [{"doc_id": "d2", "chunk_index": 0}]},
            {"id": "e3", "name": "d1d2 都引", "count": 2,
             "chunk_refs": [{"doc_id": "d1", "chunk_index": 1},
                            {"doc_id": "d2", "chunk_index": 1}]},
        ],
        "relations": [
            {"id": "r1", "source": "e1", "target": "e3", "weight": 1.0,
             "chunk_refs": [{"doc_id": "d1", "chunk_index": 0}]},
            {"id": "r2", "source": "e2", "target": "e3", "weight": 1.0,
             "chunk_refs": [{"doc_id": "d2", "chunk_index": 0}]},
        ],
    }
    out = filter_graph_by_active_docs(graph, {"d1"})  # d2 被禁用/删除

    assert [e["name"] for e in out["entities"]] == ["只引 d1", "d1d2 都引"], \
        "只引用非活跃文档的实体应被剔除"
    e3 = next(e for e in out["entities"] if e["id"] == "e3")
    assert e3["chunk_refs"] == [{"doc_id": "d1", "chunk_index": 1}]
    assert e3["count"] == 1, "混合引用时保留实体但剔掉非活跃来源"
    assert [r["id"] for r in out["relations"]] == ["r1"], \
        "引用全来自非活跃文档的关系应被剔除"
