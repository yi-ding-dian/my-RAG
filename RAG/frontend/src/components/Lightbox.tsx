import React, { useEffect, useRef, useState } from 'react';

interface LightboxProps {
  src: string;
  alt?: string;
  onClose: () => void;
}

/** 缩放范围：1x ~ 8x */
const MIN_SCALE = 1;
const MAX_SCALE = 8;
/** 判断「点击 vs 拖动」的位移阈值（px），小于该位移视为点击 */
const DRAG_THRESHOLD = 5;

/**
 * 图片点击放大查看（Lightbox）：
 * - 暗色遮罩（rgba(0,0,0,0.75)）fixed 全屏，z-index 2000（高于 AntD Modal 默认 1000，
 *   弹窗内图片同样能盖住弹窗层）
 * - 图片居中，max-width 92vw / max-height 88vh，object-fit contain，圆角 6
 * - 点击空白处（遮罩本体）/ 右上角关闭按钮 / 按 ESC → onClose 还原；点击图片本身不关闭
 * - 挂载时注册 keydown 监听 + body 滚动锁（overflow hidden），卸载时全部移除/恢复
 * - 滚轮缩放：1x~8x，锚点为鼠标位置（transform-origin 恒为图片中心，缩放时用 translate
 *   补偿，保证鼠标下的内容点不动——与跟随鼠标的 origin 数学等价，实现更简单可靠）；
 *   wheel 用原生监听 { passive:false } 才能 preventDefault（React 合成 onWheel 是
 *   passive 的，preventDefault 无效且会告警）
 * - 放大（>1x）后按住拖动查看（pointer 事件 + setPointerCapture），位移 ≥5px 视为拖动、
 *   拖动结束抑制紧随的 click 防止误关遮罩；双击图片还原 1x 并归位（0.18s 过渡，
 *   滚轮/拖动无过渡避免卡顿）
 * - 倍率指示：放大时遮罩底部显示 "120%" 小胶囊，1x 隐藏
 * - 淡入过渡：class lightbox-overlay（CSS keyframes lightbox-fade-in，0.2s）
 *
 * 由父组件（MdImages）条件渲染：zoomTarget 为 null 时组件卸载即消失（不做关闭动画，
 * 简单可靠）；scale/translate 等状态随组件卸载自然重置，不跨次保留。
 */
