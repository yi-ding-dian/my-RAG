import React from 'react';
import { renderTableBlocks } from '../components/MarkdownTable';

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
      return <React.Fragment key={`t${i}`}>{renderText(seg, offset)}</React.Fragment>;
    }
    return block;
  });
};

export default renderTextWithTables;
