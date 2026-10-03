"""文档重复检测 / 待确认更新流程测试

对应实现：``backend/services/fingerprint.py``（判定逻辑）+
``backend/routers/documents/crud.py`` 的 upload / pending-update 接口 +
``backend/services/ingestion/service.py`` 的 `_stage_pending_update`（解析后暂停）。

覆盖：
- 指纹服务纯函数：file_hash / content_hash 的稳定性、区分度、空输入
- 上传分支：字节完全相同的重复上传 → 409（同库内；含改名后重复上传）
- 上传分支：同名且既有文档正在解析 → 409（并发安全）
- 上传分支：同名但字节不同 → 放行 + 自动解析 + 停在 pending_update
- 上传分支：全新文档 → 照常上传（与改动前行为一致）
- 老文档无 file_hash → 上传同名时当场补算并持久化
- resolve(keep)：保留为独立文档，旧文档不受影响
- resolve(update)：旧文档内容换新 + 临时文档消失 + 旧文档重新入库

全部离线（conftest 已 mock 解析器探测与 embedding）。
"""
from __future__ import annotations

import io

import pytest

from conftest import create_kb, upload_and_ingest, upload_doc, wait_for_status

from backend.services.document_service import get_document_service
from backend.services.fingerprint import (EXACT_DUP, NONE, SAME_NAME,
                                          SAME_NAME_BUSY, check_upload,
                                          compute_content_hash,
                                          compute_file_hash)

TXT_MIME = "text/plain"


def _upload(client, kb_id, filename, content, headers, **params):
    """直接调上传接口（不 assert 200——本文件要断言 409 分支）"""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return client.post(
        f"/api/kbs/{kb_id}/documents/upload",
        files={"file": (filename, content, TXT_MIME)},
        headers=headers, params=params or None)


# ==================== 指纹服务纯函数 ====================

class TestFingerprintPure:
    """指纹计算与判定的纯函数层（无 IO、无 HTTP）"""

    def test_file_hash_stable_and_distinct(self):
        assert compute_file_hash(b"abc") == compute_file_hash(b"abc")
        assert compute_file_hash(b"abc") != compute_file_hash(b"abd")
        # sha256 十六进制固定 64 位
        assert len(compute_file_hash(b"abc")) == 64
        # 空文件不崩（有确定的 hash，不是空串）
        assert len(compute_file_hash(b"")) == 64

    def test_content_hash_distinguishes_whitespace(self):
        """文本指纹对空白敏感——只差一个空格就是不同内容"""
        assert compute_content_hash("你好世界") == compute_content_hash("你好世界")
        assert compute_content_hash("你好世界") != compute_content_hash("你好世界 ")
        # 中文按 utf-8 编码计算（与解析产物落盘编码一致）
        assert compute_content_hash("你好") == compute_content_hash("你好")

    def test_check_upload_returns_file_hash(self):
        """判定结果自带本次文件的 file_hash，调用方不必重算"""
        r = check_upload("__no_such_kb__", "x.txt", b"abc")
        assert r.kind == NONE
        assert r.file_hash == compute_file_hash(b"abc")
        assert not r.blocked

    def test_blocked_only_for_dup_and_busy(self):
        from backend.services.fingerprint import DupCheck
        assert DupCheck(EXACT_DUP).blocked
        assert DupCheck(SAME_NAME_BUSY).blocked
        assert not DupCheck(SAME_NAME).blocked   # 同名待确认要放行
        assert not DupCheck(NONE).blocked


# ==================== 上传时的重复检测 ====================

