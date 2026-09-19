import React, { useEffect, useMemo, useState } from 'react';
import {
  DatePicker,
  Form,
  Input,
  InputNumber,
  Radio,
  Select,
  Space,
  Switch,
  Tooltip,
  Typography,
} from 'antd';
import dayjs, { type Dayjs } from 'dayjs';
import AppModal from '../../../shared/components/common/AppModal';
import type {
  ExtQuery,
  ExtQueryConfig,
  KnowledgeBase,
} from '../../../shared/api/client';
import {
  EXPIRY_PRESETS,
  expiresFromDays,
  parseExpires,
  toExpiresString,
} from '../expiry';

const { TextArea } = Input;

/** 查询配置表单值（空 = 跟随全局，提交时转 null） */
export interface ConfigFormValues {
  system_prompt?: string;
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
  top_k?: number | null;
  similarity_threshold?: number | null;
  enable_multi_turn?: boolean;
  history_rounds?: number | null;
  enable_images?: boolean;
}

/** 表单提交值 */
export interface ExtQueryFormValues {
  name: string;
  kb_ids: string[];
  config: ExtQueryConfig;
  /** 空串 = 永久有效（后端语义：空 = 清除有效期） */
  expires_at: string;
}

/** 表单配置 → 提交载荷（空值转 null = 跟随全局；system_prompt 空串 = 默认模板） */
export const configToPayload = (c: ConfigFormValues): ExtQueryConfig => ({
  system_prompt: c.system_prompt ?? '',
  temperature: c.temperature ?? null,
  top_p: c.top_p ?? null,
  max_tokens: c.max_tokens ?? null,
  top_k: c.top_k ?? null,
  similarity_threshold: c.similarity_threshold ?? null,
  enable_multi_turn: c.enable_multi_turn ?? true,
  history_rounds: c.history_rounds ?? null,
  enable_images: c.enable_images ?? true,
});

/** 默认配置表单值 */
export const defaultConfigForm = (
  config: ExtQueryConfig = {},
): ConfigFormValues => ({
  system_prompt: config.system_prompt ?? '',
  temperature: config.temperature ?? null,
  top_p: config.top_p ?? null,
  max_tokens: config.max_tokens ?? null,
  top_k: config.top_k ?? null,
  similarity_threshold: config.similarity_threshold ?? null,
  enable_multi_turn: config.enable_multi_turn ?? true,
  history_rounds: config.history_rounds ?? null,
  enable_images: config.enable_images ?? true,
});

/** 有效期选择：非负数为预设天数，-1 = 自定义日期 */
type ExpiryPreset = number;

interface ExtQueryFormProps {
  open: boolean;
  /** 编辑对象（null = 新建） */
  editing: ExtQuery | null;
  kbs: KnowledgeBase[];
  deptName: Record<string, string>;
  submitting: boolean;
  onSubmit: (values: ExtQueryFormValues) => void;
  onCancel: () => void;
}

/**
 * 新建 / 编辑外部查询弹窗（含有效期设置）
 *
 * 有效期以绝对时间提交（后端存 expires_at）：新建时由预设天数换算，
 * 编辑时回显为"自定义日期"——原预设天数早已随时间流逝，再回显成"30 天"
 * 会与实际到期时间不符。
 */
