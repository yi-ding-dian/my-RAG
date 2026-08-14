import React from 'react';
import { Form, Select } from 'antd';
import type { ParseLang } from '../../api/client';

interface LangSelectFieldProps {
  /** 表单字段名（默认 lang_list） */
  name?: string;
  /** 默认值 */
  initialValue?: ParseLang;
}

/** 解析语言选择：ch=中文（推荐）| en=英文 */
const LangSelectField: React.FC<LangSelectFieldProps> = ({
  name = 'lang_list',
  initialValue = 'ch',
}) => (
  <Form.Item name={name} label="语言" initialValue={initialValue} extra="解析文档内容的主要语言">
    <Select
      options={[
        { value: 'ch', label: '中文（推荐）' },
        { value: 'en', label: '英文' },
      ]}
    />
  </Form.Item>
);

export default LangSelectField;
