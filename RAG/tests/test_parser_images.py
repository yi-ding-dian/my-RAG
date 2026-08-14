"""Markdown 图片引用处理纯函数单测（parser_images.py）

覆盖：extract_image_refs 提取（普通/多张/空文本）、rewrite_image_refs
替换（basename 命中替换、http 外链保留、data URI 保留、未命中保留、
带 query/hash/路径前缀的 basename 归一、撞名不误替换）。全部离线。
"""
from __future__ import annotations

from backend.services.parser_images import extract_image_refs, rewrite_image_refs


class TestExtractImageRefs:
    """图片引用提取"""

    def test_extract_basic(self):
        """普通 ![](src) 提取 src"""
        md = "看这张图：![示意图](diagram.png)"
        assert extract_image_refs(md) == ["diagram.png"]

    def test_extract_multiple(self):
        """多张图片全部提取，顺序保持"""
        md = "![a](a.png) 文字 ![b](images/b.jpg) 结束"
        assert extract_image_refs(md) == ["a.png", "images/b.jpg"]

    def test_extract_no_images(self):
        """无图片 → 空列表"""
        assert extract_image_refs("纯文本，没有图片") == []

    def test_extract_empty_text(self):
        """空文本/None → 空列表"""
        assert extract_image_refs("") == []
        assert extract_image_refs(None) == []

    def test_extract_alt_normal(self):
        """普通 alt 文本（含空格/中文）不影响 src 提取"""
        md = "![带 空格与中文的 alt 文本](photo.png)"
        assert extract_image_refs(md) == ["photo.png"]


class TestRewriteImageRefs:
    """图片引用替换"""

    def test_rewrite_basename_hit(self):
        """basename 命中 mapping → 整条引用替换"""
        md = "![图](a.png)"
        mapping = {"a.png": "/api/files/images/doc123/a.png"}
        out = rewrite_image_refs(md, mapping)
        assert out == "![图](/api/files/images/doc123/a.png)"

    def test_rewrite_path_prefix_hit(self):
        """带路径前缀的引用（images/a.png）按 basename 命中"""
        md = "![图](images/a.png)"
        out = rewrite_image_refs(md, {"a.png": "/api/files/images/doc1/a.png"})
        assert out == "![图](/api/files/images/doc1/a.png)"

    def test_rewrite_query_and_hash_normalized(self):
        """src 带 ?query 或 #hash 时 basename 归一化后命中"""
        md = "![图](a.png?v=123#frag)"
        out = rewrite_image_refs(md, {"a.png": "/api/files/images/doc1/a.png"})
        assert out == "![图](/api/files/images/doc1/a.png)"

    def test_rewrite_http_link_kept_even_collision(self):
        """http 外链始终保留原样（即使 basename 与 mapping 撞名）"""
        md = "![图](http://example.com/a.png)"
        out = rewrite_image_refs(md, {"a.png": "/api/files/images/doc1/a.png"})
        assert out == md

    def test_rewrite_data_uri_kept(self):
        """data URI 始终保留原样（即使 basename 与 mapping 撞名）"""
        src = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA=="
        md = f"![图]({src})"
        out = rewrite_image_refs(md, {"AAAA": "/api/files/images/doc1/x"})
        assert out == md

    def test_rewrite_miss_kept(self):
        """basename 未命中 → 保留原样"""
        md = "![图](other.png)"
        out = rewrite_image_refs(md, {"a.png": "/api/files/images/doc1/a.png"})
        assert out == md

    def test_rewrite_multiple_mixed(self):
        """多张图片混合：命中的替换、外链/未命中的保留"""
        md = ("![a](a.png)\n![b](http://x.com/b.png)\n![c](c.png)\n"
              "![d](local/other.png)")
        mapping = {
            "a.png": "/api/files/images/doc1/a.png",
            "c.png": "/api/files/images/doc1/c.png",
            "other.png": "/api/files/images/doc1/other.png",
        }
        out = rewrite_image_refs(md, mapping)
        assert "/api/files/images/doc1/a.png" in out
        assert "http://x.com/b.png" in out
        assert "/api/files/images/doc1/c.png" in out
        assert "/api/files/images/doc1/other.png" in out
        assert "![b](http://x.com/b.png)" in out, "外链整条引用格式不变"

    def test_rewrite_empty(self):
        """空文本 / 空 mapping → 原样"""
        assert rewrite_image_refs("", {"a.png": "x"}) == ""
        assert rewrite_image_refs("![图](a.png)", {}) == "![图](a.png)"
        assert rewrite_image_refs(None, {"a.png": "x"}) == ""

    def test_rewrite_alt_preserved(self):
        """替换时保留 alt 文本"""
        out = rewrite_image_refs("![重要示意图](a.png)",
                                 {"a.png": "/api/files/images/d1/a.png"})
        assert out == "![重要示意图](/api/files/images/d1/a.png)"
