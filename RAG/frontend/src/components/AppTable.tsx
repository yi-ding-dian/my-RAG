import React from 'react';
import { Table } from 'antd';
import type { TableProps } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { ResizeCallbackData } from 'react-resizable';
import ResizableTitle from './ResizableTitle';

/** 列宽拖拽的硬性最小宽度（兜底；实际最小 = max(此值, 内容实际宽)） */
const MIN_COLUMN_WIDTH = 60;
/** 内容测量样本行数（每列取前 N 行 cell 实际宽作为内容最小宽） */
const MEASURE_SAMPLE_ROWS = 15;
/** auto 智能默认对齐的"短列"宽度阈值：<= 此值居中，否则左对齐 */
const SHORT_COLUMN_WIDTH = 140;

export type AppTableAlign = 'center' | 'left' | 'auto';

/**
 * 可拖拽列宽的通用表格（基于 antd Table + ResizableTitle）
 *
 * 拖拽语义（方案 Z，此消彼长）：
 * - 拖"列 A 右缘"的分隔线 → 列 A 变宽/变窄，相邻右列 B 此消彼长
 *   （总宽不变；B 无 width / 是最后一列 / resizable:false 时只改 A）
 * - 最小宽 = max(60px, 该列内容实际宽)（内容宽由采样测量获得：表头 +
 *   前 MEASURE_SAMPLE_ROWS 行 cell 隐藏克隆实际渲染宽）
 * - 列组（children）与 resizable:false 的列不参与拖拽
 * - scroll.x 自动 = 各列宽之和（antd fixed 布局下防比例缩放漂移）
 *
 * 用法：<AppTable rowKey="id" dataSource={items} columns={columns} />
 * （columns 定义了 width 的列即可拖；页面不再需要手动三件套）
 */
interface AppTableProps<RecordType extends object> extends Omit<TableProps<RecordType>, 'components' | 'columns'> {
  columns?: ColumnsType<RecordType>;
  /** 整表对齐（默认为 auto 智能判断）：
   * - center：全部列居中（表头默认跟随）
   * - left：全部列左（antd 默认）
   * - auto：短列（width <= SHORT_COLUMN_WIDTH）居中，长文本列左
   * 优先级：页面列定义里显式 align > 本参数 > auto 智能判断 */
  align?: AppTableAlign;
  /** 表头对齐（默认跟随 align：align=center 则表头居中，left 则左） */
  headerAlign?: 'center' | 'left';
  /** 列分隔线（主题色，2px 竖线 + 淡主题色行线）：默认关；
   * 开启后列间有可见主题色分隔线（与 antd 无 bordered 默认的"几乎无线"对比）。
   * 只影响本表（挂 .apptable-divider class），不影响其他表格 */
  divider?: boolean;
}

/** 空类型守卫：判断列是否已显式设置 align */
function hasExplicitAlign(col: Record<string, unknown>): boolean {
  return col.align != null && col.align !== 'auto';
}

/**
 * 列对齐注入（三级优先级：列显式 > 整表参数 > auto 智能判断）
 * - 列显式 align（非 auto）→ 保留不动
 * - align=center → 列居中；align=left → 列左
 * - auto → 列宽 <= SHORT_COLUMN_WIDTH 居中，否则左
 */
function withAlign<T extends object>(
  col: T,
  colObj: Record<string, unknown>,
  align: AppTableAlign,
): T {
  if (hasExplicitAlign(colObj)) return col; // 列显式设置优先
  let cellAlign: 'center' | 'left';
  if (align === 'center') cellAlign = 'center';
  else if (align === 'left') cellAlign = 'left';
  else {
    // auto：短列居中，长文本列左
    const w = Number(colObj.width) || 0;
    cellAlign = w > 0 && w <= SHORT_COLUMN_WIDTH ? 'center' : 'left';
  }
  return { ...col, align: cellAlign } as T;
}

/** 隐藏克隆测量：克隆表格前 N 行（含表头）的 cell 到不可见容器里，
 *  脱离 table 布局约束后量出"内容自然宽"（含 padding）。一次 O(样本行×列)，
 *  只在 dataSource 变化后重测。无法量（空表/结构异常）返回 null → 退 60px。 */
