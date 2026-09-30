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

const IMG_FIXED_TAIL = `
要求：
- 只描述你确实看到的内容，不要推测、不要评价、不要补充常识
- 某个字段确实没有内容时，写"无"
- 不要开场白，不要总结
`;

/** 按选项 + 输出格式拼默认提示词
 *
 *  brief 格式不读图中文字——适合"整页全是文字但不需要理解含义"的图，
 *  代价是图里的字检索不到，故不适合证照类。 */
export function buildDefaultImgPrompt(
  opts: Record<string, boolean>, fmt: string,
): string {
  if (fmt === 'brief') {
    return '用一句话说明这张图片大致是什么、用来做什么的'
      + '（如「某公司生产厂房外景照片」「设备接线示意图」）。\n\n'
      + '要求：\n'
      + '- 只描述你确实看到的，不要推测、不要评价、不要陈述图中的具体内容与文字\n'
      + '- 不超过 30 字，写成一句话\n'
      + '- 直接输出这句话，不要加「这张图片」之类的开场白，不要换行';
  }
  const lines = IMG_OPTION_ORDER.filter(k => opts[k]).map(k => IMG_OPTION_LINES[k]);
  const use = lines.length ? lines : [IMG_OPTION_LINES.read_text];
  if (fmt === 'prose') {
    return '请查看这张图片，用中文写一段 2~4 句的客观描述，用于文档检索。\n\n'
      + '要点：\n' + use.map(l => `- ${l}`).join('\n') + '\n' + IMG_FIXED_TAIL;
  }
  return '请查看这张图片，按下面的字段输出中文描述，用于文档检索。\n\n'
    + '每行一个字段，只输出这几行，不要加其他说明：\n'
    + use.join('\n') + '\n' + IMG_FIXED_TAIL;
}
