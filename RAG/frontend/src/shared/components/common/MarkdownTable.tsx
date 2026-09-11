import React from 'react';
import MdImages from './MdImages';

/**
 * 回答正文的表格渲染（轻量子集，与 MdImages 同风格，不引入渲染库）：
 *
 * 支持两种输入：
 * 1. markdown 管道表格块（| 列 | 列 |）：多行连续 | 行，且"表头行 + 分隔行"
 *    齐全才渲染为 <table>——流式增量中半个表格（只有表头/无分隔行）自动
 *    回退纯文本原样展示，不存在闪烁的半截表格（与后端 HTML→管道转换后
 *    的产物格式一致）；
 * 2. HTML <table> 块（存量文档/MinerU 原始产物的兜底）：用浏览器内置
 *    DOMParser 解析（不执行脚本、只取文本，无 XSS 面），rowspan/colspan
 *    展开为重复单元格（与后端 table_normalizer 同算法，信息保真）。
 *
 * 单元格内图片引用（![]()）交给 MdImages 渲染；其余文本原样（React
 * 文本节点天然转义，不注入 innerHTML）。
 */

/** 管道表格行：`| 单元格 |` 首尾竖线（后端生成格式） */
const PIPE_ROW_RE = /^\s*\|.*\|\s*$/;
/** GFM 分隔行：| --- | --- |（单元格只含 - 与 :，支持 :-- 对齐冒号变体） */
const SEP_ROW_RE = /^\s*\|(?:[ \t:]*:?-{2,}:?[ \t:]*\|)+\s*$/;
const SEP_CELL_RE = /^[ \t:]*:?-{2,}:?[ \t:]*$/;

/** 单元格内换行/多段折叠（与后端 _WS_RE 一致） */
const fold = (s: string): string => s.replace(/\s+/g, ' ').trim();

/** 拆分管道行单元格：\| 为字面竖线占位（私用区字符）→ 拆分后还原 */
const PIPE_ESC = '';
const splitPipeCells = (line: string): string[] => {
  const inner = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  return inner
    .replace(/\\\|/g, PIPE_ESC)
    .split('|')
    .map((c) => c.replace(new RegExp(PIPE_ESC, 'g'), '|').trim());
};

const isSepRow = (line: string): boolean => {
  const t = line.trim();
  if (!t) return false;
  if (SEP_ROW_RE.test(t)) return true;
  // 宽松形态（LLM 生成的变体，无首尾竖线）：整行去竖线后逐格校验
  const inner = t.replace(/^\|/, '').replace(/\|$/, '');
  if (!inner.includes('|') || !/-{2,}/.test(inner)) return false;
  return inner.split('|').every((c) => SEP_CELL_RE.test(c));
};

interface TableModel {
  header: string[];
  rows: string[][];
}

/** 管道表格行块 → 模型；缺分隔行返回 null（调用方回退纯文本） */
const parsePipeTable = (lines: string[]): TableModel | null => {
  if (lines.length < 2) return null;
  if (!PIPE_ROW_RE.test(lines[0]) || !isSepRow(lines[1])) return null;
  const header = splitPipeCells(lines[0]);
  const rows = lines
    .slice(2)
    .filter((l) => PIPE_ROW_RE.test(l))
    .map((l) => splitPipeCells(l));
  return { header, rows };
};

/**
 * HTML 表格解析（DOMParser）：单元格文本折叠，rowspan/colspan 展开为重复
 * 单元格（与后端 table_normalizer 同一算法），首个含 <th> 行为表头，
 * 无 th 时首行为表头。
 */
