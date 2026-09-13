import React from 'react';
import { renderTableBlocks } from '../components/common/MarkdownTable';

/**
 * 富文本段落渲染助手：先提取表格块（管道/HTML 表格 → 真表格），
 * 其余文本段交给调用方自己的渲染（MdImages 图片、高亮、pre-wrap 等）。
 *
 * renderText(seg, offset)：seg 为该段文本、offset 为它在原 text 中的
 * 起始偏移——调用方如有相对整体文本的高亮区间，据此换算到段内坐标。
 *
 * 召回侧三处共用（SourcePanel 引用来源 / RetrievalTest 检索测试 /
 * ChunkCompareView 切块对比）——表格展示与聊天气泡（MessageList）
 * 走同一套 MarkdownTable，交互与高亮逻辑由调用方保持。
 */
export const renderTextWithTables = (
  text: string,
  renderText: (seg: string, offset: number) => React.ReactNode,
): React.ReactNode[] => {
  let cursor = 0;
  return renderTableBlocks(text).map((block, i) => {
    if (typeof block === 'string') {
      const seg = block;
      const offset = cursor;
      cursor += seg.length;
      // key 前缀 rt（renderText）刻意避开 MarkdownTable 的 `t${i}`：表格块由
      // renderTableBlocks 直接产出、文本段由本函数产出，两者同处一个数组，
      // 前缀相同会撞 key（React 警告 two children with the same key，可能导致
      // 子元素被复制/遗漏）
      return <React.Fragment key={`rt${i}`}>{renderText(seg, offset)}</React.Fragment>;
    }
    return block;
  });
};

export default renderTextWithTables;
