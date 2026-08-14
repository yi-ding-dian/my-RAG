import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Empty, Input, List, Pagination, Tag, Typography, theme } from 'antd';
import { ArrowDownOutlined, ArrowUpOutlined, SearchOutlined } from '@ant-design/icons';
import { useTheme } from '../theme';
import MdImages from './MdImages';
import { safeTruncateWithImages } from '../utils/safeTruncate';

const { Text } = Typography;

/** 左栏切块文本截断长度 */
const MAX_TEXT_LEN = 200;
/** 左右栏分页大小（左右两栏共用同一页码：右栏只渲染当前页块的原文区间） */
const PAGE_SIZE = 30;
/** 左右两栏图片统一显示高度上限（同一张图在两栏显示尺寸一致；宽度随比例，超容器时再受 max-width 100% 约束） */
const MAX_IMG_HEIGHT = 160;
/** 全文搜索匹配上限（防超大文档导致渲染爆炸；超出部分不再计入） */
const MAX_MATCHES = 500;

export interface ChunkViewChunk {
  index: number;
  text: string;
  /** 原文偏移（可选；缺失或非法时该块不参与对比高亮） */
  char_start?: number | null;
  char_end?: number | null;
  /** 上下文摘要（上下文检索增强开启时生成；仅展示，不影响偏移与原文对比） */
  context?: string | null;
}

interface ChunkCompareViewProps {
  /** 切块列表（含可选偏移） */
  chunks: ChunkViewChunk[];
  /** 文档全文（可选） */
  fullText?: string;
  /** 挂载后自动定位的切块下标（引用溯源用；缺省取第一块） */
  initialIndex?: number;
  /** 撑满父容器高度（放大态弹窗使用；默认固定高度） */
  fillHeight?: boolean;
}

interface RawChunk {
  index: number;
  start: number;
  end: number;
}

interface Segment {
  /** 段在原文中的起止偏移 */
  start: number;
  end: number;
  text: string;
  /** 覆盖该段的切块 index（重叠区间会属于多块） */
  chunkIndexes: number[];
}

/** 全文搜索匹配（原文偏移区间，不含图片引用内部的匹配） */
interface SearchMatch {
  start: number;
  end: number;
}

/** 右栏原文区间（段或非块区间，均带可定位 id） */
interface RangeSpan {
  start: number;
  end: number;
  id: string;
}

/** 全文图片引用完整区间（![..](..)，含左右括号），按原文顺序返回 */
const collectImgSpans = (fullText: string): Array<{ start: number; end: number }> => {
  const spans: Array<{ start: number; end: number }> = [];
  // 与 MdImages 使用同一匹配模式（局部正则，无共享 lastIndex）
  const re = /!\[([^\]]*)\]\(([^)]+)\)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(fullText)) !== null) {
    spans.push({ start: m.index, end: m.index + m[0].length });
  }
  return spans;
};

/**
 * 将全文按所有 chunks 的 char_start/char_end 切分为互斥段：
 * 只生成"至少被一个 chunk 覆盖"的段，段之间自然留出非块原文。
 * 越界/非法偏移会被 clamp 或过滤。
 *
 * 图片引用完整性：段边界若落在某个 ![..](..) 引用内部，引用会被切成两半，
 * 两段各自匹配不到完整引用 → 图片不显示。因此切段后对边界做扩展——
 * 边界落入引用内部时前移/后移到引用两端，把完整引用纳入某一段
 * （扩展后相邻段可能出现极短重叠，随即合并保持互斥）。
 */
const buildSegments = (chunks: ChunkViewChunk[], fullText: string): Segment[] => {
  const len = fullText.length;
  const valid: RawChunk[] = [];
  for (const c of chunks) {
    if (typeof c.char_start !== 'number' || typeof c.char_end !== 'number') continue;
    if (!Number.isFinite(c.char_start) || !Number.isFinite(c.char_end)) continue;
    const start = Math.max(0, Math.min(Math.round(c.char_start), len));
    const end = Math.max(0, Math.min(Math.round(c.char_end), len));
    if (end <= start) continue;
    valid.push({ index: c.index, start, end });
  }
  if (valid.length === 0) return [];
  const bounds = Array.from(new Set(valid.flatMap(c => [c.start, c.end]))).sort((a, b) => a - b);
  const segments: Segment[] = [];
  for (let i = 0; i < bounds.length - 1; i++) {
    const s = bounds[i];
    const e = bounds[i + 1];
    const chunkIndexes = valid.filter(c => c.start <= s && e <= c.end).map(c => c.index);
    if (chunkIndexes.length > 0) {
      segments.push({ start: s, end: e, text: fullText.slice(s, e), chunkIndexes });
    }
  }
  // —— 图片引用完整性扩展 ——
  const spans = collectImgSpans(fullText);
  if (spans.length > 0) {
    for (const seg of segments) {
      for (const sp of spans) {
        // 段起点落在引用内部 → 前移到引用起点；段终点落在引用内部 → 后移到引用终点
        if (sp.start < seg.start && seg.start < sp.end) seg.start = sp.start;
        if (sp.start < seg.end && seg.end < sp.end) seg.end = sp.end;
      }
      seg.text = fullText.slice(seg.start, seg.end);
    }
    // 扩展后相邻段可能重叠：后段起点前移到前段末尾，保持互斥（key/id 唯一）
    const merged: Segment[] = [];
    for (const seg of segments) {
      const prev = merged[merged.length - 1];
      if (prev && seg.start < prev.end) {
        seg.start = prev.end;
        if (seg.end <= seg.start) continue; // 整段被前段扩展覆盖：文本仍由前段显示
        seg.text = fullText.slice(seg.start, seg.end);
      }
      merged.push(seg);
    }
    return merged;
  }
  return segments;
};

