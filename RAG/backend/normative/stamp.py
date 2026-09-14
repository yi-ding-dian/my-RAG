"""盖章合成：把浮动章（电子章）叠到内嵌底图上

docx 里的电子章是**浮动图片**（wp:anchor，浮于文字上方），被盖章的是内嵌
扫描件（wp:inline）。本模块把它们合成成一张，还原"纸面盖章"的观感。

DocxParser 持有一个 StampComposer 实例并转发 _stamp_merge_markdown 调用；
本类共享 parser 的 doc 与 images 列表（合成图直接 append 进去，保持编号连续）。

页面/段落度量（页边距、行高、样式继承）也在本模块：它们只服务盖章换算——
posV 参照系为 page 时需按页面绝对锚定反推章相对段落的位置，需要用段落度量。

实现要点与保守边界见各方法 docstring。
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from backend.normative.ooxml import (
    _IMG_DIR, _R_EMBED, _R_ID, _TAG_AFTER, _TAG_ANCHOR, _TAG_BASED_ON, 
    _TAG_BEFORE, _TAG_BLIP, _TAG_BOTTOM, _TAG_BR, _TAG_DOC_DEFAULTS, 
    _TAG_EXTENT, _TAG_H, _TAG_IMAGEDATA, _TAG_INLINE, _TAG_LEFT, _TAG_LINE, 
    _TAG_LINE_RULE, _TAG_P, _TAG_PAGE_BREAK_BEFORE, _TAG_PG_MAR, 
    _TAG_PG_SZ, _TAG_POS_H, _TAG_POS_OFFSET, _TAG_POS_V, _TAG_PPR, 
    _TAG_PSTYLE, _TAG_RIGHT, _TAG_RPR, _TAG_RPR_DEFAULT, _TAG_SECT_PR, 
    _TAG_SPACING, _TAG_STYLE, _TAG_STYLE_ID, _TAG_SZ, _TAG_T, _TAG_TOP, 
    _TAG_TYPE, _TAG_VAL, _TAG_W, _int_attr, 
    _styles_element as _styles_of,
)

# ---- 常量 ----
# 浮动图面积须小于底图的该比例才算"盖上去的小图"（避免把并排的两张大图误叠）
_STAMP_MAX_AREA_RATIO = 0.4
# 一个章跨盖多张图时：每张底图宽度须 ≥ 内容区宽度的该比例，才算「独占一行」
# ——垂直位置才能用「前面图高累加」估算；见 _stamp_lay_out
_MULTI_MIN_LINE_RATIO = 0.75
# 章跨段落续盖时最多跳过的空段落数（防跨度失控；遇文字/anchor 段落即停）
_MAX_BLANK_BLOCKS = 3
# 度量换算：1 twip（1/20 磅）= 635 EMU；字号 w:sz 单位为半磅
_TWIP_EMU = 635


class StampComposer:
    """盖章合成器（一个解析器实例持有一个）

    - doc / images 由 parser 传入并共享：合成图 append 进同一个 images 列表，
      编号与普通图片连续（下游按 basename 匹配替换为鉴权 URL）；
    - consumed：已被拼进合成画布的底图 rid，parser 输出原图时据此跳过，
      避免同一张图被输出两次。
    """

    def __init__(self, doc, images: List[dict]):
        self.doc = doc
        self.images = images
        self.consumed: set = set()

    @staticmethod
    def _blip_rid(container) -> Optional[str]:
        """容器内首个图片引用 ID（a:blip 的 r:embed / VML 的 r:id）"""
        for element in container.iter():
            if element.tag == _TAG_BLIP:
                rid = element.get(_R_EMBED)
            elif element.tag == _TAG_IMAGEDATA:
                rid = element.get(_R_ID)
            else:
                continue
            if rid:
                return rid
        return None

    def _blob_of(self, rid: str) -> Optional[bytes]:
        """关系 ID → 图片二进制（外链图片无 blob → None）"""
        part = self.doc.part.related_parts.get(rid)
        return getattr(part, "blob", None) or None

    def merge_markdown(self, p_element) -> Optional[str]:
        """「浮动图 + 内嵌图」合成（还原盖章效果），返回图片引用

        背景：电子章是**浮动图片**（wp:anchor，浮于文字上方），被盖章的是内嵌
        扫描件（wp:inline）。两者可能同段落，也可能只隔一个回车——Word 里两张
        图之间按一次回车即分段，而章跨图时会越过这条段落边界。逐图输出会让
        两者分家（章一张、底图一张），这里按 anchor 的 posOffset 把章叠加到底图。

        两条路径：
        - 单图且章不越出底图 → _stamp_merge_one（原实现，行为逐字不变）；
        - 章越出底图 / 同段落多图 → _stamp_lay_out（把章覆盖到的底图纵向拼成
          一张、章画在拼接画布上；被拼走的图记入 _stamp_consumed，后续段落
          的原图输出会跳过它们）。

        保守边界（任一不满足即返回 None → 走原逐图输出，绝不比现状更差）：
        - 段落内恰好 1 个 anchor（章）；
        - 参照系可换算为相对图片的坐标（page 参照系的换算见 _anchor_offset_v）；
        - 章的面积明显小于底图（是"盖上去的小图"，避免把并排大图叠一起）；
        - 图片都能被 PIL 解码（WMF/EMF 等矢量格式放弃）。
        """
        anchors = [el for el in p_element.iter() if el.tag == _TAG_ANCHOR]
        inlines = [el for el in p_element.iter() if el.tag == _TAG_INLINE]
        if not inlines:
            return None
        if len(anchors) == 1:
            stamp = self._stamp_payload(anchors[0], p_element)
            if stamp is None:
                # 位置换算不出：单图仍可走原实现，多图放弃
                return (self._stamp_merge_one(anchors[0], inlines[0])
                        if len(inlines) == 1 else None)
            if len(inlines) == 1 and not self._stamp_overflows(stamp, inlines[0]):
                return self._stamp_merge_one(anchors[0], inlines[0])
            return self._stamp_lay_out(stamp, inlines, p_element)
        return None

    def _stamp_merge_one(self, anchor, inline) -> Optional[str]:
        """单张底图：章按 posOffset 叠加到底图上（原 _stamp_merge_markdown 实现）

        保守边界（任一不满足即返回 None → 走原逐图输出）：
        - 参照系为 column/paragraph（可换算为相对图片的坐标；page/margin
          需要完整页面布局，算不了）；
        - 章的面积明显小于底图；
        - 两张图都能被 PIL 解码。
        """
        ph, pv = anchor.find(_TAG_POS_H), anchor.find(_TAG_POS_V)
        if ph is None or pv is None:
            return None
        if (ph.get("relativeFrom") != "column"
                or pv.get("relativeFrom") != "paragraph"):
            return None
        try:
            off_h = int(ph.findtext(_TAG_POS_OFFSET) or 0)
            off_v = int(pv.findtext(_TAG_POS_OFFSET) or 0)
            a_ext, i_ext = anchor.find(_TAG_EXTENT), inline.find(_TAG_EXTENT)
            if a_ext is None or i_ext is None:
                return None
            a_cx, a_cy = int(a_ext.get("cx")), int(a_ext.get("cy"))
            i_cx, i_cy = int(i_ext.get("cx")), int(i_ext.get("cy"))
        except (TypeError, ValueError):
            return None
        if min(a_cx, a_cy, i_cx, i_cy) <= 0:
            return None
        if a_cx * a_cy > i_cx * i_cy * _STAMP_MAX_AREA_RATIO:
            return None
        a_rid, i_rid = self._blip_rid(anchor), self._blip_rid(inline)
        if not a_rid or not i_rid:
            return None
        merged = self._compose_stamp(self._blob_of(i_rid),
                                     self._blob_of(a_rid),
                                     a_cx, a_cy, i_cx, i_cy, off_h, off_v)
        if merged is None:
            return None
        name = f"image{len(self.images) + 1}.jpg"
        self.images.append({"name": name, "data": merged})
        return f"![]({_IMG_DIR}/{name})"

    @staticmethod
    def _compose_stamp(base_blob: Optional[bytes], stamp_blob: Optional[bytes],
                       st_cx: int, st_cy: int, base_cx: int, base_cy: int,
                       off_h: int, off_v: int) -> Optional[bytes]:
        """把章按 docx 的显示比例叠加到底图上（EMU 尺寸 → 像素等比换算）

        失败一律返回 None（格式不支持/解码异常等），由调用方回退原输出。
        """
        if not base_blob or not stamp_blob:
            return None
        try:
            import io as _io

            from PIL import Image
            base = Image.open(_io.BytesIO(base_blob)).convert("RGBA")
            stamp = Image.open(_io.BytesIO(stamp_blob)).convert("RGBA")
            # EMU → 像素：以底图实际像素 / 其 docx 显示尺寸为比例尺
            sx, sy = base.width / base_cx, base.height / base_cy
            stamp = stamp.resize((max(1, int(st_cx * sx)),
                                  max(1, int(st_cy * sy))), Image.LANCZOS)
            base.alpha_composite(stamp, (int(off_h * sx), int(off_v * sy)))
            out = _io.BytesIO()
            base.convert("RGB").save(out, format="JPEG", quality=88)
            return out.getvalue()
        except Exception:
            return None

    def _stamp_payload(self, anchor, p_element) -> Optional[dict]:
        """章图片 + 它相对「锚点段落顶部」的垂直偏移；不适用 → None

        posH 须为 column（横向可换算到图片列内）；posV 为 paragraph 时偏移
        本身即相对段落顶部（精确），为 page 时按页面绝对锚定换算
        （见 _anchor_offset_v，该路径含前序段落高度估算）。
        """
        ph, pv = anchor.find(_TAG_POS_H), anchor.find(_TAG_POS_V)
        if ph is None or pv is None or ph.get("relativeFrom") != "column":
            return None
        a_cx = _int_attr(anchor.find(_TAG_EXTENT), "cx")
        a_cy = _int_attr(anchor.find(_TAG_EXTENT), "cy")
        if not a_cx or not a_cy:
            return None
        try:
            offset_h = int(ph.findtext(_TAG_POS_OFFSET) or 0)
        except (TypeError, ValueError):
            return None
        v = self._anchor_offset_v(pv, p_element)
        if v is None:
            return None
        rid = self._blip_rid(anchor)
        blob = self._blob_of(rid) if rid else None
        if not blob:
            return None
        return {"blob": blob, "cx": a_cx, "cy": a_cy, "h": offset_h, "v": v}

    @staticmethod
    def _stamp_overflows(stamp: dict, inline) -> bool:
        """章是否越出该底图的下边界（越界 → 需往后续段落续盖）"""
        cy = _int_attr(inline.find(_TAG_EXTENT), "cy")
        return cy is None or stamp["v"] + stamp["cy"] > cy

    def _stamp_lay_out(self, stamp: dict, inlines,
                       p_element=None) -> Optional[str]:
        """把章覆盖到的底图纵向拼成一张，章画在拼接画布上（还原跨图盖章）

        为什么不逐图各画一次：Word 里章是**浮动图**，画在页面上，能连续跨过
        图片之间的空白（段落间距、行距余量）；而逐图合成时每张图之间没有画布，
        章穿过空白的那部分无处安放，视觉上就断成两截。故改为拼接。

        垂直定位：inline 图随文字流排版，docx 不存其页面坐标，故以「锚点段落
        顶部」为基准，第 k 张底图起点 = 前 k-1 张图高之和 —— 该累加成立的前提
        是每张底图独占一行（宽度 ≥ 内容区宽度 × _MULTI_MIN_LINE_RATIO，否则
        可能两图并排 → 放弃）。图片之间的间距按 0（紧贴拼接）。

        章越过最后一张可用底图的下边界时，超出部分按不可见裁掉（与单图路径
        行为一致）；若章与图区域完全没有交集（章整个落在图外），则不做拼接，
        回退原逐图输出。
        """
        content_w = self._content_width()
        if content_w is None:
            return None
        items = self._covered_images(stamp, inlines, p_element, content_w)
        if not items:
            return None
        # 章须与图区域真正有交集才拼接：章若整个落在图区域之外（如锚在图片
        # 段落、实际却盖在旁边的正文上），画上去本就被裁掉看不见——此时拼接
        # 只会白白把图重编码一遍并改掉输出结构，故回退原逐图输出。
        covered = sum(cy for _, _, cy, _ in items)
        if stamp["v"] >= covered or stamp["v"] + stamp["cy"] <= 0:
            return None
        merged = self._compose_group(stamp, items)
        if merged is None:
            return None
        for _blob, _cx, _cy, rid in items:
            self.consumed.add(rid)  # 后续段落不再重复输出这些图
        name = f"image{len(self.images) + 1}.jpg"
        self.images.append({"name": name, "data": merged})
        return f"![]({_IMG_DIR}/{name})"

    def _covered_images(self, stamp: dict, inlines, p_element, content_w):
        """章覆盖到的底图序列 [(blob, cx, cy, rid)]，按版面先后排列

        从锚点段落的图开始，不够则顺着「纯图段落」往后取，直到章盖完或没有
        更多图为止。任一图宽度不足以独占一行（可能并排）、章相对它过大、
        或取不到二进制 → None。
        """
        limit = stamp["v"] + stamp["cy"]  # 章底（相对锚点段落顶部，EMU）
        items: List[Tuple[bytes, int, int, str]] = []
        covered = 0
        block = p_element
        group = list(inlines)
        while True:
            for inline in group:
                rid = self._blip_rid(inline)
                blob = self._blob_of(rid) if rid else None
                cx = _int_attr(inline.find(_TAG_EXTENT), "cx")
                cy = _int_attr(inline.find(_TAG_EXTENT), "cy")
                if not rid or not blob or not cx or not cy:
                    return None
                if cx < content_w * _MULTI_MIN_LINE_RATIO:
                    return None
                if stamp["cx"] * stamp["cy"] > cx * cy * _STAMP_MAX_AREA_RATIO:
                    return None  # 章相对该图过大，不像"盖上去的小图"
                items.append((blob, cx, cy, rid))
                covered += cy
                if covered >= limit:
                    return items
            block = self._next_image_block(block) if block is not None else None
            if block is None:
                return items  # 没有更多图 → 章超出部分裁掉
            group = list(block.iter(_TAG_INLINE))

    @staticmethod
    def _compose_group(stamp: dict, items) -> Optional[bytes]:
        """按显示尺寸把底图首尾相接拼成一张，并把章叠到拼接画布上

        以**首图**的比例尺（像素/EMU）为基准：章画在首图上，保持其像素不被
        重采样；其余底图缩放到同一比例尺后紧贴拼接、横向居左对齐。
        失败一律返回 None（格式不支持/解码异常等），由调用方回退。
        """
        try:
            import io as _io

            from PIL import Image
            first = Image.open(_io.BytesIO(items[0][0])).convert("RGBA")
            if not items[0][1] or not items[0][2]:
                return None
            sx, sy = first.width / items[0][1], first.height / items[0][2]
            widths = [max(1, int(cx * sx)) for _, cx, _, _ in items]
            heights = [max(1, int(cy * sy)) for _, _, cy, _ in items]
            canvas = Image.new("RGBA", (max(widths), sum(heights)),
                               (255, 255, 255, 255))
            y = 0
            for idx, (blob, _cx, _cy, _rid) in enumerate(items):
                layer = Image.open(_io.BytesIO(blob)).convert("RGBA")
                if (layer.width, layer.height) != (widths[idx], heights[idx]):
                    layer = layer.resize((widths[idx], heights[idx]),
                                         Image.LANCZOS)
                canvas.alpha_composite(layer, (0, y))
                y += heights[idx]
            stamp_im = Image.open(_io.BytesIO(stamp["blob"])).convert("RGBA")
            stamp_im = stamp_im.resize(
                (max(1, int(stamp["cx"] * sx)), max(1, int(stamp["cy"] * sy))),
                Image.LANCZOS)
            canvas.alpha_composite(
                stamp_im, (int(stamp["h"] * sx), int(stamp["v"] * sy)))
            out = _io.BytesIO()
            canvas.convert("RGB").save(out, format="JPEG", quality=88)
            return out.getvalue()
        except Exception:
            return None

    def _next_image_block(self, p_element):
        """本段落之后第一张图所在的「纯图段落」；不适用 → None

        章跨段落续盖用。遇到含文字或含 anchor 的段落即停——不跨越正文与标题，
        保证拼接只发生在同一章节内（否则会改变图片引用的切块归属）；空段落
        最多跳过 _MAX_BLANK_BLOCKS 个，防跨度失控。
        """
        blocks = list(self.doc.element.body.iter(_TAG_P))
        try:
            idx = blocks.index(p_element)
        except ValueError:
            return None
        blanks = 0
        for nxt in blocks[idx + 1:]:
            if list(nxt.iter(_TAG_ANCHOR)):
                return None
            if any((t.text or "").strip() for t in nxt.iter(_TAG_T)):
                return None
            if list(nxt.iter(_TAG_INLINE)):
                return nxt
            blanks += 1
            if blanks > _MAX_BLANK_BLOCKS:
                return None
        return None

    def _sect_pr(self):
        """文档末节 sectPr（页面设置：页宽 / 页边距）"""
        return self.doc.element.body.find(_TAG_SECT_PR)

    def _content_width(self) -> Optional[int]:
        """内容区宽度（EMU）= 页宽 - 左右页边距"""
        return self._sect_len("w")

    def _content_height(self) -> Optional[int]:
        """内容区高度（EMU）= 页高 - 上下页边距"""
        return self._sect_len("h")

    def _sect_len(self, axis: str) -> Optional[int]:
        """页面内容区尺寸（EMU）：axis='w' 宽 / 'h' 高"""
        sect = self._sect_pr()
        if sect is None:
            return None
        pg_sz, mar = sect.find(_TAG_PG_SZ), sect.find(_TAG_PG_MAR)
        if pg_sz is None or mar is None:
            return None
        if axis == "w":
            total = _int_attr(pg_sz, _TAG_W)
            margins = ((_int_attr(mar, _TAG_LEFT) or 0)
                       + (_int_attr(mar, _TAG_RIGHT) or 0))
        else:
            total = _int_attr(pg_sz, _TAG_H)
            margins = ((_int_attr(mar, _TAG_TOP) or 0)
                       + (_int_attr(mar, _TAG_BOTTOM) or 0))
        if total is None:
            return None
        size = (total - margins) * _TWIP_EMU
        return size if size > 0 else None

    def _page_top(self) -> Optional[int]:
        """内容区顶部相对页面顶部的距离（= 上边距，EMU）"""
        sect = self._sect_pr()
        if sect is None:
            return None
        mar = sect.find(_TAG_PG_MAR)
        if mar is None:
            return None
        top = _int_attr(mar, _TAG_TOP)
        return top * _TWIP_EMU if top is not None else None

    def _anchor_offset_v(self, pv, p_element) -> Optional[int]:
        """章相对「锚点段落顶部」的垂直偏移（EMU）；无法确定 → None

        - paragraph：posOffset 本就相对段落顶部，直接可用（精确）；
        - page：posOffset 相对**页面顶部**（与段落排版位置无关的绝对锚定），
          需减去上边距 + 「页首到本段落之前」的累计高度。docx 只存内容与
          格式、不存排版结果，后者只能估算（见 _prior_height）—— 这是本
          路径唯一的近似来源，误差量级 = 前序段落高度的估算误差。
        """
        try:
            off = int(pv.findtext(_TAG_POS_OFFSET) or 0)
        except (TypeError, ValueError):
            return None
        rel = pv.get("relativeFrom")
        if rel == "paragraph":
            return off
        if rel != "page":
            return None
        top = self._page_top()
        prior = self._prior_height(p_element)
        if top is None or prior is None:
            return None
        delta = off - top - prior
        return delta if delta > 0 else None

    def _prior_height(self, p_element) -> Optional[int]:
        """页首（最近硬分页符之后）到本段落之前的累计高度（EMU 估算）

        任一段落高度算不出、或累计已超过一页可用高度（说明本段并不在该页
        顶端，「位于页首」的前提不成立）→ None，由调用方放弃合成。
        """
        blocks = list(self.doc.element.body.iter(_TAG_P))
        try:
            idx = blocks.index(p_element)
        except ValueError:
            return None
        limit = self._content_height()
        total = 0
        for prev in reversed(blocks[:idx]):
            if self._is_page_start(prev):
                break
            height = self._block_height(prev)
            if height is None:
                return None
            total += height
            if limit is not None and total > limit:
                return None
        return total

    @staticmethod
    def _is_page_start(p) -> bool:
        """该段落是否为「页首分界」（含硬分页符，或设置了段前分页）"""
        if any(br.get(_TAG_TYPE) == "page" for br in p.iter(_TAG_BR)):
            return True
        pPr = p.find(_TAG_PPR)
        return pPr is not None and pPr.find(_TAG_PAGE_BREAK_BEFORE) is not None

    def _block_height(self, p) -> Optional[int]:
        """段落占位高度估算（EMU）：含图段落取图高，纯文字段落按行数 × 行高"""
        heights = [cy for cy in
                   (_int_attr(il.find(_TAG_EXTENT), "cy")
                    for il in p.iter(_TAG_INLINE)) if cy]
        if heights:
            return max(heights)
        font, line, before, after = self._para_metrics(p)
        if line is None:
            return None
        text = "".join(t.text or "" for t in p.iter(_TAG_T))
        if not text.strip():
            return line + before + after
        width = self._content_width()
        if width is None or not font:
            return None
        return before + after + line * self._text_lines(text, font, width)

    @staticmethod
    def _text_lines(text: str, font_emu: int, content_w: int) -> int:
        """文本占行数估算（东亚字符按全角、其余按半角计宽，至少 1 行）"""
        if font_emu <= 0 or content_w <= 0:
            return 1
        width = sum(font_emu if ord(ch) > 0x2E7F else font_emu // 2
                    for ch in text)
        return max(1, -(-width // content_w))

    def _para_metrics(self, p) -> Tuple[Optional[int], Optional[int], int, int]:
        """段落生效的 (字号EMU, 行高EMU, 段前EMU, 段后EMU)

        取值优先级：段落自身 → pStyle 样式链（basedOn 上溯）→ docDefaults，
        与 ECMA-376 的继承语义一致。刻意独立于 _Styles（后者只服务标题编号），
        以免改动既有解析路径。行高解析不出时返回 None，调用方放弃合成。
        """
        styles = _styles_of(self.doc)
        found: Dict[str, Optional[int]] = {"sz": None, "line": None,
                                           "rule": None, "before": None,
                                           "after": None}

        def take(rpr, ppr) -> None:
            if found["sz"] is None and rpr is not None:
                found["sz"] = _int_attr(rpr.find(_TAG_SZ))
            if ppr is None:
                return
            sp = ppr.find(_TAG_SPACING)
            if sp is None:
                return
            if found["line"] is None:
                found["line"] = _int_attr(sp, _TAG_LINE)
                found["rule"] = sp.get(_TAG_LINE_RULE)
            if found["before"] is None:
                found["before"] = _int_attr(sp, _TAG_BEFORE)
            if found["after"] is None:
                found["after"] = _int_attr(sp, _TAG_AFTER)

        pPr = p.find(_TAG_PPR)
        take(pPr.find(_TAG_RPR) if pPr is not None else None, pPr)
        if styles is not None:
            by_id = {s.get(_TAG_STYLE_ID): s for s in styles.iter(_TAG_STYLE)
                     if s.get(_TAG_STYLE_ID)}
            sid = None
            if pPr is not None:
                ref = pPr.find(_TAG_PSTYLE)
                sid = ref.get(_TAG_VAL) if ref is not None else None
            seen: set = set()
            while sid and sid not in seen:  # 环防护：损坏文件可能成环
                seen.add(sid)
                node = by_id.get(sid)
                if node is None:
                    break
                take(node.find(_TAG_RPR), node.find(_TAG_PPR))
                parent = node.find(_TAG_BASED_ON)
                sid = parent.get(_TAG_VAL) if parent is not None else None
            defaults = styles.find(_TAG_DOC_DEFAULTS)
            if defaults is not None:
                rpd = defaults.find(_TAG_RPR_DEFAULT)
                take(rpd.find(_TAG_RPR) if rpd is not None else None, None)

        # 字号 w:sz 以半磅计：1 磅 = 12700 EMU → 半磅 = 6350 = _TWIP_EMU × 10
        font = found["sz"] * _TWIP_EMU * 10 if found["sz"] else None
        line = None
        if found["line"] is not None:
            rule = found["rule"]
            if rule in ("exact", "atLeast"):
                line = found["line"] * _TWIP_EMU
            elif font and (rule == "auto" or rule is None):
                line = int(font * found["line"] / 240)  # auto：240 = 单倍行距
        if line is None and font:
            line = int(font * 1.2)  # 兜底：单倍行距经验值
        return (font, line,
                (found["before"] or 0) * _TWIP_EMU,
                (found["after"] or 0) * _TWIP_EMU)
