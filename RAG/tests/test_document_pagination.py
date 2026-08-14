"""P2-10 文档列表分页测试

问题：GET /api/kbs/{kb_id}/documents 全量返回，文档多时性能差。
修复：支持可选 page/page_size（page>=1，page_size>0 且上限 200）→ 返回
{total, page, page_size, items}；不传或 page_size=0 → 返回全量数组（旧调用
兼容）。回收站列表同样分页可选。
"""
from __future__ import annotations

from conftest import create_kb, upload_doc


class TestDocumentPagination:

    def _setup(self, client, admin_headers, count=5):
        kb = create_kb(client)
        docs = [upload_doc(client, kb["id"], filename=f"文档{i}.txt",
                           content=f"第{i}个文档内容")
                for i in range(count)]
        return kb, docs

    def test_no_params_returns_full_array(self, client, admin_headers):
        """不传分页参数 → 返回全量数组（旧调用/现有测试兼容）"""
        kb, docs = self._setup(client, admin_headers)
        resp = client.get(f"/api/kbs/{kb['id']}/documents",
                          headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == len(docs)

    def test_pagination_structure_and_bounds(self, client, admin_headers):
        """传参 → {total, page, page_size, items}；页码边界正确"""
        kb, docs = self._setup(client, admin_headers)
        # 第 1 页 2 条
        r1 = client.get(f"/api/kbs/{kb['id']}/documents?page=1&page_size=2",
                        headers=admin_headers).json()
        assert r1["total"] == 5 and r1["page"] == 1 and r1["page_size"] == 2
        assert len(r1["items"]) == 2
        # 第 3 页 2 条（5 = 2+2+1）
        r3 = client.get(f"/api/kbs/{kb['id']}/documents?page=3&page_size=2",
                        headers=admin_headers).json()
        assert len(r3["items"]) == 1
        # 越界页 → 空 items，total 仍正确
        r99 = client.get(f"/api/kbs/{kb['id']}/documents?page=99&page_size=2",
                         headers=admin_headers).json()
        assert r99["total"] == 5 and r99["items"] == []
        # page<1 → 按第 1 页处理
        r0 = client.get(f"/api/kbs/{kb['id']}/documents?page=0&page_size=2",
                        headers=admin_headers).json()
        assert r0["page_size"] == 2 and len(r0["items"]) == 2

    def test_page_size_cap_and_zero(self, client, admin_headers):
        """page_size 上限 200；page_size=0 → 全量数组"""
        kb, docs = self._setup(client, admin_headers)
        # 超上限被截断为 200
        r = client.get(f"/api/kbs/{kb['id']}/documents?page=1&page_size=1000",
                       headers=admin_headers).json()
        assert r["page_size"] == 200 and len(r["items"]) == 5
        # page_size=0 → 全量数组（与省略等价）
        r0 = client.get(f"/api/kbs/{kb['id']}/documents?page=1&page_size=0",
                        headers=admin_headers).json()
        assert isinstance(r0, list) and len(r0) == 5

    def test_pages_do_not_overlap(self, client, admin_headers):
        """相邻页无重叠无遗漏（倒序按创建时间）"""
        kb, docs = self._setup(client, admin_headers)
        page1 = client.get(f"/api/kbs/{kb['id']}/documents?page=1&page_size=2",
                           headers=admin_headers).json()["items"]
        page2 = client.get(f"/api/kbs/{kb['id']}/documents?page=2&page_size=2",
                           headers=admin_headers).json()["items"]
        ids1 = {d["id"] for d in page1}
        ids2 = {d["id"] for d in page2}
        assert not ids1 & ids2, "分页不应重叠"
        assert len(ids1 | ids2) == 4

    def test_trash_pagination(self, client, admin_headers):
        """回收站列表同样支持分页（不传参仍全量）"""
        kb, docs = self._setup(client, admin_headers)
        for doc in docs[:3]:
            client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                          headers=admin_headers)
        # 不传参 → 全量数组
        all_trash = client.get(f"/api/kbs/{kb['id']}/documents/trash",
                               headers=admin_headers).json()
        assert isinstance(all_trash, list) and len(all_trash) == 3
        # 分页
        r = client.get(f"/api/kbs/{kb['id']}/documents/trash?page=1&page_size=1",
                       headers=admin_headers).json()
        assert r["total"] == 3 and len(r["items"]) == 1

    def test_empty_kb_pagination(self, client, admin_headers):
        """空知识库分页：total=0、items 空，不报错"""
        kb = create_kb(client)
        r = client.get(f"/api/kbs/{kb['id']}/documents?page=1&page_size=10",
                       headers=admin_headers).json()
        assert r["total"] == 0 and r["items"] == []
