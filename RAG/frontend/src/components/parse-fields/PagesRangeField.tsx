import React from 'react';
import { Button, Form, InputNumber, Typography } from 'antd';
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons';

interface PagesRangeFieldProps {
  /** 表单字段名（默认 pages，Form.List 每项 {from, to}） */
  name?: string;
  /** 预填页码范围（[[from, to]] 形式，组件内部转 {from, to}） */
  initialValue?: number[][];
}

/**
 * 页码范围 Form.List：每项显示为「第 [from] - [to] 页」，可增删。
 * 校验：from/to ≥ 1 且 to ≥ from；第二段起 from 需大于上一段 to（参考 KnowFlow chunk-method-modal）。
 */
const PagesRangeField: React.FC<PagesRangeFieldProps> = ({ name = 'pages', initialValue }) => {
  const defaultPairs =
    initialValue && initialValue.length > 0
      ? initialValue.map(p => ({ from: p[0], to: p[1] }))
      : [{ from: 1, to: 1000000 }];

  return (
    <Form.Item
      label="页码范围"
      extra="只解析指定页范围，可多段；默认全篇（第 1 页至末尾）"
    >
      <Form.List name={name} initialValue={defaultPairs}>
        {(fields, { add, remove }) => (
          <>
            {fields.map(({ key, name: fieldName, ...restField }) => (
              <div
                key={key}
                style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 8 }}
              >
                <Typography.Text style={{ lineHeight: '32px' }} type="secondary">
                  第
                </Typography.Text>
                <Form.Item
                  {...restField}
                  name={[fieldName, 'from']}
                  dependencies={fieldName > 0 ? [[name, fieldName - 1, 'to']] : []}
                  rules={[
                    { required: true, message: '请输入起始页' },
                    { type: 'number', min: 1, message: '起始页 ≥ 1' },
                    ({ getFieldValue }) => ({
                      validator(_, value) {
                        if (fieldName === 0 || !value) return Promise.resolve();
                        const prevTo = getFieldValue([name, fieldName - 1, 'to']);
                        if (prevTo == null || prevTo < value) return Promise.resolve();
                        return Promise.reject(new Error('需大于上一段结束页'));
                      },
                    }),
                  ]}
                  style={{ width: 110, marginBottom: 0 }}
                >
                  <InputNumber min={1} precision={0} placeholder="1" style={{ width: 110 }} />
                </Form.Item>
                <Typography.Text style={{ lineHeight: '32px' }} type="secondary">
                  -
                </Typography.Text>
                <Form.Item
                  {...restField}
                  name={[fieldName, 'to']}
                  dependencies={[[name, fieldName, 'from']]}
                  rules={[
                    { required: true, message: '请输入结束页' },
                    { type: 'number', min: 1, message: '结束页 ≥ 1' },
                    ({ getFieldValue }) => ({
                      validator(_, value) {
                        if (!value) return Promise.resolve();
                        const from = getFieldValue([name, fieldName, 'from']);
                        if (from == null || from <= value) return Promise.resolve();
                        return Promise.reject(new Error('结束页需 ≥ 起始页'));
                      },
                    }),
                  ]}
                  style={{ width: 110, marginBottom: 0 }}
                >
                  <InputNumber min={1} precision={0} placeholder="结束" style={{ width: 110 }} />
                </Form.Item>
                <Typography.Text style={{ lineHeight: '32px' }} type="secondary">
                  页
                </Typography.Text>
                {fields.length > 1 && (
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<MinusCircleOutlined />}
                    onClick={() => remove(fieldName)}
                    style={{ marginTop: 4 }}
                  />
                )}
              </div>
            ))}
            <Form.Item noStyle>
              <Button type="dashed" block icon={<PlusOutlined />} onClick={() => add()}>
                新增页码范围
              </Button>
            </Form.Item>
          </>
        )}
      </Form.List>
    </Form.Item>
  );
};

export default PagesRangeField;
