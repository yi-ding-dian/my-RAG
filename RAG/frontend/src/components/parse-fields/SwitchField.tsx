import React from 'react';
import { Form, Switch } from 'antd';

interface SwitchFieldProps {
  /** 表单字段名（表单扁平结构） */
  name: string;
  /** 开关标签 */
  label: string;
  /** 说明文字（标签下方小字） */
  desc?: string;
  /** 默认值（表单尚未设置该字段时生效） */
  defaultValue?: boolean;
}

/**
 * 通用开关行：标签+说明在左，Switch 在右。
 * 行式布局参考 KnowFlow chunking-config 开关面板；作为独立组件便于各配置项复用。
 */
const SwitchField: React.FC<SwitchFieldProps> = ({ name, label, desc, defaultValue = false }) => (
  <div
    style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      gap: 16,
      padding: '6px 0',
    }}
  >
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 14, fontWeight: 500 }}>{label}</div>
      {desc && (
        <div style={{ fontSize: 12, color: 'rgba(0,0,0,0.45)', marginTop: 2 }}>{desc}</div>
      )}
    </div>
    <Form.Item name={name} valuePropName="checked" initialValue={defaultValue} noStyle>
      <Switch />
    </Form.Item>
  </div>
);

export default SwitchField;
