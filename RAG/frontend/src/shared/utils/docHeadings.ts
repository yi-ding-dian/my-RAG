/**
 * 解析产物（Markdown）的标题抽取与目录树构建。
 *
 * 文档预览与切块详情两处的「目录」共用这一份：正则、层级嵌套、默认展开规则都从
 * 这里来——各写一份的话，两边的标题数量、层级、点击落点迟早对不上。
 */

export interface DocHeading {
  /** 层级（# 的个数，1~6） */
  level: number;
  /** 标题文本（去掉 # 前缀与首尾空白） */
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

/** Markdown 标题行：行首 1~6 个 # + 至少一个空格/制表符 + 文本 */
const HEADING_SRC = '^(#{1,6})[ \\t]+(.+?)[ \\t]*$';

/** 抽取全文标题（按调用新建正则实例，避免共享 lastIndex 在多次调用间串状态） */
export const extractHeadings = (fullText: string): DocHeading[] => {
  if (!fullText) return [];
  const re = new RegExp(HEADING_SRC, 'gm');
  const out: DocHeading[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(fullText)) !== null) {
    out.push({
      level: m[1].length,
      text: m[2].trim(),
      pos: m.index,
      end: m.index + m[0].length,
      index: out.length,
      raw: m[0],
    });
  }
  return out;
};

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
