import React from 'react';
import { Col, Form, Input, InputNumber, Row, Select } from 'antd';
import type { ParserLlmModelItem } from '../../shared/api/types';

const { Password } = Input;

/** 句末标点候选（与后端 chunking.common._END_PUNCT_CHARS 一致）：只有末尾是
 *  这些符号时才受白名单裁决。冒号/顿号/右括号等是标题的正常组成部分，不在此列、
 *  也不受本名单管辖——纳入会误杀"注："、"1）xx"、"一、总则" 这类真标题 */
const END_PUNCT_OPTIONS = [
  { value: '？', label: '？ 全角问号' },
  { value: '！', label: '！ 全角叹号' },
  { value: '…', label: '… 省略号' },
  { value: '。', label: '。 全角句号' },
  { value: '；', label: '； 全角分号' },
  { value: '?', label: '? 半角问号' },
  { value: '!', label: '! 半角叹号' },
  { value: '.', label: '. 半角句号' },
  { value: ';', label: '; 半角分号' },
];

/** 面板 props：标题分层模型下拉数据源（GET /api/settings/llm/models） */
interface Props {
  headingModelOptions: ParserLlmModelItem[];
}

/** 检索与切块面板（配置档案弹窗）：top_k / 混合检索 / Rerank / 切块 / 标题分层模型 */
const RetrievalPanel: React.FC<Props> = ({ headingModelOptions }) => (
  <>
    <Row gutter={12}>
      <Col span={6}>
        <Form.Item name="retrieval_top_k" label="检索 top_k">
          <InputNumber min={1} max={50} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="retrieval_enable_hybrid"
          label="混合检索（BM25+向量）"
          tooltip="开启后关键词检索（BM25）与向量检索 RRF 融合，关键词精准命中也能找回"
        >
          <Select
            options={[
              { value: true, label: '开启' },
              { value: false, label: '关闭' },
            ]}
          />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="rerank_enabled"
          label="Rerank 重排序"
          tooltip="开启且服务地址/模型均填写才生效；调用失败自动降级为原排序，不影响检索"
        >
          <Select
            options={[
              { value: true, label: '开启' },
              { value: false, label: '关闭' },
            ]}
          />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item name="rerank_top_n" label="重排候选数 top_n">
          <InputNumber min={1} max={100} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
    </Row>
    <Row gutter={12}>
      <Col span={12}>
        <Form.Item
          name="rerank_base_url"
          label="Rerank 服务地址（OpenAI 兼容）"
          tooltip="如本地 http://127.0.0.1:8400/v1 或云端 https://api.siliconflow.cn/v1，POST {base_url}/rerank"
        >
          <Input placeholder="http://127.0.0.1:8400/v1" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item name="rerank_model" label="Rerank 模型">
          <Input placeholder="bge-reranker-v2-m3" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="rerank_api_key"
          label="API Key"
          tooltip="云端服务（如 SiliconFlow）需要，本机 vLLM 无鉴权可留空；保存后仅显示脱敏值，不修改请留空"
        >
          <Password placeholder="***" />
        </Form.Item>
      </Col>
    </Row>
    <Row gutter={12}>
      <Col span={6}>
        <Form.Item name="chunk_size" label="切块大小（字符）">
          <InputNumber min={100} max={4000} step={100} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item name="chunk_overlap" label="切块重叠（字符）">
          <InputNumber min={0} max={1000} step={50} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
      <Col span={12}>
        <Form.Item
          name="contextual_retrieval_max_full_doc_chars"
          label="上下文检索完整文档阈值（字）"
          tooltip="解析文本不超过该字数时，上下文检索增强把完整文档作为摘要生成的上下文（全局视角）；文档超过该字数时效果不佳，不建议使用"
        >
          <InputNumber min={1000} max={1000000} step={1000} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
    </Row>
    <Row gutter={12}>
      <Col span={12}>
        <Form.Item
          name="heading_llm_model"
          label="标题分层模型"
          extra="可选。用于给解析产物（PDF 等）的标题重新分层，以支持层级聚合切块。不配置则只用规则分层，不调用 LLM。"
        >
          <Select
            allowClear
            placeholder="不配置（只用规则分层）"
            options={headingModelOptions.map(m => ({
              value: m.name,
              label: `${m.name}${m.model && m.model !== m.name ? `（${m.model}）` : ''}`,
            }))}
          />
        </Form.Item>
      </Col>
      <Col span={12}>
        <Form.Item
          name="heading_end_punct_whitelist"
          label="标题末尾标点白名单"
          extra="标题末尾是句末标点时，须在名单内才认作标题；默认按国标取问号/叹号/省略号，留空回退默认。冒号、顿号、右括号等不受管辖"
        >
          <Select
            mode="tags"
            placeholder="默认为 ？ ！ … ? !"
            options={END_PUNCT_OPTIONS}
          />
        </Form.Item>
      </Col>
    </Row>
  </>
);

export default RetrievalPanel;
