import React, { useEffect, useMemo, useState } from 'react';
import {
  DatePicker,
  Form,
  Input,
  InputNumber,
  Popover,
  Radio,
  Select,
  Space,
  Switch,
  Tooltip,
  Typography,
} from 'antd';
import { QuestionCircleOutlined } from '@ant-design/icons';
import dayjs, { type Dayjs } from 'dayjs';
import AppModal from '../../../shared/components/common/AppModal';
import {
  getExtQueryDefaults,
  getLlmModelList,
  type ExtQuery,
  type ExtQueryConfig,
  type ExtQueryDefaults,
  type KnowledgeBase,
  type ParserLlmModelItem,
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
  /** 引用的提示词库条目名（空 = 不用库条目） */
  system_prompt_ref?: string;
  temperature?: number | null;
  top_p?: number | null;
  max_tokens?: number | null;
  top_k?: number | null;
  similarity_threshold?: number | null;
  enable_multi_turn?: boolean;
  enable_images?: boolean;
  /** 指定 LLM 模型（空 = 跟随全局激活模型） */
  llm_model?: string;
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
  system_prompt_ref: c.system_prompt_ref ?? '',
  temperature: c.temperature ?? null,
  top_p: c.top_p ?? null,
  max_tokens: c.max_tokens ?? null,
  top_k: c.top_k ?? null,
  similarity_threshold: c.similarity_threshold ?? null,
  enable_multi_turn: c.enable_multi_turn ?? false,
  enable_images: c.enable_images ?? true,
  llm_model: c.llm_model ?? '',
});

/** 默认配置表单值 */
export const defaultConfigForm = (
  config: ExtQueryConfig = {},
): ConfigFormValues => ({
  system_prompt: config.system_prompt ?? '',
  system_prompt_ref: config.system_prompt_ref ?? '',
  temperature: config.temperature ?? null,
  top_p: config.top_p ?? null,
  max_tokens: config.max_tokens ?? null,
  top_k: config.top_k ?? null,
  similarity_threshold: config.similarity_threshold ?? null,
  enable_multi_turn: config.enable_multi_turn ?? false,
  enable_images: config.enable_images ?? true,
  llm_model: config.llm_model ?? '',
});

/** 有效期选择：非负数为预设天数，-1 = 自定义日期 */
type ExpiryPreset = number;

/** 系统提示词下拉里的"自定义"标记（取不可能撞上条目名的串） */
const CUSTOM_PROMPT = '__custom__';

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

/** 字段标签：标题 + 问号图标，悬浮显示简短含义说明 */
const FieldLabel: React.FC<{ text: string; hint: string }> = ({ text, hint }) => (
  <Space size={4}>
    <span>{text}</span>
    <Tooltip title={hint}>
      <QuestionCircleOutlined
        style={{ color: '#94a3b8', fontSize: 12, cursor: 'help' }}
      />
    </Tooltip>
  </Space>
);

/** 「跟随全局（实际值）」样式的 placeholder：留空时也看得到真正生效的数字 */
const followGlobal = (v: number | null | undefined, suffix = ''): string =>
  v === null || v === undefined ? '跟随全局' : `跟随全局（${v}${suffix}）`;

