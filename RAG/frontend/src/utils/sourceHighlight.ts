/**
 * 引用面板相关文本高亮（回答-引用对齐）
 *
 * 背景：检索链路不保留"命中 span"（Source 只有块级 char_start/char_end），
 * 因此改用"回答文本 ↔ 引用文本"的重叠匹配标出引用里被回答用到的部分——
 * 让用户一眼看到"回答用了这段里的哪一部分"。
 *
 * 算法（简单可靠优先，零 LLM 成本，纯字符串匹配）：
 * 1. 引用文本去空白压缩（记录压缩坐标→原始坐标映射）；图片标签 ![alt](src)
 *    整体替换为等长空格占位（占位符不参与匹配），保证高亮区间绝不切开图片标签；
 * 2. 回答文本同样去空白压缩，构建全部长度为 4 的连续窗口集合；
 * 3. 引用压缩文本滑窗查集合（命中=回答里出现过该连续片段），标记命中位置；
 * 4. 命中位置合并为连续区间（容忍 ≤2 个未命中间隔；间隔含图片占位则断开）；
 * 5. 长度过滤（<4 字忽略）→ 坐标映射回引用原文（半开区间 [start, end)）。
 *
 * 容错：任何异常/空输入返回 []，调用方原样显示，绝不报错。
 */

/** 高亮区间：[start, end) 半开区间，坐标相对引用原文（原始文本） */
export type HighlightRange = [number, number];

/** 图片标签正则（与 MdImages 一致）：![alt](src) */
const IMG_RE = /!\[[^\]]*\]\([^)]+\)/g;

/** 匹配窗口长度：4 字（中文短语粒度；"图灵测试"这类 4 字概念可命中） */
const WINDOW = 4;
/** 区间合并容忍的最大未命中间隔（压缩坐标，字符数） */
const MAX_GAP = 2;

/**
 * 计算回答与引用文本的重叠区间。
 * @param query 回答文本（消息 content）
 * @param text  引用文本（Source.parent_text / Source.text，原始文本）
 * @returns 高亮区间数组（相对 text 原始坐标），无命中/异常返回 []
 */
export function computeHighlightRanges(query: string, text: string): HighlightRange[] {
  try {
    if (!query || !text) return [];

    // ---- 1. 引用文本压缩 + 坐标映射 + 图片占位 ----
    const chars: string[] = [];
    const origPos: number[] = []; // 压缩坐标 → 原始坐标
    const isPlaceholder: boolean[] = []; // 压缩坐标是否为图片占位符
    let prev = 0;
    for (const m of text.matchAll(IMG_RE)) {
      for (let i = prev; i < m.index; i++) {
        const ch = text[i];
        if (ch !== undefined && !/\s/.test(ch)) {
          origPos.push(i);
          chars.push(ch);
          isPlaceholder.push(false);
        }
      }
      // 图片整体替换为等长空格占位：占位符永远不命中集合（回答压缩文本无空格），
      // 且高亮区间（合并不允许跨越占位符）不可能覆盖图片 → 渲染层不会切坏 ![alt](src)
      for (let i = 0; i < m[0].length; i++) {
        origPos.push(m.index + i);
        chars.push(' ');
        isPlaceholder.push(true);
      }
      prev = m.index + m[0].length;
    }
    for (let i = prev; i < text.length; i++) {
      const ch = text[i];
      if (ch !== undefined && !/\s/.test(ch)) {
        origPos.push(i);
        chars.push(ch);
        isPlaceholder.push(false);
      }
    }
    const textCmp = chars.join('');
    if (textCmp.length < WINDOW) return [];

    // ---- 2. 回答压缩 + 4 字窗口集合 ----
    const queryCmp = query.replace(/\s+/g, '');
    if (queryCmp.length < WINDOW) return [];
    const windows = new Set<string>();
    for (let i = 0; i + WINDOW <= queryCmp.length; i++) {
      windows.add(queryCmp.slice(i, i + WINDOW));
    }

    // ---- 3. 引用压缩文本滑窗命中 ----
    const hit = new Array(textCmp.length).fill(false);
    for (let i = 0; i + WINDOW <= textCmp.length; i++) {
      if (isPlaceholder[i]) continue;
      if (windows.has(textCmp.slice(i, i + WINDOW))) {
        for (let k = 0; k < WINDOW; k++) hit[i + k] = true;
      }
    }

    // ---- 4. 合并相邻命中为连续区间 ----
    const ranges: Array<[number, number]> = [];
    let start = -1;
    let end = -1;
    let gap = 0;
    for (let i = 0; i < hit.length; i++) {
      if (hit[i]) {
        if (start === -1) {
          start = i;
          end = i + 1;
          gap = 0;
        } else if (gap <= MAX_GAP) {
          end = i + 1;
          gap = 0;
        } else {
          ranges.push([start, end]);
          start = i;
          end = i + 1;
          gap = 0;
        }
      } else if (start !== -1) {
        if (isPlaceholder[i]) {
          // 间隔含图片占位：断开区间（防高亮覆盖图片标签）
          ranges.push([start, end]);
          start = -1;
          end = -1;
          gap = 0;
        } else {
          gap++;
        }
      }
    }
    if (start !== -1) ranges.push([start, end]);

    // ---- 5. 长度过滤 + 映射回原始坐标 ----
    const out: HighlightRange[] = [];
    for (const [cs, ce] of ranges) {
      if (ce - cs < WINDOW) continue; // 合并后仍不足 4 字的噪声区间忽略
      out.push([origPos[cs], origPos[ce - 1] + 1]);
    }
    return out;
  } catch {
    return [];
  }
}

/** 高亮切分段：把文本按高亮区间切为普通段/高亮段交替（区间自动 clamp 到文本范围） */
export interface HighlightSeg {
  text: string;
  highlighted: boolean;
}

export function splitByHighlights(
  text: string,
  highlights: HighlightRange[] | undefined,
): HighlightSeg[] {
  if (!text) return [];
  if (!highlights || highlights.length === 0) {
    return [{ text, highlighted: false }];
  }
  const sorted = [...highlights].sort((a, b) => a[0] - b[0]);
  const segs: HighlightSeg[] = [];
  let cur = 0;
  for (const [s, e] of sorted) {
    const start = Math.max(0, s);
    const end = Math.min(text.length, e);
    if (end <= cur || end <= start) continue;
    if (start > cur) segs.push({ text: text.slice(cur, start), highlighted: false });
    segs.push({ text: text.slice(start, end), highlighted: true });
    cur = end;
  }
  if (cur < text.length) segs.push({ text: text.slice(cur), highlighted: false });
  return segs;
}
