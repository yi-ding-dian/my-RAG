import React from 'react';
import { Col, Form, Input, InputNumber, Row } from 'antd';

const { Password } = Input;

/** 数据库面板（配置档案弹窗）：主机 / 端口 / 库名 / 账号 / 连接串覆盖 */
const MysqlPanel: React.FC = () => (
  <>
    <Row gutter={12}>
      <Col span={10}>
        <Form.Item name="mysql_host" label="主机">
          <Input placeholder="127.0.0.1" />
        </Form.Item>
      </Col>
      <Col span={4}>
        <Form.Item name="mysql_port" label="端口">
          <InputNumber min={1} max={65535} style={{ width: '100%' }} placeholder="5455" />
        </Form.Item>
      </Col>
      <Col span={10}>
        <Form.Item name="mysql_database" label="数据库名">
          <Input placeholder="my_rag" />
        </Form.Item>
      </Col>
    </Row>
    <Row gutter={12}>
      <Col span={10}>
        <Form.Item name="mysql_user" label="用户名">
          <Input placeholder="ragflow" />
        </Form.Item>
      </Col>
      <Col span={10}>
        <Form.Item
          name="mysql_password"
          label="密码"
          tooltip="保存后仅显示脱敏值；不修改请留空"
        >
          <Password placeholder="******" />
        </Form.Item>
      </Col>
    </Row>
    <Row gutter={12}>
      <Col span={20}>
        <Form.Item
          name="mysql_url"
          label="连接串覆盖（可选）"
          tooltip="非空时优先生效（如测试注入 sqlite+aiosqlite://）；正常使用留空"
        >
          <Input placeholder="mysql+aiomysql://user:pass@host:port/db" />
        </Form.Item>
      </Col>
    </Row>
  </>
);

export default MysqlPanel;
