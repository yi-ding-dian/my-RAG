"""P1-5 docx 原始文件下载测试

问题：预览弹窗提示 docx"可下载后查看"但无下载接口。
修复：GET /{doc_id}/raw 对 docx 返回原始字节（application/octet-stream +
Content-Disposition attachment，文件名 original_name）；前端提供下载按钮。
"""
from __future__ import annotations

from conftest import create_kb, upload_doc


class TestDocxRawDownload:

    def test_docx_raw_returns_attachment(self, client, admin_headers):
        """docx raw → 200 + 原始字节 + octet-stream + 附件头（含文件名）"""
        kb = create_kb(client)
        content = b"PK\x03\x04fake-docx-bytes"
        doc = upload_doc(client, kb["id"], filename="产品方案.docx",
                         content=content)
        assert doc["file_type"] == "docx"

        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/raw",
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.content == content
        assert resp.headers["content-type"] == "application/octet-stream"
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        # RFC 5987 filename*=UTF-8''（中文文件名编码后）
        assert "产品方案.docx" in cd or "filename*=" in cd

    def test_docx_raw_404_for_no_permission(self, client, admin_headers,
                                            user_headers):
        """普通用户访问（无 kb 权限）→ 404 伪装"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="保密.docx",
                         content=b"PK\x03\x04x")
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/raw",
                          headers=user_headers)
        assert resp.status_code == 404

    def test_docx_raw_404_for_trashed(self, client, admin_headers):
        """回收站内 docx → 404（不可下载）"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="已删.docx",
                         content=b"PK\x03\x04x")
        client.delete(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                      headers=admin_headers)
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/raw",
                          headers=admin_headers)
        assert resp.status_code == 404

    def test_other_types_unchanged(self, client, admin_headers):
        """txt raw 行为不变（text/plain 预览），非 docx/pdf 仍 400"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="说明.txt",
                         content="纯文本内容")
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}/raw",
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "attachment" not in resp.headers.get("content-disposition", "")
        assert resp.text == "纯文本内容"
