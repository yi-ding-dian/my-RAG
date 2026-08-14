/**
 * 统一页头组件（波2）：大标题 + 副标题（灰字）+ 右上操作区 + 可选面包屑。
 * 各页页头统一视觉结构：标题与副标题左侧一列，extra 操作区右侧（自动换行）。
 * 颜色全部走 AntD token，自动适配暗色模式。
 */
import React from 'react';
import { Space, Typography } from 'antd';

interface PageHeaderProps {
  /** 页面大标题 */
  title: React.ReactNode;
  /** 副标题（灰字小字） */
  description?: React.ReactNode;
  /** 面包屑：渲染在标题上方 */
  breadcrumb?: React.ReactNode;
  /** 右上操作区（flex 布局） */
  extra?: React.ReactNode;
  /** 整体下方留白 */
  style?: React.CSSProperties;
}

const PageHeader: React.FC<PageHeaderProps> = ({
  title,
  description,
  breadcrumb,
  extra,
  style,
}) => (
  <div style={{ marginBottom: 16, ...style }}>
    {breadcrumb && <div style={{ marginBottom: 8 }}>{breadcrumb}</div>}
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        gap: 16,
        flexWrap: 'wrap',
      }}
    >
      <div style={{ minWidth: 0 }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {title}
        </Typography.Title>
        {description && (
          <Typography.Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
            {description}
          </Typography.Text>
        )}
      </div>
      {extra && <Space wrap>{extra}</Space>}
    </div>
  </div>
);

export default PageHeader;
