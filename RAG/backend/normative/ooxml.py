"""OOXML（docx）通用词汇：命名空间、标签名、属性取值

docx_parser（解析主流程）与 stamp（盖章合成）共用的 XML 常量与取值工具。
独立成模块是为了避免两者互相 import 形成循环依赖：stamp 需要这些标签名，
而 docx_parser 需要 stamp 的合成器。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---- 命名空间标签（python-docx 的 qn() 不含 VML 映射，用字面 URI）----
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_V_NS = "urn:schemas-microsoft-com:vml"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# 绘图定位命名空间（wp:anchor 浮动图 / wp:inline 内嵌图的尺寸与位置）
_WP_NS = ("http://schemas.openxmlformats.org/drawingml/2006/"
          "wordprocessingDrawing")

_TAG_P = f"{{{_W_NS}}}p"
_TAG_TBL = f"{{{_W_NS}}}tbl"
_TAG_R = f"{{{_W_NS}}}r"
_TAG_T = f"{{{_W_NS}}}t"
_TAG_TAB = f"{{{_W_NS}}}tab"
_TAG_BR = f"{{{_W_NS}}}br"
_TAG_CR = f"{{{_W_NS}}}cr"
_TAG_PPR = f"{{{_W_NS}}}pPr"
_TAG_RPR = f"{{{_W_NS}}}rPr"
_TAG_STYLE = f"{{{_W_NS}}}style"
_TAG_PSTYLE = f"{{{_W_NS}}}pStyle"
_TAG_B = f"{{{_W_NS}}}b"
_TAG_I = f"{{{_W_NS}}}i"
_TAG_NUM_PR = f"{{{_W_NS}}}numPr"
_TAG_NUM_ID = f"{{{_W_NS}}}numId"
_TAG_ILVL = f"{{{_W_NS}}}ilvl"
_TAG_OUTLINE_LVL = f"{{{_W_NS}}}outlineLvl"
_TAG_INSTR_TEXT = f"{{{_W_NS}}}instrText"
_TAG_FLD_SIMPLE = f"{{{_W_NS}}}fldSimple"
_TAG_SDT = f"{{{_W_NS}}}sdt"
_TAG_SDT_CONTENT = f"{{{_W_NS}}}sdtContent"
_TAG_BLIP = f"{{{_A_NS}}}blip"
_TAG_IMAGEDATA = f"{{{_V_NS}}}imagedata"
_TAG_ANCHOR = f"{{{_WP_NS}}}anchor"      # 浮动图片（如电子章：浮于文字上方）
_TAG_INLINE = f"{{{_WP_NS}}}inline"      # 内嵌图片（随文字流，通常是被盖章的扫描件）
_TAG_EXTENT = f"{{{_WP_NS}}}extent"      # 图片显示尺寸（EMU）
_TAG_POS_H = f"{{{_WP_NS}}}positionH"
_TAG_POS_V = f"{{{_WP_NS}}}positionV"
_TAG_POS_OFFSET = f"{{{_WP_NS}}}posOffset"
_TAG_VAL = f"{{{_W_NS}}}val"
_TAG_TYPE = f"{{{_W_NS}}}type"
_TAG_SECT_PR = f"{{{_W_NS}}}sectPr"
_TAG_PG_SZ = f"{{{_W_NS}}}pgSz"
_TAG_PG_MAR = f"{{{_W_NS}}}pgMar"
_TAG_SPACING = f"{{{_W_NS}}}spacing"
_TAG_SZ = f"{{{_W_NS}}}sz"
_TAG_LINE = f"{{{_W_NS}}}line"
_TAG_LINE_RULE = f"{{{_W_NS}}}lineRule"
_TAG_BEFORE = f"{{{_W_NS}}}before"
_TAG_AFTER = f"{{{_W_NS}}}after"
_TAG_DOC_DEFAULTS = f"{{{_W_NS}}}docDefaults"
_TAG_RPR_DEFAULT = f"{{{_W_NS}}}rPrDefault"
_TAG_PAGE_BREAK_BEFORE = f"{{{_W_NS}}}pageBreakBefore"
# 页面度量的属性名带 w: 前缀（如 <w:pgSz w:w="11906"/>），须用限定名取值
_TAG_W = f"{{{_W_NS}}}w"
_TAG_H = f"{{{_W_NS}}}h"
_TAG_TOP = f"{{{_W_NS}}}top"
_TAG_BOTTOM = f"{{{_W_NS}}}bottom"
_TAG_LEFT = f"{{{_W_NS}}}left"
_TAG_RIGHT = f"{{{_W_NS}}}right"
_TAG_NAME = f"{{{_W_NS}}}name"
_TAG_BASED_ON = f"{{{_W_NS}}}basedOn"
_TAG_STYLE_ID = f"{{{_W_NS}}}styleId"
_TAG_ABSTRACT_NUM = f"{{{_W_NS}}}abstractNum"
_TAG_ABSTRACT_NUM_ID = f"{{{_W_NS}}}abstractNumId"
_TAG_NUM = f"{{{_W_NS}}}num"
_TAG_LVL = f"{{{_W_NS}}}lvl"
_TAG_LVL_OVERRIDE = f"{{{_W_NS}}}lvlOverride"
_TAG_START = f"{{{_W_NS}}}start"
_TAG_START_OVERRIDE = f"{{{_W_NS}}}startOverride"
_TAG_NUM_FMT = f"{{{_W_NS}}}numFmt"
_TAG_LVL_TEXT = f"{{{_W_NS}}}lvlText"
_TAG_IS_LGL = f"{{{_W_NS}}}isLgl"
_TAG_NUM_STYLE_LINK = f"{{{_W_NS}}}numStyleLink"
_TAG_STYLE_LINK = f"{{{_W_NS}}}styleLink"

_R_EMBED = f"{{{_R_NS}}}embed"
_R_ID = f"{{{_R_NS}}}id"
_W_INSTR = f"{{{_W_NS}}}instr"

# 图片引用目录：与 MinerU 产物一致（下游按 basename 匹配替换为鉴权 URL）
_IMG_DIR = "images"


def _int_attr(element, attr: str = _TAG_VAL) -> Optional[int]:
    """取元素属性的整数（元素缺失/非数字 → None，编号属性异常不影响整篇）"""
    if element is None:
        return None
    raw = element.get(attr)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _styles_element(doc):
    """doc 的 styles.xml 元素；样式部件缺失（损坏文件）不影响正文解析"""
    try:
        return doc.styles.element
    except Exception as exc:
        logger.warning("styles.xml 读取失败: %s", exc)
        return None
