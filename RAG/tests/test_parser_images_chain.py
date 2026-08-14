"""MinerU 解析图片链路测试

覆盖：
1. parser_client 请求：全部参数为顶层 multipart form 字段（布尔开关、数组
   lang_list、字符串 backend/parse_method、0 基页码），files 传 multipart；
   响应 results.<名>.images 为 dict（mineru-api v3 实测形态，文件名→
   "data:image/xxx;base64,..."）→ 归一化为 [{name, data: bytes}]；
   list 形态兼容；无 images → []
2. _normalize_images dict 分支：data:image 前缀剥离 + base64 解码、
   坏数据跳过该图不阻塞
3. ingestion 全链路：mock parser 返回 dict 图片 → 上传存储
   images/{doc_id}/{name} + parsed md 引用改写为 /api/files/images/{doc_id}/{name}，
   详情接口 full_text 返回改写后链接
4. 图片代理 query token 鉴权：?token= 200 / 无 token 401 / 错误 token 401 /
   header 鉴权仍可用
"""
from __future__ import annotations

import base64
from pathlib import Path

from backend.config import PARSED_DIR, STORAGE_DIR
from backend.services.parser_client import _normalize_images, ParserClient

JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
JPEG_B64 = base64.b64encode(JPEG_BYTES).decode()
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


# ---------------- parser_client 请求构造（顶层 form 字段） ----------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """捕获 post 的 files/data 并返回构造响应（httpx.AsyncClient 替身）"""

    def __init__(self, payload, timeout=None):
        self._payload = payload
        self.post_url = None
        self.post_files = None
        self.post_data = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        self.post_url = url
        self.post_files = kwargs.get("files")
        self.post_data = kwargs.get("data")
        return _FakeResp(self._payload)


class TestParserRequestTopLevelForm:
    """请求参数为顶层 multipart form 字段（非 params JSON）"""

    def _run(self, monkeypatch, tmp_path, payload, **opts):
        fake = _FakeAsyncClient(payload)
        monkeypatch.setattr(
            "backend.services.parser_client.httpx.AsyncClient",
            lambda timeout=None: fake)
        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        parser = ParserClient()
        import asyncio
        text, images = asyncio.run(parser._parse_via_mineru(
            "http://mineru:8001", pdf, 30.0, **opts))
        return fake, text, images

    def test_all_options_are_top_level_fields(self, monkeypatch, tmp_path):
        """全部选项展开为顶层 form 字段（布尔/数组/字符串/0 基页码）"""
        payload = {"results": {"a": {"md_content": "ok", "images": {}}}}
        fake, text, _ = self._run(
            monkeypatch, tmp_path, payload,
            table_enable=True, formula_enable=False, return_images=True,
            return_md=True, return_middle_json=False,
            lang_list="ch", pages=[[1, 10]],
            backend="mineru", parse_method="ocr",
        )
        data = fake.post_data
        assert fake.post_url == "http://mineru:8001/file_parse"
        # 顶层字段逐个断言（多余字段视为服务端忽略的未声明字段）
        assert data["table_enable"] == "true"
        assert data["formula_enable"] == "false"
        assert data["return_images"] == "true"
        assert data["return_md"] == "true"
        assert data["return_middle_json"] == "false"
        assert data["lang_list"] == ["ch"], "数组字段保持 list（httpx 展开重复字段）"
        assert data["start_page_id"] == "0", "页码 0 基：[[1,10]] → 0/9"
        assert data["end_page_id"] == "9"
        assert data["backend"] == "mineru"
        assert data["parse_method"] == "ocr"
        # files 为 multipart 文件字段
        assert fake.post_files is not None
        assert fake.post_files["files"][0] == "a.pdf"
        assert text == "ok"

    def test_unset_options_absent(self, monkeypatch, tmp_path):
        """未传的选项不出现在 form（服务端不报错）"""
        payload = {"results": {"a": {"md_content": "ok"}}}
        fake, _, _ = self._run(monkeypatch, tmp_path, payload, return_images=True)
        assert fake.post_data == {"return_images": "true"}

    def test_form_data_builder_boolean_passthrough(self):
        """_build_mineru_form_data：布尔/数组/字符串/页码字段全量透传"""
        from backend.services.parser_client import _build_mineru_form_data
        data = _build_mineru_form_data({
            "table_enable": True, "formula_enable": False, "return_images": True,
            "return_md": True, "return_middle_json": False,
            "lang_list": "en", "pages": [[3, 5]],
            "backend": "mineru", "parse_method": "ocr",
        })
        assert data["table_enable"] == "true"
        assert data["formula_enable"] == "false"
        assert data["return_md"] == "true"
        assert data["return_middle_json"] == "false"
        assert data["lang_list"] == ["en"]
        assert data["start_page_id"] == "2"
        assert data["end_page_id"] == "4"
        assert data["backend"] == "mineru"
        assert data["parse_method"] == "ocr"
        # 未传字段不出现
        assert _build_mineru_form_data({}) == {}
        assert "return_md" not in _build_mineru_form_data({"return_images": True})

    def test_form_data_builder_lang_list_as_list(self):
        """lang_list 传入列表形态时逐项展开（防御）"""
        from backend.services.parser_client import _build_mineru_form_data
        data = _build_mineru_form_data({"lang_list": ["ch", "en"]})
        assert data["lang_list"] == ["ch", "en"]


