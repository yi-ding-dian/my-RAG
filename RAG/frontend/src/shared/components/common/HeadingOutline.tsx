import React, { useMemo } from 'react';
import { Button, Popover, Tree, Typography } from 'antd';
import { UnorderedListOutlined } from '@ant-design/icons';
import {
  buildOutlineTree,
  defaultExpandedKeys,
  type DocHeading,
} from '../../utils/docHeadings';

const { Text } = Typography;

interface HeadingOutlineProps {
  /** 标题列表（extractHeadings 产物）；空数组时弹层提示未识别到标题 */
  headings: DocHeading[];
  /** 点击标题的落点，由调用方决定：预览全文已渲染 → 锚点滚动；
   *  切块详情右栏只渲染当前页 → 跨页定位 */
  onJump: (h: DocHeading) => void;
  /** 切换文档时重置树的展开与选中（一般传文档 id）；
   *  调用方已按文档 key 重挂载时可不传 */
  resetKey?: string | number;
}

/**
 * 目录按钮 + 标题树弹层（文档预览 / 切块详情共用）：
 * 按钮文案「目录（N）」，弹层内把扁平标题嵌套成树，默认展开一级（首屏可见前两级），
 * 三级及以下点箭头展开；点击标题交给 onJump 跳转。
 */
const HeadingOutline: React.FC<HeadingOutlineProps> = ({ headings, onJump, resetKey }) => {
  const treeData = useMemo(() => buildOutlineTree(headings), [headings]);
  const expandedKeys = useMemo(() => defaultExpandedKeys(headings), [headings]);

  return (
    <Popover
      trigger="click"
      placement="bottomRight"
      content={(
        <div style={{ minWidth: 280, maxWidth: 460 }}>
          {headings.length === 0 ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              未识别到标题（解析产物里没有 Markdown 标题）
            </Text>
          ) : (
            <Tree
              // 换文档重挂载：展开态与树内滚动位置跟着重置
              key={resetKey}
              treeData={treeData}
              defaultExpandedKeys={expandedKeys}
              height={380}
              onSelect={keys => {
                const h = headings.find(x => `h-${x.index}` === String(keys[0] ?? ''));
                if (h) onJump(h);
              }}
            />
          )}
        </div>
      )}
    >
      <Button size="small" icon={<UnorderedListOutlined />}>
        目录{headings.length > 0 ? `（${headings.length}）` : ''}
      </Button>
    </Popover>
  );
};

export default HeadingOutline;
