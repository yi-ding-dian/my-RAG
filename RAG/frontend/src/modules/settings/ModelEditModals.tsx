import React, { useEffect } from 'react';
import { Alert, Col, Form, Input, InputNumber, Row, Select, Space, Tooltip } from 'antd';
import { QuestionCircleOutlined } from '@ant-design/icons';
import AppModal from '../../shared/components/common/AppModal';
import type { LLMModelItem, VisionModelItem } from '../../shared/api/client';
import type { ThinkingControl } from '../../shared/api/types';

const { Password } = Input;

/** Tooltip 里统一的小字样式（说明文字较长，用 12px/18px 行高） */
const HINT_STYLE: React.CSSProperties = { fontSize: 12, lineHeight: '18px' };

/**
 * LLM 模型添加/编辑弹窗
 *
 * 表单值在此组件内自持；保存时把整条模型交给父组件去插入/替换（列表与
 * 激活索引都归父组件管，因为档案卡片和面板都要读它）。
 */
export const LlmModelEditModal: React.FC<{
  open: boolean;
  models: LLMModelItem[];
  /** 编辑第几条；null = 添加 */
  editIdx: number | null;
  onSave: (item: LLMModelItem) => void;
  onCancel: () => void;
}> = ({ open, models, editIdx, onSave, onCancel }) => {
  const [form] = Form.useForm();

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    const m = editIdx !== null ? models[editIdx] : null;
    if (m) {
      form.setFieldsValue({
        model_name: m.name, model_base_url: m.base_url,
        model_api_key: m.api_key, model_model: m.model,
        model_temperature: m.temperature, model_max_tokens: m.max_tokens,
        model_timeout: m.timeout,
        // 旧数据无该字段 → 回填 0.9（与后端 LLMConfig 默认一致）
        model_top_p: m.top_p ?? 0.9,
        // 旧数据无该字段 → 回填 none（与后端默认一致）
        model_thinking_control: m.thinking_control ?? 'none',
      });
    } else {
      form.setFieldsValue({
        model_temperature: 0.3, model_max_tokens: 4096, model_timeout: 120,
        model_top_p: 0.9,
        model_thinking_control: 'none',
      });
    }
    // 只跟"打开 / 换编辑对象"走：models 每次改动都会变，进依赖会把用户
    // 正在填的表单重置掉
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editIdx]);

  const handleOk = async () => {
    const v = await form.validateFields();
    onSave({
      name: (v.model_name ?? '').trim(),
      base_url: (v.model_base_url ?? '').trim(),
      api_key: v.model_api_key || '',
      model: (v.model_model ?? '').trim(),
      temperature: v.model_temperature ?? 0.3,
      top_p: v.model_top_p ?? 0.9,
      max_tokens: v.model_max_tokens ?? 4096,
      timeout: v.model_timeout ?? 120,
      thinking_control: (v.model_thinking_control ?? 'none') as ThinkingControl,
    });
  };

  return (
    <AppModal
      dimension="auto"
      // 锁定型：内容就绪后定住高度，不再跟着内容变——表单里换行/滚动条出现
      // 会改变内容高度，若继续跟随就形成"测高→出滚动条→换行变高→再测"的
      // 振荡（表现为弹窗抽搐）。内容固定，直接锁。
      autoLock
      defaultSize={{ w: 600, h: 460 }}
      rememberKey="settings-2"
      title={editIdx !== null
        ? `编辑模型${models[editIdx] ? `：${models[editIdx].name}` : ''}`
        : '添加 LLM 模型'}
      open={open}
      onCancel={onCancel}
      onOk={handleOk}
      okText="保存"
      width={600}
    >
      <Form form={form} layout="vertical" size="small">
        <Row gutter={12}>
          <Col span={8}>
            <Form.Item
              name="model_name"
              label="模型名称（显示名）"
              rules={[{ required: true, message: '请输入模型名称' }]}
            >
              <Input placeholder="如：本地 Qwen" />
            </Form.Item>
          </Col>
          <Col span={16}>
            <Form.Item
              name="model_base_url"
              label="API 地址"
              rules={[{ required: true, message: '请输入 API 地址' }]}
            >
              <Input placeholder="http://127.0.0.1:1234/v1" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={12}>
            <Form.Item
              name="model_model"
              label="模型标识"
              rules={[{ required: true, message: '请输入模型标识' }]}
            >
              <Input placeholder="qwen3.6-35b-a3b-apex-quality" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="model_api_key"
              label="API Key"
              tooltip="编辑时留空 = 保留原值；保存后仅显示脱敏值"
            >
              <Password placeholder="sk-***" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={6}>
            <Form.Item
              name="model_temperature"
              label={
                <Space size={4}>
                  Temperature
                  <Tooltip
                    overlayStyle={{ maxWidth: 380 }}
                    title={
                      <div style={HINT_STYLE}>
                        控制回答的随机性（0~2）：越低越稳定、可复现，
                        越高越发散、越有创造性。
                        <div style={{ marginTop: 6 }}>
                          知识库问答要忠实复述原文，建议 0.1~0.3；
                          设太高模型容易改写甚至编造引用里没有的内容。
                        </div>
                      </div>
                    }
                  >
                    <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                  </Tooltip>
                </Space>
              }
            >
              <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item
              name="model_top_p"
              label={
                <Space size={4}>
                  Top P
                  <Tooltip
                    overlayStyle={{ maxWidth: 380 }}
                    title={
                      <div style={HINT_STYLE}>
                        核采样范围（0~1）：只从累计概率最高的这部分候选词里
                        挑下一个字，越小越保守、越大越多样。
                        <div style={{ marginTop: 6 }}>
                          它与 Temperature 是两个独立的采样旋钮，通常
                          只调其中一个：保持 Temperature 小而调 Top P，
                          比单纯降温度更不容易陷入重复。
                        </div>
                        <div style={{ marginTop: 6 }}>
                          外部查询「Top P 留空」时用的就是这个值，默认 0.9。
                        </div>
                      </div>
                    }
                  >
                    <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                  </Tooltip>
                </Space>
              }
            >
              <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item
              name="model_max_tokens"
              label={
                <Space size={4}>
                  Max Tokens
                  <Tooltip
                    overlayStyle={{ maxWidth: 380 }}
                    title={
                      <div style={HINT_STYLE}>
                        单次回答最多生成的 token 数——这是
                        <b>输出上限</b>，不是输入限制（输入多长由检索到的
                        引用决定，不在这里设）。
                        <div style={{ marginTop: 6 }}>
                          它与输入共同占用模型的上下文窗口：
                          <div style={{ marginTop: 2 }}>
                            输入 + Max Tokens ≤ 窗口长度
                          </div>
                          设得过大（尤其接近窗口长度）会在调用前被模型服务
                          直接拒绝，报"maximum context length"错误。
                        </div>
                        <div style={{ marginTop: 6 }}>
                          按期望的最长回答留 1.5 倍余量即可，
                          典型值 2048~4096。
                        </div>
                      </div>
                    }
                  >
                    <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                  </Tooltip>
                </Space>
              }
            >
              <InputNumber min={64} max={32768} step={128} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item
              name="model_timeout"
              label={
                <Space size={4}>
                  超时（秒）
                  <Tooltip
                    overlayStyle={{ maxWidth: 380 }}
                    title={
                      <div style={HINT_STYLE}>
                        等待模型返回的最长时间，超过即本次问答失败。
                        <div style={{ marginTop: 6 }}>
                          本地大模型首字延迟高（参数大、或开启思考时更明显），
                          建议按实测调整：设太小会频繁超时，
                          设太大则真卡住时要白等很久。
                        </div>
                      </div>
                    }
                  >
                    <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                  </Tooltip>
                </Space>
              }
            >
              <InputNumber min={1} max={600} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={24}>
            <Form.Item
              name="model_thinking_control"
              label={
                <Space size={4}>
                  思考控制方式
                  <Tooltip
                    overlayStyle={{ maxWidth: 420 }}
                    title={
                      <div style={HINT_STYLE}>
                        <div>
                          非思考模型指的是：模型本身不支持思考，或者支持思考但部署端
                          已经关闭（如 vLLM 启动服务时指定关闭思考）——这类模型选
                          「不处理」，系统不会再做任何事。
                        </div>
                        <div style={{ marginTop: 6 }}>
                          模型自带思考、且部署端关不掉（如 LM Studio 上的 Qwen 思考
                          模型）→ 选「注入 &lt;think&gt; 跳过思考」；
                          在线 API 且支持 thinking 参数（DeepSeek 等）→ 选「传 thinking 参数」。
                        </div>
                        <div style={{ marginTop: 6 }}>
                          选错的影响：给不思考的模型注入 prefill，会把回答压短、
                          图片标签被省略。
                        </div>
                      </div>
                    }
                  >
                    <QuestionCircleOutlined style={{ color: 'rgba(0,0,0,0.45)' }} />
                  </Tooltip>
                </Space>
              }
            >
              <Select
                options={[
                  { value: 'none', label: '不处理（模型不思考，或部署端已关闭）' },
                  { value: 'prefill', label: '注入 <think></think> 跳过思考（Qwen 系 + LM Studio 等本地部署）' },
                  { value: 'api', label: '传 thinking 参数（DeepSeek 等在线 API）' },
                ]}
              />
            </Form.Item>
          </Col>
        </Row>
      </Form>
    </AppModal>
  );
};