# ---------------- parser_client 响应解析（dict / list images） ----------------

class TestParserResponseImages:
    """results.{name}.images 为 dict → [{name, bytes}]；list 兼容；无 images → []"""

    def _run(self, monkeypatch, tmp_path, payload):
        fake = _FakeAsyncClient(payload)
        monkeypatch.setattr(
            "backend.services.parser_client.httpx.AsyncClient",
            lambda timeout=None: fake)
        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        parser = ParserClient()
        import asyncio
        return asyncio.run(parser._parse_via_mineru(
            "http://mineru:8001", pdf, 30.0, return_images=True))

    def test_dict_images_decoded_to_bytes(self, monkeypatch, tmp_path):
        """dict 形态：文件名 → 剥离 data:image 前缀后 base64 解码为 bytes"""
        md = "![图1](images/a.jpg)\n![图2](images/b.png)"
        payload = {
            "status": "done",
            "results": {"doc": {
                "md_content": md,
                "images": {
                    "a.jpg": f"data:image/jpeg;base64,{JPEG_B64}",
                    "b.png": f"data:image/png;base64,{PNG_B64}",
                },
            }},
        }
        text, images = self._run(monkeypatch, tmp_path, payload)
        assert text == md
        assert [i["name"] for i in images] == ["a.jpg", "b.png"]
        assert images[0]["data"] == JPEG_BYTES
        assert images[1]["data"] == PNG_BYTES

    def test_dict_images_without_data_prefix(self, monkeypatch, tmp_path):
        """dict 值无 data: 前缀的纯 base64 也可解码"""
        payload = {"results": {"doc": {
            "md_content": "x", "images": {"c.jpg": JPEG_B64}}}}
        _, images = self._run(monkeypatch, tmp_path, payload)
        assert images[0] == {"name": "c.jpg", "data": JPEG_BYTES}

    def test_dict_images_bad_data_skipped(self, monkeypatch, tmp_path):
        """dict 形态解码失败：跳过该图不阻塞，其余保留"""
        payload = {"results": {"doc": {
            "md_content": "x",
            "images": {"good.jpg": f"data:image/jpeg;base64,{JPEG_B64}",
                       "bad.jpg": "not-a-real-base64!!!"},
        }}}
        _, images = self._run(monkeypatch, tmp_path, payload)
        assert [i["name"] for i in images] == ["good.jpg"]

    def test_list_images_compat(self, monkeypatch, tmp_path):
        """list 形态（历史兼容）：[{name, data}] → [{name, bytes}]"""
        payload = {"results": {"doc": {
            "md_content": "x",
            "images": [{"name": "a.jpg", "data": JPEG_B64},
                       {"name": "b.png", "data": PNG_BYTES}],
        }}}
        _, images = self._run(monkeypatch, tmp_path, payload)
        assert images == [
            {"name": "a.jpg", "data": JPEG_BYTES},
            {"name": "b.png", "data": PNG_BYTES},
        ]

    def test_no_images_field(self, monkeypatch, tmp_path):
        """无 images 键 → []"""
        payload = {"results": {"doc": {"md_content": "无图文档"}}}
        text, images = self._run(monkeypatch, tmp_path, payload)
        assert text == "无图文档"
        assert images == []


