import React from 'react';
import { Col, Form, InputNumber, Row } from 'antd';

/**
 * 聊天设置面板（配置档案弹窗）
 *
 * - 单条输入长度：问题/检索 query 的最大长度（原在「入库与限制」面板——
 *   它属于 chat 段、语义是聊天输入限制而非入库限制，故挪到此处）
 * - 引用设置：鼠标悬停在回答里的引用标 [n] 上时，浮层显示的字数上限。
 *   为什么是"窗口大小"而不是"截断长度"：回答实际用到的内容常落在块的中后段
 *   （表格块的有效数字几乎都在表格下方），浮层会先在全量文本上定位命中、
 *   再围绕命中开窗（见 MessageList 的 buildSnippet）。从头硬截会让命中整段
 *   落在窗口外，浮层里什么也标不出来——这正是此前"引用浮层看不到高亮"的原因。
 */
const ChatPanel: React.FC = () => (
  <Row gutter={12}>
    <Col span={8}>
      <Form.Item
        name="chat_max_query_len"
        label="单条输入长度（字）"
        tooltip="问题/检索 query 最大长度，超出返回友好提示（防超长粘贴耗尽上下文与费用）"
      >
        <InputNumber min={100} max={20000} step={100} style={{ width: '100%' }} />
      </Form.Item>
    </Col>
    <Col span={8}>
      <Form.Item
        name="chat_citation_snippet_chars"
        label="引用设置"
        tooltip={
          <div style={{ fontSize: 12, lineHeight: '18px' }}>
            鼠标悬停在回答里的引用标 [n] 上时，浮层显示的字数上限。
            <div style={{ marginTop: 6 }}>
              浮层会围绕回答实际用到的那段内容开窗——命中位置靠后时
              不会从头截断（否则命中会落在窗口外，浮层里看不到高亮）。
            </div>
            <div style={{ marginTop: 6 }}>
              范围 100~2000 字，默认 600。
            </div>
          </div>
        }
      >
        <InputNumber min={100} max={2000} step={50}
                     style={{ width: '100%' }} addonAfter="字" />
      </Form.Item>
    </Col>
    <Col span={8}>
      <Form.Item
        name="chat_prompt_total_max_tokens"
        label="上下文预算"
        tooltip={
          <div style={{ fontSize: 12, lineHeight: '18px' }}>
            进 prompt 的检索片段<b>总量</b>上限（token）。超出即停止追加后续
            片段；首条就超预算时截断它（有上下文总比一条都没有强）。
            <div style={{ marginTop: 6 }}>
              <b>只设总量、不设单条</b>：总量天然隐含单条约束，再配单条只会
              无谓截断大块——而大块往往正是最相关的那条。
            </div>
            <div style={{ marginTop: 6 }}>
              怎么定：<b>模型窗口 − 输出 max_tokens − 历史/系统提示的余量</b>。
              例：窗口 15000、输出 4096 → 输入留约 6000。
            </div>
            <div style={{ marginTop: 6 }}>
              token 数优先调模型服务的 /tokenize 精确计（vLLM 内置），
              服务不支持时按 字符数 × 0.62 估算。范围 500~200000，默认 6000。
            </div>
          </div>
        }
      >
        <InputNumber min={500} max={200000} step={500}
                     style={{ width: '100%' }} addonAfter="token" />
      </Form.Item>
    </Col>
  </Row>
);

export default ChatPanel;
