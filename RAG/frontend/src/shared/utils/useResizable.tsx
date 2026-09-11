import { useCallback, useRef, useState } from 'react';

/** 可拖拽调整大小 hook（8 向手柄，带最小值 clamp，可复用）
 *
 * 用法：const { box, handlers } = useResizable(init);
 * 容器 style 用 box 尺寸；8 个手柄绑 handlers.start('se') 等，
 * move/up 由 pointer capture 确保手柄持续接收。
 */
export type ResizeDir = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';

export interface ResizeBox {
  w: number;
  h: number;
  /** 容器相对视口位置（西/北向拖动时使用；非位移方向时不变） */
  left: number;
  top: number;
}

/** 最小尺寸：拖到不能更小，防"缩没了"（用户要求给个最小值） */
export const MIN_SIZE = { w: 520, h: 360 };

export interface ResizableResult {
  box: ResizeBox;
  reset: () => void;
  /** 当前拖拽方向（拖动中非空，用于手柄高亮） */
  draggingDir: ResizeDir | null;
  handlers: {
    start: (dir: ResizeDir) => (e: React.PointerEvent) => void;
    move: (e: React.PointerEvent) => void;
    end: () => void;
  };
}

export const useResizable = (
  init: ResizeBox,
  targetRef?: React.RefObject<HTMLElement | null>,
): ResizableResult => {
  const [box, setBox] = useState(init);
  const [draggingDir, setDraggingDir] = useState<ResizeDir | null>(null);
  const session = useRef<{
    dir: ResizeDir;
    x: number;
    y: number;
    box: ResizeBox;
  } | null>(null);

  const start = useCallback(
    (dir: ResizeDir) => (e: React.PointerEvent) => {
      e.preventDefault();
      e.stopPropagation();
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      // 拖动手柄的基准取容器当前实际矩形（位置/尺寸都准确，
      // 尤其西/北向拖动需要真实 left/top 做位置回调）
      const rect = targetRef?.current?.getBoundingClientRect();
      const base: ResizeBox = rect
        ? { w: rect.width, h: rect.height, left: rect.left, top: rect.top }
        : box;
      session.current = { dir, x: e.clientX, y: e.clientY, box: base };
      setDraggingDir(dir);
    },
    [box, targetRef],
  );

  const move = useCallback((e: React.PointerEvent) => {
    const s = session.current;
    if (!s) return;
    const dx = e.clientX - s.x;
    const dy = e.clientY - s.y;
    setBox(() => {
      const n = { ...s.box };
      if (s.dir.includes('e')) n.w = s.box.w + dx;
      if (s.dir.includes('s')) n.h = s.box.h + dy;
      if (s.dir.includes('w')) n.w = s.box.w - dx;
      if (s.dir.includes('n')) n.h = s.box.h - dy;
      // clamp 最小值；西/北向被 clamp 时回调位置（右边/下边保持不动）
      if (n.w < MIN_SIZE.w) n.w = MIN_SIZE.w;
      if (n.h < MIN_SIZE.h) n.h = MIN_SIZE.h;
      if (s.dir.includes('w')) n.left = s.box.left + (s.box.w - n.w);
      if (s.dir.includes('n')) n.top = s.box.top + (s.box.h - n.h);
      return n;
    });
  }, []);

  const end = useCallback(() => {
    session.current = null;
    setDraggingDir(null);
  }, []);

  const reset = useCallback(() => setBox(init), [init]);

  return { box, reset, draggingDir, handlers: { start, move, end } };
};

export default useResizable;
