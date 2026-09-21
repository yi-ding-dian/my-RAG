"""解析引擎建议（纯规则：文件类型 + 解析器可用性探测 + docx 规范性）

探测结果由调用方注入（不在这里发起网络探测），所以本模块是纯函数、可直测。

注意与 backend/services/ingestion/params.py 的 resolve_parser_engine 分工：
那里是"用户/配置指定了引擎后如何解析与降级"，这里是"没指定时建议用哪个"。
"""
from __future__ import annotations

from backend.services.smart_parse.types import (is_doc_like, is_spreadsheet,
                                                is_text)


def suggest_engine(file_type: str, probe: dict,
                   docx_probe: dict | None = None,
                   gotenberg_ready: bool = False) -> dict:
    """基于文件类型 + 解析器可用性探测的引擎建议（纯规则）

    - probe：probe_parsers() 的产物 {mineru:{available,reason}, deepdoc:{...}, ...}
    - docx_probe：docx/doc 结构探测结果（可选，见 extract.probe_docx_structure）；
      判定为规范文档（is_normative）时建议本地结构化解析 docx_struct
      （标题层级/自动编号保留）；非 docx/doc 或未探测时建议逻辑不变
    - gotenberg_ready：文档转换服务（Gotenberg）是否已配置。老版 .doc 无规范
      样式时优先走「转 PDF → MinerU」（保留标题层级），但**前提是转换服务可用**；
      缺省 False = 走本地结构化解析
    """
    mineru = probe.get("mineru") or {}
    deepdoc = probe.get("deepdoc") or {}
    if is_spreadsheet(file_type):
        return {"suggested": "spreadsheet",
                "reason": "Excel/CSV 本地结构化直读（表格→管道），无需解析引擎"}
    if is_text(file_type):
        return {"suggested": "plain",
                "reason": "纯文本直读，无需外部解析器"}
    if file_type == "pdf":
        if mineru.get("available"):
            return {"suggested": "mineru",
                    "reason": "PDF 混排文档，MinerU 高精度解析（服务可用，推荐）"}
        if deepdoc.get("available"):
            return {"suggested": "deepdoc",
                    "reason": "MinerU 不可用；DeepDoc 可用，表格输出可检索 HTML（仅 PDF）"}
        return {"suggested": "plain",
                "reason": "MinerU/DeepDoc 均不可用，降级纯文本提取（pypdf）"}
    if is_doc_like(file_type) and (docx_probe or {}).get("is_normative"):
        return {"suggested": "docx_struct",
                "reason": "检测到规范标题样式，可用结构解析保留层级"}
    if file_type == "doc":
        # 无规范样式（有样式的已被上面拦走）：
        # 经 Gotenberg 转 PDF 交 MinerU —— MinerU 直接吃 docx 只提文本、标题会
        # 全丢（实测同一文档对比：直接给 docx 一个标题都识别不出，转 PDF 后
        # 层级完整）。
        # **前提是转换服务可用**：没配 Gotenberg 就只能走本地结构化解析
        # （否则转 PDF 失败会回退成把 .doc 原样交给 MinerU，而它不认这个格式）
        if mineru.get("available") and gotenberg_ready:
            return {"suggested": "mineru",
                    "reason": "doc 经文档转换服务转 PDF 后由 MinerU 解析（保留标题层级）"}
        return {"suggested": "docx_struct",
                "reason": "老版 Word（doc）经 LibreOffice 转换后结构化解析"
                          "（本地，无服务依赖）"}
    if file_type in ("docx", "ppt", "pptx"):
        # ppt/pptx 与 docx 同理：MinerU 直接吃这些格式会丢标题层级（PPT 更是
        # 先经 Gotenberg 转 PDF 才能解析）
        if mineru.get("available"):
            label = "PPT" if file_type in ("ppt", "pptx") else "docx"
            return {"suggested": "mineru",
                    "reason": f"{label} 由 MinerU 解析（服务可用，推荐）"}
        return {"suggested": "plain",
                "reason": "MinerU 不可用，python-docx 纯文本提取"}
    return {"suggested": "auto",
            "reason": "该类型文档使用默认引擎"}