/**
 * 图片解析模型添加/编辑弹窗（条目比 LLM 少 temperature/max_tokens：
 * 摘要用固定生成参数）
 */
export const VisionModelEditModal: React.FC<{
  open: boolean;
  models: VisionModelItem[];
  /** 编辑第几条；null = 添加 */
  editIdx: number | null;
  onSave: (item: VisionModelItem) => void;
  onCancel: () => void;
}> = ({ open, models, editIdx, onSave, onCancel }) => {
  const [form] = Form.useForm();

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    const m = editIdx !== null ? models[editIdx] : null;
    if (m) {
      form.setFieldsValue({
        vision_name: m.name, vision_base_url: m.base_url,
        vision_api_key: m.api_key, vision_model: m.model,
        vision_timeout: m.timeout,
      });
    } else {
      form.setFieldsValue({ vision_timeout: 120 });
    }
    // 同 LlmModelEditModal：models 不进依赖，避免编辑中重置表单
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editIdx]);

  const handleOk = async () => {
    const v = await form.validateFields();
    onSave({
      name: (v.vision_name ?? '').trim(),
      base_url: (v.vision_base_url ?? '').trim(),
      api_key: v.vision_api_key || '',
      model: (v.vision_model ?? '').trim(),
      timeout: v.vision_timeout ?? 120,
    });
  };

  return (
    <AppModal
      title={editIdx !== null
        ? `编辑图片模型${models[editIdx] ? `：${models[editIdx].name}` : ''}`
        : '添加图片解析模型'}
      open={open}
      onCancel={onCancel}
      onOk={handleOk}
      okText="保存"
      width={600}
    >
      <Form form={form} layout="vertical" size="small">
        <Row gutter={12}>
          <Col span={8}>
            <Form.Item
              name="vision_name"
              label="模型名称（显示名）"
              rules={[{ required: true, message: '请输入模型名称' }]}
            >
              <Input placeholder="如：qwen-vl" />
            </Form.Item>
          </Col>
          <Col span={16}>
            <Form.Item
              name="vision_base_url"
              label="API 地址"
              rules={[{ required: true, message: '请输入 API 地址' }]}
            >
              <Input placeholder="http://127.0.0.1:8000/v1" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={12}>
          <Col span={8}>
            <Form.Item name="vision_model" label="模型名"
              rules={[{ required: true, message: '请输入模型名' }]}>
              <Input placeholder="Qwen3.5-9B-GPTQ-4bit" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="vision_api_key" label="API Key（可空）">
              <Input.Password placeholder="本地服务通常留空" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="vision_timeout" label="超时（秒）">
              <InputNumber min={1} max={600} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
        </Row>
        <Alert
          type="info"
          showIcon
          message="部门管理员从这些模型里选一个用；提示词与输出格式在「部门配置 → 图片摘要」里配。"
        />
      </Form>
    </AppModal>
  );
};
