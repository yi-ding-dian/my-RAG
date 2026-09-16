import React, { useEffect, useRef, useState } from 'react';
import { Button, Modal } from 'antd';
import type { ModalProps } from 'antd';
import useResizable, { MIN_SIZE, type ResizeDir } from '../../utils/useResizable';

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
 * - min/max 默认：520×360 / 视口内缩 48px（max 始终钳制不出屏）。
 */
export type AppDimension = 'auto' | 'fixed' | 'resizable';

export interface AppModalProps extends ModalProps {
  /** 尺寸模式：auto=内容自适应高度（默认）| resizable=可拖拽 | fixed=固定 */
  dimension?: AppDimension;
  /** 默认宽高（resizable 初始 / auto 宽度 / fixed 尺寸）；默认 760×520 */
  defaultSize?: { w: number; h: number };
  /** 最小尺寸（默认 520×360） */
  minSize?: { w: number; h: number };
  /** 最大尺寸（超出视口时钳制到视口内缩 48px 内） */
  maxSize?: { w: number; h: number };
  /** 记忆键：记住用户拖拽尺寸（localStorage），下次打开恢复 */
  rememberKey?: string;
  /** auto 模式：**内容就绪后锁定高度**，之后不跟内容变（多步/内容会变的弹窗用，
   *  避免切换时界面忽大忽小；内容多则内部滚动，用户可自己拖）。
   *
   *  传了即"锁定型"（决定 rememberKey 是否记高度）；值表示"现在能锁了吗"
   *  ——异步加载的传 `!loading && !!data`，内容固定的可直接传 true。
   *  不传 = 高度永远跟内容（单屏弹窗的默认行为）。 */
  autoLock?: boolean;
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
  autoLock,
  busy = false,
  /** 默认关闭"点击遮罩关闭"（防误触丢内容；需开启时显式传 true；busy 时强制关闭） */
  maskClosable = false,
  /** 垂直居中（antd 语义）。默认 false=靠上显示：小屏/大卡时顶部不过高、
   *  底部不出屏；内容矮的小弹窗需要居中时显式传 true */
  centered = false,
  open,
  children,
  // width 与 styles 都由本组件接管（要按视口 clamp、并自己控制 body 高度与滚动），
  // 调用方传的这两个会被忽略；style 则随 rest 透传，可用它覆盖默认定位（如 top）
  width: _widthProp,
  styles: _stylesProp,
  ...rest
}) => {
  const [manual, setManual] = useState<{ w: number; h: number } | null>(null);
  const [contentH, setContentH] = useState(0);
  /** 锁定后的内容高度（null = 未锁，实时跟内容走） */
  const [lockedH, setLockedH] = useState<number | null>(null);
  /** 上一帧的 open：记忆恢复只在**上升沿**做，见下方 effect */
  const prevOpenRef = useRef(false);
  /** 视口尺寸：随 resize 更新。**不能每次 render 直接读 innerWidth/Height**——
   *  那样窗口变化不会触发重渲染，钳制用的还是旧视口，窗口变小后弹窗会被裁 */
  const [vp, setVp] = useState(() => ({
    w: typeof window !== 'undefined' ? window.innerWidth : 1920,
    h: typeof window !== 'undefined' ? window.innerHeight : 1080,
  }));
  useEffect(() => {
    // rAF 合并：拖动窗口时 resize 触发极密，逐个 setState 会让整棵子树（含
    // children）跟着重渲染，children 重（表格/图表）时明显掉帧
    let raf = 0;
    const onResize = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => setVp({
        w: window.innerWidth,
        h: window.innerHeight,
      }));
    };
    window.addEventListener('resize', onResize);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', onResize);
    };
  }, []);
  /** 传了 autoLock 即"锁定型弹窗"：高度定住不跟内容，且 rememberKey 记高度 */
  const isLockType = autoLock !== undefined;
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
    // **只在 open 的上升沿恢复**：本 effect 的依赖里有 minSize/rememberKey 等，
    // 弹窗打开期间它们一变就会重跑，把用户刚拖好的尺寸又打回记忆值
    const rising = !!open && !prevOpenRef.current;
    prevOpenRef.current = !!open;
    // fixed 无手柄、尺寸本就固定，不参与记忆恢复（否则记忆值会盖掉 defaultSize）
    if (!rising || !rememberKey || dimension === 'fixed') return;
    try {
      const raw = localStorage.getItem(`am.${rememberKey}`);
      if (raw) {
        const saved = JSON.parse(raw) as { w: number; h: number };
        if (saved.w >= minSize.w && saved.h >= minSize.h) {
          // 单屏 auto 弹窗不恢复记忆的高度：高度跟内容走，记住固定值会在内容变少
          // 时留白。锁定型（autoLock）不受此限——它本就定高，记忆正是用户所需。
          const keepH = dimension !== 'auto' || isLockType;
          setManual({ w: saved.w, h: keepH ? saved.h : 0 });
        }
      }
    } catch {
      /* 记忆损坏忽略 */
    }
  // 依赖取 minSize 的**具体数值**：调用方多数字面量传 { w, h }，对象引用每次都变，
  // 会让本 effect 反复重跑并把用户刚拖好的尺寸打回记忆值
  }, [open, rememberKey, minSize.w, minSize.h, dimension, isLockType]);

  // ---- 内容自适应（auto 模式）：观察内容自然高度 ----
  useEffect(() => {
    if (dimension !== 'auto' || !open) return;
    const el = contentRef.current;
    if (!el) return;
    const measure = () => {
      // **不能直接用 scrollHeight**：contentRef 是 height:100% 的容器，元素高被
      // 弹窗高度撑满，而 scrollHeight = max(内容高, 元素高)——于是"容器高"被误
      // 当成"内容高"，与"弹窗高度由内容决定"形成循环依赖。结果取决于打开动画
      // 期间的初始扰动：内容少的弹窗会收敛到偏大的高度，中间留一大片白。
      // 这里临时解除高度约束量真实内容高，同步改回——两次赋值在同一帧内完成，
      // 浏览器不会绘制中间态，无闪烁；高度值不变故不会触发本 ResizeObserver 回调。
      const prev = el.style.height;
      el.style.height = 'auto';
      const h = el.scrollHeight || el.offsetHeight;
      el.style.height = prev;
      setContentH(h);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    // **还要观察子元素**：el 是 height:100%，高度被 body 撑满，内容异步变化不会
    // 改变它自身的高度 → 只 observe(el) 就永不重测，自适应会停在首次（多半还空着）
    // 的量上。子元素高度由内容决定，能真正感知变化；MutationObserver 跟进动态增删。
    const watched = new Set<Element>();
    const watchChildren = () => {
      Array.from(el.children).forEach((c) => {
        if (!watched.has(c)) {
          ro.observe(c);
          watched.add(c);
        }
      });
    };
    watchChildren();
    const mo = new MutationObserver(() => {
      watchChildren();
      measure();
    });
    mo.observe(el, { childList: true });
    return () => {
      ro.disconnect();
      mo.disconnect();
    };
  }, [dimension, open]);

  // ---- 锁定型（autoLock）：内容就绪后定住高度，之后不跟内容变 ----
  // 多步/内容会变的弹窗用它（智能解析向导切换步骤、批量导入增删文件），否则
  // 每变一次界面就跳一次；定住后内容多则内部滚动（contentRef 本就 overflow:auto）。
  useEffect(() => {
    if (!isLockType || !open || !autoLock || lockedH !== null) return;
    // **现场量一次**：contentH state 还是上一帧（内容就绪前）的值，读它会锁早。
    // autoLock 翻转时本 effect 在 DOM 更新之后执行，直接量才是"第一个窗口"的高度。
    const el = contentRef.current;
    if (!el) return;
    const prev = el.style.height;
    el.style.height = 'auto';
    const h = el.scrollHeight || el.offsetHeight;
    el.style.height = prev;
    if (h > 0) setLockedH(h);
  }, [isLockType, open, autoLock, lockedH]);

  // 关闭时清掉锁定，下次打开重新按"第一个窗口"量
  useEffect(() => {
    if (!open) setLockedH(null);
  }, [open]);

  // ---- 拖拽锁手动 + 保存记忆（基准 = 最外层卡片 shell）----
  const { handlers, box: dragBox, draggingDir } = useResizable(
    { w: defaultSize.w, h: defaultSize.h, left: -1, top: -1 },
    shellRef,
    // 把本组件的 minSize 透传下去：不传的话拖拽下限会是 useResizable 里写死的
    // 520×360，内容少的弹窗就永远拖不小（且比自己的默认高度还大）
    minSize,
  );
  // 拖拽过程中实时同步手动尺寸（useResizable 内部 box 更新）
  const dragTimer = useRef<number>(0);
  useEffect(() => {
    if (dragBox.left < 0) return; // 未拖拽
    // 拖拽中即时生效（含高度）；但 auto 模式的**高度不持久化**——它跟内容走，
    // 记住了反而在内容变少时留白（见记忆恢复处的说明）。宽度照常记住。
    setManual({ w: dragBox.w, h: dragBox.h });
    if (rememberKey) {
      dragTimer.current = window.setTimeout(() => {
        // 单屏 auto 弹窗只记宽度（高度下次打开回到自适应）；锁定型照记高度
        const keepH = dimension !== 'auto' || isLockType;
        localStorage.setItem(`am.${rememberKey}`, JSON.stringify(
          keepH ? dragBox : { ...dragBox, h: 0 }));
      }, 400);
    }
    return () => {
      // 卸载时清掉待写入的定时器，别在组件消失后再动 localStorage
      window.clearTimeout(dragTimer.current);
    };
  }, [dragBox, rememberKey, dimension, isLockType]);

  // 视口钳制（max）：硬框不出屏（上下左右各留 24px）
  const vw = vp.w;
  const vh = vp.h;
  const max = {
    w: Math.min(maxSize ? maxSize.w : vw, vw - 48),
    h: Math.min(maxSize ? maxSize.h : vh, vh - 48),
  };

  // 宽度始终取手动/默认（拖拽直接作用于卡片外层）
  const w = clamp(manual?.w ?? defaultSize.w, minSize.w, max.w);
  // body 高度：自动模式量内容自然高；手动/固定模式 = 卡片高 - header/footer 高
  // （手动时拖拽的是"最外层卡片"，含标题栏，换算回 body 高度）
  // manualH 为 null = 没有手动高度（auto 模式恢复记忆时给 0，见记忆恢复处）
  const manualH = manual && manual.h > 0 ? manual.h : null;
  // 锁定型弹窗用定住的高度，否则用实时内容高（多步向导靠这个"切步骤不跳"）
  const effectiveH = lockedH ?? contentH;
  const cardH = manualH ?? (dimension === 'auto'
    ? (effectiveH || defaultSize.h) + nonBodyH
    : defaultSize.h);
  // 高度下限：自适应模式只保"内容区最小可见高"。
  // **不能套 minSize.h**——它是"拖拽能拖到多小"的下限，拿来当内容下限会把内容少的
  // 弹窗撑高、中间留出空白。拖过之后才按 minSize 钳制，保证拖不出畸形容器。
  const bodyFloor = (dimension === 'auto' && !manualH)
    ? 96
    : Math.max(minSize.h - nonBodyH, 96);
  // 上限先与下限取大：clamp 在 lo > hi 时返回 lo，会反而突破视口（极小窗口下
  // minSize.h 可能比视口还高）。取舍——**优先保内容区的最小可见高，允许整体出屏**：
  // 内容被压成一条缝比溢出更难用，出屏由外层滚动/调窗口解决
  const bodyCeil = Math.max(bodyFloor, max.h - Math.max(nonBodyH, 0));
  const bodyH = clamp(cardH - nonBodyH, bodyFloor, bodyCeil);
  // 未拖拽时顶部自适应：小屏/大卡时靠上且底部不出屏（大卡已按视口 clamp）
  const cardTotal = bodyH + nonBodyH;
  const topPx = Math.round(Math.min(96, Math.max(12, (vh - cardTotal) / 2)));
  const showHandles = dimension !== 'fixed' && Boolean(open);
  // 未拖拽时的定位：默认靠上（top ≤96，小屏/大卡不出屏）；centered 时交给 antd
  // 的垂直居中，**不能再传 top**——内联 top 会盖掉居中布局，弹窗仍偏上。
  // centered + 拖拽时同样不传 left/top：居中布局下弹窗实际位置不由 left/top 决定，
  // 传了会让弹窗跳到指针处（实测拖右下角手柄整个弹窗跑到屏幕右下、按钮出屏），
  // 所以居中模式下拖拽**只改尺寸、位置仍由居中决定**。
  const modalStyle =
    dragBox.left >= 0 || dragBox.top >= 0
      ? (centered
          ? undefined
          : {
              left: dragBox.left >= 0 ? dragBox.left : undefined,
              top: dragBox.top >= 0 ? dragBox.top : undefined,
              margin: 0,
            })
      : centered
        ? undefined
        : { top: topPx };

  return (
    <Modal
      open={open}
      width={w}
      centered={centered}
      style={modalStyle}
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