/**
 * 切块对比视图：左栏切块列表 + 右栏原文高亮，双向联动，左右同页分页。
 * 左右两栏共用同一页码（左栏第 N 页的 30 个切片 ↔ 右栏这 30 个切片的原文区间）：
 * 右栏把原文按当前页 chunks 偏移渲染为区间段，翻页两边同步切换。
 * 选中块区间主色 15% 高亮 + 块号角标；
 * 点击左栏块 → 右侧区间高亮并平滑滚动居中；点击原文区间/角标 → 左侧对应块选中并滚动。
 * 任一侧手动滚动（防抖 100ms）→ 另一侧自动滚动到视口顶部对应位置（程序滚动期间
 * 通过标志抑制回环，避免来回抖动）。
 * 顶部搜索框：全文（含非块区间）匹配，Enter 下一个 / Shift+Enter 上一个，
 * 当前匹配主色高亮、其余浅色高亮，定位时左右栏同步跳转（跨页自动切页后统一定位）。
 * full_text 为空或 chunks 无偏移时降级为纯列表 + "暂无原文预览"。
 */
const ChunkCompareView: React.FC<ChunkCompareViewProps> = ({ chunks, fullText, initialIndex, fillHeight }) => {
  const { token } = theme.useToken();
  // 当前主题主色：块/区间高亮跟随预设（原硬编码 #2563eb）
  const { preset } = useTheme();
  const highlight = preset.colorPrimary;
  // 初始选中：优先 initialIndex（引用溯源定位），不存在（数据不一致）时回退第一块
  const [selectedIndex, setSelectedIndex] = useState<number | null>(() => {
    if (initialIndex != null && chunks.some(c => c.index === initialIndex)) return initialIndex;
    return chunks[0]?.index ?? null;
  });
  // 左右栏共用分页页码（左栏展示当前页切片；右栏渲染当前页切片的原文区间）
  const [page, setPage] = useState(1);
  // 全文搜索
  const [searchText, setSearchText] = useState('');
  const [currentMatchIdx, setCurrentMatchIdx] = useState(0);
  // 当前定位结果对应的搜索词：搜索词变化后首次 Enter/按钮操作应先定位第 1 处，
  // 而非从重置后的序号（0）继续"下一个"跳到第 2 处
  const lastGotoTextRef = useRef('');

  // 左右滚动容器 ref（联动读取视口顶部对应元素用）
  const leftScrollRef = useRef<HTMLDivElement | null>(null);
  const rightScrollRef = useRef<HTMLDivElement | null>(null);
  // 引用溯源：挂载后把右侧原文平滑滚动到目标块区间居中（仅挂载时一次）
  const initialScrolledRef = useRef(false);
  // 程序滚动标志：scrollIntoView 触发的滚动不参与对方联动（防回环抖动）；
  // 平滑动画可能持续数百 ms，用定时器在动画窗口后自动解除
  const programmaticRef = useRef(false);
  const programmaticTimerRef = useRef<number | null>(null);
  // 滚动联动防抖定时器（100ms）
  const debounceTimerRef = useRef<number | null>(null);
  // 待滚动的左栏块 index：跨页联动/跳页后，等目标页渲染完成再滚动（块 DOM 尚不存在）
  const pendingLeftRef = useRef<number | null>(null);
  // 待滚动的右栏原文位置（跨页搜索定位/引用溯源定位：目标页渲染完成后滚到该位置所在区间）
  const pendingRightRef = useRef<number | null>(null);

  /** 标记一次程序滚动：期间另一侧滚动不触发联动 */
  const setProgrammatic = useCallback(() => {
    programmaticRef.current = true;
    if (programmaticTimerRef.current) window.clearTimeout(programmaticTimerRef.current);
    programmaticTimerRef.current = window.setTimeout(() => {
      programmaticRef.current = false;
    }, 400);
  }, []);

  /** 滚动联动防抖：用户滚动触发（程序滚动期间直接忽略） */
  const scheduleSync = useCallback((fn: () => void) => {
    if (programmaticRef.current) return;
    if (debounceTimerRef.current) window.clearTimeout(debounceTimerRef.current);
    debounceTimerRef.current = window.setTimeout(() => {
      if (!programmaticRef.current) fn();
    }, 100);
  }, []);

  // 组件卸载清理定时器
  useEffect(() => {
    return () => {
      if (debounceTimerRef.current) window.clearTimeout(debounceTimerRef.current);
      if (programmaticTimerRef.current) window.clearTimeout(programmaticTimerRef.current);
    };
  }, []);

  // 跨页联动：目标页渲染完成后滚动到 pending 元素（左栏块 nearest / 右栏区间 center）
  useEffect(() => {
    const leftIdx = pendingLeftRef.current;
    const rightPos = pendingRightRef.current;
    if (leftIdx == null && rightPos == null) return;
    pendingLeftRef.current = null;
    pendingRightRef.current = null;
    requestAnimationFrame(() => {
      if (leftIdx != null) {
        const el = document.getElementById(`chunk-list-${leftIdx}`);
        if (el) {
          setProgrammatic();
          // 即时滚动：平滑动画超出程序滚动窗口会误触发右栏联动，把右栏定位拉走
          el.scrollIntoView({ block: 'nearest', behavior: 'auto' });
        }
      }
      if (rightPos != null) {
        const id = rangeIdOf(rightPos);
        if (id) {
          setProgrammatic();
          document.getElementById(id)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  // 按偏移排序（无偏移的排在后面），保证段构建与角标顺序稳定
  const sortedChunks = useMemo(
    () =>
      [...chunks].sort(
        (a, b) =>
          (typeof a.char_start === 'number' ? a.char_start : Infinity) -
            (typeof b.char_start === 'number' ? b.char_start : Infinity) || a.index - b.index,
      ),
    [chunks],
  );

  // 分页派生数据（当前页块；左右两栏共用同一页码）
  const pageChunks = useMemo(
    () => sortedChunks.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [sortedChunks, page],
  );

  // 右栏段基于当前页块构建：右栏只渲染当前页块的原文区间（左右分页同步）
  const segments = useMemo(
    () => (fullText ? buildSegments(pageChunks, fullText) : []),
    [pageChunks, fullText],
  );

  /**
   * 包含给定原文位置的段（段边界做过图片引用完整性扩展，seg.start 可能
   * 已前移，不能直接用"块起点 == seg.start"反查 id）
   */
  const segOf = useCallback(
    (pos: number): Segment | undefined => segments.find(s => s.start <= pos && pos < s.end),
    [segments],
  );

  // 挂载：初始页定为"初始选中块"（initialIndex 优先，否则第一块）所在页，保证目标块可见
  useEffect(() => {
    const target =
      initialIndex != null && chunks.some(c => c.index === initialIndex)
        ? initialIndex
        : chunks[0]?.index ?? null;
    const idx = sortedChunks.findIndex(c => c.index === target);
    if (idx >= 0) setPage(Math.floor(idx / PAGE_SIZE) + 1);
    // 仅挂载时一次（chunks/sortedChunks 后续变化不重置分页）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 引用溯源：挂载后定位到目标块（初始页已切到该块所在页）。
  // 右栏分页后目标段可能不在当前页 DOM：目标块在第 1 页（挂载默认页）时直接滚动，
  // 否则记 pending，等 [page] effect 在切页渲染完成后统一滚动左右两侧。
  useEffect(() => {
    if (initialIndex == null || initialScrolledRef.current) return;
    initialScrolledRef.current = true;
    const chunk = chunks.find(c => c.index === initialIndex);
    if (!chunk || !fullText || typeof chunk.char_start !== 'number' || !Number.isFinite(chunk.char_start)) return;
    const start = Math.max(0, Math.min(Math.round(chunk.char_start), fullText.length));
    const idx = sortedChunks.findIndex(c => c.index === initialIndex);
    if (idx < 0) return;
    if (Math.floor(idx / PAGE_SIZE) + 1 === 1) {
      // 目标块在第 1 页：DOM 已就绪，直接滚动（仅挂载时一次）
      window.setTimeout(() => {
        const seg = segOf(start);
        setProgrammatic();
        document.getElementById(`chunk-seg-${seg ? seg.start : start}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        // 左栏同步滚到目标块（即时滚动防联动回拉）
        document.getElementById(`chunk-list-${initialIndex}`)?.scrollIntoView({ block: 'nearest', behavior: 'auto' });
      }, 0);
      return;
    }
    // 目标块不在第 1 页：初始页 effect 已 setPage(目标页)，切页渲染后由 [page] effect 定位
    pendingLeftRef.current = initialIndex;
    pendingRightRef.current = start;
    // 仅挂载时定位一次（弹窗场景由外部 key 重挂载驱动）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const canCompare = !!fullText && segments.length > 0;

  // 右栏全部可定位区间（段 + 当前页覆盖范围内的非块区间，按原文顺序排列，与渲染一致；
  // 只覆盖当前页块的原文区间，页外/文档首尾非块原文不属于右栏渲染范围）
  const orderedRanges = useMemo<RangeSpan[]>(() => {
    if (!fullText || segments.length === 0) return [];
    const ranges: RangeSpan[] = [];
    let cursor = segments[0].start;
    for (const seg of segments) {
      if (seg.start > cursor) ranges.push({ start: cursor, end: seg.start, id: `chunk-plain-${cursor}` });
      ranges.push({ start: seg.start, end: seg.end, id: `chunk-seg-${seg.start}` });
      cursor = seg.end;
    }
    return ranges;
  }, [segments, fullText]);

  /** 包含给定原文位置的区间 id（找不到时回退最后一个区间，越界兜底） */
  const rangeIdOf = useCallback(
    (pos: number): string | undefined => {
      const r = orderedRanges.find(r => r.start <= pos && pos < r.end);
      return r ? r.id : orderedRanges[orderedRanges.length - 1]?.id;
    },
    [orderedRanges],
  );

  /** 某块在原文中的起点（clamp 后），用于段定位与角标归属判断 */
  const chunkStartOf = useCallback(
    (chunk: ChunkViewChunk): number | null => {
      if (!fullText || typeof chunk.char_start !== 'number' || !Number.isFinite(chunk.char_start)) return null;
      return Math.max(0, Math.min(Math.round(chunk.char_start), fullText.length));
    },
    [fullText],
  );

  /** 覆盖给定原文位置的块（取排序后第一个；非块区间返回 undefined） */
  const chunkCoveringOf = useCallback(
    (pos: number): ChunkViewChunk | undefined => {
      if (!fullText) return undefined;
      const len = fullText.length;
      return sortedChunks.find(c => {
        if (typeof c.char_start !== 'number' || typeof c.char_end !== 'number') return false;
        if (!Number.isFinite(c.char_start) || !Number.isFinite(c.char_end)) return false;
        const s = Math.max(0, Math.min(Math.round(c.char_start), len));
        const e = Math.max(0, Math.min(Math.round(c.char_end), len));
        return s <= pos && pos < e;
      });
    },
    [sortedChunks, fullText],
  );

  /** 与 pos 距离最近的块（覆盖块优先；非块空隙匹配找不到直接覆盖块时，取 char_start <= pos 的最后一个块
   *  作为跨页定位目标——该块所在页的段间空隙最可能包含 pos） */
  const nearestChunkOf = useCallback(
    (pos: number): ChunkViewChunk | undefined => {
      const covering = chunkCoveringOf(pos);
      if (covering) return covering;
      let prev: ChunkViewChunk | undefined;
      for (const c of sortedChunks) {
        if (typeof c.char_start !== 'number' || !Number.isFinite(c.char_start)) continue;
        if (Math.round(c.char_start) <= pos) prev = c;
        else break;
      }
      return prev ?? sortedChunks[0];
    },
    [chunkCoveringOf, sortedChunks],
  );

  /** 左栏滚动到指定块：当前页直接滚；跨页自动跳页并在渲染后滚动 */
  const scrollLeftToChunk = useCallback(
    (index: number, block: ScrollLogicalPosition = 'nearest', behavior: ScrollBehavior = 'auto') => {
      const el = document.getElementById(`chunk-list-${index}`);
      if (el) {
        setProgrammatic();
        el.scrollIntoView({ block, behavior });
        return;
      }
      // 块不在当前页：记下 pending，切页后 effect 滚动
      const idx = sortedChunks.findIndex(c => c.index === index);
      if (idx < 0) return;
      pendingLeftRef.current = index;
      setPage(Math.floor(idx / PAGE_SIZE) + 1);
    },
    [sortedChunks],
  );

  /** 点击左栏块：选中 + 右侧对应区间平滑滚动居中 */
  const selectChunk = useCallback(
    (chunk: ChunkViewChunk) => {
      setSelectedIndex(chunk.index);
      const start = chunkStartOf(chunk);
      if (start == null) return;
      const seg = segOf(start);
      // 段始终渲染（选中仅换色），等 React flush 后再滚动；
      // 程序滚动期间抑制另一侧联动（防来回抖动）
      setProgrammatic();
      window.setTimeout(() => {
        document.getElementById(`chunk-seg-${seg ? seg.start : start}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }, 0);
    },
    [chunkStartOf, segOf],
  );

  /** 点击原文段（反向联动）：选中所属第一个块并滚动左栏（跨页自动跳页） */
  const handleSegmentClick = useCallback(
    (seg: Segment) => {
      if (seg.chunkIndexes.length > 0) {
        setSelectedIndex(seg.chunkIndexes[0]);
        scrollLeftToChunk(seg.chunkIndexes[0]);
      }
    },
    [scrollLeftToChunk],
  );

  /** 右栏手动滚动 → 左栏同步：可视顶部段落所属块滚到左栏可视顶部（nearest） */
  const syncLeftFromRight = useCallback(() => {
    if (programmaticRef.current) return;
    const container = rightScrollRef.current;
    if (!container) return;
    const containerTop = container.getBoundingClientRect().top;
    for (const seg of segments) {
      const el = document.getElementById(`chunk-seg-${seg.start}`);
      if (!el) continue;
      // 第一个跨过可视区顶部的段 = 当前可视顶部段落
      if (el.getBoundingClientRect().bottom > containerTop + 8) {
        const idx = seg.chunkIndexes[0];
        if (idx != null) scrollLeftToChunk(idx, 'nearest', 'auto');
        break;
      }
    }
  }, [segments, scrollLeftToChunk]);

  /** 左栏手动滚动 → 右栏同步：可视顶部块对应段滚到右栏可视顶部（nearest） */
  const syncRightFromLeft = useCallback(() => {
    if (programmaticRef.current) return;
    const container = leftScrollRef.current;
    if (!container) return;
    const containerTop = container.getBoundingClientRect().top;
    for (const c of sortedChunks) {
      const el = document.getElementById(`chunk-list-${c.index}`);
      if (!el) continue; // 跨页块不在当前 DOM，跳过
      // 第一个跨过可视区顶部的块 = 当前可视顶部块
      if (el.getBoundingClientRect().bottom > containerTop + 8) {
        const start = chunkStartOf(c);
        if (start == null) continue; // 无偏移块无法定位右栏，继续找下一个
        const seg = segOf(start);
        const target = document.getElementById(`chunk-seg-${seg ? seg.start : start}`);
        if (target) {
          setProgrammatic();
          target.scrollIntoView({ block: 'nearest', behavior: 'auto' });
        }
        break;
      }
    }
  }, [sortedChunks, chunkStartOf, segOf]);

  // ---------- 全文搜索 ----------

  /** 全文匹配列表（小写化后 indexOf 循环，不重叠；上限 MAX_MATCHES 防爆） */
  const searchMatches = useMemo<SearchMatch[]>(() => {
    const q = searchText.trim();
    if (!q || !fullText) return [];
    const lower = fullText.toLowerCase();
    const ql = q.toLowerCase();
    const out: SearchMatch[] = [];
    let i = 0;
    for (;;) {
      const idx = lower.indexOf(ql, i);
      if (idx < 0 || out.length >= MAX_MATCHES) break;
      out.push({ start: idx, end: idx + ql.length });
      i = idx + ql.length;
    }
    return out;
  }, [searchText, fullText]);

  /** 取 [lo, hi) 原文区间内的匹配（匹配按 start 有序，二分定位避免逐段全量过滤；
   *  跨区间边界的匹配（start < lo 或 end > hi）无法在本片段内完整渲染，一并排除） */
  const matchesInRange = useCallback(
    (lo: number, hi: number): SearchMatch[] => {
      if (searchMatches.length === 0 || hi <= lo) return [];
      let l = 0;
      let r = searchMatches.length;
      while (l < r) {
        const mid = (l + r) >> 1;
        if (searchMatches[mid].end <= lo) l = mid + 1;
        else r = mid;
      }
      const from = l;
      l = from;
      r = searchMatches.length;
      while (l < r) {
        const mid = (l + r) >> 1;
        if (searchMatches[mid].start < hi) l = mid + 1;
        else r = mid;
      }
      return searchMatches.slice(from, l).filter(m => m.start >= lo && m.end <= hi);
    },
    [searchMatches],
  );

  /** 定位到第 n 个匹配（n 越界 clamp）：右栏滚到所在区间居中 + 左栏选中覆盖块并滚动。
   *  右栏与左栏同页分页后，匹配不在当前页渲染范围时先切页，渲染完成后由 [page] effect 定位。 */
  const gotoMatch = useCallback(
    (n: number) => {
      if (searchMatches.length === 0) return;
      lastGotoTextRef.current = searchText;
      const idx = Math.max(0, Math.min(n, searchMatches.length - 1));
      setCurrentMatchIdx(idx);
      const m = searchMatches[idx];
      const chunk = chunkCoveringOf(m.start);
      if (chunk) setSelectedIndex(chunk.index);
      // 匹配是否落在当前页渲染范围（右栏只渲染当前页块的原文区间）
      const inPage =
        segments.length > 0 && m.start >= segments[0].start && m.start < segments[segments.length - 1].end;
      if (inPage) {
        // 右栏：滚动到匹配所在区间（段或非块区间），block center 保证可见
        const id = rangeIdOf(m.start);
        if (id) {
          setProgrammatic();
          document.getElementById(id)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
        // 左栏：命中块覆盖该位置则选中并滚动（左栏用即时滚动 auto：
        // 平滑动画超出程序滚动窗口会误触发右栏联动，把搜索结果拉走）
        if (chunk) scrollLeftToChunk(chunk.index, 'nearest', 'auto');
        return;
      }
      // 跨页：切到匹配所在页（覆盖块所在页；空隙匹配取最近块），渲染后统一定位
      const target = chunk ?? nearestChunkOf(m.start);
      if (target) {
        pendingRightRef.current = m.start;
        scrollLeftToChunk(target.index, 'nearest', 'auto');
      }
    },
    [searchMatches, searchText, segments, rangeIdOf, chunkCoveringOf, nearestChunkOf, scrollLeftToChunk],
  );

  /** 下一个匹配（搜索词变化后的首次操作先定位第 1 处） */
  const nextMatch = useCallback(() => {
    if (searchMatches.length === 0) return;
    if (lastGotoTextRef.current !== searchText) gotoMatch(0);
    else gotoMatch(currentMatchIdx + 1);
  }, [searchMatches, searchText, currentMatchIdx, gotoMatch]);

  /** 上一个匹配（搜索词变化后的首次操作先定位第 1 处） */
  const prevMatch = useCallback(() => {
    if (searchMatches.length === 0) return;
    if (lastGotoTextRef.current !== searchText) gotoMatch(0);
    else gotoMatch(currentMatchIdx - 1);
  }, [searchMatches, searchText, currentMatchIdx, gotoMatch]);

  /**
   * 文本片段渲染：与 MdImages 图片渲染共存——图片引用区间交给 MdImages，
   * 文本区间按搜索匹配包 <mark>（当前匹配主色底白字，其余浅色底）。
   * base 为该片段在全文中的起始偏移（匹配位置为全文绝对偏移）。
   */
  const renderHighlighted = (text: string, base: number): React.ReactNode => {
    if (!text) return null;
    const currentMatch = searchMatches[currentMatchIdx] as SearchMatch | undefined;
    const imgSpans = collectImgSpans(text);
    const nodes: React.ReactNode[] = [];
    let cursor = 0;
    let n = 0;
    // 文本区间（不含图片引用）：按匹配包 <mark>（当前匹配主色底白字，其余浅色底）
    const pushPiece = (piece: string, lo: number) => {
      if (!piece) return;
      const matches = matchesInRange(lo, lo + piece.length);
      if (matches.length === 0) {
        nodes.push(<React.Fragment key={`t${n++}`}>{piece}</React.Fragment>);
        return;
      }
      let c2 = 0;
      for (const m of matches) {
        const ls = m.start - lo;
        const le = m.end - lo;
        if (ls > c2) nodes.push(<React.Fragment key={`t${n++}`}>{piece.slice(c2, ls)}</React.Fragment>);
        const isCurrent = currentMatch != null && m.start === currentMatch.start;
        nodes.push(
          <mark
            key={`m${n++}`}
            style={{
              backgroundColor: isCurrent ? highlight : `${highlight}33`,
              color: isCurrent ? '#fff' : 'inherit',
              borderRadius: 2,
              padding: '0 1px',
            }}
          >
            {piece.slice(ls, le)}
          </mark>,
        );
        c2 = le;
      }
      if (c2 < piece.length) nodes.push(<React.Fragment key={`t${n++}`}>{piece.slice(c2)}</React.Fragment>);
    };
    // 图片引用区间交给 MdImages（图片优先），两侧文本区间做搜索高亮
    for (const sp of imgSpans) {
      if (sp.start > cursor) pushPiece(text.slice(cursor, sp.start), base + cursor);
      nodes.push(<MdImages key={`i${n++}`} text={text.slice(sp.start, sp.end)} maxHeight={MAX_IMG_HEIGHT} />);
      cursor = sp.end;
    }
    if (cursor < text.length) pushPiece(text.slice(cursor), base + cursor);
    return nodes;
  };

  /** 右栏原文：分段渲染（块区间 span 带 data-chunk-index + 角标，非块区间普通 span）。
   *  与左栏同页分页：只渲染当前页块的覆盖区间 [首段起点, 尾段终点]，文档首尾非块原文不渲染 */
  const renderOriginal = () => {
    if (!canCompare || !fullText) return null;
    const parts: React.ReactNode[] = [];
    let cursor = segments[0]?.start ?? 0;
    segments.forEach(seg => {
      // 非块区间：普通文本（含 markdown 图片引用渲染为真实图片；搜索命中高亮）
      if (seg.start > cursor) {
        parts.push(
          <span key={`plain-${cursor}`} id={`chunk-plain-${cursor}`}>
            {renderHighlighted(fullText.slice(cursor, seg.start), cursor)}
          </span>,
        );
      }
      const selected = selectedIndex != null && seg.chunkIndexes.includes(selectedIndex);
      // 角标：该块起点落在本段区间内时显示其 #index（点击可直接选中该块）。
      // 段边界经过图片引用完整性扩展后，块起点可能不再等于段起点，改用区间包含判断。
      const starters = seg.chunkIndexes.filter(idx => {
        const chunk = sortedChunks.find(c => c.index === idx);
        if (!chunk) return false;
        const cs = chunkStartOf(chunk);
        return cs != null && cs >= seg.start && cs < seg.end;
      });
      parts.push(
        <span
          key={`seg-${seg.start}`}
          id={`chunk-seg-${seg.start}`}
          data-chunk-index={seg.chunkIndexes.join(',')}
          onClick={() => handleSegmentClick(seg)}
          style={{
            backgroundColor: selected ? `${highlight}26` : `${highlight}0d`,
            borderRadius: 3,
            cursor: 'pointer',
            transition: 'background-color 0.2s',
          }}
        >
          {starters.map(idx => (
            <span
              key={`badge-${idx}`}
              onClick={e => {
                e.stopPropagation();
                setSelectedIndex(idx);
              }}
              title={`切块 #${idx}`}
              style={{
                display: 'inline-block',
                fontSize: 10,
                lineHeight: '14px',
                padding: '0 4px',
                marginRight: 4,
                borderRadius: 4,
                color: selectedIndex === idx ? '#fff' : highlight,
                backgroundColor: selectedIndex === idx ? highlight : `${highlight}1f`,
                cursor: 'pointer',
                verticalAlign: 'middle',
                userSelect: 'none',
              }}
            >
              #{idx}
            </span>
          ))}
          {renderHighlighted(seg.text, seg.start)}
        </span>,
      );
      cursor = seg.end;
    });
    // 尾部非块原文不渲染（右栏只显示当前页块的覆盖区间，文档尾部原文属于其他页）
    return parts;
  };

  /** 搜索词变化：重置当前匹配序号到第一个，并清除定位记录（首次操作定位第 1 处） */
  const handleSearchChange = (value: string) => {
    setSearchText(value);
    setCurrentMatchIdx(0);
    lastGotoTextRef.current = '';
  };

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: fillHeight ? '100%' : 'calc(70vh - 150px)',
        minHeight: 420,
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 12,
          flexShrink: 0,
          flexWrap: 'wrap',
        }}
      >
        <Text strong>共 {chunks.length} 块</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {canCompare ? '点击左右侧双向联动，滚动任一侧自动同步；支持全文搜索' : '原文预览不可用，仅展示切块列表'}
        </Text>
        {/* 全文搜索：搜索词变化重置匹配序号；Enter 下一个 / Shift+Enter 上一个 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginLeft: 'auto' }}>
          <Input
            size="small"
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索所有切块内容"
            value={searchText}
            onChange={e => handleSearchChange(e.target.value)}
            onPressEnter={e => {
              if (e.shiftKey) prevMatch();
              else nextMatch();
            }}
            style={{ width: 200 }}
          />
          {searchText.trim().length > 0 && searchMatches.length === 0 && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              未找到
            </Text>
          )}
          {searchMatches.length > 0 && (
            <>
              <Text type="secondary" style={{ fontSize: 12 }}>
                第 {Math.min(currentMatchIdx + 1, searchMatches.length)}/{searchMatches.length} 处
              </Text>
              <Button
                size="small"
                icon={<ArrowUpOutlined />}
                disabled={searchMatches.length === 0}
                onClick={prevMatch}
              />
              <Button
                size="small"
                icon={<ArrowDownOutlined />}
                disabled={searchMatches.length === 0}
                onClick={nextMatch}
              />
            </>
          )}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 16, flex: 1, minHeight: 0 }}>
        {/* 左栏：切块列表（约 45%，分页器控制左右共用页码） */}
        <div style={{ width: '45%', minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <div style={{ flexShrink: 0, marginBottom: 8 }}>
            <Pagination
              size="small"
              current={page}
              total={sortedChunks.length}
              pageSize={PAGE_SIZE}
              showSizeChanger={false}
              onChange={p => {
                setPage(p);
                // 切页后若选中块在当前页，滚动到它（跨页联动同样走 pending 机制）
                if (selectedIndex != null) pendingLeftRef.current = selectedIndex;
              }}
            />
          </div>
          <div
            ref={leftScrollRef}
            onScroll={() => scheduleSync(syncRightFromLeft)}
            style={{ flex: 1, minHeight: 0, overflowY: 'auto', paddingRight: 4 }}
          >
            <List
              dataSource={pageChunks}
              locale={{ emptyText: <Empty description="暂无切块数据" /> }}
              renderItem={c => {
                const selected = c.index === selectedIndex;
                return (
                  <List.Item
                    id={`chunk-list-${c.index}`}
                    onClick={() => selectChunk(c)}
                    style={{
                      cursor: 'pointer',
                      alignItems: 'flex-start',
                      gap: 8,
                      padding: '8px 10px',
                      marginBottom: 6,
                      borderRadius: 6,
                      background: selected ? `${highlight}14` : 'transparent',
                      outline: selected ? `1px solid ${highlight}` : '1px solid transparent',
                      transition: 'background-color 0.2s',
                    }}
                  >
                    <Tag color={selected ? 'blue' : 'default'} style={{ flexShrink: 0, marginTop: 2 }}>
                      #{c.index}
                    </Tag>
                    <div style={{ minWidth: 0, flex: 1 }}>
                      {/* 上下文摘要标签（上下文检索增强开启时生成；向量化/检索文本含【上下文】前缀，
                          此处仅展示摘要本体，原文对比仍以 full_text + 偏移为准） */}
                      {c.context && (
                        <div
                          style={{
                            marginBottom: 4,
                            padding: '3px 6px',
                            borderRadius: 4,
                            background: `${highlight}0d`,
                            border: `1px solid ${highlight}22`,
                            color: token.colorText,
                            fontSize: 12,
                            lineHeight: 1.5,
                          }}
                        >
                          <Tag color="purple" style={{ marginRight: 4, fontSize: 11 }}>
                            上下文
                          </Tag>
                          {c.context}
                        </div>
                      )}
                      {/* 字号与右栏原文统一（13px / 1.7）：左右对照时行高/字号一致，便于逐行比对 */}
                      <Text
                        type="secondary"
                        style={{
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                          fontSize: 13,
                          lineHeight: 1.7,
                        }}
                      >
                        {/* 左栏同样渲染图片（与右栏同一显示规则：max-width 100% + 统一高度上限，
                            保证同一张图左右显示尺寸一致），用户无需点开即可确认块内图片；
                            安全截断：截断点切在图片引用内时后移到引用闭合后，避免引用不完整导致图片失配显示为文本 */}
                        <MdImages
                          text={c.text ? safeTruncateWithImages(c.text, MAX_TEXT_LEN) : c.text}
                          maxWidth="100%"
                          maxHeight={MAX_IMG_HEIGHT}
                        />
                      </Text>
                    </div>
                  </List.Item>
                );
              }}
            />
          </div>
        </div>
        {/* 右栏：原文面板（约 55%，只渲染当前页块的原文区间，与左栏同页同步） */}
        <div
          ref={rightScrollRef}
          onScroll={() => scheduleSync(syncLeftFromRight)}
          style={{
            width: '55%',
            minWidth: 0,
            overflowY: 'auto',
            border: `1px solid ${token.colorBorderSecondary}`,
            borderRadius: 8,
            padding: '12px 16px',
            background: token.colorFillQuaternary,
          }}
        >
          {canCompare ? (
            // 字号与左栏切块文本统一（13px / 1.7），行高对齐便于左右对照
            <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.7, fontSize: 13 }}>
              {renderOriginal()}
            </div>
          ) : (
            <Empty description="暂无原文预览" style={{ marginTop: 80 }} />
          )}
        </div>
      </div>
    </div>
  );
};

export default ChunkCompareView;
