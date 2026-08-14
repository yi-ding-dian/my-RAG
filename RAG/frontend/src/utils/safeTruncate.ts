/**
 * 安全截断：截断点（maxLen）落在 markdown 图片引用 ![..](..) 内部时，
 * 把截断点后移到该引用闭合 `)` 之后，完整保留被切到的引用（图片优先）。
 *
 * 背景：ChunkCompareView 左栏对 chunk 文本 slice(0, MAX) + '…' 截断后
 * 再交给 MdImages 渲染——chunk 末尾多为图片引用，截断点常切在引用内部，
 * 引用不完整导致 MdImages 正则失配、图片显示为文本。本函数保证任何
 * 截断结果都不破坏引用完整性。
 *
 * 规则：
 * - 文本不超长 → 原样返回（与无截断行为一致）；
 * - 截断点在引用外 → 正常 slice + '…'；
 * - 截断点在引用内 → 后移到该引用闭合之后（相邻引用会被循环覆盖：
 *   后移点若又落进下一个引用，继续后移）；仅当后移点后仍有文本时才加
 *   '…'（省略号永远位于引用外，不会切进引用）；
 * - 引用本身超长（如 64 hex 文件名 + 前缀约 90 字符）时，后移后文本
 *   可能超出 maxLen——可接受（完整图片优先，省略号不会出现在引用内）。
 */
export const safeTruncateWithImages = (text: string, maxLen: number): string => {
  if (text.length <= maxLen) return text;
  // 与 MdImages / ChunkCompareView.collectImgSpans 同一匹配模式（局部正则，无共享 lastIndex）
  const re = /!\[([^\]]*)\]\(([^)]+)\)/g;
  const spans: Array<{ start: number; end: number }> = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    spans.push({ start: m.index, end: m.index + m[0].length });
  }
  let cut = maxLen;
  while (cut < text.length) {
    const sp = spans.find(s => s.start < cut && cut < s.end);
    if (!sp) break;
    cut = sp.end;
  }
  return cut >= text.length ? text : `${text.slice(0, cut)}…`;
};
