import React from 'react';
import { Alert, Col, Form, Input, InputNumber, Row } from 'antd';

const { Password } = Input;

/** DeepDoc 解析面板（配置档案弹窗，RAGFlow）：服务地址 / 超时 / 账号 / 临时数据集前缀 */
const DeepdocPanel: React.FC = () => (
  <>
    <Alert
      type="info"
      showIcon
      style={{ marginBottom: 12 }}
      message="DeepDoc 通过 RAGFlow 服务解析 PDF，表格输出为可检索的 HTML（vs MinerU 表格为图片不可检索）"
    />
    <Row gutter={12}>
      <Col span={12}>
        <Form.Item name="deepdoc_base_url" label="服务地址">
          <Input placeholder="http://127.0.0.1:9380" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item name="deepdoc_timeout" label="超时（秒）">
          <InputNumber min={30} max={3600} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
    </Row>
    <Row gutter={12}>
      <Col span={6}>
        <Form.Item name="deepdoc_email" label="邮箱">
          <Input placeholder="user@example.com" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="deepdoc_password"
          label="密码"
          tooltip="保存后仅显示脱敏值；不修改请留空"
        >
          <Password placeholder="******" />
        </Form.Item>
      </Col>
      <Col span={8}>
        <Form.Item
          name="deepdoc_dataset_prefix"
          label="临时数据集前缀"
          tooltip="解析时在 RAGFlow 创建临时数据集（自动清理），此前缀便于排查残留"
        >
          <Input placeholder="myrag-tmp-" />
        </Form.Item>
      </Col>
    </Row>
  </>
);

export default DeepdocPanel;
