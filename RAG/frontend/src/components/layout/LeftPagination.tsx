import React from 'react';
import { Pagination } from 'antd';

interface LeftPaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onChange?: (page: number, pageSize: number) => void;
  pageSizeOptions?: number[];
}

/**
 * 统一分页条（表格数据区通用）：固定左下、不随表格滚动。
 * 所有表格页分页条样式唯一规范（与日志查看页一致）：
 * 共 N 条 <上一页 1..N 下一页> N条/页(下拉)
 */
const LeftPagination: React.FC<LeftPaginationProps> = ({
  page,
  pageSize,
  total,
  onChange,
  pageSizeOptions = [10, 20, 50],
}) => (
  <div style={{ display: 'flex', justifyContent: 'flex-start', marginTop: 16, flexShrink: 0 }}>
    <Pagination
      current={page}
      pageSize={pageSize}
      total={total}
      showSizeChanger
      pageSizeOptions={pageSizeOptions}
      showTotal={t => `共 ${t} 条`}
      onChange={(p, ps) => onChange?.(p, ps)}
    />
  </div>
);

export default LeftPagination;
