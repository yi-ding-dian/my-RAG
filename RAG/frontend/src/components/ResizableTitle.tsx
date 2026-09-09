import React from 'react';
import { Resizable } from 'react-resizable';
import type { ResizeCallbackData } from 'react-resizable';

/**
 * 可拖拽调整列宽的表头单元格（antd 官方 Table 列宽拖拽示例同款模式）
 *
 * 用 react-resizable 的 Resizable 包裹 <th>，右侧拖拽手柄调整列宽；
 * onResize 由 Table 列配置的 onHeaderCell 注入（更新列 width 后 antd 重渲染）。
 * 未配置 width 的列（width 为 0/undefined）直接返回普通 <th>，不包裹。
 *
 * 最小列宽 = 表头名宽度（含 padding）：拖拽左侧手柄在 `title` 传入时
 * 自动按标题文字估算 minWidth（中文≈14px/字、英文数字≈8.5px/字，
 * 加 padding 24px 与基础下限 80px），`onResize` 回调在最小宽处截断，
 * 拖不回去（适配 Logs/Users/Documents 等各表，无需页面层自己算）。
 */
const estimateTitleWidth = (title: string): number => {
  let px = 0;
  for (const ch of title) {
    px += /[一-鿿　-〿＀-￯]/.test(ch) ? 14 : 8.5;
  }
  return Math.max(Math.ceil(px) + 24, 80); // +24 表头两侧 padding；下限 80
};

interface ResizableTitleProps extends React.HTMLAttributes<HTMLTableCellElement> {
  onResize: (e: React.SyntheticEvent<Element>, data: ResizeCallbackData) => void;
  width: number;
  /** 表头文本（估算最小列宽用）；不传则不钳制 */
  title?: string;
}

const ResizableTitle = ({
  onResize,
  width,
  title,
  ...restProps
}: ResizableTitleProps) => {
  if (!width) {
    return <th {...restProps} />;
  }

  // 最小列宽：表头名宽度（title 缺失时退回 80px 下限）
  const minWidth = Math.max(estimateTitleWidth(title ?? ''), 80);

  const handleResize = (e: React.SyntheticEvent<Element>, data: ResizeCallbackData) => {
    onResize(e, { ...data, size: { ...data.size, width: Math.max(data.size.width, minWidth) } });
  };

  return (
    <Resizable
      width={width}
      height={0}
      minConstraints={[minWidth, 0]}
      handle={
        <span
          className="react-resizable-handle"
          onClick={e => {
            e.stopPropagation();
          }}
        />
      }
      onResize={handleResize}
      draggableOpts={{ enableUserSelectHack: false }}
    >
      <th {...restProps} />
    </Resizable>
  );
};

export default ResizableTitle;
