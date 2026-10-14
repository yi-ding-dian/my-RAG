import React from 'react';
import { Form, InputNumber } from 'antd';

interface TaskPageSizeFieldProps {
  /** 表单字段名（默认 task_page_size） */
  name?: string;
  /** 默认值 */
  initialValue?: number;
}

/** 任务页面大小：每个解析任务处理的页数（1-128，默认 12）。
 *
 * **当前未渲染**（2026-10-14 起从解析配置弹窗隐藏）：后端单任务解析不消费该
 * 参数——它不在透传白名单 _PARSER_PARSE_OPTS 里（见
 * backend/services/ingestion/params.py），值只存进 parser_config、传不到 MinerU。
 * 溯源：该字段照搬 KnowFlow/RAGFlow 范式（那边大文档拆多个解析任务分批跑，此为
 * 批大小）。8 月 C1 曾以"后端已存配置未来可能生效"为由保留显示 +"暂不生效"
 * 提示；10 月 14 日用户拍板隐藏（界面已重组，留一个填了不生效的必填项是认知
 * 负担）。后端默认值(12)、范围校验、三级合并（请求 > 文档已存 > 默认）全部
 * 保留，已存配置不受影响；未来实现分批解析时把本组件加回弹窗渲染即可。 */
const TaskPageSizeField: React.FC<TaskPageSizeFieldProps> = ({
  name = 'task_page_size',
  initialValue = 12,
}) => (
  <Form.Item
    name={name}
    label="任务页面大小"
    initialValue={initialValue}
    extra="每个解析任务处理的页数（1-128）。暂不生效：当前单任务解析，此参数主要给 MinerU 分页参考，后端暂不消费"
    rules={[
      { required: true, message: '请输入任务页面大小' },
      { type: 'number', min: 1, max: 128, message: '范围 1-128' },
    ]}
  >
    <InputNumber min={1} max={128} style={{ width: '100%' }} />
  </Form.Item>
);

export default TaskPageSizeField;