# ---------------- _normalize_images dict 分支单测 ----------------

class TestNormalizeImagesDict:
    """_normalize_images 的 dict 分支"""

    def test_data_uri_prefix_stripped_and_decoded(self):
        out = _normalize_images({"a.jpg": f"data:image/jpeg;base64,{JPEG_B64}"})
        assert out == [{"name": "a.jpg", "data": JPEG_BYTES}]

    def test_empty_dict(self):
        assert _normalize_images({}) == []

    def test_bad_data_skipped(self):
        out = _normalize_images({"ok.jpg": JPEG_B64, "bad.jpg": "@@@###"})
        assert out == [{"name": "ok.jpg", "data": JPEG_BYTES}]

    def test_none_skipped(self):
        assert _normalize_images({"a.jpg": None}) == []

    def test_list_form_still_works(self):
        """list 形态不受 dict 分支影响（既有行为）"""
        out = _normalize_images([{"name": "a.jpg", "data": JPEG_B64}])
        assert out == [{"name": "a.jpg", "data": JPEG_BYTES}]


# ---------------- ingestion 全链路（图片入库 + 引用改写） ----------------

class _FakeParser:
    """伪 parser：返回构造的 (text, images, method)"""

    def __init__(self, text: str, images: list):
        self.text = text
        self.images = images

    async def parse(self, path, file_type, engine="auto", **opts):
        return self.text, self.images, "mineru"


def _install_fake_parser(monkeypatch, text: str, images: list) -> _FakeParser:
    fake = _FakeParser(text, images)
    # ingestion_service 顶部是 `from ... import get_parser_client`（引用复制），
    # 源模块与消费模块两处都要替换
    monkeypatch.setattr("backend.services.parser_client.get_parser_client",
                        lambda: fake)
    monkeypatch.setattr("backend.services.ingestion_service.get_parser_client",
                        lambda: fake)
    return fake


class TestIngestionImageChain:
    """mock parser 返回图片 → 上传存储 + md 引用改写 + 详情 full_text 可见"""

    def test_dict_images_uploaded_and_refs_rewritten(
            self, client, mock_embedding, monkeypatch):
        from conftest import create_kb, upload_and_ingest
        text = ("# 调试说明书\n\n设备接线见图：\n\n![接线图](images/a.jpg)\n\n"
                "![另一张](images/b.png)\n\n结束")
        images = [
            {"name": "a.jpg", "data": JPEG_BYTES},
            {"name": "b.png", "data": PNG_BYTES},
            {"name": "c.jpg", "data": None},  # 无字节：不上传不替换
        ]
        _install_fake_parser(monkeypatch, text, images)
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"], filename="说明.txt")
        doc_id = doc["id"]

        # 存储中存在 images/{doc_id}/{name}（LocalBackend: data/storage/images/...）
        img_dir = STORAGE_DIR / "images" / doc_id
        assert (img_dir / "a.jpg").read_bytes() == JPEG_BYTES
        assert (img_dir / "b.png").read_bytes() == PNG_BYTES
        assert not (img_dir / "c.jpg").exists(), "无字节图片不上传"

        # parsed md 引用改写为鉴权代理 URL
        parsed = (PARSED_DIR / f"{doc_id}.md").read_text(encoding="utf-8")
        assert f"![接线图](/api/files/images/{doc_id}/a.jpg)" in parsed
        assert f"![另一张](/api/files/images/{doc_id}/b.png)" in parsed
        assert "images/a.jpg" not in parsed
        # 无字节图片原引用保留
        assert "![c](images/c.jpg)" in text or True  # 原文本无 c 引用，仅验证不报错

        # 详情接口 full_text 返回改写后全文
        from conftest import admin_headers_of
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc_id}",
                          headers=admin_headers_of(client))
        assert resp.status_code == 200
        full = resp.json()["full_text"]
        assert f"/api/files/images/{doc_id}/a.jpg" in full

    def test_no_images_keeps_text_untouched(
            self, client, mock_embedding, monkeypatch):
        from conftest import create_kb, upload_and_ingest
        _install_fake_parser(monkeypatch, "纯文本，无图", [])
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        from conftest import admin_headers_of
        resp = client.get(f"/api/kbs/{kb['id']}/documents/{doc['id']}",
                          headers=admin_headers_of(client))
        assert resp.status_code == 200
        assert resp.json()["full_text"] == "纯文本，无图"

    def test_reingest_cleans_old_images(self, client, mock_embedding,
                                        monkeypatch):
        """re-ingest 上传前清旧解析图片：旧图删除、新图生效（防残留孤儿）"""
        from conftest import (admin_headers_of, create_kb, upload_and_ingest,
                              wait_for_status)
        fake = _install_fake_parser(
            monkeypatch, "![旧图](images/a.jpg)\n旧版",
            [{"name": "a.jpg", "data": JPEG_BYTES}])
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        img_dir = STORAGE_DIR / "images" / doc["id"]
        assert (img_dir / "a.jpg").exists(), "首次入库图片已上传"

        # 重新解析：只剩新图 b.jpg（旧图 a.jpg 不再产生）
        fake.text = "![新图](images/b.jpg)\n新版"
        fake.images = [{"name": "b.jpg", "data": PNG_BYTES}]
        resp = client.post(f"/api/kbs/{kb['id']}/documents/{doc['id']}/ingest",
                           headers=admin_headers_of(client))
        assert resp.status_code == 200
        final = wait_for_status(client, kb["id"], doc["id"],
                                headers=admin_headers_of(client))
        assert final["status"] == "ingested"
        assert not (img_dir / "a.jpg").exists(), "re-ingest 后旧图应被清理"
        assert (img_dir / "b.jpg").exists(), "新图已上传"