/**
 * 新建 / 编辑外部查询弹窗（含有效期设置）
 *
 * 有效期以绝对时间提交（后端存 expires_at）：新建时由预设天数换算，
 * 编辑时回显为"自定义日期"——原预设天数早已随时间流逝，再回显成"30 天"
 * 会与实际到期时间不符。
 *
 * 各参数留空即"跟随全局"，placeholder 里直接显示全局实际值（来源
 * GET /ext-queries/defaults），避免超管看不到真正生效的数字而盲填。
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

  // LLM 模型选项：取自**全局活跃档案**的模型列表（不选 = 跟随全局激活模型）。
  // 每次打开弹窗刷新一次——用户可能刚在系统配置里增删过模型
  const [llmModels, setLlmModels] = useState<ParserLlmModelItem[]>([]);
  const [globalModel, setGlobalModel] = useState('');
  // 留空时各项实际生效的全局值（placeholder 展示）
  const [defaults, setDefaults] = useState<ExtQueryDefaults | null>(null);
  // 系统提示词的当前选择（='' 跟随全局 / 条目名 / CUSTOM_PROMPT）：
  // 决定是否显示"自定义"文本框
  const promptRefValue = Form.useWatch(['config', 'system_prompt_ref'], form);

  useEffect(() => {
    if (!open) return;
    getLlmModelList()
      .then(res => {
        setLlmModels(res.data.models ?? []);
        setGlobalModel(
          res.data.models?.[res.data.active ?? 0]?.name ?? '',
        );
      })
      .catch(() => undefined); // 取不到选项不阻塞保存（下拉留空）
    getExtQueryDefaults()
      .then(res => setDefaults(res.data))
      .catch(() => undefined); // 取不到就退回纯"跟随全局"文案
  }, [open]);

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
    // 系统提示词二选一落到两个字段上：自定义模式清掉引用名并带上正文，
    // 引用/默认模式则清掉正文（后端解析顺序是 自定义 → 引用 → 全局 → 内置）
    const isCustom = (values.config.system_prompt_ref ?? '') === CUSTOM_PROMPT;
    onSubmit({
      name: values.name.trim(),
      kb_ids: values.kb_ids,
      config: configToPayload({
        ...values.config,
        system_prompt_ref: isCustom
          ? ''
          : (values.config.system_prompt_ref ?? ''),
        system_prompt: isCustom ? (values.config.system_prompt ?? '') : '',
      }),
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
          label={
            <FieldLabel
              text="暴露的知识库（多选，1-10 个）"
              hint="外部提问只在这些知识库里检索，跨部门可选"
            />
          }
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

        <Form.Item
          label={
            <FieldLabel
              text="有效期"
              hint="到期后链接自动失效（外部一律提示链接无效）；留空 = 永久，可随时续期"
            />
          }
          required
          style={{ marginBottom: 12 }}
        >
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
          </Space>
        </Form.Item>

        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          查询参数（留空 = 跟随全局配置）
        </Typography.Text>
        <Form.Item
          name={['config', 'llm_model']}
          label={
            <FieldLabel
              text="LLM 模型"
              hint="用哪个模型生成回答；不选则跟随全局激活模型"
            />
          }
          style={{ marginTop: 8 }}
        >
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ width: 380 }}
            placeholder="跟随全局"
            options={[
              // 显式给出"跟随全局"项（值空串）：比让用户去点清除更直观，
              // 且能一眼看到当前全局用的哪个模型
              {
                value: '',
                label: `跟随全局${globalModel ? `（当前 ${globalModel}）` : ''}`,
              },
              ...llmModels.map(m => ({
                value: m.name,
                label: m.model && m.model !== m.name
                  ? `${m.name}（${m.model}）`
                  : m.name,
              })),
            ]}
          />
        </Form.Item>
        {/* 系统提示词：三选一 —— 跟随全局默认 / 引用配置档案里的提示词库 / 临时自定义。
            存的是**条目名**（引用）而非内容副本：改库里的正文，所有引用它的
            链接立刻生效，不会出现 N 条链接各存一份过期副本 */}
        <Form.Item
          name={['config', 'system_prompt_ref']}
          label={
            <FieldLabel
              text="系统提示词"
              hint="定义回答的风格与规则；可引用配置档案里的提示词库，或临时自定义一条"
            />
          }
          extra={
            // 自定义模式下文本框就在下方，无需"查看"；跟随默认/引用库时才给入口
            promptRefValue === CUSTOM_PROMPT ? null : (
              <Popover
                title={promptRefValue
                  ? `提示词库条目：${promptRefValue}`
                  : '「跟随全局默认」实际使用的提示词'}
                trigger="click"
                content={
                  <div style={{
                    maxWidth: 480,
                    maxHeight: 340,
                    overflowY: 'auto',
                    whiteSpace: 'pre-wrap',
                    fontSize: 12,
                    lineHeight: 1.7,
                  }}>
                    {promptRefValue
                      ? ((defaults?.prompt_options ?? [])
                        .find(p => p.name === promptRefValue)?.content
                        ?? '（该条目已不存在，运行时将回退到全局默认）')
                      : (defaults?.default_system_prompt || '加载中…')}
                  </div>
                }
              >
                <Typography.Link style={{ fontSize: 12 }}>
                  查看提示词
                </Typography.Link>
              </Popover>
            )
          }
        >
          <Select
            allowClear
            style={{ width: 380 }}
            placeholder="跟随全局默认"
            options={[
              { value: '', label: '跟随全局默认' },
              ...(defaults?.prompt_options ?? []).map(p => ({
                value: p.name,
                label: `提示词库：${p.name}`,
              })),
              { value: CUSTOM_PROMPT, label: '自定义…' },
            ]}
          />
        </Form.Item>
        {promptRefValue === CUSTOM_PROMPT && (
          <Form.Item
            name={['config', 'system_prompt']}
            label="自定义系统提示词"
            extra="可含 {knowledge} 占位符（检索原文逐字注入）或 {refs}（带来源标注的引用内容）"
          >
            <TextArea rows={4} placeholder="输入这个链接专属的系统提示词…" />
          </Form.Item>
        )}
        <Space size={16} wrap>
          <Form.Item
            name={['config', 'temperature']}
            label={
              <FieldLabel text="温度（0-2）" hint="越高回答越随机：0 稳定，2 发散" />
            }
          >
            <InputNumber
              min={0} max={2} step={0.1} style={{ width: 140 }}
              placeholder={followGlobal(defaults?.temperature)}
            />
          </Form.Item>
          <Form.Item
            name={['config', 'top_p']}
            label={
              <FieldLabel text="Top P（0-1）" hint="采样范围，越小越保守" />
            }
          >
            <InputNumber
              min={0} max={1} step={0.05} style={{ width: 140 }}
              placeholder={followGlobal(defaults?.top_p)}
            />
          </Form.Item>
          <Form.Item
            name={['config', 'top_k']}
            label={
              <FieldLabel text="检索条数（1-20）" hint="每次检索取多少条片段作为回答依据" />
            }
          >
            <InputNumber
              min={1} max={20} step={1} style={{ width: 140 }}
              placeholder={followGlobal(defaults?.top_k)}
            />
          </Form.Item>
          <Form.Item
            name={['config', 'similarity_threshold']}
            label={
              <FieldLabel text="相似度阈值（0-1）" hint="低于此分数的片段会被过滤掉；0 = 不过滤" />
            }
          >
            <InputNumber
              min={0} max={1} step={0.05} style={{ width: 140 }}
              placeholder={followGlobal(defaults?.similarity_threshold)}
            />
          </Form.Item>
          <Form.Item
            name={['config', 'max_tokens']}
            label={
              <FieldLabel text="最大输出 Token" hint="回答长度上限，超出会被截断" />
            }
          >
            <InputNumber
              min={1} max={16384} step={128} style={{ width: 140 }}
              placeholder={followGlobal(defaults?.max_tokens)}
            />
          </Form.Item>
          <Form.Item
            name={['config', 'enable_multi_turn']}
            label={
              <FieldLabel
                text="多轮对话"
                hint="同一页面内记住前几轮问答；Agent / MCP 接入不受此影响（本就无会话）"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={['config', 'enable_images']}
            label={
              <FieldLabel
                text="显示图片"
                hint="回答与引用来源里展示文档中的截图；含敏感内容时建议关闭"
              />
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
