"""B2: 文档列表状态筛选下沉后端 测试

背景：前端状态筛选（失败/解析中）叠加服务端分页只筛当前页（误导）。
修复：GET /documents 加 status 参数（uploaded/parsing/parsed/ingested/
failed/all，空或缺省=全部；parsed 历史中间态归入「待解析」uploaded），
服务端先过滤再分页：total 为过滤后数量。
"""
from __future__ import annotations

from backend.services.document_service import get_document_service
from conftest import create_kb, upload_and_ingest, upload_doc


class TestDocumentStatusFilter:

    def _setup(self, client, admin_headers, mock_embedding):
        """构造 5 个不同状态的文档：
        d1=uploaded / d2=parsed（历史中间态）/ d3=ingested / d4=failed / d5=parsing"""
        kb = create_kb(client)
        d1 = upload_doc(client, kb["id"], filename="待解析.txt")
        d2 = upload_doc(client, kb["id"], filename="已解析.txt")
        d3 = upload_and_ingest(client, kb["id"], filename="已入库.txt")
        d4 = upload_doc(client, kb["id"], filename="失败.txt")
        d5 = upload_doc(client, kb["id"], filename="解析中.txt")
        svc = get_document_service()
        # 状态机合法迁移构造历史/异常状态
        svc.transition(d2["id"], "parsing")
        svc.transition(d2["id"], "parsed")
        svc.transition(d4["id"], "parsing")
        svc.transition(d4["id"], "failed")
        svc.transition(d5["id"], "parsing")
        return kb, {"uploaded": d1["id"], "parsed": d2["id"],
                    "ingested": d3["id"], "failed": d4["id"],
                    "parsing": d5["id"]}

    def _ids(self, payload):
        return [d["id"] for d in payload]

    def _list(self, client, kb_id, headers, query=""):
        """带分页参数请求（返回 {total, items} 结构；不传分页返回裸数组）"""
        return client.get(
            f"/api/kbs/{kb_id}/documents?page=1&page_size=200{query}",
            headers=headers).json()

    def test_status_filter_returns_only_matching(self, client, admin_headers,
                                                 mock_embedding):
        """status 过滤只返回该状态文档（含分页响应结构）"""
        kb, ids = self._setup(client, admin_headers, mock_embedding)
        r = self._list(client, kb["id"], admin_headers, "&status=ingested")
        assert r["total"] == 1 and self._ids(r["items"]) == [ids["ingested"]]
        r = self._list(client, kb["id"], admin_headers, "&status=failed")
        assert r["total"] == 1 and self._ids(r["items"]) == [ids["failed"]]
        r = self._list(client, kb["id"], admin_headers, "&status=parsing")
        assert r["total"] == 1 and self._ids(r["items"]) == [ids["parsing"]]

    def test_status_uploaded_includes_parsed(self, client, admin_headers,
                                             mock_embedding):
        """status=uploaded 包含历史「已解析」中间态（前端「待解析」筛选语义）"""
        kb, ids = self._setup(client, admin_headers, mock_embedding)
        r = self._list(client, kb["id"], admin_headers, "&status=uploaded")
        assert r["total"] == 2
        assert sorted(self._ids(r["items"])) == sorted(
            [ids["uploaded"], ids["parsed"]])

    def test_status_unparsed_maps_two_states(self, client, admin_headers,
                                             mock_embedding):
        """status=unparsed（前端「未入库」value）映射 uploaded+parsed 两态"""
        kb, ids = self._setup(client, admin_headers, mock_embedding)
        r = self._list(client, kb["id"], admin_headers, "&status=unparsed")
        assert r["total"] == 2
        assert sorted(self._ids(r["items"])) == sorted(
            [ids["uploaded"], ids["parsed"]])

    def test_filter_combines_with_pagination(self, client, admin_headers,
                                             mock_embedding):
        """分页叠加过滤：先过滤后分页，total 为过滤后数量"""
        kb, ids = self._setup(client, admin_headers, mock_embedding)
        r = client.get(
            f"/api/kbs/{kb['id']}/documents?status=uploaded&page=1&page_size=1",
            headers=admin_headers).json()
        assert r["total"] == 2          # 过滤后总数（不是全量 5）
        assert r["page"] == 1 and r["page_size"] == 1
        assert len(r["items"]) == 1
        # 第 2 页取到剩余 1 条，无重叠
        r2 = client.get(
            f"/api/kbs/{kb['id']}/documents?status=uploaded&page=2&page_size=1",
            headers=admin_headers).json()
        assert len(r2["items"]) == 1
        assert not set(self._ids(r["items"])) & set(self._ids(r2["items"]))

    def test_no_status_returns_all(self, client, admin_headers, mock_embedding):
        """不传 status → 全部；status=all 等价"""
        kb, ids = self._setup(client, admin_headers, mock_embedding)
        r = self._list(client, kb["id"], admin_headers)
        assert r["total"] == 5 and len(r["items"]) == 5
        r = self._list(client, kb["id"], admin_headers, "&status=all")
        assert r["total"] == 5 and len(r["items"]) == 5
        # 空字符串等价不传
        r = self._list(client, kb["id"], admin_headers, "&status=")
        assert r["total"] == 5

    def test_invalid_status_400(self, client, admin_headers, mock_embedding):
        """非法 status → 400（不落空返回）"""
        kb, _ = self._setup(client, admin_headers, mock_embedding)
        resp = client.get(f"/api/kbs/{kb['id']}/documents?status=deleting",
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "非法状态筛选" in resp.json()["detail"]
