"""B2: 文档列表状态筛选下沉后端 测试

背景：前端状态筛选（失败/解析中）叠加服务端分页只筛当前页（误导）。
修复：GET /documents 加 status 参数（uploaded/parsing/parsed/ingested/
failed/all，空或缺省=全部；parsed 历史中间态归入「待解析」uploaded），
服务端先过滤再分页：total 为过滤后数量。
"""
from __future__ import annotations

from backend.services.document.service import get_document_service
from conftest import create_kb, upload_and_ingest, upload_doc


class TestDocumentStatusFilter:

    def _setup(self, client, admin_headers, mock_embedding):
        """构造 7 个不同状态的文档：
        d1=uploaded / d2=parsed（历史中间态）/ d3=ingested / d4=failed /
        d5=parsing / d6=pending_confirm（Agentic 超限待确认）/
        d7=converting（ppt 转 PDF 中间态）"""
        kb = create_kb(client)
        d1 = upload_doc(client, kb["id"], filename="待解析.txt")
        d2 = upload_doc(client, kb["id"], filename="已解析.txt")
        d3 = upload_and_ingest(client, kb["id"], filename="已入库.txt")
        d4 = upload_doc(client, kb["id"], filename="失败.txt")
        d5 = upload_doc(client, kb["id"], filename="解析中.txt")
        d6 = upload_doc(client, kb["id"], filename="待确认.txt")
        svc = get_document_service()
        # 状态机合法迁移构造历史/异常状态
        svc.transition(d2["id"], "parsing")
        svc.transition(d2["id"], "parsed")
        svc.transition(d4["id"], "parsing")
        svc.transition(d4["id"], "failed")
        svc.transition(d5["id"], "parsing")
        svc.transition(d6["id"], "parsing")
        svc.transition(d6["id"], "pending_confirm",
                       error="文档约 1.2 万字，确认继续请重试")
        # converting 在 _TRANSITIONS 里只有出边没有入边，只能作为**初始状态**
        # 创建；不走上传接口——那条路会真起 Gotenberg 后台转换任务，测试环境
        # 无此服务，任务失败后状态会竞态变成 failed
        d7 = svc.create(kb["id"], "转换中.pptx", 1024, status="converting")
        return kb, {"uploaded": d1["id"], "parsed": d2["id"],
                    "ingested": d3["id"], "failed": d4["id"],
                    "parsing": d5["id"], "pending_confirm": d6["id"],
                    "converting": d7.id}

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
        # 「失败」筛选组含 pending_confirm 待确认（同为异常/挂起态）
        assert r["total"] == 2
        assert sorted(self._ids(r["items"])) == sorted(
            [ids["failed"], ids["pending_confirm"]])
        # 「解析中」组含 converting（ppt 转 PDF 中间态）——两者都是"系统在
        # 处理、用户等着"，且都不可触发解析（前端 busy 判定同款）
        r = self._list(client, kb["id"], admin_headers, "&status=parsing")
        assert r["total"] == 2
        assert sorted(self._ids(r["items"])) == sorted(
            [ids["parsing"], ids["converting"]])
        # pending_confirm 单独筛选（后端契约支持，前端筛选用 failed 组）
        r = self._list(client, kb["id"], admin_headers, "&status=pending_confirm")
        assert r["total"] == 1
        assert self._ids(r["items"]) == [ids["pending_confirm"]]
        # 待确认文档 error 提示随列表返回（前端状态列 tooltip 展示原因）
        assert "约 1.2 万字" in r["items"][0]["error"]

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

    def test_status_counts_cover_all_states(self, client, admin_headers,
                                            mock_embedding):
        """计数四分组之和 == total（不重不漏）——两个接口同款不变式

        回归（2026-10-08）：converting（ppt 转 PDF 中间态）在计数里不属于
        任何分组，「全部」比四个徽标之和多 1，且该状态**筛不出来**（只能在
        「全部」里肉眼翻）。现归入「解析中」组（与前端 busy 判定
        parsing||converting 一致）。

        断言用不变式而非具体数字：四分组恰是全部状态的划分，将来新增状态
        忘了归组时会立刻失败——这正是本次 bug 的形态。
        """
        kb, ids = self._setup(client, admin_headers, mock_embedding)
        # 部门内接口：单库，数字确定，可精确断言归组位置
        c = client.get(f"/api/kbs/{kb['id']}/documents/status-counts",
                       headers=admin_headers).json()
        assert c["parsing"] == 2, f"converting 应计入「解析中」组: {c}"
        assert c["unparsed"] + c["parsing"] + c["ingested"] + c["failed"] \
            == c["total"], f"计数不闭合: {c}"
        # 超管全局接口：跨库聚合，只锁闭合性
        a = client.get("/api/admin/documents/status-counts",
                       headers=admin_headers).json()
        assert a["unparsed"] + a["parsing"] + a["ingested"] + a["failed"] \
            == a["total"], f"超管计数不闭合: {a}"

    def test_filter_combines_with_pagination(self, client, admin_headers,
                                             mock_embedding):
        """分页叠加过滤：先过滤后分页，total 为过滤后数量"""
        kb, ids = self._setup(client, admin_headers, mock_embedding)
        r = client.get(
            f"/api/kbs/{kb['id']}/documents?status=uploaded&page=1&page_size=1",
            headers=admin_headers).json()
        assert r["total"] == 2          # 过滤后总数（不是全量 7）
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
        assert r["total"] == 7 and len(r["items"]) == 7
        r = self._list(client, kb["id"], admin_headers, "&status=all")
        assert r["total"] == 7 and len(r["items"]) == 7
        # 空字符串等价不传
        r = self._list(client, kb["id"], admin_headers, "&status=")
        assert r["total"] == 7

    def test_invalid_status_400(self, client, admin_headers, mock_embedding):
        """非法 status → 400（不落空返回）"""
        kb, _ = self._setup(client, admin_headers, mock_embedding)
        resp = client.get(f"/api/kbs/{kb['id']}/documents?status=deleting",
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "非法状态筛选" in resp.json()["detail"]
