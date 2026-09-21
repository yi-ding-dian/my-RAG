/**
 * 解析产物（Markdown）的标题 → 目录树。
 *
 * **标题识别在后端**（`backend.chunking.common._iter_headings`，与切块同源）：
 * 本文件只把后端下发的 `headings` 转成 DocHeading，并负责层级嵌套与默认展开。
 * 文档预览与切块详情两处的「目录」共用这一份，标题数量、层级、点击落点自然一致。
 *
 * 为什么不再在前端抽：前端曾用正则只认 `#` 标题，与后端两套逻辑各行其是——
 * MinerU 漏标 `##` 的裸编号标题后端切块认得出、目录树却缺章。
 * **不要在前端重写识别规则。**
 */
import type { ApiHeading } from '../api/types';

export interface DocHeading {
  /** 层级（后端推断，1~6；编号体系下为文档内相对层级） */
  level: number;
  /** 标题文本（不含 # 前缀） */
  text: string;
  /** 标题行在全文中的起始偏移（切块详情跨页定位的基准） */
  pos: number;
  /** 标题行结束偏移（不含换行符；预览按标题切分块时用） */
  end: number;
  /** 全文出现顺序编号（0 起）；预览的锚点 id 即为 doc-h-{index} */
  index: number;
  /** 标题行原文（含 # 前缀；预览整行渲染用） */
  raw: string;
}

/** 后端下发的标题 → DocHeading（补全文顺序编号 index） */
export const toDocHeadings = (items?: ApiHeading[] | null): DocHeading[] =>
  (items ?? []).map((h, i) => ({ ...h, index: i }));

/** 目录树节点（antd Tree 的 DataNode 子集） */
export interface OutlineNode {
  key: string;
  title: string;
  children: OutlineNode[];
}

/**
 * 扁平标题按层级嵌套成树，供 Tree 折叠浏览（大文档上百个标题，全平铺翻不动）。
 * 用栈记住"当前路径"：新标题比栈顶浅就往上弹，直到找到层级更小的节点挂上去。
 * 层级跳变（如 # 直接到 ###）按实际层级差处理，不补齐中间层。
 */
export const buildOutlineTree = (headings: DocHeading[]): OutlineNode[] => {
  const root: OutlineNode[] = [];
  const stack: { level: number; children: OutlineNode[] }[] = [{ level: 0, children: root }];
  headings.forEach(h => {
    const node: OutlineNode = { key: `h-${h.index}`, title: h.text, children: [] };
    while (stack.length > 1 && stack[stack.length - 1].level >= h.level) stack.pop();
    stack[stack.length - 1].children.push(node);
    stack.push({ level: h.level, children: node.children });
  });
  return root;
};

/** 默认展开的节点：一级标题 = 首屏可见前两级，三级及以下点箭头再展开 */
export const defaultExpandedKeys = (headings: DocHeading[]): string[] =>
  headings.filter(h => h.level === 1).map(h => `h-${h.index}`);