const ExtQueryForm: React.FC<ExtQueryFormProps> = ({
  open,
  editing,
  kbs,
  deptName,
  submitting,
  onSubmit,
  onCancel,
}) => {
  const [form] = Form.useForm();
  const [preset, setPreset] = useState<ExpiryPreset>(0);
  const [customDate, setCustomDate] = useState<Dayjs | null>(null);

  // 打开弹窗时按当前对象重置（新建 = 永久；编辑 = 回显其到期时间）
  useEffect(() => {
    if (!open) return;
    form.resetFields();
    if (editing) {
      form.setFieldsValue({
        name: editing.name,
        kb_ids: editing.kb_ids,
        config: defaultConfigForm(editing.config),
      });
      const d = parseExpires(editing.expires_at);
      setPreset(d ? -1 : 0);
      setCustomDate(d ? dayjs(d) : null);
    } else {
      form.setFieldsValue({ config: defaultConfigForm() });
      setPreset(0);
      setCustomDate(null);
    }
  }, [open, editing, form]);

  // 知识库下拉：库名 +（部门名 / 全局）
  const kbOptions = useMemo(
    () =>
      kbs.map(k => ({
        value: k.id,
        label: `${k.name}（${k.department_id ? deptName[k.department_id] ?? '未知部门' : '全局'}）`,
      })),
    [kbs, deptName],
  );

  const handleOk = async () => {
    let values: { name: string; kb_ids: string[]; config: ConfigFormValues };
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    // 有效期 → 绝对时间串（空串 = 永久，后端据此清除/不设有效期）
    let expiresAt = '';
    if (preset > 0) {
      expiresAt = expiresFromDays(preset) ?? '';
    } else if (preset === -1) {
      if (!customDate) return; // 自定义但未选日期：校验已拦，双保险
      expiresAt = toExpiresString(customDate.toDate());
    }
    onSubmit({
      name: values.name.trim(),
      kb_ids: values.kb_ids,
      config: configToPayload(values.config),
      expires_at: expiresAt,
    });
  };

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 680, h: 520 }}
      rememberKey="extq-form"
      title={editing ? '编辑外部查询' : '新建外部查询'}
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      confirmLoading={submitting}
      okText={editing ? '保存' : '生成链接'}
      cancelText="取消"
      width={680}
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="name"
          label="名称"
          rules={[{ required: true, whitespace: true, message: '请输入名称' }]}
        >
          <Input placeholder="例如：产品知识对外查询" maxLength={50} showCount />
        </Form.Item>
        <Form.Item
          name="kb_ids"
          label="暴露的知识库（多选，1-10 个）"
          rules={[{ required: true, message: '请至少选择一个知识库' }]}
        >
          <Select
            mode="multiple"
            placeholder="选择要对外的知识库（跨部门可见）"
            options={kbOptions}
            optionFilterProp="label"
            maxTagCount={5}
          />
        </Form.Item>

        <Form.Item label="有效期" required style={{ marginBottom: 12 }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Radio.Group
              value={preset}
              onChange={e => setPreset(e.target.value as ExpiryPreset)}
              optionType="button"
              buttonStyle="solid"
              options={[
                ...EXPIRY_PRESETS.map(p => ({ label: p.label, value: p.days })),
                { label: '自定义', value: -1 },
              ]}
            />
            {preset === -1 && (
              <DatePicker
                showTime
                style={{ width: 240 }}
                value={customDate}
                onChange={d => setCustomDate(d)}
                placeholder="选择到期时间"
                disabledDate={d => !!d && d.isBefore(dayjs().startOf('day'))}
              />
            )}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              到期后外部访问一律返回「链接无效或已失效」，可随时续期；留空即永久有效。
            </Typography.Text>
          </Space>
        </Form.Item>

        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          查询参数（留空 = 跟随全局配置）
        </Typography.Text>
        <Form.Item
          name={['config', 'system_prompt']}
          label="系统提示词"
          style={{ marginTop: 8 }}
        >
          <TextArea
            rows={3}
            placeholder="留空使用默认提示词；可含 {knowledge} 占位符（检索原文逐字注入）或 {refs}（带来源标注的引用内容）"
          />
        </Form.Item>
        <Space size={16} wrap>
          <Form.Item name={['config', 'temperature']} label="温度（0-2）">
            <InputNumber min={0} max={2} step={0.1} style={{ width: 140 }} placeholder="跟随全局" />
          </Form.Item>
          <Form.Item name={['config', 'top_p']} label="Top P（0-1）">
            <InputNumber min={0} max={1} step={0.05} style={{ width: 140 }} placeholder="跟随全局" />
          </Form.Item>
          <Form.Item name={['config', 'top_k']} label="检索条数（1-20）">
            <InputNumber min={1} max={20} step={1} style={{ width: 140 }} placeholder="跟随全局" />
          </Form.Item>
          <Form.Item name={['config', 'similarity_threshold']} label="相似度阈值（0-1）">
            <InputNumber min={0} max={1} step={0.05} style={{ width: 140 }} placeholder="跟随全局" />
          </Form.Item>
          <Form.Item name={['config', 'max_tokens']} label="最大输出 Token">
            <InputNumber min={1} max={16384} step={128} style={{ width: 140 }} placeholder="跟随全局" />
          </Form.Item>
          <Form.Item name={['config', 'history_rounds']} label="历史轮数（1-20）">
            <InputNumber min={1} max={20} step={1} style={{ width: 140 }} placeholder="跟随全局" />
          </Form.Item>
          <Form.Item
            name={['config', 'enable_multi_turn']}
            label="多轮对话"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={['config', 'enable_images']}
            label={
              <Tooltip title="开启后外部页会展示文档中的示意图与截图（并引导模型在回答中原样输出图片）。文档含敏感信息时建议关闭——关闭后图片不会展示，也不会留下裂图或死链。">
                <span style={{ cursor: 'help', borderBottom: '1px dashed #d9d9d9' }}>
                  显示图片
                </span>
              </Tooltip>
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Space>
      </Form>
    </AppModal>
  );
};

export default ExtQueryForm;
