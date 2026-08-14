import React, { useState } from 'react';
import { getToken } from '../auth/token';
import Lightbox from './Lightbox';

/** 图片代理前缀（与后端 /api/files/images/{doc_id}/{name} 一致） */
const IMAGE_PROXY_PREFIX = '/api/files/images/';

/**
 * 图片代理 URL 追加鉴权 token：<img> 标签无法携带 Authorization header，
 * 后端图片代理支持 ?token= query 参数（与 header 二选一，同一 JWT，
 * 24h 有效期内进 URL，内网企业环境可接受）。仅对本站图片代理 URL 追加，
 * 外链原样返回。
 */
export const withImageToken = (url: string): string => {
  if (!url || !url.startsWith(IMAGE_PROXY_PREFIX)) return url;
  const token = getToken();
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
};

interface MdImagesProps {
  text: string;
  /** img 最大宽度（默认 100%） */
  maxWidth?: number | string;
  /** img 最大高度（默认不限制）。与 maxWidth 同时设置时按比例缩放同时满足两者，
   *  多栏（如 ChunkCompareView 左右对照）用同一 maxHeight 可保证同一张图显示尺寸一致 */
  maxHeight?: number | string;
  /** 每张图片加载完成时回调（外层限高容器可用它重测溢出——图片加载前高度为 0 会漏判） */
  onImageLoad?: () => void;
}

/**
 * 轻量 Markdown 图片渲染：识别 ![alt](src) 引用并渲染为 <img>（src 自动
 * 追加鉴权 token），其余文本原样输出（保持调用方 pre-wrap 排版）。
 *
 * 设计说明（为何不引入 react-markdown）：
 * 1) ChunkCompareView 原文区是"切块分段高亮 + 双向联动"结构，整体
 *    markdown 化会破坏块区间点击/角标/滚动定位交互，只做图片引用替换；
 * 2) 需求仅"图片能显示"；引用片段（SourcePanel）保持纯文本（引用场景
 *    图片非必需）；
 * 3) 避免新增运行时依赖（离线构建环境不可控）。
 */
/** 当前放大查看的图片（null 关闭） */
interface ZoomTarget {
  src: string;
  alt: string;
}

const MdImages: React.FC<MdImagesProps> = ({ text, maxWidth = '100%', maxHeight, onImageLoad }) => {
  // 图片点击放大（方案 A：组件内 state 管理当前放大目标，无全局 Provider/context）：
  // Lightbox 是纯展示组件，state 只存"当前放大哪张图"，条件渲染单个遮罩实例，
  // 关闭即卸载。MdImages 保持纯函数式渲染——流式增量（图片未闭合先文本后补全）
  // 每次 text 更新走相同渲染路径，onClick 只在完整 <img> 渲染后绑定，不受影响。
  // hooks 必须位于下方 early return（if (!text)）之前，保证每次渲染调用顺序一致。
  const [zoomTarget, setZoomTarget] = useState<ZoomTarget | null>(null);
  if (!text) return null;
  const parts: React.ReactNode[] = [];
  // 每次渲染创建局部正则实例：全局（g）正则的 lastIndex 是共享可变状态，
  // 并发渲染/重入（React 18 并发特性、StrictMode 双调用、热更新）下多实例
  // 交错 exec 会互相改写 lastIndex，导致匹配位置错乱、部分图片随机不显示。
  const re = /!\[([^\]]*)\]\(([^)]+)\)/g;
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      parts.push(
        <React.Fragment key={`t${key++}`}>{text.slice(last, m.index)}</React.Fragment>,
      );
    }
    const src = withImageToken(m[2].trim());
    const alt = m[1];
    parts.push(
      <img
        key={`i${key++}`}
        src={src}
        alt={alt}
        title={alt}
        onLoad={onImageLoad}
        onClick={() => setZoomTarget({ src, alt })}
        className="md-image-zoom"
        style={{ maxWidth, maxHeight, width: 'auto', height: 'auto', display: 'block', margin: '4px 0' }}
      />,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    parts.push(
      <React.Fragment key={`t${key++}`}>{text.slice(last)}</React.Fragment>,
    );
  }
  return (
    <>
      {parts}
      {zoomTarget && (
        <Lightbox
          src={zoomTarget.src}
          alt={zoomTarget.alt}
          onClose={() => setZoomTarget(null)}
        />
      )}
    </>
  );
};

export default MdImages;