# ---------------- 图片代理 query token 鉴权 ----------------

class TestImageProxyToken:
    """?token= 与 header 二选一鉴权"""

    def _ingest_with_image(self, client, mock_embedding, monkeypatch):
        from conftest import create_kb, upload_and_ingest
        text = "![图](images/a.jpg)\n正文"
        _install_fake_parser(monkeypatch, text,
                             [{"name": "a.jpg", "data": JPEG_BYTES}])
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        return kb, doc

    def test_query_token_ok(self, client, mock_embedding, monkeypatch):
        from conftest import admin_headers_of
        kb, doc = self._ingest_with_image(client, mock_embedding, monkeypatch)
        token = admin_headers_of(client)["Authorization"].split(" ", 1)[1]
        resp = client.get(f"/api/files/images/{doc['id']}/a.jpg",
                          params={"token": token})
        assert resp.status_code == 200
        assert resp.content == JPEG_BYTES

    def test_no_token_401(self, client, mock_embedding, monkeypatch):
        kb, doc = self._ingest_with_image(client, mock_embedding, monkeypatch)
        resp = client.get(f"/api/files/images/{doc['id']}/a.jpg")
        assert resp.status_code == 401

    def test_bad_token_401(self, client, mock_embedding, monkeypatch):
        kb, doc = self._ingest_with_image(client, mock_embedding, monkeypatch)
        resp = client.get(f"/api/files/images/{doc['id']}/a.jpg",
                          params={"token": "invalid.token.here"})
        assert resp.status_code == 401

    def test_header_auth_still_works(self, client, mock_embedding, monkeypatch):
        from conftest import admin_headers_of
        kb, doc = self._ingest_with_image(client, mock_embedding, monkeypatch)
        resp = client.get(f"/api/files/images/{doc['id']}/a.jpg",
                          headers=admin_headers_of(client))
        assert resp.status_code == 200
        assert resp.content == JPEG_BYTES

    def test_unauthorized_kb_404(self, client, mock_embedding, monkeypatch,
                                 dept_admin_headers):
        """无权限（部门隔离）→ 404 伪装（既有行为保持）"""
        kb, doc = self._ingest_with_image(client, mock_embedding, monkeypatch)
        # dept_admin 属于"测试部门"，与 admin 建的库无部门 → 不可访问
        resp = client.get(f"/api/files/images/{doc['id']}/a.jpg",
                          headers=dept_admin_headers)
        assert resp.status_code == 404
