import React from 'react';
import { Col, Form, Input, InputNumber, Row } from 'antd';

/** MinerU 文档解析面板（配置档案弹窗）：服务地址 / 超时 */
const MineruPanel: React.FC = () => (
  <Row gutter={12}>
    <Col span={14}>
      <Form.Item name="mineru_url" label="服务地址">
        <Input placeholder="http://localhost:8001" />
      </Form.Item>
    </Col>
    <Col span={6}>
      <Form.Item name="mineru_timeout" label="超时（秒）">
        <InputNumber min={10} max={3600} style={{ width: '100%' }} />
      </Form.Item>
    </Col>
  </Row>
);

export default MineruPanel;
