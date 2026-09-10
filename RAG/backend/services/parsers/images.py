"""Markdown 图片引用处理纯函数（可单测）

- extract_image_refs：正则提取全部图片 src（格式为 ![...](src)）；
- rewrite_image_refs：src 的 basename 命中 mapping key → 替换为 value
  （如 "/api/files/images/{doc_id}/{name}"）；未命中（含 http 外链）保留原样。
"""
from __future__ import annotations

import re
from typing import Dict, List

# 图片引用: ![alt](src)，src 不含右括号
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def extract_image_refs(markdown: str) -> List[str]:
    """提取 markdown 中全部图片 src 列表"""
    if not markdown:
        return []
    return [m.group(2).strip() for m in _IMG_RE.finditer(markdown)]


def rewrite_image_refs(markdown: str, mapping: Dict[str, str]) -> str:
    """替换图片引用：src 的 basename（去 query/hash 后缀）命中 mapping key → 替换

    - mapping: {原文件名: 新 URL}（如 {"a.png": "/api/files/images/{doc_id}/a.png"}）
    - http(s) 外链或未命中的本地引用保留原样
    """
    if not markdown:
        return ""
    if not mapping:
        return markdown

    def _sub(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2).strip()
        # http(s) 外链 / data URI 等绝对地址一律保留原样（即使 basename 与 mapping 撞名）
        if "://" in src or src.startswith("data:"):
            return m.group(0)
        # basename 提取（兼容 "images/a.png"、"a.png?v=1"、"./a.png" 等）
        base = src.split("/")[-1].split("#")[0].split("?")[0].strip()
        if base and base in mapping:
            return f"![{alt}]({mapping[base]})"
        return m.group(0)

    return _IMG_RE.sub(_sub, markdown)
