import React from 'react';
import { App as AntApp, Form, Select } from 'antd';
import type { LayoutRecognize } from '../../api/client';

interface LayoutRecognizeFieldProps {
  /** 表单字段名（默认 layout_recognize） */
  name?: string;
  /** 默认值 */
  initialValue?: LayoutRecognize;
}

/**
 * PDF 版面识别引擎选择（参考 KnowFlow layout-recognize 配置项）。
 * MinerU=高精度（默认）/ DeepDOC=通过 RAGFlow 服务解析，表格输出为
 * 可检索 HTML（已生效，需配置 DeepDoc 服务）/ PlainText=纯文本直提
 * （pypdf/python-docx 直接提取，无表格/图片识别，选择后解析引擎自动
 * 切换为纯文本提取）。
 */
const LayoutRecognizeField: React.FC<LayoutRecognizeFieldProps> = ({
  name = 'layout_recognize',
  initialValue = 'MinerU',
}) => {
  const { message } = AntApp.useApp();
  return (
    <Form.Item
      name={name}
      label="版面识别"
      initialValue={initialValue}
      extra="MinerU 表格为图片；DeepDOC 表格输出为可检索 HTML（需配置 DeepDoc 服务）；PlainText 用 pypdf/python-docx 直接提取纯文本（无表格/图片识别）"
    >
      <Select
        onChange={(v: LayoutRecognize) => {
          if (v === 'DeepDOC') {
            message.info('DeepDoc：表格将输出为可检索 HTML（需在系统设置中配置 DeepDoc 服务）');
          } else if (v === 'PlainText') {
            message.info('PlainText：将直接用 pypdf/python-docx 提取纯文本（无表格/图片识别）');
          }
        }}
        options={[
          { value: 'MinerU', label: 'MinerU（推荐，高精度）' },
          { value: 'DeepDOC', label: 'DeepDOC（表格输出为可检索 HTML）' },
          { value: 'PlainText', label: 'PlainText（纯文本直提）' },
        ]}
      />
    </Form.Item>
  );
};

export default LayoutRecognizeField;
