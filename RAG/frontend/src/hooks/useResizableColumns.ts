import { useCallback, useMemo, useState } from 'react';
import type { ColumnsType } from 'antd/es/table';
import type { ResizeCallbackData } from 'react-resizable';

/**
 * 列宽拖拽统一 hook（配合 ResizableTitle 使用）：
 * 维护每列拖拽后的宽度（colWidths，按列 key），
 * 提供 handleResize（拖拽回调，注入 onHeaderCell）与 tableWidth（各列宽求和，
 * 供 Table scroll.x 使用——动态对齐避免 fixed 布局拖拽漂移）。
 *
 * 用法（Table 列注释出要拖拽的列加 onHeaderCell + width）：
 *   const { colWidths, handleResize, tableWidth } = useResizableColumns();
 *   columns = [{ title:'时间', key:'time', width: colWidths.time ?? 170,
 *                onHeaderCell: () => ({ width: colWidths.time ?? 170,
 *                                       onResize: handleResize('time'), title: '时间' }), ... }]
 *   <Table columns={columns} scroll={{ x: tableWidth }}
 *          components={{ header: { cell: ResizableTitle } }} />
 */
export function useResizableColumns<T>() {
  // 列宽拖拽：拖拽后的宽度存 colWidths（按列 key），未拖过的列用初始 width
  const [colWidths, setColWidths] = useState<Record<string, number>>({});

  const handleResize = useCallback(
    (key: React.Key) =>
      (_: React.SyntheticEvent<Element>, { size }: ResizeCallbackData) => {
        setColWidths(prev => ({ ...prev, [String(key)]: size.width }));
      },
    [],
  );

  // 表格总宽 = 当前各列宽度之和（跟随列宽拖拽动态变化）。
  // 必须让 scroll.x === 总宽：antd 表格为 fixed 布局，scroll.x > 总宽时浏览器会按
  // 比例放大各列渲染宽度，拖拽中比例随总宽变化 → 列实际位移量 ≠ 鼠标位移量，
  // 导致列宽拖拽严重漂移。动态对齐后缩放比例恒为 1。
  const tableWidth = useMemo(
    () => (columns: ColumnsType<T>) =>
      columns.reduce((s, c) => s + ((c.width as number) || 0), 0),
    [],
  );

  return { colWidths, handleResize, tableWidth };
}
