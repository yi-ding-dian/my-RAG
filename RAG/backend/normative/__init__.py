"""规范性文档结构解析与切块（OOXML 直读，产出与 MinerU 同构的解析产物）

parse_docx(path) -> (markdown, images, parse_method)
- markdown：标题层级用 # 数量表示、自动编号已还原、表格为管道表格、图片
  引用为 images/{name}；
- images：[{"name": "image1.png", "data": bytes}]，与 MinerU 产物同构
  （切块/检索/前端预览链路零改动）；
- parse_method：结构化解析标识（记录实际解析方式用）。

extract_outline(path) -> [{"level": 井号个数, "title": 标题文本}]：只提取标题
层级树（「查看文档结构」预览用，不产出正文/图片，层级判定与 parse_docx 同源）。

HierarchicalChunker：自底向上的章节树聚合切块（专为写作格式规范的文档设计，
见 chunker.py）。

实现见 docx_parser.py / chunker.py。
"""
from backend.normative.chunker import HierarchicalChunker
from backend.normative.docx_parser import extract_outline, parse_docx

__all__ = ["parse_docx", "extract_outline", "HierarchicalChunker"]