const parseHtmlTable = (html: string): TableModel | null => {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const el = doc.querySelector('table');
  if (!el) return null;
  const grid: string[][] = [];
  const pending = new Map<number, [number, string]>(); // col -> (剩余行, 文本)
  let headerIdx = -1;
  el.querySelectorAll('tr').forEach((tr, i) => {
    const out: string[] = [];
    let col = 0;
    tr.querySelectorAll('th, td').forEach((c) => {
      while (pending.has(col)) {
        const [rem, txt] = pending.get(col)!;
        out.push(txt);
        if (rem <= 1) pending.delete(col);
        else pending.set(col, [rem - 1, txt]);
        col += 1;
      }
      const text = fold(c.textContent ?? '');
      const rs = Math.max(parseInt(c.getAttribute('rowspan') ?? '1', 10) || 1, 1);
      const cs = Math.max(parseInt(c.getAttribute('colspan') ?? '1', 10) || 1, 1);
      const startCol = col;
      for (let k = 0; k < cs; k++) out.push(text);
      if (rs > 1) {
        for (let k = 0; k < cs; k++) pending.set(startCol + k, [rs - 1, text]);
      }
      if (headerIdx === -1 && c.tagName.toLowerCase() === 'th') headerIdx = i;
      col += cs;
    });
    grid.push(out);
  });
  if (!grid.length) return null;
  if (headerIdx === -1) headerIdx = 0;
  const width = Math.max(...grid.map((r) => r.length), 0);
  const padded = grid.map((r) => r.concat(Array(Math.max(0, width - r.length)).fill('')));
  return {
    header: padded[headerIdx],
    rows: padded.filter((_, j) => j !== headerIdx),
  };
};

/** HTML 表格块正则（未闭合的整块回退纯文本，不渲染） */
const HTML_TABLE_OPEN = /<table\b/i;

/** 渲染模型 → React <table>：表头行 th + 数据行 td，容器横向滚动 */
const MarkdownTable: React.FC<{ model: TableModel }> = ({ model }) => {
  if (!model.header.length && !model.rows.length) return null;
  return (
    <div className="md-table-wrap">
      <table className="md-table">
        <thead>
          <tr>
            {model.header.map((h, i) => (
              <th key={i}>{h || ' '}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {model.rows.map((row, ri) => (
            <tr key={ri}>
              {row.map((cell, ci) => (
                <td key={ci}>
                  <MdImages text={cell} maxWidth={320} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

/**
 * 文本流中的表格块识别：返回 ReactNode 数组（表格为节点、其余按字符串
 * 原样返回，调用方继续走 [n] 引用标 / MdImages 管道）。
 */
export const renderTableBlocks = (text: string): React.ReactNode[] => {
  if (!text || (!text.includes('|') && !HTML_TABLE_OPEN.test(text))) return [text];
  const nodes: React.ReactNode[] = [];
  const lines = text.split('\n');
  // 每行起始偏移（行间恰好一个 \n）
  const starts: number[] = [];
  let acc = 0;
  for (const line of lines) {
    starts.push(acc);
    acc += line.length + 1;
  }
  let last = 0;
  let key = 0;
  const flushText = (from: number, to: number) => {
    if (to > from) nodes.push(text.slice(from, to));
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const lineStart = starts[i];

    // 1) HTML 表格块：<table…> … </table>（起始与闭合同行/跨行均可）
    if (HTML_TABLE_OPEN.test(line)) {
      const openIdx = text.indexOf('<table', lineStart);
      const closeEnd = text.indexOf('</table>', openIdx);
      if (closeEnd !== -1) {
        const blockEnd = closeEnd + 8;
        const model = parseHtmlTable(text.slice(openIdx, blockEnd));
        if (model) {
          flushText(last, openIdx);
          nodes.push(<MarkdownTable key={`t${key++}`} model={model} />);
          last = blockEnd;
          i = text.slice(0, blockEnd).split('\n').length - 1;
          continue;
        }
      }
    }

    // 2) 管道表格：当前行 | 行 且下一行为分隔行 → 收集连续 | 行成块
    if (PIPE_ROW_RE.test(line) && i + 1 < lines.length && isSepRow(lines[i + 1])) {
      let j = i;
      while (j < lines.length && PIPE_ROW_RE.test(lines[j])) j++;
      const model = parsePipeTable(lines.slice(i, j));
      if (model) {
        // 消费区间：lineStart 起 j-i 行（含行间换行）
        const blockEnd = starts[j - 1] + lines[j - 1].length + 1;
        flushText(last, lineStart);
        nodes.push(<MarkdownTable key={`t${key++}`} model={model} />);
        last = blockEnd;
        i = j - 1;
        continue;
      }
    }
  }
  flushText(last, text.length);
  return nodes;
};

export default MarkdownTable;
