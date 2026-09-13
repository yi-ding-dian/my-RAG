/**
 * 部门配置页（仅 dept_admin）：本部门的 LLM 段 + chat 段 + retrieval/agentic 覆盖配置
 *
 * 与「系统配置」解耦——超管管基础设施（Embedding/MinIO/MinerU…），部门管理员
 * 只管本部门业务配置。保存走 POST /api/settings/chat（llm/chat/retrieval/agentic
 * 白名单），后端强制写入本部门 department_config，对本部门成员即时生效。
 *
 * 语义（与后端一致）：**留空 = 跟随全局**（表单占位显示全局值）；api_key 为
 * 脱敏值，原样回传 = 不覆盖部门原值。
 *
 * 配置收口约定：聊天设置弹窗只做**只读展示**，修改一律在此页进行。
 *
 * 布局：折叠面板（Collapse）+ 两列表单——占满宽度不留白、收起时一屏放下
 * 全部条目、改哪张展开哪张。面板按 Form 实例分组（同一 Form 的字段不能拆到
 * 两个面板），故"对话配置"与"检索增强"共用一个面板。
 */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Button,
  Col,
  Collapse,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Slider,
  Space,
  Spin,
  Switch,
  Tag,
} from 'antd';
import type { CollapseProps } from 'antd';
import { asApiError, getChatSettings, updateChatSettings } from '../../shared/api/client';
import type { ThinkingMode } from '../../shared/api/client';

const { TextArea } = Input;

/** 部门 LLM 表单字段 */
interface DeptLlmValues {
  llm_base_url?: string;
  llm_api_key?: string;
  llm_model?: string;
  llm_temperature?: number | null;
  llm_max_tokens?: number | null;
  llm_timeout?: number | null;
}

/** 部门对话/检索增强表单字段（前 6 项对应后端 chat 段必填字段） */
interface DeptChatValues {
  chat_enable_multi_turn: boolean;
  chat_history_rounds: number;
  chat_system_prompt: string;
  use_default_temperature: boolean;
  chat_temperature: number;
  chat_top_p: number;
  chat_max_tokens: number | null;
  chat_thinking_mode: ThinkingMode;
  chat_kg_enhance: boolean;
  chat_query_rewrite: boolean;
  chat_query_rewrite_rounds: number;
}

/** 部门检索 / Agentic 表单字段 */
interface DeptRetrievalValues {
  retrieval_top_k: number;
  retrieval_similarity_threshold: number;
  agentic_enabled: boolean;
  agentic_max_retries: number;
  agentic_recheck_threshold: number;
  agentic_abstain_threshold: number;
}