function measureContentWidths(tableEl: HTMLTableElement): number[] | null {
  const thead = tableEl.querySelector('thead');
  const tbody = tableEl.querySelector('tbody');
  if (!thead || !tbody) return null;
  const ths = Array.from(thead.querySelectorAll('th'));
  const rows = Array.from(tbody.querySelectorAll('tr')).slice(0, MEASURE_SAMPLE_ROWS);
  if (ths.length === 0) return null;
  // 克隆容器：不可见、不占位（克隆 cell 保持 antd 样式类，但脱离表格布局）
  const holder = document.createElement('div');
  holder.className = 'apptable-measure-holder';
  holder.style.position = 'absolute';
  holder.style.left = '-9999px';
  holder.style.top = '0';
  holder.style.display = 'block';
  holder.style.visibility = 'hidden';
  document.body.appendChild(holder);
  try {
    const minWidths: number[] = [];
    for (let col = 0; col < ths.length; col++) {
      holder.textContent = ''; // 清空上次残留
      const th = ths[col].cloneNode(true) as HTMLElement;
      th.style.width = ''; // 去掉 inline width → 走内容自然宽
      holder.appendChild(th);
      let maxW = th.offsetWidth;
      for (const tr of rows) {
        const td = tr.children[col] as HTMLElement | undefined;
        if (!td) break;
        const c = td.cloneNode(true) as HTMLElement;
        c.style.width = '';
        holder.appendChild(c);
        if (c.offsetWidth > maxW) maxW = c.offsetWidth;
      }
      minWidths.push(maxW || 0);
    }
    return minWidths.length === ths.length ? minWidths : null;
  } finally {
    holder.parentElement?.removeChild(holder);
  }
}

