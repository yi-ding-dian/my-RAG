import React from 'react';
import { Resizable } from 'react-resizable';
import type { ResizeCallbackData } from 'react-resizable';

/**
 * 可拖拽调整列宽的表头单元格（antd 官方 Table 列宽拖拽示例同款模式）
 *
 * 用 react-resizable 的 Resizable 包裹 <th>，右侧拖拽手柄调整列宽；
 * onResize 由 Table 列配置的 onHeaderCell 注入（更新列 width 后 antd 重渲染）。
 * 未配置 width 的列（width 为 0/undefined）直接返回普通 <th>，不包裹。
 */
const ResizableTitle = (
  props: React.HTMLAttributes<any> & {
    onResize: (e: React.SyntheticEvent<Element>, data: ResizeCallbackData) => void;
    width: number;
  },
) => {
  const { onResize, width, ...restProps } = props;

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
          onClick={e => {
            e.stopPropagation();
          }}
        />
      }
      onResize={onResize}
      draggableOpts={{ enableUserSelectHack: false }}
    >
      <th {...restProps} />
    </Resizable>
  );
};

export default ResizableTitle;