const DeptConfig: React.FC = () => {
  const { message } = AntApp.useApp();
  const [llmForm] = Form.useForm<DeptLlmValues>();
  const [chatForm] = Form.useForm<DeptChatValues>();
  const [retrForm] = Form.useForm<DeptRetrievalValues>();
  const [loading, setLoading] = useState(true);
  const [savingLlm, setSavingLlm] = useState(false);
  const [savingChat, setSavingChat] = useState(false);
  const [savingRetr, setSavingRetr] = useState(false);

  /** 合并值回填：未设置字段显示全局值（占位提示），api_key 为脱敏值 */
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getChatSettings();
      const llm = res.data.llm;
      llmForm.setFieldsValue({
        llm_base_url: llm?.base_url ?? '',
        llm_api_key: llm?.api_key ?? '',
        llm_model: llm?.model ?? '',
        llm_temperature: llm?.temperature ?? undefined,
        llm_max_tokens: llm?.max_tokens ?? undefined,
        llm_timeout: llm?.timeout ?? undefined,
      });
      const chat = res.data.chat;
      chatForm.setFieldsValue({
        chat_enable_multi_turn: chat?.enable_multi_turn ?? true,
        chat_history_rounds: chat?.history_rounds ?? 8,
        chat_system_prompt: chat?.system_prompt ?? '',
        use_default_temperature: chat?.temperature == null,
        chat_temperature: chat?.temperature ?? 0.7,
        chat_top_p: chat?.top_p ?? 0.9,
        chat_max_tokens: chat?.max_tokens ?? null,
        chat_thinking_mode: (chat?.thinking_mode ?? 'disabled') as ThinkingMode,
        chat_kg_enhance: chat?.kg_enhance ?? true,
        chat_query_rewrite: chat?.query_rewrite ?? true,
        chat_query_rewrite_rounds: chat?.query_rewrite_rounds ?? 3,
      });
      const retr = res.data.retrieval;
      const agentic = res.data.agentic;
      retrForm.setFieldsValue({
        retrieval_top_k: retr?.top_k ?? 5,
        retrieval_similarity_threshold: retr?.similarity_threshold ?? 0,
        agentic_enabled: agentic?.enabled ?? false,
        agentic_max_retries: agentic?.max_retries ?? 1,
        agentic_recheck_threshold: agentic?.recheck_threshold ?? 0.55,
        agentic_abstain_threshold: agentic?.abstain_threshold ?? 0.25,
      });
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '加载本部门配置失败');
    } finally {
      setLoading(false);
    }
  }, [llmForm, chatForm, retrForm, message]);

  useEffect(() => {
    void load();
  }, [load]);

  const saveLlm = async () => {
    const vals = await llmForm.validateFields();
    setSavingLlm(true);
    try {
      // 只提交 llm 段（后端白名单 6 字段）：空串/null = 跟随全局；
      // api_key 脱敏值原样回传 = 保留部门原值
      await updateChatSettings({
        llm: {
          base_url: vals.llm_base_url ?? '',
          api_key: vals.llm_api_key ?? '',
          model: vals.llm_model ?? '',
          temperature: vals.llm_temperature ?? null,
          max_tokens: vals.llm_max_tokens ?? null,
          timeout: vals.llm_timeout ?? null,
        },
      });
      message.success('部门 LLM 配置已保存，对本部门成员即时生效');
      await load();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '保存部门 LLM 配置失败');
    } finally {
      setSavingLlm(false);
    }
  };

  const saveChat = async () => {
    const vals = await chatForm.validateFields();
    setSavingChat(true);
    try {
      // 只提交 chat 段白名单字段（后端校验）；temperature 用 LLM 配置默认时提交 null
      await updateChatSettings({
        chat: {
          enable_multi_turn: vals.chat_enable_multi_turn,
          history_rounds: vals.chat_history_rounds,
          system_prompt: vals.chat_system_prompt ?? '',
          temperature: vals.use_default_temperature ? null : vals.chat_temperature,
          top_p: vals.chat_top_p,
          max_tokens: vals.chat_max_tokens ?? null,
          thinking_mode: vals.chat_thinking_mode,
          kg_enhance: vals.chat_kg_enhance,
          query_rewrite: vals.chat_query_rewrite,
          query_rewrite_rounds: vals.chat_query_rewrite_rounds,
        },
      });
      message.success('部门对话配置已保存，对本部门成员即时生效');
      await load();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '保存部门对话配置失败');
    } finally {
      setSavingChat(false);
    }
  };

  const saveRetrieval = async () => {
    const vals = await retrForm.validateFields();
    setSavingRetr(true);
    try {
      // 检索参数 + Agentic 决策层（均为部门可覆盖段，后端白名单校验）
      await updateChatSettings({
        retrieval: {
          top_k: vals.retrieval_top_k,
          similarity_threshold: vals.retrieval_similarity_threshold,
        },
        agentic: {
          enabled: vals.agentic_enabled,
          max_retries: vals.agentic_max_retries,
          recheck_threshold: vals.agentic_recheck_threshold,
          abstain_threshold: vals.agentic_abstain_threshold,
        },
      });
      message.success('部门检索配置已保存，对本部门成员即时生效');
      await load();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '保存部门检索配置失败');
    } finally {
      setSavingRetr(false);
    }
  };

  /** 面板标题右侧的保存按钮：阻止冒泡（否则会连带展开/收起面板） */
  const saveBtn = (loadingFlag: boolean, onSave: () => void) => (
    <Button
      type="primary"
      size="small"
      loading={loadingFlag}
      onClick={(e) => { e.stopPropagation(); onSave(); }}
    >
      保存
    </Button>
  );

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}>
        <Spin tip="加载本部门配置…" />
      </div>
    );
  }

  const items: CollapseProps['items'] = [
    {
      key: 'llm',
      label: (
        <Space>
          <Tag color="blue">LLM</Tag>
          部门 LLM 配置
          <span style={{ color: '#8c8c8c', fontSize: 12, fontWeight: 400 }}>
            留空 = 跟随全局
          </span>
        </Space>
      ),
      extra: saveBtn(savingLlm, () => void saveLlm()),
      children: (
        <Form form={llmForm} layout="vertical">
          {/* 栅格分配按内容长短：长字段（地址/模型名）占得多，短字段（数字/开关）
              占得少——开关曾经 span=8（1/3 屏放一个开关）；也不用 flex 弹性，
              否则余量全被最后一个元素吃掉，拉出一条超长输入框 */}
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="llm_base_url" label="服务地址（base_url）"
                extra="OpenAI 兼容端点，如 http://192.168.0.74:1234/v1">
                <Input placeholder="留空 = 跟随全局" allowClear />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="llm_api_key" label="API Key"
                extra="脱敏值原样保留；填写新值即覆盖">
                <Input.Password placeholder="留空 = 跟随全局" autoComplete="new-password" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="llm_model" label="模型名">
                <Input placeholder="留空 = 跟随全局" allowClear />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="llm_temperature" label="温度">
                <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }}
                  placeholder="跟随全局" />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="llm_max_tokens" label="最大 Token">
                <InputNumber min={1} style={{ width: '100%' }} placeholder="跟随全局" />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="llm_timeout" label="超时（秒）">
                <InputNumber min={1} style={{ width: '100%' }} placeholder="跟随全局" />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      ),
    },
    {
      key: 'chat',
      label: (
        <Space>
          <Tag color="green">对话</Tag>
          部门对话与检索增强
          <span style={{ color: '#8c8c8c', fontSize: 12, fontWeight: 400 }}>
            聊天页「聊天设置」只展示生效值，修改在本页
          </span>
        </Space>
      ),
      extra: saveBtn(savingChat, () => void saveChat()),
      children: (
        <Form form={chatForm} layout="vertical">
          <Row gutter={16}>
            <Col span={4}>
              <Form.Item name="chat_enable_multi_turn" label="多轮对话" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="chat_history_rounds" label="历史轮数"
                extra="携带多少轮对话历史"
                rules={[{ type: 'number', min: 1, max: 20, message: '范围 1-20' }]}>
                <InputNumber min={1} max={20} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item name="chat_thinking_mode" label="思考模式"
                extra="disabled=关闭（更快更省）">
                <Select
                  options={[
                    { value: 'disabled', label: '关闭思考' },
                    { value: 'enabled_low', label: '开启 · 低强度' },
                    { value: 'enabled_high', label: '开启 · 高强度' },
                    { value: 'enabled_max', label: '开启 · 最高强度' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col span={5}>
              <Form.Item name="use_default_temperature" label="温度跟随模型默认"
                valuePropName="checked" extra="关闭后可自定义温度与 Top P">
                <Switch />
              </Form.Item>
            </Col>
            <Col span={5}>
              <Form.Item name="chat_max_tokens" label="最大 Token" extra="留空 = 模型默认">
                <InputNumber min={1} style={{ width: '100%' }} placeholder="跟随默认" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item noStyle shouldUpdate={(p, c) => p.use_default_temperature !== c.use_default_temperature}>
            {({ getFieldValue }) => !getFieldValue('use_default_temperature') && (
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="chat_temperature" label="温度">
                    <Slider min={0} max={2} step={0.1} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="chat_top_p" label="Top P">
                    <Slider min={0} max={1} step={0.05} />
                  </Form.Item>
                </Col>
              </Row>
            )}
          </Form.Item>
          <Form.Item name="chat_system_prompt" label="系统提示词"
            extra="留空 = 使用内置默认模板；可含 {knowledge} / {refs} 占位符">
            <TextArea rows={4} placeholder="留空 = 使用内置默认模板" />
          </Form.Item>
          <Row gutter={16}>
            <Col span={7}>
              <Form.Item name="chat_kg_enhance" label="知识图谱增强" valuePropName="checked"
                extra="需文档构建过知识图谱（无图谱自动跳过）">
                <Switch />
              </Form.Item>
            </Col>
            <Col span={13}>
              <Form.Item name="chat_query_rewrite" label="查询改写" valuePropName="checked"
                extra="多轮时用 LLM 结合历史改写检索查询（消除「它/上面那个」等指代）">
                <Switch />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="chat_query_rewrite_rounds" label="改写轮数"
                extra="只影响改写"
                rules={[{ type: 'number', min: 1, max: 10, message: '范围 1-10' }]}>
                <InputNumber min={1} max={10} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      ),
    },
    {
      key: 'retrieval',
      label: (
        <Space>
          <Tag color="orange">检索</Tag>
          部门检索与 Agentic 配置
          <span style={{ color: '#8c8c8c', fontSize: 12, fontWeight: 400 }}>
            召回条数、相似度阈值、检索决策增强
          </span>
        </Space>
      ),
      extra: saveBtn(savingRetr, () => void saveRetrieval()),
      children: (
        <Form form={retrForm} layout="vertical">
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item name="retrieval_top_k" label="检索条数 Top K"
                rules={[{ type: 'number', min: 1, max: 20, message: '范围 1-20' }]}>
                <InputNumber min={1} max={20} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={18}>
              <Form.Item name="retrieval_similarity_threshold" label="相似度阈值"
                extra="低于该阈值的检索片段被过滤；0 = 不过滤">
                <Slider min={0} max={1} step={0.05} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="agentic_enabled" label="Agentic 检索增强" valuePropName="checked"
            extra="开启后问答链路增加「检索→分档→（改写重检）→拒答」决策；中间档自动改写查询重检，乱问/无相关内容自动拒答（默认关闭 = 原检索链路）">
            <Switch />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(p, c) => p.agentic_enabled !== c.agentic_enabled}>
            {({ getFieldValue }) => getFieldValue('agentic_enabled') && (
              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item name="agentic_max_retries" label="改写重试次数"
                    rules={[{ type: 'number', min: 0, max: 5, message: '范围 0-5' }]}>
                    <InputNumber min={0} max={5} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="agentic_recheck_threshold" label="直接回答阈值"
                    extra="相似度 ≥ 该值直接回答">
                    <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item name="agentic_abstain_threshold" label="拒答阈值"
                    extra="相似度 < 该值直接拒答">
                    <InputNumber min={0} max={1} step={0.05} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              </Row>
            )}
          </Form.Item>
        </Form>
      ),
    },
  ];

  return (
    <div>
      {/* 头部吸附：向下滚动时说明条贴在内容区顶部不动，只有折叠面板在滚——
          sticky 由浏览器算偏移，无需猜 100vh - padding（换屏幕/缩放都不失准） */}
      <Alert
        type="info"
        showIcon
        style={{
          marginBottom: 12,
          position: 'sticky',
          top: 0,
          zIndex: 2,
        }}
        message="本页配置对本部门所有成员生效；未设置的项沿用全局配置"
      />
      <Collapse defaultActiveKey={['llm']} items={items} />
      <div style={{ marginTop: 12 }}>
        <Button onClick={() => void load()}>重新加载</Button>
      </div>
    </div>
  );
};

export default DeptConfig;
