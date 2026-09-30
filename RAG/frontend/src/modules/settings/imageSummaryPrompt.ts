/**
 * 图片摘要提示词的默认模板（按内容选项 + 输出格式拼装）
 *
 * 与后端 `backend/services/image_summary.py` 的 `default_prompt` **同措辞**。
 * 前端这份只用于「选项一变就实时显示会生成什么」，真正生效的默认值仍由后端
 * 兜底——两边万一不一致，以后端为准（用户看到的与实际的只会在这种极端情况下
 * 短暂对不上，不会写坏数据）。
 *
 * 超管（系统配置 → 图片摘要）与部门管理员（部门配置 → 图片摘要）两处都用它：
 * 同一套选项该生成同一份提示词，各写一份迟早会漂。
 */

/** 每个内容选项对应提示词里的一行 */
export const IMG_OPTION_LINES: Record<string, string> = {
  label_type: '类型：这是什么（文件类型或场景）',
  read_text: '文字：图中可见的关键文字（标题、单位名称、编号、日期、金额、规格等）',
  describe_scene: '画面：画面主要对象与特征（设备、场地、人物、签章等）',
  describe_layout: '版式：版式结构（表格行列、签章位置、分区布局等）',
};

/** 选项在提示词里的出现顺序 */
export const IMG_OPTION_ORDER = [
  'label_type', 'read_text', 'describe_scene', 'describe_layout',
];

/** 「简介」行：**恒排在首位，不受内容选项控制**（与后端 `_BRIEF_LINE` 同措辞）
 *
 *  它是整段的语义锚点（一句话概括），向量检索靠它聚焦；后面的字段补实体词，
 *  供关键词命中。两者在同一个 chunk 里，混合检索的两路才都吃得到。
 */
export const IMG_BRIEF_LINE = '简介：用一句话说明这是什么（文件类型或场景），30 字以内';

const IMG_FIXED_TAIL = `
要求：
- 只描述你确实看到的内容，不要推测、不要评价、不要补充常识
- 某个字段确实没有内容时，写"无"
- 不要开场白，不要总结
`;

/** 按选项 + 输出格式 + **读图模板**拼默认提示词
 *
 *  `template`：选中的读图模板正文（`image_template_options[].prompt`）。它与
 *  聊天识图**共用**——拼在最前面，后面才接各自的输出要求，这样界面上看到的
 *  提示词与实际发给模型的完全一致（此前前端不拼模板，预览少一截，看着像
 *  「读图模板」和「图片摘要提示词」两回事）。
 *
 *  brief 格式**不用模板**：它明确"不读图中文字"，与模板的"逐字抄录"直接
 *  冲突，硬拼会给模型自相矛盾的指令。 */
export function buildDefaultImgPrompt(
  opts: Record<string, boolean>, fmt: string, template = '',
): string {
  if (fmt === 'brief') {
    return '用一句话说明这张图片大致是什么、用来做什么的'
      + '（如「某公司生产厂房外景照片」「设备接线示意图」）。\n\n'
      + '要求：\n'
      + '- 只描述你确实看到的，不要推测、不要评价、不要陈述图中的具体内容与文字\n'
      + '- 不超过 30 字，写成一句话\n'
      + '- 直接输出这句话，不要加「这张图片」之类的开场白，不要换行';
  }
  const body = (template || '').trim();
  const head = body ? `${body}\n\n` : '';
  const lines = IMG_OPTION_ORDER.filter(k => opts[k]).map(k => IMG_OPTION_LINES[k]);
  const use = lines.length ? lines : [IMG_OPTION_LINES.read_text];
  if (fmt === 'prose') {
    return head
      + '请用中文写一段 2~4 句的客观描述，用于文档检索。\n'
      + '**首句先用一句话概括这是什么（文件类型或场景）**，再展开细节。\n'
      + '要点：\n' + use.map(l => `- ${l}`).join('\n') + '\n' + IMG_FIXED_TAIL;
  }
  return head
    + '请按下面的字段输出中文描述，用于文档检索。\n'
    + '每行一个字段，只输出这几行，不要加其他说明：\n'
    + [IMG_BRIEF_LINE, ...use].join('\n') + '\n' + IMG_FIXED_TAIL;
}
