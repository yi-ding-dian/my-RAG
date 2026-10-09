/**
 * 统计分析跨领域公共资源（概览页 + 3 个详情页共用）。
 *
 * 只留真正跨领域的三样：
 * - `statusOf`：任务状态文案/颜色——键虽取自 RAGAS 状态（queued/running/…），
 *   但概览页摘要卡也在用，故不挪进 ragas/
 * - `scoreColor`：分数颜色——检索质量页与 RAGAS 页共用
 * - `useVisibleCount`：按容器高度算可见条数——概览页与反馈汇总卡共用
 *
 * 按领域拆出去的部分：RAGAS 专用常量见 `ragas/ragas-shared.ts`，
 * 反馈汇总卡见 `feedback/FeedbackSummary.tsx`。
 * （原先这里 9 个导出有 5 个只有 RAGAS 用，名不副实）
 */
import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * 按容器可用高度算"能完整显示几条"——让摘要卡的列表**填满卡片、且不出现滚动条**，
 * 放不下的部分由列表末尾的「共 N 条，其余见详情」兜底。
 *
 * 行高由调用方给（各卡行高不同）；容器高度变化（窗口缩放、侧栏伸缩）时重算。
 *
 * **用回调 ref 而不是 useEffect + useRef**：列表多半在 `loading ? Skeleton : <列表>`
 * 这类条件渲染里，首帧元素根本不存在，而 effect 依赖只有 rowHeight、不会重跑，
 * 结果永远停在初值上（实测：RAGAS 卡一直只显示 5 条）。回调 ref 在元素挂载那一刻
 * 就测量，天然不受渲染时序影响。
 *
 * ref 要绑在**列表自己的容器**上——卡片里列表上方还有指标行等内容，绑到卡片上会算多。
 */
export function useVisibleCount<T extends HTMLElement>(rowHeight: number, fallback = 5) {
  const [count, setCount] = useState(fallback);
  const roRef = useRef<ResizeObserver | null>(null);

  const ref = useCallback((el: T | null) => {
    roRef.current?.disconnect();
    roRef.current = null;
    if (!el) return;
    const calc = () => {
      const h = el.clientHeight;
      if (h > 0) setCount(Math.max(1, Math.floor(h / rowHeight)));
    };
    calc();
    const ro = new ResizeObserver(calc);
    ro.observe(el);
    roRef.current = ro;
  }, [rowHeight]);

  // 卸载时断开，避免观察器泄漏
  useEffect(() => () => {
    roRef.current?.disconnect();
    roRef.current = null;
  }, []);

  return { ref, count };
}

// RAGAS 任务状态文案/颜色
export const statusConfig: Record<string, { color: string; label: string }> = {
  queued: { color: 'warning', label: '排队中' },
  pending: { color: 'default', label: '等待中' },
  running: { color: 'processing', label: '运行中' },
  completed: { color: 'success', label: '已完成' },
  failed: { color: 'error', label: '失败' },
};

export const statusOf = (s: string) => statusConfig[s] || { color: 'default', label: s };

// 分数颜色（0.8 绿 / 0.5 黄 / 红）
export const scoreColor = (v: number) => (v >= 0.8 ? '#52c41a' : v >= 0.5 ? '#faad14' : '#f5222d');