function AppTable<RecordType extends object>({
  columns,
  scroll,
  dataSource,
  // 默认全居中（统一风格）；auto=智能判断（短列居中长文本左）可显式传参
  align = 'center',
  headerAlign,
  divider = false,
  ...rest
}: AppTableProps<RecordType>) {
  // 拖拽后的列宽（按列 key）；未拖过的列用 columns 里的初始 width
  const [colWidths, setColWidths] = React.useState<Record<string, number>>({});
  // 内容测量得到的最小宽（按列 index）；null=未测成功
  const [contentMinWidths, setContentMinWidths] = React.useState<number[] | null>(null);
  // 拖拽中的列 index（null=无）——用于跨列高亮（左右两列表头打标记）
  const [draggingCol, setDraggingCol] = React.useState<number | null>(null);
  // 悬停中的列 index（null=无）——与拖拽同机制，hover 时也显示左右浅块
  const [hoverCol, setHoverCol] = React.useState<number | null>(null);
  const wrapperRef = React.useRef<HTMLDivElement>(null);

  // 内容宽测量：dataSource 变化后克隆采样。只在首次及数据变化时执行。
  React.useLayoutEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;
    const table = el.querySelector('table') as HTMLTableElement | null;
    if (!table) return;
    try {
      const m = measureContentWidths(table);
      setContentMinWidths(m);
    } catch {
      setContentMinWidths(null); // 测量异常退 60px 兜底
    }
  }, [dataSource]);

  // 当前某列的实际 width（colWidths 优先，未拖过回退初始 width）
  const widthOf = React.useCallback(
    (col: Record<string, unknown>, index: number): number => {
      const key = String((col as { key?: React.Key; dataIndex?: React.Key })
        .key ?? (col as { dataIndex?: React.Key }).dataIndex ?? `_${index}`);
      const base = (col.width as number | undefined) ?? 0;
      return colWidths[key] ?? base;
    },
    [colWidths],
  );

  // 某列的最小宽 = max(60 兜底, 内容测量宽)
  const minWidthOf = React.useCallback((index: number): number => {
    const content = contentMinWidths && contentMinWidths[index] != null
      ? Number(contentMinWidths[index])
      : 0;
    return Math.max(MIN_COLUMN_WIDTH, content);
  }, [contentMinWidths]);

  // 拖拽回调：列 i 右缘分隔线 → i 与 i+1 差值互补；i+1 不可让则只改 i。
  // 最小宽按该列内容实际宽（measureContentWidths），拖到比内容还窄时锁止。
  const handleResize = React.useCallback(
    (index: number) =>
      (_: React.SyntheticEvent<Element>, { size }: ResizeCallbackData) => {
        setColWidths(prev => {
          const next = { ...prev };
          const cols = columns ?? [];
          const colI = cols[index] as Record<string, unknown> | undefined;
          if (!colI || 'children' in colI) return prev;
          const keyI = String((colI as { key?: React.Key; dataIndex?: React.Key })
            .key ?? (colI as { dataIndex?: React.Key }).dataIndex ?? `_${index}`);
          const curI = prev[keyI] ?? (colI.width as number | undefined) ?? 0;
          const target = Math.round(size.width);
          const delta = target - curI;
          const minI = minWidthOf(index);
          const colNext = cols[index + 1] as Record<string, unknown> | undefined;
          const resizableNext = colNext
            ? (colNext as { resizable?: boolean }).resizable !== false
              && 'children' in colNext === false
            : false;
          const hasNext = colNext && colNext.width != null
            && colNext.width !== 0
            && !('children' in colNext) && resizableNext;

          if (!hasNext) {
            // 无相邻可让 → 只改 A（总宽变化，横向滚动）；A 不越内容最小宽
            if (curI + delta < minI) return prev;
            next[keyI] = curI + delta;
            return next;
          }
          // 相邻可让：A 增 B 减 / A 减 B 增
          const keyNext = String(
            (colNext as { key?: React.Key; dataIndex?: React.Key }).key
            ?? (colNext as { dataIndex?: React.Key }).dataIndex
            ?? `_${index + 1}`);
          const curNext = prev[keyNext] ?? (colNext.width as number) ?? 0;
          const minNext = minWidthOf(index + 1);
          if (delta > 0) {
            // A 变宽 → B 变小；B 到内容最小宽则锁
            if (curNext - delta < minNext) return prev;
          } else {
            // A 变窄 → B 变大；A 到内容最小宽则锁
            if (curI + delta < minI) return prev;
          }
          next[keyI] = curI + delta;
          next[keyNext] = curNext - delta;
          return next;
        });
      },
    [columns, minWidthOf],
  );

  // 跨列高亮生效列（悬停优先显示，拖拽覆盖）
  const activeCol = hoverCol ?? draggingCol;

  // 列宽注入：已有 width 的列 → onHeaderCell 接上 Resizable 回调
  const resizeColumns = React.useMemo<ColumnsType<RecordType>>(() => {
    if (!columns) return [];
    return columns.map((col, index) => {
      const colObj = col as Record<string, unknown>;
      if ('children' in col) return col;
      if ((col as { resizable?: boolean }).resizable === false
        || !(col.width as number | undefined)) {
        // 非拖拽列也需对齐注入（align 参数对整表生效）
        return withAlign(col, colObj, align);
      }
      return withAlign({
        ...col,
        width: widthOf(colObj, index),
        onHeaderCell: () => ({
          // 跨列高亮：hover/拖拽中的当前列（左）与右邻列（右）的表头打标记，
          // CSS 伪元素各画 10px 浅块（左右对称）——探测区右下超出 th 被
          // 下个表头背景盖住的半边，用下个 th 自己的 ::before 补上
          width: widthOf(colObj, index),
          onResize: handleResize(index),
          onResizeStateChange: (active: boolean) =>
            setDraggingCol(active ? index : null),
          onHoverStateChange: (active: boolean) =>
            setHoverCol(active ? index : null),
          'data-resize-side': activeCol === index
            ? 'left'
            : activeCol != null && activeCol === index - 1
              ? 'right'
              : undefined,
        }),
      }, colObj, align);
    });
  }, [columns, widthOf, handleResize, align, activeCol]);

  // scroll 合并：x = 各列宽之和（动态跟随拖拽）；调用方 scroll 保留其余（y）
  const computedX = resizeColumns.reduce((s, c) => s + ((c.width as number) || 0), 0);
  const finalScroll = ((): TableProps<RecordType>['scroll'] => {
    if (computedX <= 0) return scroll ?? undefined;
    const s = (scroll ?? {}) as { x?: number | string | true; y?: number | string };
    return { ...s, x: computedX };
  })();

  return (
    <div
      ref={wrapperRef}
      style={{ width: '100%' }}
      data-apptable-align={align}
      data-apptable-header-align={headerAlign ?? 'follow'}
      className={divider ? 'apptable-divider' : undefined}
    >
      <Table<RecordType>
        {...rest}
        // 默认斑马纹（行区分，替代浓行线——方案 1）；页面显式 className 合并
        className={[rest.className, 'table-zebra'].filter(Boolean).join(' ')}
        dataSource={dataSource}
        columns={resizeColumns}
        components={{ header: { cell: ResizableTitle } }}
        scroll={finalScroll as TableProps<RecordType>['scroll']}
      />
    </div>
  );
}

export default AppTable;
