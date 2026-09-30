import React from 'react';
import {
  Alert, Button, Col, Form, Input, InputNumber, Row, Select, Space, Switch, Typography,
} from 'antd';
import type { VisionModelItem } from '../../shared/api/types';
import { buildDefaultImgPrompt } from './imageSummaryPrompt';

const { Text } = Typography;

/**
 * 全局「图片摘要」面板（配置档案弹窗）——超管定的**默认**，部门留空则跟随
 *
 * 字段落在档案的 `image_summary.*` 段，与「部门配置 → 图片摘要」一一对应：
 * 生效顺序是 **部门覆盖 → 全局（这里）→ 内置模板**（后端 resolve_summary_cfg
 * 就是这么合并的）。在此之前全局这一环**没有任何界面**，超管想让所有部门
 * 默认用同一套提示词只能挨个部门配，或者改后端内置模板。
 *
 * 提示词**不自动填值**（与部门配置那边的做法不同，是刻意的）：留空 = 跟随
 * 后端内置模板，一旦自动填进去就等于把"跟随模板"变成"锁定这份文字副本"，
 * 以后模板更新它不跟。超管要看生成的原文，点「按选项生成」填进来即可，那时
 * 他改的是自己的副本，语义明确。
 */
const ImageSummaryPanel: React.FC<{
  /** 图片解析模型列表：模型下拉的候选（超管在「图片解析模型」面板里维护） */
  visionModels: VisionModelItem[];
  /** 通知外层"内容被改过"：程序化填值不触发 Form.onValuesChange（那是脏标记唯一的
   *  来源），不手动报到就会关窗不提示、白填一场 */
  onEdit: () => void;
}> = ({ visionModels, onEdit }) => {
  const form = Form.useFormInstance();
  const fmt = Form.useWatch('image_summary_output_format', form);
  const optLabel = Form.useWatch('image_summary_opt_label_type', form);
  const optText = Form.useWatch('image_summary_opt_read_text', form);
  const optScene = Form.useWatch('image_summary_opt_describe_scene', form);
  const optLayout = Form.useWatch('image_summary_opt_describe_layout', form);

  /** 把"当前选项 + 格式"生成的提示词填进输入框，供超管在此基础上改 */
  const fillGenerated = () => {
    form.setFieldsValue({
      image_summary_prompt: buildDefaultImgPrompt({
        label_type: optLabel ?? true,
        read_text: optText ?? true,
        describe_scene: optScene ?? true,
        describe_layout: optLayout ?? false,
      }, fmt ?? 'fields'),
    });
    onEdit();
  };

  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12, padding: '5px 12px' }}
        message={
          <span style={{ fontSize: 12 }}>
            这是<b>全局默认</b>：部门没在「部门配置 → 图片摘要」里覆盖时用它。
            解析时勾选「图片摘要」才生效——把图里的文字读进正文，让证照/扫描件能被检索。
          </span>
        }
      />

      <Row gutter={12}>
        <Col span={8}>
          <Form.Item
            name="image_summary_model"
            label="图片解析模型"
            tooltip="用哪个多模态模型读图。留空 = 用「图片解析模型」列表里的第一个"
          >
            <Select
              allowClear
              placeholder="跟随「图片解析模型」列表第一个"
              options={visionModels.map(m => ({
                value: m.name,
                label: m.model && m.model !== m.name ? `${m.name}（${m.model}）` : m.name,
              }))}
              notFoundContent="请先在「图片解析模型」里添加"
            />
          </Form.Item>
        </Col>
        <Col span={8}>
          <Form.Item
            name="image_summary_output_format"
            label="输出格式"
            tooltip="固定字段的检索命中率最高；简介不读图中文字，适合整页文字但无需理解含义的图"
          >
            <Select options={[
              { value: 'fields', label: '固定字段（类型/文字/画面）' },
              { value: 'prose', label: '自然段' },
              { value: 'brief', label: '一句话简介（不读图中文字）' },
            ]} />
          </Form.Item>
        </Col>
        <Col span={4}>
          <Form.Item
            name="image_summary_text_max_chars"
            label="文字字段上限"
            tooltip="fields 模式下「文字」字段的长度上限，超出截断"
          >
            <InputNumber min={0} max={2000} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
        <Col span={4}>
          <Form.Item
            name="image_summary_max_images"
            label="单文档图数上限"
            tooltip="一份文档最多摘要多少张图，0 = 不限"
          >
            <InputNumber min={0} max={1000} style={{ width: '100%' }} />
          </Form.Item>
        </Col>
      </Row>

      <Form.Item label="摘要内容（决定提示词里要模型输出什么）" style={{ marginBottom: 8 }}>
        <Space size="large" wrap>
          <Form.Item name="image_summary_opt_label_type" valuePropName="checked" noStyle>
            <Switch size="small" />
          </Form.Item>
          <span style={{ marginLeft: -14 }}>标注文件类型</span>
          <Form.Item name="image_summary_opt_read_text" valuePropName="checked" noStyle>
            <Switch size="small" />
          </Form.Item>
          <span style={{ marginLeft: -14 }}>读出图中文字</span>
          <Form.Item name="image_summary_opt_describe_scene" valuePropName="checked" noStyle>
            <Switch size="small" />
          </Form.Item>
          <span style={{ marginLeft: -14 }}>描述主体与场景</span>
          <Form.Item name="image_summary_opt_describe_layout" valuePropName="checked" noStyle>
            <Switch size="small" />
          </Form.Item>
          <span style={{ marginLeft: -14 }}>描述布局与结构</span>
        </Space>
      </Form.Item>

      <Row gutter={12}>
        <Col span={24}>
          {/* 标题行自己排（不用 Form.Item 的 label）——label 容器宽度不受控，
              marginLeft:auto 推不到右边，与部门配置同款处理 */}
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
            <Space size={8}>
              <span style={{ fontWeight: 500 }}>提示词</span>
              <Text type="secondary" style={{ fontSize: 12 }}>
                留空 = 按上面的选项自动生成；要改就先点右边填一份出来
              </Text>
            </Space>
            <Button size="small" style={{ marginLeft: 'auto' }} onClick={fillGenerated}>
              按选项生成
            </Button>
          </div>
          <Form.Item name="image_summary_prompt" style={{ marginBottom: 0 }}>
            <Input.TextArea
              rows={6}
              style={{ fontSize: 12, lineHeight: '18px' }}
              placeholder="留空时，后端会按上面的内容选项拼一份默认提示词（与「部门配置 → 图片摘要」里自动生成的同一套模板）。点「按选项生成」可填入当前选项对应的那份，再自行增删。"
            />
          </Form.Item>
        </Col>
      </Row>
    </>
  );
};

export default ImageSummaryPanel;
