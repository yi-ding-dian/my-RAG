import React from 'react';
import { Col, Form, Input, Row, Select } from 'antd';

const { Password } = Input;

/** 对象存储面板（配置档案弹窗，MinIO）：Endpoint / Bucket / HTTPS / Region / 密钥 */
const MinioPanel: React.FC = () => (
  <>
    <Row gutter={12}>
      <Col span={10}>
        <Form.Item name="minio_endpoint" label="Endpoint">
          <Input placeholder="127.0.0.1:9000" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item name="minio_bucket" label="Bucket">
          <Input placeholder="my-rag" />
        </Form.Item>
      </Col>
      <Col span={4}>
        <Form.Item name="minio_secure" label="HTTPS">
          <Select
            options={[
              { value: true, label: '是' },
              { value: false, label: '否' },
            ]}
          />
        </Form.Item>
      </Col>
      <Col span={4}>
        <Form.Item name="minio_region" label="Region">
          <Input placeholder="us-east-1" />
        </Form.Item>
      </Col>
    </Row>
    <Row gutter={12}>
      <Col span={10}>
        <Form.Item name="minio_access_key" label="Access Key">
          <Input placeholder="rag_flow" />
        </Form.Item>
      </Col>
      <Col span={10}>
        <Form.Item
          name="minio_secret_key"
          label="Secret Key"
          tooltip="保存后仅显示脱敏值；不修改请留空"
        >
          <Password placeholder="******" />
        </Form.Item>
      </Col>
    </Row>
  </>
);

export default MinioPanel;
