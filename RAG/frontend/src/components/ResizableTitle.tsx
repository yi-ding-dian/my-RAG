import React from 'react';
import { Resizable } from 'react-resizable';
import type { ResizeCallbackData } from 'react-resizable';
import { theme } from 'antd';

/**
 * 可拖拽调整列宽的表头单元格（antd 官方 Table 列宽拖拽示例同款模式）
 *
 * 用 react-resizable 的 Resizable 包裹 <th>，右侧拖拽手柄调整列宽；
 * onResize 由 Table 列配置的 onHeaderCell 注入（更新列 width 后 antd 重渲染）。
 * 未配置 width 的列（width 为 0/undefined）直接返回普通 <th>，不包裹。
 *
 * 视觉反馈：手柄（分隔线）悬浮时出现主题色竖线 + 双向箭头 cursor，
 * 拖拽时竖线主题色加深（-border 高亮），让用户知道"此处可拖"。
 * 主纹理颜色取 antd 主题 token（浅色/暗色主题自适应，无硬编码）。
 */
const ResizableTitle = (
  props: React.HTMLAttributes<HTMLTableCellElement> & {
    onResize: (e: React.SyntheticEvent<Element>, data: ResizeCallbackData) => void;
    width: number;
    /** 拖拽开始/结束通知（AppTable 用它驱动跨列高亮 draggingCol） */
    onResizeStateChange?: (active: boolean) => void;
    /** 悬停开始/离开通知（AppTable 用它驱动跨列高亮 hoverCol） */
    onHoverStateChange?: (active: boolean) => void;
  },
) => {
  const { onResize, width, onResizeStateChange, onHoverStateChange, ...restProps } = props;
  const { token } = theme.useToken();
  const [hovering, setHovering] = React.useState(false);
  const [dragging, setDragging] = React.useState(false);

  const setDrag = React.useCallback((active: boolean) => {
    setDragging(active);
    onResizeStateChange?.(active);
  }, [onResizeStateChange]);

  const setHover = React.useCallback((active: boolean) => {
    setHovering(active);
    onHoverStateChange?.(active);
  }, [onHoverStateChange]);

  // 手柄线：仅 hover/拖拽时显示（平时 transparent——列分隔线由 AppTable
  // divider 统一提供，手柄线常驻会与列线叠加出"多一根"）
  const showLine = hovering || dragging;

  if (!width) {
    return <th {...restProps} />;
  }

  return (
    <Resizable
      width={width}
      height={0}
      handle={
        <span
          className="react-resizable-handle"
          style={{
            // 探测区：20px 宽，**中心骑在列边界**（左右各 10px 对称）——
            // 拖拽时左右两侧均有浅主题色块（用户要求对称可见）
            width: 20,
            height: '100%',
            right: -10, // span 右缘 = 列边界右 10px → span 中心 = 列边界
            position: 'absolute',
            bottom: 0,
            zIndex: 1,
            cursor: 'col-resize',
            display: 'inline-flex',
            alignItems: 'stretch',
            justifyContent: 'center',
            // 拖拽时浅主题色背景（左右对称块）；平时透明
            backgroundColor: dragging ? token.colorPrimaryBgHover : 'transparent',
            transition: 'background-color 0.15s',
          }}
          onMouseEnter={() => setHover(true)}
          onMouseLeave={() => setHover(false)}
          onMouseDown={() => setDrag(true)}
          onMouseUp={() => setDrag(false)}
          onClick={e => {
            e.stopPropagation();
          }}
        >
          {/* 线本体：3px 主题色，居中于探测区（= 列边界），hover/拖拽时显示 */}
          <span
            style={{
              width: 3,
              height: '100%',
              flexShrink: 0,
              backgroundColor: showLine ? token.colorPrimary : 'transparent',
              transition: 'background-color 0.15s',
            }}
          />
        </span>
      }
      onResize={onResize}
      onResizeStart={() => setDrag(true)}
      onResizeStop={() => {
        setDrag(false);
        setHover(false);
      }}
      draggableOpts={{ enableUserSelectHack: false }}
    >
      <th {...restProps} />
    </Resizable>
  );
};

export default ResizableTitle;
