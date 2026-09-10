import React from 'react';
import { Col, Form, Input, Row, Select } from 'antd';

/** 向量存储面板（配置档案弹窗）：后端（Chroma/Milvus）/ Milvus 地址 */
const VectorStorePanel: React.FC = () => (
  <>
    <Row gutter={12}>
      <Col span={8}>
        <Form.Item name="vector_store_backend" label="后端">
          <Select
            options={[
              { value: 'chroma', label: 'Chroma（本地嵌入式）' },
              { value: 'milvus', label: 'Milvus（服务化）' },
            ]}
          />
        </Form.Item>
      </Col>
      <Col span={16}>
        <Form.Item
          name="vector_store_milvus_uri"
          label="Milvus 地址"
          tooltip="backend=milvus 时生效；仅填主机端口会自动补 http://；切换后检索/入库在下一请求生效"
        >
          <Input placeholder="192.168.0.74:19530" />
        </Form.Item>
      </Col>
    </Row>
  </>
);

export default VectorStorePanel;
