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

/** 数字串：整数/千分位/小数/多段小数（2026.7.12 这类日期整体匹配） */
const NUMBER_RE = /\d[\d,]*(?:\.\d+)*/g;
/** 数字标记的最小数字位数（忽略逗号小数点）：低于此值不框——
 *  表格引用里 1/2/5 这类满屏都是，框出来纯噪声，反而盖住真正要核对的金额 */
const MIN_NUMBER_DIGITS = 3;

/** 数字归一化：去千分位逗号（回答写 1649.58、引用写 ¥1,649.58 也要能对上） */
const normalizeNumber = (s: string) => s.replace(/,/g, '');
/** 数字位数（忽略逗号与小数点） */
const digitCount = (s: string) => s.replace(/[^\d]/g, '').length;

/**
 * 构建"压缩 + 图片占位"的比对文本及其坐标映射（文字高亮与数字框共用）。
 *
 * - 压缩：去掉全部空白字符，并记录压缩坐标 → 原文坐标的映射；
 * - 图片标签整体替换为等长空格占位：占位符永远不参与匹配，保证任何区间
 *   都不可能覆盖图片标签（渲染时不会切坏 `![alt](src)`）。
 */
function buildComparisonText(text: string): {
  textCmp: string;
  origPos: number[];
  isPlaceholder: boolean[];
} {
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
  return { textCmp: chars.join(''), origPos, isPlaceholder };
}

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
    const { textCmp, origPos, isPlaceholder } = buildComparisonText(text);
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

/**
 * 计算"回答里出现过的数字"在引用文本中的区间（渲染层据此给数字加方框）。
 *
 * 为什么只标回答里出现过的：数字核对是看引用时最高频的动作（"回答里的
 * 792.27 是不是从这儿抄的"），但表格类引用几乎每个格子都是数字——全标等于
 * 一屏方框，反而盖住真正要核对的那个。只标回答里出现过的，一眼就能对上。
 *
 * 归一化：回答写 "1649.58 元"、引用写 "¥1,649.58"，去千分位逗号后可比；
 * 位数过少的（<3 位数字）不参与——表格里 1/2/5 满地都是，标出来纯噪声。
 *
 * @param query 回答文本（消息 content）
 * @param text  引用文本（Source.parent_text / Source.text，原始文本）
 * @returns 数字区间数组（相对 text 原始坐标），无命中/异常返回 []
 */
