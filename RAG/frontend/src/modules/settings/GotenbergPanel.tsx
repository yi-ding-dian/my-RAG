import React from 'react';
import { Alert, Col, Form, Input, InputNumber, Row } from 'antd';

/** Gotenberg 文档转换面板（配置档案弹窗）：Office → PDF
 *
 *  为什么需要它：MinerU 的主场是 PDF——它按字号/字体/位置做**版面分析**，
 *  能还原标题层级；而处理 docx 时只提取文本、不做版面分析，**标题会全丢**
 *  （实测同一份文档：直接给 docx → 0 个标题；转成 PDF 再给 → 94 个标题）。
 *
 *  为什么用容器版而非宿主机直接跑 LibreOffice：容器能加内存硬上限，转换大
 *  文档内存暴涨时**只有容器被杀**，不拖垮整机（宿主机裸跑 soffice 曾在
 *  105MB / 795 张扫描图的 .doc 上吃到 27.9GB，把机器拖到 OOM 卡死数分钟）。
 *
 *  地址留空 = 不做转换，.doc 仍走原有路径。
 */
const GotenbergPanel: React.FC = () => (
  <>
    <Alert
      type="info"
      showIcon
      style={{ marginBottom: 12 }}
      message="文档转换服务：把 .doc / .docx / ppt 等 Office 文档转成 PDF"
    />
    <Row gutter={12}>
      <Col span={14}>
        <Form.Item
          name="gotenberg_base_url"
          label="服务地址"
          tooltip="留空表示不做转换，.doc 走原有解析路径"
        >
          <Input placeholder="http://127.0.0.1:3000" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="gotenberg_timeout"
          label="超时（秒）"
          tooltip="单次转换的超时上限；几百页的扫描件可适当放大"
        >
          <InputNumber min={10} max={1800} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
    </Row>
  </>
);

export default GotenbergPanel;
