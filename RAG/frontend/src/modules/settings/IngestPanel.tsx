import React from 'react';
import { Col, Form, InputNumber, Row } from 'antd';

/** 入库与限制面板（配置档案弹窗）：并发 / 单库上限 / 上传限制 / 单条输入长度 */
const IngestPanel: React.FC = () => (
  <Row gutter={12}>
    <Col span={6}>
      <Form.Item
        name="ingestion_concurrency"
        label="入库并发数"
        tooltip="同时解析入库的文档数上限，超出排队等待（并发过高可能打爆解析/向量服务）"
      >
        <InputNumber min={1} max={10} style={{ width: '100%' }} />
      </Form.Item>
    </Col>
    <Col span={6}>
      <Form.Item
        name="ingestion_kb_doc_limit"
        label="单库文档上限"
        tooltip="单知识库最大文档数，0=不限；上传/URL 导入时校验，超限提示后删除文档或调大配额"
      >
        <InputNumber min={0} max={50000} step={100} style={{ width: '100%' }} />
      </Form.Item>
    </Col>
    <Col span={6}>
      <Form.Item
        name="ingestion_max_upload_mb"
        label="上传大小限制（MB）"
        tooltip="单文件上传上限（默认 100MB）；nginx 反代层上限已放宽到 1024m，精确大小由后端按此配置校验"
      >
        <InputNumber min={1} max={2048} step={10} style={{ width: '100%' }} />
      </Form.Item>
    </Col>
    <Col span={6}>
      <Form.Item
        name="chat_max_query_len"
        label="单条输入长度（字）"
        tooltip="问题/检索 query 最大长度，超出返回友好提示（防超长粘贴耗尽上下文与费用）"
      >
        <InputNumber min={100} max={20000} step={100} style={{ width: '100%' }} />
      </Form.Item>
    </Col>
  </Row>
);

export default IngestPanel;
