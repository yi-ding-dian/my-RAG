import React from 'react';
import { Col, Divider, Form, Input, InputNumber, Row, Select, Switch, Typography } from 'antd';
import type { VisionModelItem } from '../../shared/api/types';

const { Text } = Typography;

/**
 * 内置读图提示词——**仅用于输入框的 placeholder，不与后端联动**
 *
 * 真值以后端 `chat_service._CHAT_IMAGE_PROMPT` 为准（前端这份改了不影响实际
 * 调用）；贴在这里只是为了让"留空 = 用默认"不是个黑盒：用户得先看见默认长
 * 什么样，才知道该往哪儿改。改后端常量时同步改这里即可，不一致最多是提示
 * 文案过期，不会有行为差异。
 */
const DEFAULT_IMAGE_PROMPT = `请描述这张图片，供知识库检索与问答使用。要求：
1. **逐字抄录图中所有文字**：标题、编号、型号、参数、报错信息、日期、人名、地名、单位。这些是检索命中的关键，不得概括或改写；
2. 描述画面主体、版式布局、图表走势、界面元素等可见信息；
3. 若是界面或报错截图，说明是什么系统、什么操作、什么提示；
4. **只描述真实看到的内容**：不推测、不补充常识、不回答图片之外的问题；
5. 简洁中文，不超过 {max_chars} 字。`;

interface Props {
  /** 可选的识图模型（配置档案 vision 段；空 = 跟随「图片摘要」选的模型） */
  visionModels: VisionModelItem[];
}

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
 * - 聊天识图：发送图片 → 视觉模型读图 → 描述参与检索与回答（下方分组）
 */
const ChatPanel: React.FC<Props> = ({ visionModels }) => (
  <>
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

    <Divider orientation="left" style={{ margin: '0 0 12px' }}>
      <Text type="secondary" style={{ fontSize: 12 }}>聊天识图</Text>
    </Divider>

    <Row gutter={12}>
      <Col span={6}>
        <Form.Item
          name="chat_image_enabled"
          label="发送图片"
          valuePropName="checked"
          tooltip="关闭后聊天界面不出现图片入口，后端也拒绝带图请求"
        >
          <Switch />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="chat_image_max_count"
          label="单次最多"
          tooltip="一条消息最多带几张图。前端超限直接拦（不发请求），后端再校验一次"
        >
          <InputNumber min={1} max={20} style={{ width: '100%' }} addonAfter="张" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="chat_image_max_mb"
          label="单张大小"
          tooltip="单张图片大小上限。前端选图时拦（提示更即时、不费上传往返），上传时后端再拦一次"
        >
          <InputNumber min={0.1} max={50} step={0.5}
                       style={{ width: '100%' }} addonAfter="MB" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="chat_image_desc_max_chars"
          label="描述长度上限"
          tooltip={
            <div style={{ fontSize: 12, lineHeight: '18px' }}>
              视觉模型读图产出的描述被<b>硬截断</b>到此长度。
              <div style={{ marginTop: 6 }}>
                <b>提示词里那句"不超过 N 字"只是软约束</b>——实测配置 800 字、
                模型输出 1498 字，所以这里有硬截断兜底。
              </div>
              <div style={{ marginTop: 6 }}>
                这段描述一次生成、两处用（并入检索词 + 注入回答），太长会把
                原问题淹没，故不是越大越好。
              </div>
              <div style={{ marginTop: 6 }}>
                <b>要模型多抄内容，就把它一起调大</b>：只改提示词不改这里，
                多抄的部分会被切掉，反而比不改更差。范围 100~4000，默认 800。
              </div>
            </div>
          }
        >
          <InputNumber min={100} max={4000} step={100}
                       style={{ width: '100%' }} addonAfter="字" />
        </Form.Item>
      </Col>
    </Row>

    <Row gutter={12}>
      <Col span={12}>
        <Form.Item
          name="chat_image_model"
          label="识图模型"
          tooltip={
            <div style={{ fontSize: 12, lineHeight: '18px' }}>
              读图用哪个多模态模型（选项来自下方「图片解析模型」列表）。
              <div style={{ marginTop: 6 }}>
                <b>留空 = 跟随「图片摘要」选的模型</b>。填了则识图与文档入库
                解耦：入库摘要要跑几十上百张图、求快求省，聊天识图是用户发完图
                实时等着的、求准，两者要求本就不同。
              </div>
              <div style={{ marginTop: 6 }}>
                指定的模型被删除后会自动回退到默认，不会让识图直接不可用。
              </div>
            </div>
          }
        >
          <Select
            allowClear
            placeholder="跟随「图片摘要」选的模型"
            options={visionModels.map(m => ({
              value: m.name,
              label: m.model && m.model !== m.name ? `${m.name}（${m.model}）` : m.name,
            }))}
            notFoundContent="尚未配置多模态模型"
          />
        </Form.Item>
      </Col>
    </Row>

    <Form.Item
      name="chat_image_prompt"
      label="读图提示词"
      tooltip={
        <div style={{ fontSize: 12, lineHeight: '18px' }}>
          发给视觉模型的提示词。<b>留空 = 用下方 placeholder 里的内置默认。</b>
          <div style={{ marginTop: 6 }}>
            <b>支持 {'{max_chars}'} 占位符</b>，运行时替换为上面的「描述长度
            上限」。建议保留——不写它长度约束就只剩硬截断兜底。
          </div>
          <div style={{ marginTop: 6 }}>
            部门管理员可在「部门配置 → 对话增强」再覆盖一层；部门留空则跟随这里。
          </div>
          <div style={{ marginTop: 6 }}>
            调优提示：这段描述会<b>同时</b>并入检索词并注入回答，所以要
            「逐字抄录关键文字 + 简洁叙述」。若只想让模型回答得更细，改这里
            没用——主模型看到的是描述而非原图。
          </div>
        </div>
      }
    >
      <Input.TextArea
        rows={6}
        placeholder={DEFAULT_IMAGE_PROMPT}
        style={{ fontSize: 12, lineHeight: '18px' }}
      />
    </Form.Item>
  </>
);

export default ChatPanel;
