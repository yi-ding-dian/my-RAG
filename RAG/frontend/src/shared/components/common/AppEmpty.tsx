/**
 * 统一空态组件（波2）：图标 + 主标题 + 描述（灰字）+ 可选操作按钮。
 * 替代各页散落的 <Empty description="..."> 手动空态，统一视觉；文案由各页传入，不覆盖页面逻辑。
 */
import React from 'react';
import { Empty, Typography } from 'antd';

interface AppEmptyProps {
  /** 主标题（默认「暂无数据」） */
  title?: React.ReactNode;
  /** 描述文字（灰字小字，可传 JSX） */
  description?: React.ReactNode;
  /** 操作区（按钮等） */
  action?: React.ReactNode;
  style?: React.CSSProperties;
}

const AppEmpty: React.FC<AppEmptyProps> = ({
  title = '暂无数据',
  description,
  action,
  style,
}) => (
  <Empty
    image={Empty.PRESENTED_IMAGE_SIMPLE}
    style={{ padding: '32px 0', ...style }}
    description={
      <div>
        <Typography.Text strong>{title}</Typography.Text>
        {description && (
          <div style={{ marginTop: 4 }}>
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              {description}
            </Typography.Text>
          </div>
        )}
      </div>
    }
  >
    {action}
  </Empty>
);

export default AppEmpty;
