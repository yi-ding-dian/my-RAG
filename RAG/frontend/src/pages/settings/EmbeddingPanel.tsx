import React from 'react';
import { Col, Form, Input, InputNumber, Row } from 'antd';

const { Password } = Input;

/** Embedding 模型面板（配置档案弹窗）：API 地址 / 模型 / 向量维度 */
const EmbeddingPanel: React.FC = () => (
  <>
    <Row gutter={12}>
      <Col span={14}>
        <Form.Item
          name="embedding_base_url"
          label="API 地址"
          rules={[{ required: true, message: '必填' }]}
        >
          <Input placeholder="http://127.0.0.1:8300/v1" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="embedding_model"
          label="模型名称"
          rules={[{ required: true, message: '必填' }]}
        >
          <Input placeholder="bge-m3" />
        </Form.Item>
      </Col>
      <Col span={4}>
        <Form.Item
          name="embedding_api_key"
          label="API Key"
          tooltip="保存后仅显示脱敏值；不修改请留空"
        >
          <Password placeholder="***" />
        </Form.Item>
      </Col>
    </Row>
    <Row gutter={12}>
      <Col span={6}>
        <Form.Item name="embedding_dimension" label="向量维度">
          <InputNumber min={64} max={8192} step={64} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
    </Row>
  </>
);

export default EmbeddingPanel;
