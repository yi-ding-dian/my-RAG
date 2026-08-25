import React, { useEffect, useRef, useState } from 'react';
import { Button, Modal } from 'antd';
import type { ModalProps } from 'antd';
import useResizable, { MIN_SIZE, type ResizeDir } from '../utils/useResizable';

/**
 * AppModal：项目统一弹窗门面（可复用，未来所有弹窗收敛于此）
 *
 * 尺寸模式 dimension：
 * - `auto`（默认）：**高度跟随内容自适应**（ResizeObserver 测内容自然高度，
 *   min~max 钳制；超 max 内容区内部滚动，绝不撑破屏幕）。宽度 = defaultSize.w，
 *   可拖拽把手柄切到手动尺寸；
 * - `resizable`：初始 defaultSize，8 向自由拖拽（min/max 钳制）；
 * - `fixed`：固定 defaultSize，无手柄不可拖。
 *
 * 其他能力：
 * - rememberKey：记忆用户拖拽后的尺寸（localStorage），下次打开恢复；
 * - 统一视觉（antd Modal 透传 + 手柄热区），一处改样式全局生效；
 * - min/max 默认：520×360 / 视口 95vw×88vh（max 始终钳制不出屏）。
 */
export type AppDimension = 'auto' | 'fixed' | 'resizable';

export interface AppModalProps extends ModalProps {
  /** 尺寸模式：auto=内容自适应高度（默认）| resizable=可拖拽 | fixed=固定 */
  dimension?: AppDimension;
  /** 默认宽高（resizable 初始 / auto 宽度 / fixed 尺寸）；默认 760×520 */
  defaultSize?: { w: number; h: number };
  /** 最小尺寸（默认 520×360） */
  minSize?: { w: number; h: number };
  /** 最大尺寸（超出视口自动钳制到 95vw×88vh 内） */
  maxSize?: { w: number; h: number };
  /** 记忆键：记住用户拖拽尺寸（localStorage），下次打开恢复 */
  rememberKey?: string;
  /** 提交/加载中：禁用右上角 ✕、遮罩点击、Esc（防忙时误关）
   * 与 antd confirmLoading 不同——本开关作用于整个弹窗交互 */
  busy?: boolean;
}

/** 统一底部按钮组（确认/取消/危险 + 左侧附加区）
 *
 * 用法：`<AppModal footer={<AppModalFooter okText="导入" okLoading={importing}
 *  onOk={handleOk} onCancel={onCancel} />} …>`
 */
export interface AppModalFooterProps {
  okText?: string;
  cancelText?: string | null;
  /** 提交中：确定按钮 loading + 全部按钮禁用 */
  okLoading?: boolean;
  /** 危险操作（删除等）：确定按钮红色 */
  danger?: boolean;
  onOk?: () => void;
  onCancel?: () => void;
  /** 左侧附加元素（可选） */
  extra?: React.ReactNode;
}