class TestUploadDupCheck:
    """上传接口的重复检测分支（含老文档补算）"""

    def test_exact_duplicate_blocked(self, client, admin_headers):
        """同一文件传两次 → 第二次 409，且文案点明"无需重复上传\""""
        kb = create_kb(client)
        upload_doc(client, kb["id"], filename="a.txt", content="同样的内容")
        resp = _upload(client, kb["id"], "a.txt", "同样的内容", admin_headers)
        assert resp.status_code == 409, resp.text
        assert "无需重复上传" in resp.json()["detail"]

    def test_renamed_same_content_allowed(self, client, admin_headers):
        """改名后传同一内容**不拦**：刻意不做全局内容查重（理由见 fingerprint.py）

        曾实现过"无同名但字节相同也拦"，会误伤"同内容以不同文件名入库"的合理
        用法（空文件/占位文件内容都为空、同一模板按用途各存一份），实测一次打挂
        19 个既有用例，收益远小于误拦风险。
        """
        kb = create_kb(client)
        upload_doc(client, kb["id"], filename="a.txt", content="同样的内容")
        doc = upload_doc(client, kb["id"], filename="b.txt", content="同样的内容")
        assert doc["status"] == "uploaded"
        assert doc["pending_update_of"] is None

    def test_force_bypasses_exact_dup(self, client, admin_headers):
        """force=true 仍可强制入库（向后兼容老前端"确认后重传"）"""
        kb = create_kb(client)
        upload_doc(client, kb["id"], filename="a.txt", content="同样的内容")
        resp = _upload(client, kb["id"], "a.txt", "同样的内容", admin_headers,
                       force=True)
        assert resp.status_code == 200, resp.text

    def test_new_document_uploaded_normally(self, client, admin_headers):
        """无同名无同内容 → 正常上传（行为与改动前一致），并记下 file_hash"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="全新.txt", content="全新内容")
        assert doc["status"] == "uploaded"
        assert doc["pending_update_of"] is None
        assert doc["file_hash"] == compute_file_hash("全新内容".encode())

    def test_legacy_doc_without_hash_backfilled(self, client, admin_headers):
        """老文档（本功能上线前入库、无 file_hash）→ 上传同名时当场补算并持久化"""
        kb = create_kb(client)
        old = upload_doc(client, kb["id"], filename="a.txt", content="老内容")
        doc_svc = get_document_service()
        doc_svc.update_doc(old["id"], file_hash=None)   # 模拟老数据

        resp = _upload(client, kb["id"], "a.txt", "老内容", admin_headers)
        # 补算后能认出"字节完全相同"
        assert resp.status_code == 409, resp.text
        assert doc_svc.get(old["id"]).file_hash == compute_file_hash("老内容".encode())

    def test_legacy_doc_content_hash_backfilled(self, client, admin_headers,
                                                mock_embedding):
        """老文档缺 content_hash → 一并补算（读已落盘的解析产物，不重新解析）

        不补的话老文档首次遇到同名新版本只能判 unknown（"无法确认内容是否
        变化"）——用户就拿不到"其实没变"这个结论，而它恰恰最省钱（选不更新
        就不用重跑 embedding）。
        """
        kb = create_kb(client)
        old = upload_and_ingest(client, kb["id"], filename="a.txt",
                                content="旧版本内容" * 30)
        doc_svc = get_document_service()
        # 模拟老数据：两个指纹字段都为空（本功能上线前入库的文档）
        doc_svc.update_doc(old["id"], file_hash=None, content_hash=None)

        new = _upload(client, kb["id"], "a.txt", "新版本内容" * 30,
                      admin_headers).json()
        pending = wait_for_status(client, kb["id"], new["id"],
                                  status="pending_update")
        # 补算成功 → 给出确定结论（changed），而不是退化成 unknown
        assert pending["pending_update_verdict"] == "changed"
        assert doc_svc.get(old["id"]).content_hash

    def test_same_name_busy_blocked(self, client, admin_headers, mock_embedding):
        """同名且既有文档正在解析 → 409（并发安全，force 也不放行）"""
        kb = create_kb(client)
        old = upload_doc(client, kb["id"], filename="a.txt", content="旧内容")
        # 手动拨到 parsing，模拟"解析中"
        get_document_service().transition(old["id"], "parsing")
        resp = _upload(client, kb["id"], "a.txt", "新内容", admin_headers,
                       force=True)   # force 也不该放行
        assert resp.status_code == 409, resp.text
        assert "正在解析中" in resp.json()["detail"]

    def test_rejected_upload_audited(self, client, admin_headers):
        """被拦的重复上传要留痕（action=doc.upload.rejected，status=failed）

        这是**唯一**记录"未成功写操作"的地方——审计的意义是追溯行为，
        "谁反复传了同一个文件"是有价值的排查线索（多半是没意识到文档已在库）。
        """
        kb = create_kb(client)
        upload_doc(client, kb["id"], filename="留痕.txt", content="同样的内容")
        assert _upload(client, kb["id"], "留痕.txt", "同样的内容",
                       admin_headers).status_code == 409

        resp = client.get("/api/audit/logs",
                          params={"action": "doc.upload.rejected"},
                          headers=admin_headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert items, "被拦的上传应当留痕"
        log = items[0]
        assert log["status"] == "failed"
        assert log["target_name"] == "留痕.txt"
        # detail 里带被拦原因，便于区分"内容完全相同"与"文档正在解析中"
        assert "exact_dup" in str(log["detail"])


# ==================== 待确认更新：暂停与判定 ====================

class TestPendingUpdateFlow:
    """同名不同内容 → 自动解析 → 停 pending_update → 用户选定处置"""

    def _upload_new_version(self, client, kb_id, filename, content, headers):
        resp = _upload(client, kb_id, filename, content, headers)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_content_changed_stops_at_pending_update(self, client, admin_headers,
                                                     mock_embedding):
        """同名不同内容 → 放行、自动解析、判定 changed、停在 pending_update"""
        kb = create_kb(client)
        old = upload_and_ingest(client, kb["id"], filename="a.txt",
                                content="旧版本内容" * 30)
        assert old["status"] == "ingested"

        new = self._upload_new_version(client, kb["id"], "a.txt",
                                       "新版本内容" * 30, admin_headers)
        assert new["pending_update_of"] == old["id"]
        # 上传响应里的状态可能是 uploaded，也可能已被后台自动解析推进
        # （响应返回的是同一内存对象，create_task 的任务可能先跑完——
        #   前端无影响：列表每 2s 轮询会拿到最终状态）
        assert new["status"] in ("uploaded", "parsing", "pending_update")

        pending = wait_for_status(client, kb["id"], new["id"],
                                  status="pending_update")
        assert pending["pending_update_verdict"] == "changed"
        # 两个指纹都落库了：file_hash 来自上传、content_hash 来自解析
        assert pending["file_hash"] == compute_file_hash(
            ("新版本内容" * 30).encode())
        assert pending["content_hash"]

    def test_pending_doc_is_not_ingested(self, client, admin_headers,
                                         mock_embedding):
        """待确认文档没有切块（→ 无向量 → 确认前不参与检索）"""
        kb = create_kb(client)
        old = upload_and_ingest(client, kb["id"], filename="a.txt",
                                content="旧版本内容" * 30)
        new = self._upload_new_version(client, kb["id"], "a.txt",
                                       "新版本内容" * 30, admin_headers)
        pending = wait_for_status(client, kb["id"], new["id"],
                                  status="pending_update")
        assert pending["chunk_count"] == 0
        assert pending["chunks_meta"] == []
        # 旧文档仍是 ingested（内容未动，直到用户确认更新）
        old_now = client.get(
            f"/api/kbs/{kb['id']}/documents/{old['id']}",
            headers=admin_headers).json()
        assert old_now["status"] == "ingested"

    def test_resolve_keep_clears_marker(self, client, admin_headers,
                                        mock_embedding):
        """keep → 清标记、回到 uploaded，旧文档不受影响"""
        kb = create_kb(client)
        old = upload_and_ingest(client, kb["id"], filename="a.txt",
                                content="旧版本内容" * 30)
        new = self._upload_new_version(client, kb["id"], "a.txt",
                                       "新版本内容" * 30, admin_headers)
        wait_for_status(client, kb["id"], new["id"], status="pending_update")

        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{new['id']}/pending-update",
            json={"action": "keep"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        kept = client.get(f"/api/kbs/{kb['id']}/documents/{new['id']}",
                          headers=admin_headers).json()
        assert kept["status"] == "uploaded"
        assert kept["pending_update_of"] is None
        assert kept["pending_update_verdict"] is None
        # 旧文档内容与状态不变
        old_now = client.get(f"/api/kbs/{kb['id']}/documents/{old['id']}",
                             headers=admin_headers).json()
        assert old_now["status"] == "ingested"
        assert old_now["file_hash"] == old["file_hash"]

    def test_resolve_update_replaces_old_document(self, client, admin_headers,
                                                  mock_embedding):
        """update → 新内容写进旧文档、临时文档消失、旧文档重新入库"""
        kb = create_kb(client)
        old = upload_and_ingest(client, kb["id"], filename="a.txt",
                                content="旧版本内容" * 30)
        new = self._upload_new_version(client, kb["id"], "a.txt",
                                       "新版本内容" * 30, admin_headers)
        pending = wait_for_status(client, kb["id"], new["id"],
                                  status="pending_update")

        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{new['id']}/pending-update",
            json={"action": "update"}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["doc_id"] == old["id"]   # 复用旧文档 ID

        # 临时文档已被彻底删除
        gone = client.get(f"/api/kbs/{kb['id']}/documents/{new['id']}",
                          headers=admin_headers)
        assert gone.status_code == 404

        # 旧文档重新入库完成，且内容是**新版本**
        updated = wait_for_status(client, kb["id"], old["id"], status="ingested")
        assert updated["file_hash"] == pending["file_hash"]
        assert updated["content_hash"] == pending["content_hash"]
        parsed = get_document_service().get_parsed_path(
            get_document_service().get(old["id"])).read_text(encoding="utf-8")
        assert "新版本内容" in parsed
        assert "旧版本内容" not in parsed

    def test_resolve_update_rejected_when_old_doc_deleted(self, client,
                                                          admin_headers,
                                                          mock_embedding):
        """旧文档已被删除 → update 拒绝（提示改用 keep）"""
        kb = create_kb(client)
        old = upload_and_ingest(client, kb["id"], filename="a.txt",
                                content="旧版本内容" * 30)
        new = self._upload_new_version(client, kb["id"], "a.txt",
                                       "新版本内容" * 30, admin_headers)
        wait_for_status(client, kb["id"], new["id"], status="pending_update")
        # 彻底删除旧文档
        client.delete(f"/api/kbs/{kb['id']}/documents/{old['id']}",
                      headers=admin_headers)
        client.post(f"/api/kbs/{kb['id']}/documents/{old['id']}/purge",
                    headers=admin_headers)

        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{new['id']}/pending-update",
            json={"action": "update"}, headers=admin_headers)
        assert resp.status_code == 409, resp.text
        assert "已不存在" in resp.json()["detail"]

    def test_resolve_rejects_wrong_status(self, client, admin_headers):
        """非待确认状态调 resolve → 409；非法 action → 400"""
        kb = create_kb(client)
        doc = upload_doc(client, kb["id"], filename="a.txt", content="内容")
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}/pending-update",
            json={"action": "update"}, headers=admin_headers)
        assert resp.status_code == 409

        new2 = upload_doc(client, kb["id"], filename="b.txt", content="内容2")
        resp = client.post(
            f"/api/kbs/{kb['id']}/documents/{new2['id']}/pending-update",
            json={"action": "explode"}, headers=admin_headers)
        assert resp.status_code in (400, 409), resp.text


# ==================== 状态筛选与计数语义 ====================

class TestPendingUpdateListing:
    """pending_update 在列表筛选/计数里的归属（归「未入库」组）"""

    def test_listed_and_counted_as_unparsed(self, client, admin_headers,
                                            mock_embedding):
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], filename="a.txt",
                          content="旧版本内容" * 30)
        new = _upload(client, kb["id"], "a.txt", "新版本内容" * 30,
                      admin_headers).json()
        wait_for_status(client, kb["id"], new["id"], status="pending_update")

        # 筛选 unparsed 能看到它
        items = client.get(f"/api/kbs/{kb['id']}/documents",
                           params={"status": "unparsed"},
                           headers=admin_headers).json()
        assert any(d["id"] == new["id"] for d in items)

        # 计数里归入 unparsed
        counts = client.get(f"/api/kbs/{kb['id']}/documents/status-counts",
                            headers=admin_headers).json()
        assert counts["unparsed"] >= 1


# ============ 核心场景：文件被重新保存（字节变、内容没变） ============

DOCX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document")


def _make_docx(text: str) -> bytes:
    """生成一个最小 docx（每行一段落）"""
    import docx
    d = docx.Document()
    for line in (text.splitlines() or [text]):
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _resave_docx(data: bytes) -> bytes:
    """模拟"用 Office 重新打开保存一遍"：**字节变、正文一字不差**

    手法是改写 docProps/core.xml 的 dcterms:modified 时间戳后重新打包——
    这正是"重新保存"的**最小本质**（Word/LibreOffice 保存一定会更新它，
    实测见 record.md 2026-10-03 17:15）。不调 LibreOffice 是为了让用例
    离线、稳定、不依赖外部进程与其启动耗时。
    """
    import re
    import zipfile
    src = zipfile.ZipFile(io.BytesIO(data))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            raw = src.read(item.filename)
            if item.filename == "docProps/core.xml":
                raw = re.sub(
                    rb"<dcterms:modified[^>]*>[^<]*</dcterms:modified>",
                    b'<dcterms:modified xsi:type="dcterms:W3CDTF">'
                    b"2030-01-01T00:00:00Z</dcterms:modified>",
                    raw)
            out.writestr(item, raw)
    return buf.getvalue()


class TestVerdictSame:
    """重新保存场景：应当判定 same，而不是误导用户去更新"""

    def test_resaved_docx_judged_unchanged(self, client, admin_headers,
                                           mock_embedding):
        kb = create_kb(client)
        v1 = _make_docx("第一段内容\n第二段内容")
        old = upload_doc(client, kb["id"], filename="报告.docx", content=v1,
                         mime=DOCX_MIME)
        # 用 plain 引擎入库（离线、快；文本提取走 python-docx，输出稳定）
        client.post(f"/api/kbs/{kb['id']}/documents/{old['id']}/ingest",
                    json={"parser_engine": "plain"}, headers=admin_headers)
        wait_for_status(client, kb["id"], old["id"], status="ingested")

        v2 = _resave_docx(v1)
        assert v2 != v1, "重存后字节应当不同（否则本用例失去意义）"

        new = upload_doc(client, kb["id"], filename="报告.docx", content=v2,
                         mime=DOCX_MIME)
        # 字节不同 → 不是"完全重复"，走待确认流程
        assert new["pending_update_of"] == old["id"]
        assert new["file_hash"] != old["file_hash"]

        pending = wait_for_status(client, kb["id"], new["id"],
                                  status="pending_update")
        # 内容指纹一致 → 判定"没变"（前端据此提示"通常没有必要更新"）
        assert pending["pending_update_verdict"] == "same"
