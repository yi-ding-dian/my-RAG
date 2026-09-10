"""知识库文档数配额（ingestion.kb_doc_limit）测试

单函数生命周期（同一 client/token 内验证）：默认不限 → 配额=1 后上传/URL 导入被拦。
monkeypatch 路由读取的 active 配置，不改真实档案。
"""
from __future__ import annotations

import types

from tests.conftest import create_kb, upload_doc


def test_quota_default_unlimited(client, admin_headers, mock_embedding):
    """默认 kb_doc_limit=0（不限）：多文档可上传；随后配额=1 拦新文档"""
    import backend.routers.documents.crud as doc_router
    kb = create_kb(client)
    upload_doc(client, kb["id"], filename="文档一.txt", content="文档一")
    upload_doc(client, kb["id"], filename="文档二.txt", content="文档二")
    resp = client.get(f"/api/kbs/{kb['id']}/documents",
                      headers=admin_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2  # 列表接口返回裸数组

    # —— 配额=1（此时库内已有 2 个，超限）——
    fake = types.SimpleNamespace(ingestion=types.SimpleNamespace(kb_doc_limit=1))
    import pytest
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(doc_router, "get_active_config", lambda: fake)
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/upload",
            files={"file": ("三.txt", b"doc3", "text/plain")},
            headers=admin_headers)
        assert resp.status_code == 400
        assert "上限" in resp.json()["detail"]
        # URL 导入同样被拦
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/from-url",
            json={"url": "https://example.com/a"},
            headers=admin_headers)
        assert resp.status_code == 400
        assert "上限" in resp.json()["detail"]
