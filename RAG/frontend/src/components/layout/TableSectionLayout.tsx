import React from 'react';
import { Card, Typography } from 'antd';
import LeftPagination from './LeftPagination';

const { Text } = Typography;

/**
 * 表格数据区布局（子布局）：工具条 + 滚动表格 + 分页条。
 *
 * 「表格区撑满剩余屏幕、行多内部滚动、分页固定屏幕最下方」：
 * - 工具条（toolbar，可选）：筛选/搜索/刷新等，右侧自动留白显示 total
 * - 表格滚动区：flex:1 撑满 PageLayout 剩余高度（卡片到视口底、分页贴屏底）；
 *   行多超出时本区 overflow auto 内部滚动（Table 传 sticky 表头吸顶）
 * - 分页条：贴本容器底部（页面层 PageLayout children flex:1 保证位于屏幕最下方）
 * - maxRows/scrollContentHeight：可选上限（超出后内部滚动而非占满视口）——
 *   业务需要「最多显示 n 行」时传入，Table 自身 scroll.y 与之配合
 *
 * 用法（与 PageLayout 组合）：
 *   <PageLayout title=…>
 *     <TableSectionLayout
 *       total={total} page={page} pageSize={pageSize}
 *       onPageChange={…}
 *       toolbar={<><Segmented/><Input.Search/></>}
 *       maxRows={10}                            // 可选：最多 10 行，超出滚动
 *     >
 *       <Table dataSource={items} columns={columns} pagination={false} sticky />
 *     </TableSectionLayout>
 *   </PageLayout>
 */

export interface TableSectionLayoutProps {
  /** 表格（antd Table, pagination={false}）或自定义数据区 */
  children?: React.ReactNode;
  /** 工具条（可选）：筛选/搜索/刷新按钮等 */
  toolbar?: React.ReactNode;
  /** 总条数（分页显示「共 N 条」） */
  total?: number;
  page?: number;
  pageSize?: number;
  onPageChange?: (page: number, pageSize: number) => void;
  pageSizeOptions?: number[];
  /** 滚动区高度策略：
   *  - 给 scrollContentHeight / maxRows 之一：固定高（超出内部滚动）
   *  - 都不给：auto 撑满剩余（配合页面级 flex 布局自然贴底）
   * maxRows：按行数换算高度（antd Table 行高 ≈ 40px + 表头 40） */
  maxRows?: number;
  /** 直接指定滚动区高度（px，优先级高于 maxRows） */
  scrollContentHeight?: number;
  /** empty/loading 状态下渲染的内容（页面自行传入 Skeleton/空态） */
  emptyOrLoading?: React.ReactNode;
  /** 是否显示分页（默认 true） */
  showPagination?: boolean;
}

const TableSectionLayout: React.FC<TableSectionLayoutProps> = ({
  children,
  toolbar,
  total = 0,
  page = 1,
  pageSize = 50,
  onPageChange,
  pageSizeOptions = [20, 50, 100, 200],
  emptyOrLoading,
  showPagination = true,
}) => {

  return (
    <Card
      style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}
      styles={{ body: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } }}
    >
      {/* 工具条：筛选/搜索/刷新 + 右侧 total */}
      {toolbar && (
        <div
          style={{
            display: 'flex',
            gap: 12,
            alignItems: 'center',
            marginBottom: 12,
            flexWrap: 'wrap',
          }}
        >
          {toolbar}
          <span style={{ flex: 1 }} />
          <Text type="secondary">共 {total} 条</Text>
        </div>
      )}

      {/* 滚动表格区：flex:1 撑满全部剩余（卡片填到视口底、分页在屏幕最下方）；
          Table 自身 scroll.y(via children) 或本区 overflow auto 处理超出滚动；
          无固定高上限——数据多时占满视口、数据少时行少但底部仍贴屏底（分页贴底） */}
      <div
        style={{
          flex: '1 1 0%',
          minHeight: 0,
          overflowY: 'auto',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {emptyOrLoading ?? children}
      </div>

      {/* 分页条：固定左下、不随表格滚动（统一 LeftPagination 规范，
          与 Users/Logs 一致——所有表格页同一分页样式） */}
      {showPagination && (
        <LeftPagination
          page={page}
          pageSize={pageSize}
          total={total}
          pageSizeOptions={pageSizeOptions}
          onChange={(p, ps) => onPageChange?.(p, ps)}
        />
      )}
    </Card>
  );
};

export default TableSectionLayout;
