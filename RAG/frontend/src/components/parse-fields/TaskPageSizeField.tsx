import React from 'react';
import { Form, InputNumber } from 'antd';

interface TaskPageSizeFieldProps {
  /** 表单字段名（默认 task_page_size） */
  name?: string;
  /** 默认值 */
  initialValue?: number;
}

/** 任务页面大小：每个解析任务处理的页数（1-128，默认 12）。
 * C1：后端当前只存配置不消费（单任务解析，见 ingestion_service
 * _DEFAULT_PARSER_CONFIG 注释"存配置，当前单任务解析"），文案补"暂不生效"提示 */
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
