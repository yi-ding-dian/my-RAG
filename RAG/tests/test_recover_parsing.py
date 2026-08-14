"""P0-1 解析中断恢复测试

后台解析任务用 asyncio.create_task 执行（无持久化），进程重启后 status=parsing
的文档会永久卡死：无法重新解析（is_ingestable 不含 parsing → 409）、前端每 2s
无限轮询。修复：document_service.recover_stuck_parsing() 在启动（lifespan）时把
所有 parsing 文档拨回 failed + 明确 error，用户可直接重新解析。
"""
from __future__ import annotations

import json

from conftest import create_kb, upload_and_ingest, upload_doc


class TestRecoverStuckParsing:

    def test_parsing_docs_marked_failed_with_clear_error(self, client,
                                                         admin_headers):
        """构造 parsing 状态文档 → 恢复函数把其拨回 failed + 明确 error"""
        from backend.services.document_service import get_document_service
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        svc = get_document_service()
        # 直接拨到 parsing（模拟后台解析任务进行中被进程重启打断）
        svc.transition(doc["id"], "parsing")
        assert svc.get(doc["id"]).status == "parsing"

        recovered = svc.recover_stuck_parsing()
        assert doc["id"] in recovered
        d = svc.get(doc["id"])
        assert d.status == "failed"
        assert d.error == "服务重启，解析中断，请重新解析"

    def test_recover_then_reingest_success(self, client, mock_embedding,
                                           admin_headers):
        """恢复（failed）后可直接重新触发解析并成功入库（完整链路）"""
        from backend.services.document_service import get_document_service
        from conftest import wait_for_status
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"])
        svc = get_document_service()
        svc.transition(doc["id"], "parsing")
        svc.recover_stuck_parsing()

        # failed 状态允许重新解析（is_ingestable 含 failed，状态机 failed->parsing 合法）
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
            headers=admin_headers)
        assert resp.status_code == 200, resp.text
        final = wait_for_status(client, kb["id"], doc["id"])
        assert final["status"] == "ingested"
        assert final.get("error") is None

    def test_non_parsing_docs_unaffected(self, client, mock_embedding,
                                         admin_headers):
        """ingested/failed/uploaded 状态不受恢复影响"""
        from backend.services.document_service import get_document_service
        kb = create_kb(client)
        doc1 = upload_and_ingest(client, kb["id"], filename="已入库.txt")
        doc2 = upload_doc(client, kb["id"], filename="待解析.txt")
        svc = get_document_service()
        # uploaded 不能直达 failed（状态机合法路径 parsing -> failed，与入库服务一致）
        svc.transition(doc2["id"], "parsing")
        svc.mark_failed(doc2["id"], "模拟失败")
        # 一个 parsing + 三个非 parsing
        doc3 = upload_doc(client, kb["id"], filename="解析中.txt")
        svc.transition(doc3["id"], "parsing")

        recovered = svc.recover_stuck_parsing()
        assert recovered == [doc3["id"]]
        assert svc.get(doc1["id"]).status == "ingested"
        assert svc.get(doc2["id"]).status == "failed"
        assert svc.get(doc2["id"]).error == "模拟失败"  # 原 error 不被覆盖

    def test_lifespan_recovers_on_startup(self):
        """进程重启场景：磁盘残留 parsing 元数据 → 启动（lifespan）自动恢复"""
        from backend.config import DOCUMENTS_DIR
        from backend.models.rag_models import DocumentItem
        # 手工构造进程重启后残留的 parsing 文档元数据
        doc = DocumentItem(
            id="stuck1", kb_id="kb_dead", name="f.txt",
            original_name="f.txt", file_type="txt", size=10,
            status="parsing", created_at="2026-08-01 00:00:00",
            updated_at="2026-08-01 00:00:00")
        DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCUMENTS_DIR / "stuck1.json").write_text(
            json.dumps(doc.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8")

        from fastapi.testclient import TestClient
        from backend.main import app
        with TestClient(app) as c:  # 触发 lifespan → recover_stuck_parsing
            from backend.services.document_service import get_document_service
            d = get_document_service().get("stuck1")
            assert d is not None
            assert d.status == "failed"
            assert d.error == "服务重启，解析中断，请重新解析"