export function computeNumberRanges(query: string, text: string): HighlightRange[] {
  try {
    if (!query || !text) return [];
    // 回答侧：先剔除 [n] 引用标（那是编号不是数据），再收集归一化数字
    const wanted = new Set<string>();
    for (const m of query.replace(/\[\d+\]/g, ' ').matchAll(NUMBER_RE)) {
      const key = normalizeNumber(m[0]);
      if (digitCount(key) >= MIN_NUMBER_DIGITS) wanted.add(key);
    }
    if (wanted.size === 0) return [];
    // 引用侧：与文字高亮同一套坐标系（图片标签是占位符，绝不切坏它）
    const { textCmp, origPos, isPlaceholder } = buildComparisonText(text);
    const out: HighlightRange[] = [];
    for (const m of textCmp.matchAll(NUMBER_RE)) {
      const i = m.index ?? 0;
      // 图片占位区的数字不算（`image492.png` 里的 492 不是数据）
      if (isPlaceholder[i]) continue;
      if (!wanted.has(normalizeNumber(m[0]))) continue;
      out.push([origPos[i], origPos[i + m[0].length - 1] + 1]);
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
  /** 该段是"回答里出现过的数字"（叠在 highlighted 之上再画方框，见 computeNumberRanges） */
  isNumber?: boolean;
}

/** 摘要开窗时命中前保留的上下文字数 */
const CONTEXT_BEFORE = 120;

/**
 * 摘要开窗：截出"围绕命中"的窗口，并把高亮/数字区间平移到窗口坐标。
 *
 * 背景：引用浮层原先取 `text.slice(0, N)`——命中内容落在块的中后段时
 * （表格块的有效数字几乎都在表格下方），窗口里没有任何可标的标记，用户
 * 看到的是"引用什么也没标"。这里改为：命中靠后时以**首个命中**为中心开窗
 * （命中前留 CONTEXT_BEFORE 字上下文），命中在前部/无命中时维持"从头截"。
 *
 * 开窗位置同时看文字高亮与数字标记——只标了数字、文字窗口没匹配上的情形
 * （回答提到的数字所在段落未被 4 字窗口命中）也要能滚到眼前。
 *
 * @param text       引用原文（未截断）
 * @param highlights 文字高亮区间（相对 text 的原始坐标）
 * @param maxChars   窗口大小（字）
 * @param numbers    数字区间（相对 text 的原始坐标；可选）
 * @returns 窗口文本（两端截断处补省略号）与平移后的两组区间（坐标相对窗口文本）
 */
export function buildSnippet(
  text: string,
  highlights: HighlightRange[],
  maxChars: number,
  numbers: HighlightRange[] = [],
): { text: string; highlights: HighlightRange[]; numbers: HighlightRange[] } {
  if (!text || maxChars <= 0) return { text: '', highlights: [], numbers: [] };
  if (text.length <= maxChars) return { text, highlights, numbers };
  const firstHit = Math.min(
    highlights.length ? highlights[0][0] : Number.POSITIVE_INFINITY,
    numbers.length ? numbers[0][0] : Number.POSITIVE_INFINITY,
  );
  const start = Number.isFinite(firstHit) && firstHit > maxChars - CONTEXT_BEFORE
    ? Math.max(0, firstHit - CONTEXT_BEFORE)  // 命中靠后 → 围绕它开窗
    : 0;                                       // 命中在前部/无命中 → 从头截
  const end = Math.min(text.length, start + maxChars);
  const head = start > 0 ? '…' : '';
  const body = text.slice(start, end);
  const tail = end < text.length ? '…' : '';
  const windowLen = head.length + body.length;
  // 原文坐标 p → 窗口坐标 p - start + head.length
  const shift = head.length - start;
  const project = (ranges: HighlightRange[]): HighlightRange[] => {
    const out: HighlightRange[] = [];
    for (const [s, e] of ranges) {
      const ns = s + shift;
      const ne = e + shift;
      if (ne <= 0 || ns >= windowLen) continue;  // 完全落在窗口外
      out.push([Math.max(0, ns), Math.min(windowLen, ne)]);
    }
    return out;
  };
  return {
    text: head + body + tail,
    highlights: project(highlights),
    numbers: project(numbers),
  };
}

/**
 * 把文本按区间切段并标记每段归属（文字高亮 / 数字）。
 *
 * 两组区间互不隶属（数字可能落在高亮段内，也可能落在两段高亮之间），
 * 故按**全部边界点**切分，再逐段判断它与哪些区间相交——一段可以同时是
 * highlighted 与 isNumber。
 */
export function splitByHighlights(
  text: string,
  highlights: HighlightRange[] | undefined,
  numbers?: HighlightRange[] | undefined,
): HighlightSeg[] {
  if (!text) return [];
  const hl = (highlights ?? []).filter(([s, e]) => e > s);
  const nm = (numbers ?? []).filter(([s, e]) => e > s);
  if (hl.length === 0 && nm.length === 0) {
    return [{ text, highlighted: false }];
  }
  const clamp = (v: number) => Math.max(0, Math.min(text.length, v));
  const cuts = new Set<number>([0, text.length]);
  for (const [s, e] of [...hl, ...nm]) {
    cuts.add(clamp(s));
    cuts.add(clamp(e));
  }
  const points = [...cuts].sort((a, b) => a - b);
  const hit = (ranges: HighlightRange[], s: number, e: number) =>
    ranges.some(([rs, re]) => rs < e && re > s);
  const segs: HighlightSeg[] = [];
  for (let i = 0; i + 1 < points.length; i++) {
    const s = points[i];
    const e = points[i + 1];
    if (e <= s) continue;
    segs.push({
      text: text.slice(s, e),
      highlighted: hit(hl, s, e),
      isNumber: hit(nm, s, e),
    });
  }
  return segs;
}