const Lightbox: React.FC<LightboxProps> = ({ src, alt, onClose }) => {
  // 缩放与平移：scale ∈ [1, 8]；translate 为图片相对原始位置的偏移（px）
  const [scale, setScale] = useState(1);
  const [translate, setTranslate] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  // 双击还原时短暂启用 transform 过渡；滚轮/拖动保持无过渡（避免逐帧缩放卡顿）
  const [smooth, setSmooth] = useState(false);

  const overlayRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  // 状态镜像 ref：原生 wheel 监听只挂一次，handler 内取最新值
  const scaleRef = useRef(scale);
  const translateRef = useRef(translate);
  // 拖动现场：起点 + 起始 translate + 是否超过点击阈值
  const dragRef = useRef<{
    startX: number;
    startY: number;
    tx: number;
    ty: number;
    moved: boolean;
  } | null>(null);
  // 拖动结束抑制随后浏览器补发的 click（click 可能落在 stage 或遮罩，两级都消费，防残留）
  const suppressClickRef = useRef(false);
  const smoothTimerRef = useRef<number | undefined>(undefined);
  scaleRef.current = scale;
  translateRef.current = translate;

  // ESC 关闭：与组件挂载周期同生命周期，卸载自动移除监听
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  // body 滚动锁：放大期间禁止底层页面滚动，卸载恢复原值
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  // 滚轮缩放（挂在遮罩上，任意位置滚轮都可缩放）：
  // 锚点 = 鼠标位置。transform-origin 固定为图片中心，视觉框 = 围绕中心缩放的结果，
  // 鼠标相对未变换盒子中心的偏移 = (鼠标视觉坐标 - 视觉中心) / 当前scale；
  // 保持鼠标下的内容点不动，需要 t' = t + (cur - next) × 该偏移。
  useEffect(() => {
    const overlay = overlayRef.current;
    if (!overlay) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const img = imgRef.current;
      if (!img) return;
      const cur = scaleRef.current;
      // 步进：deltaY 每 ~100 单位 0.1（0.1 的整数倍，滚得快缩放得快）
      const step = Math.max(0.1, Math.round((Math.abs(e.deltaY) / 100) * 10) / 10);
      const next = Math.min(
        MAX_SCALE,
        Math.max(MIN_SCALE, Math.round((cur + (e.deltaY < 0 ? step : -step)) * 10) / 10)
      );
      if (next === cur) return;
      // 中断双击还原的过渡（用户又滚轮了）
      setSmooth(false);
      if (next <= 1) {
        // 缩回 1x：图片归位
        setScale(1);
        setTranslate({ x: 0, y: 0 });
        return;
      }
      const rect = img.getBoundingClientRect(); // 变换后的视觉框
      const offsetX = (e.clientX - rect.left - rect.width / 2) / cur;
      const offsetY = (e.clientY - rect.top - rect.height / 2) / cur;
      setScale(next);
      setTranslate((t) => ({
        x: t.x + (cur - next) * offsetX,
        y: t.y + (cur - next) * offsetY,
      }));
    };
    overlay.addEventListener('wheel', onWheel, { passive: false });
    return () => overlay.removeEventListener('wheel', onWheel);
  }, []);

  // 双击还原 1x 并归位（带短暂过渡），卸载时清理定时器
  const handleDoubleClick = () => {
    if (scaleRef.current <= 1) return;
    setSmooth(true);
    setScale(1);
    setTranslate({ x: 0, y: 0 });
    window.clearTimeout(smoothTimerRef.current);
    smoothTimerRef.current = window.setTimeout(() => setSmooth(false), 200);
  };
  useEffect(() => () => window.clearTimeout(smoothTimerRef.current), []);

  // ---- 拖动查看（仅放大 >1x 时启用） ----
  const handlePointerDown = (e: React.PointerEvent<HTMLImageElement>) => {
    if (scaleRef.current <= 1 || e.button !== 0) return;
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      tx: translateRef.current.x,
      ty: translateRef.current.y,
      moved: false,
    };
    setDragging(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const handlePointerMove = (e: React.PointerEvent<HTMLImageElement>) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    if (Math.abs(dx) + Math.abs(dy) >= DRAG_THRESHOLD) d.moved = true;
    setTranslate({ x: d.tx + dx, y: d.ty + dy });
  };
  const handlePointerEnd = (e: React.PointerEvent<HTMLImageElement>) => {
    const d = dragRef.current;
    if (!d) return;
    dragRef.current = null;
    setDragging(false);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    // 发生过拖动：抑制浏览器随后补发的 click，避免误触发关闭
    if (d.moved) suppressClickRef.current = true;
  };

  return (
    <div
      ref={overlayRef}
      className="lightbox-overlay"
      role="dialog"
      aria-modal="true"
      onClick={(e) => {
        // 拖动结束后浏览器补发的 click 可能落在遮罩上，先消费抑制标志再考虑关闭
        if (suppressClickRef.current) {
          suppressClickRef.current = false;
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        onClose();
      }}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 2000,
        background: 'rgba(0, 0, 0, 0.75)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        cursor: 'zoom-out',
        userSelect: 'none',
      }}
    >
      {/* 右上角关闭按钮：先阻断冒泡再关闭，避免经遮罩 onClick 重复触发 */}
      <button
        type="button"
        aria-label="关闭放大预览"
        onClick={(e) => {
          e.stopPropagation();
          onClose();
        }}
        style={{
          position: 'absolute',
          top: 16,
          right: 16,
          width: 36,
          height: 36,
          border: 'none',
          borderRadius: '50%',
          background: 'rgba(255, 255, 255, 0.12)',
          color: '#fff',
          fontSize: 18,
          lineHeight: '36px',
          textAlign: 'center',
          cursor: 'pointer',
        }}
      >
        ×
      </button>
      {/* 图片容器：阻断点击冒泡（图片本身不关闭），alt 小字视为图片区域的一部分 */}
      <div
        onClick={(e) => {
          e.stopPropagation();
          // 拖动后的 click 也可能落在容器上，同样消费抑制标志（避免残留到下一次点击）
          if (suppressClickRef.current) {
            suppressClickRef.current = false;
            e.preventDefault();
          }
        }}
        style={{
          maxWidth: '92vw',
          maxHeight: '88vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'default',
        }}
      >
        <img
          ref={imgRef}
          src={src}
          alt={alt}
          draggable={false}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerEnd}
          onPointerCancel={handlePointerEnd}
          onDoubleClick={handleDoubleClick}
          style={{
            maxWidth: '92vw',
            maxHeight: '88vh',
            objectFit: 'contain',
            borderRadius: 6,
            transform: `translate(${translate.x}px, ${translate.y}px) scale(${scale})`,
            transformOrigin: 'center center',
            transition: smooth ? 'transform 0.18s ease' : 'none',
            cursor: scale > 1 ? (dragging ? 'grabbing' : 'grab') : 'default',
            touchAction: 'none',
          }}
        />
        {alt && (
          <div
            style={{
              marginTop: 8,
              color: 'rgba(255, 255, 255, 0.75)',
              fontSize: 13,
              maxWidth: '80vw',
              textAlign: 'center',
              wordBreak: 'break-word',
            }}
          >
            {alt}
          </div>
        )}
      </div>
      {/* 倍率指示：放大时底部小胶囊，1x 隐藏；pointer-events none 不挡点击关闭 */}
      {scale > 1 && (
        <div
          style={{
            position: 'absolute',
            bottom: 18,
            left: '50%',
            transform: 'translateX(-50%)',
            padding: '4px 14px',
            borderRadius: 999,
            background: 'rgba(0, 0, 0, 0.55)',
            color: '#fff',
            fontSize: 12,
            lineHeight: '18px',
            whiteSpace: 'nowrap',
            pointerEvents: 'none',
          }}
        >
          {Math.round(scale * 100)}%
        </div>
      )}
    </div>
  );
};

export default Lightbox;
