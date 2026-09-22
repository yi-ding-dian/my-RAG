import React from 'react';
import { Alert, Checkbox, Col, Form, Input, InputNumber, Row, Select } from 'antd';

/** 解析引擎显示名（与解析配置弹窗的下拉保持一致口径） */
const ENGINE_LABELS: Record<string, string> = {
  'pipeline': '流水线 pipeline（无 GPU 也可跑）',
  'hybrid-engine': '混合引擎 hybrid-engine（需 8GB+ 显存）',
  'vlm-engine': '视觉大模型 vlm-engine（需 8GB+ 显存，较慢）',
};

const ALL_ENGINES = ['pipeline', 'hybrid-engine', 'vlm-engine'];

/**
 * MinerU 文档解析面板（配置档案弹窗）：服务地址 / 超时 / 可用解析引擎 / 默认解析引擎
 *
 * **可用解析引擎**：超管按服务端资源（GPU、显存）声明这台机器能跑哪些档——
 * 只有勾选的才出现在解析配置的下拉里，避免用户选中实际跑不动的档
 * （如无 GPU 时选 hybrid-engine 会直接失败）。默认只开 pipeline。
 * **默认解析引擎**：新文档不显式指定时用它，必须在可用列表内
 * （后端保存时校验；这里的下拉也随之只列已勾选项）。
 */
const MineruPanel: React.FC = () => {
  const enabled = Form.useWatch('mineru_engines_enabled') as string[] | undefined;
  // 勾选项为空时兜底 pipeline（与后端默认一致，避免下拉空着）
  const usable = (enabled ?? []).filter((e) => ALL_ENGINES.includes(e));
  const options = (usable.length ? usable : ['pipeline']).map((e) => ({
    value: e, label: ENGINE_LABELS[e] ?? e,
  }));
  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 10 }}
        message="文档解析引擎：解析 PDF 与 Office 文档，还原标题层级"
      />
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
      <Form.Item
        name="mineru_engines_enabled"
        label="可用解析引擎"
        initialValue={['pipeline']}
        extra="按服务端资源勾选：后两项需 GPU（8GB+ 显存）。只有勾选的才出现在解析配置弹窗与批量导入的下拉里；默认只开流水线——它无 GPU 也能跑，是总能出结果的兜底档"
      >
        <Checkbox.Group
          options={ALL_ENGINES.map((e) => ({ value: e, label: ENGINE_LABELS[e] }))}
        />
      </Form.Item>
      <Form.Item
        name="mineru_default_engine"
        label="默认解析引擎"
        initialValue="pipeline"
        extra="新文档不指定时用它；必须在「可用解析引擎」里（后端保存时校验）"
      >
        <Select options={options} />
      </Form.Item>
    </>
  );
};

export default MineruPanel;
