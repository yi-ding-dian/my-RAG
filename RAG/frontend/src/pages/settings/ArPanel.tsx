import React from 'react';
import { Form, Input } from 'antd';

/** 档案基本设置面板（配置档案弹窗）：档案名称 */
const ArPanel: React.FC = () => (
  <Form.Item
    name="name"
    label="档案名称"
    rules={[{ required: true, message: '请输入档案名称' }]}
  >
    <Input placeholder="例如：本地 Qwen 默认、云端 DeepSeek" />
  </Form.Item>
);

export default ArPanel;