export const AppModalFooter: React.FC<AppModalFooterProps> = ({
  okText = '确定',
  cancelText = '取消',
  okLoading = false,
  danger = false,
  onOk,
  onCancel,
  extra,
}) => (
  <div style={{ display: 'flex', alignItems: 'center',
                justifyContent: 'space-between', gap: 12 }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>{extra}</div>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      {cancelText != null && (
        <Button disabled={okLoading} onClick={onCancel}>{cancelText}</Button>
      )}
      <Button type="primary" danger={danger} loading={okLoading} onClick={onOk}>
        {okText}
      </Button>
    </div>
  </div>
);

const clamp = (n: number, lo: number, hi: number) =>
  Math.max(lo, Math.min(hi, n));

const DEFAULT_SIZE = { w: 760, h: 520 };

const AppModal: React.FC<AppModalProps> = ({
  dimension = 'auto',
  defaultSize = DEFAULT_SIZE,
  minSize = MIN_SIZE,
  maxSize,
  rememberKey,
  busy = false,
  /** 默认关闭"点击遮罩关闭"（防误触丢内容；需开启时显式传 true；busy 时强制关闭） */
  maskClosable = false,
  open,
  children,
  width: _widthProp, // eslint-disable-line @typescript-eslint/no-unused-vars -- 拦截透传（AntD Modal 不接收 width/styles），下划线表意图
  styles: _stylesProp, // eslint-disable-line @typescript-eslint/no-unused-vars -- 拦截透传，同上
  ...rest
}) => {
  const [manual, setManual] = useState<{ w: number; h: number } | null>(null);
  const [contentH, setContentH] = useState(0);
  // 初始按常规 header 高度估算，首帧避免 0 值导致"默认不超屏"计算抖动；
  // open 后立即实测校正（见下方 effect）
  const [nonBodyH, setNonBodyH] = useState(64);
  const contentRef = useRef<HTMLDivElement>(null);
  const shellRef = useRef<HTMLDivElement>(null);

  // ---- 非 body 部分高度（header+footer）：手动拖拽"卡片外框"时，
  // 卡片总高 = body 高 + 非 body 高 => body 高 = 手动卡片高 - nonBodyH ----
  // 用 ResizeObserver 持续校正：modal 打开动画期间 rect 不稳定（antd zoom
  // 过渡），单次/定时测量会取到中间态导致高度换算错位（默认底部出屏）
  useEffect(() => {
    if (!open) return;
    const o = shellRef.current;
    const c = contentRef.current;
    if (!o || !c) return;
    const measure = () => {
      const oh = o.getBoundingClientRect().height;
      const ch = c.getBoundingClientRect().height;
      if (oh > 0 && ch > 0) setNonBodyH(Math.max(12, Math.round(oh - ch)));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(o);
    ro.observe(c);
    return () => ro.disconnect();
  }, [open, manual]);

  // ---- 记忆恢复：打开时若有记忆尺寸则作为手动尺寸 ----
  useEffect(() => {
    if (!open || !rememberKey) return;
    try {
      const raw = localStorage.getItem(`am.${rememberKey}`);
      if (raw) {
        const saved = JSON.parse(raw) as { w: number; h: number };
        if (saved.w >= minSize.w && saved.h >= minSize.h) setManual(saved);
      }
    } catch {
      /* 记忆损坏忽略 */
    }
  }, [open, rememberKey, minSize]);

  // ---- 内容自适应（auto 模式）：观察内容自然高度 ----
  useEffect(() => {
    if (dimension !== 'auto' || !open) return;
    const el = contentRef.current;
    if (!el) return;
    const measure = () => {
      // 滚动高度 = 内容自然高（无滚动限制时），即使当前被钳制
      setContentH(el.scrollHeight || el.offsetHeight);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [dimension, open]);

  // ---- 拖拽锁手动 + 保存记忆（基准 = 最外层卡片 shell）----
  const { handlers, box: dragBox, draggingDir } = useResizable(
    { w: defaultSize.w, h: defaultSize.h, left: -1, top: -1 },
    shellRef,
  );
  // 拖拽过程中实时同步手动尺寸（useResizable 内部 box 更新）
  const dragTimer = useRef<number>(0);
  useEffect(() => {
    if (dragBox.left < 0) return; // 未拖拽
    setManual({ w: dragBox.w, h: dragBox.h });
    if (rememberKey) {
      window.clearTimeout(dragTimer.current);
      dragTimer.current = window.setTimeout(() => {
        localStorage.setItem(`am.${rememberKey}`, JSON.stringify(dragBox));
      }, 400);
    }
  }, [dragBox, rememberKey]);

  // 视口钳制（max）：硬框不出屏（上下左右各留 24px）
  const vw = typeof window !== 'undefined' ? window.innerWidth : 1920;
  const vh = typeof window !== 'undefined' ? window.innerHeight : 1080;
  const max = {
    w: Math.min(maxSize ? maxSize.w : vw, vw - 48),
    h: Math.min(maxSize ? maxSize.h : vh, vh - 48),
  };

  // 宽度始终取手动/默认（拖拽直接作用于卡片外层）
  const w = clamp(manual?.w ?? defaultSize.w, minSize.w, max.w);
  // body 高度：自动模式量内容自然高；手动/固定模式 = 卡片高 - header/footer 高
  // （手动时拖拽的是"最外层卡片"，含标题栏，换算回 body 高度）
  const cardH = manual ? manual.h : (dimension === 'auto'
    ? (contentH || defaultSize.h) + nonBodyH
    : defaultSize.h);
  const bodyH = clamp(cardH - nonBodyH,
    Math.max(minSize.h - nonBodyH, 96),
    max.h - Math.max(nonBodyH, 0));
  // 未拖拽时顶部自适应：小屏/大卡时靠上且底部不出屏（大卡已按视口 clamp）
  const cardTotal = bodyH + nonBodyH;
  const topPx = Math.round(Math.min(96, Math.max(12, (vh - cardTotal) / 2)));
  const showHandles = dimension !== 'fixed' && Boolean(open);

  return (
    <Modal
      open={open}
      width={w}
      style={
        dragBox.left >= 0 || dragBox.top >= 0
          ? {
              left: dragBox.left >= 0 ? dragBox.left : undefined,
              top: dragBox.top >= 0 ? dragBox.top : undefined,
              margin: 0,
            }
          : { top: topPx }
      }
      styles={{
        body: {
          height: bodyH,
          padding: 0,
          overflow: 'hidden',
          position: 'relative',
        },
      }}
      modalRender={(node) => (
        // 外层卡片壳（am-shell：hover 此壳 = 悬停在弹窗边框上，手柄淡显）
        <div
          ref={shellRef}
          className="am-shell"
          style={{ position: 'relative' }}
          onPointerMove={handlers.move}
          onPointerUp={handlers.end}
          onPointerCancel={handlers.end}
        >
          {node}
          {showHandles && (
            <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
              {(['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'] as const).map((dir: ResizeDir) => (
                <div
                  key={dir}
                  className={`rsz rsz-${dir}${draggingDir === dir ? ' rsz-active' : ''}`}
                  onPointerDown={handlers.start(dir)}
                />
              ))}
            </div>
          )}
        </div>
      )}
      maskClosable={busy ? false : maskClosable}
      {...rest}
      {...(busy ? { closable: false, keyboard: false } : {})}
    >
      {/* flex 列: 子组件(如 .logs-page-tabs 链式撑满)可 flex:1 填满剩余高度,
          左右/上下内滚独立于弹窗 body 滚动 */}
      <div
        ref={contentRef}
        style={{
          height: '100%',
          overflow: 'auto',
          position: 'relative',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {children}
      </div>
    </Modal>
  );
};

export default AppModal;
