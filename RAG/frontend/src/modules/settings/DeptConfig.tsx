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
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Button,
  Col,
  Collapse,
  Form,
  Input,
  InputNumber,
  Popover,
  Row,
  Select,
  Slider,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from 'antd';
import type { CollapseProps } from 'antd';
import { asApiError, getChatSettings, updateChatSettings } from '../../shared/api/client';
import type { ThinkingMode, ImageSummaryConfig } from '../../shared/api/client';
import { buildDefaultImgPrompt } from './imageSummaryPrompt';

const { TextArea } = Input;
const { Text } = Typography;

/** 系统提示词下拉里的"自定义"标记（取不可能撞上条目名的串） */
const CUSTOM_PROMPT = '__custom__';

/** 部门 LLM 表单字段
 *
 *  部门只**选**超管配好的模型条目，再按需微调温度/Token——连接信息与密钥
 *  不下放。原先让部门自由填 8 个字段，想换个模型就得整份抄一遍，抄漏一个
 *  就静默漂移。 */
interface DeptLlmValues {
  /** **条目名**（超管在「LLM 模型管理」里配的 name），不是模型名 */
  llm_model?: string;
  llm_temperature?: number | null;
  llm_max_tokens?: number | null;
}

/** 部门对话/检索增强表单字段（前 6 项对应后端 chat 段必填字段） */
interface DeptChatValues {
  chat_enable_multi_turn: boolean;
  chat_history_rounds: number;
  chat_system_prompt: string;
  /** 引用的提示词库条目名（超管在配置档案里维护） */
  chat_system_prompt_ref: string;
  use_default_temperature: boolean;
  chat_temperature: number;
  chat_top_p: number;
  chat_max_tokens: number | null;
  chat_thinking_mode: ThinkingMode;
  chat_kg_enhance: boolean;
  chat_query_rewrite: boolean;
  chat_query_rewrite_rounds: number;
  /** 读图模板名（空 = 用默认那套；入库摘要与聊天识图共用同一套「看图策略」） */
  chat_image_template: string;
  /** 自定义读图提示词（填了就**优先于**模板，是本部门的专用出口） */
  chat_image_prompt: string;
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

/** 部门图片摘要表单字段（选模型 + 结构化选项 + 可微调提示词 + 两个上限） */
interface DeptImageSummaryValues {
  img_model: string;
  img_output_format: string;
  img_text_max_chars: number;
  img_max_images: number;
  img_opt_label_type: boolean;
  img_opt_read_text: boolean;
  img_opt_describe_scene: boolean;
  img_opt_describe_layout: boolean;
  img_prompt: string;
}

// 默认提示词模板与拼装逻辑已移到 ./imageSummaryPrompt ——
// 系统配置（超管定全局默认）与部门配置两处共用同一份，避免两边漂移

const DeptConfig: React.FC = () => {
  const { message } = AntApp.useApp();
  const [llmForm] = Form.useForm<DeptLlmValues>();
  const [chatForm] = Form.useForm<DeptChatValues>();
  const [retrForm] = Form.useForm<DeptRetrievalValues>();
  const [imgForm] = Form.useForm<DeptImageSummaryValues>();
  const [loading, setLoading] = useState(true);
  const [savingLlm, setSavingLlm] = useState(false);
  const [savingChat, setSavingChat] = useState(false);
  const [savingRetr, setSavingRetr] = useState(false);
  const [savingImg, setSavingImg] = useState(false);
  /** 超管配的图片解析模型（只有名字，不含连接信息与密钥） */
  const [llmOptions, setLlmOptions] =
    useState<Array<{ name: string; model: string }>>([]);
  const [imageTemplateOptions, setImageTemplateOptions] =
    useState<Array<{ name: string; prompt: string; preview: string }>>([]);
  /** 没填自定义提示词时**实际会发**的完整提示词（后端按当前模板生成） */
  const [defaultImagePrompt, setDefaultImagePrompt] = useState('');
  /** 当前生效的模板正文（合并后）——拼「图片摘要提示词」预览的前半截 */
  const [imageTemplateBody, setImageTemplateBody] = useState('');
  const [visionOptions, setVisionOptions] = useState<Array<{ name: string; model: string }>>([]);
  /** 超管配的系统提示词库条目（部门只"选"用哪条，不能编辑库本身） */
  const [promptOptions, setPromptOptions] = useState<Array<{ name: string; content: string }>>([]);
  /** 「跟随全局默认」实际会用到的提示词全文（供"查看提示词"展示） */
  const [defaultSystemPrompt, setDefaultSystemPrompt] = useState('');
  /** 提示词是否被手动改过：改过就不再被"选项变化"自动覆盖 */
  const [imgPromptTouched, setImgPromptTouched] = useState(false);
  // 选项 / 输出格式一变就实时重算默认提示词填进输入框（未手动改过时）——
  // 让部门管理员看得见"当前选项会生成什么"，而不是面对一个空框
  const imgFmt = Form.useWatch('img_output_format', imgForm);
  // 系统提示词的当前选择（'' 跟随全局 / 条目名 / CUSTOM_PROMPT 自定义）
  const chatPromptRef = Form.useWatch('chat_system_prompt_ref', chatForm);
  const imgOptLabel = Form.useWatch('img_opt_label_type', imgForm);
  const imgOptText = Form.useWatch('img_opt_read_text', imgForm);
  const imgOptScene = Form.useWatch('img_opt_describe_scene', imgForm);
  const imgOptLayout = Form.useWatch('img_opt_describe_layout', imgForm);

  /** 上一次的输出格式 / 读图模板：用来识别"变了"（提示词与这两者配套，变了必须重算） */
  const prevImgFmtRef = useRef<string | undefined>(undefined);
  const prevImgTmplRef = useRef<string | undefined>(undefined);
  /** 读图模板（部门选的；空 = 跟随全局） */
  const chatTemplate = Form.useWatch('chat_image_template', chatForm);
  /** 该模板的正文：选了就用那份，没选用后端下发的"当前生效"那份 */
  const tmplBody = (imageTemplateOptions.find(t => t.name === chatTemplate)?.prompt)
    ?? imageTemplateBody;

  useEffect(() => {
    // **输出格式或读图模板变了就强制重算**（并解除"已自定义"）：旧提示词是照
    // 旧格式/旧模板写的，留着会让模型按旧的那套作答、而代码按新的解析——与解析
    // 入口的处理同一道理。其余情况（勾选项变化 / 手动微调）仍尊重 touched。
    // 首次（prev 为 undefined，含加载回填）不算"变化"，避免把存过的提示词冲掉。
    const fmtChanged = prevImgFmtRef.current !== undefined
      && prevImgFmtRef.current !== imgFmt;
    const tmplChanged = prevImgTmplRef.current !== undefined
      && prevImgTmplRef.current !== chatTemplate;
    prevImgFmtRef.current = imgFmt;
    prevImgTmplRef.current = chatTemplate;
    if (!fmtChanged && !tmplChanged && imgPromptTouched) return;
    imgForm.setFieldsValue({
      img_prompt: buildDefaultImgPrompt({
        label_type: imgOptLabel ?? true,
        read_text: imgOptText ?? true,
        describe_scene: imgOptScene ?? true,
        describe_layout: imgOptLayout ?? false,
      }, imgFmt ?? 'fields', tmplBody),
    });
    if (fmtChanged || tmplChanged) setImgPromptTouched(false);
  }, [imgPromptTouched, imgFmt, imgOptLabel, imgOptText, imgOptScene,
      imgOptLayout, imgForm, chatTemplate, tmplBody]);

  /** 合并值回填：未设置字段显示全局值（占位提示），api_key 为脱敏值 */
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getChatSettings();
      // LLM 段：model 存的是**部门选中的条目名**（不是模型名），所以回填
      // 部门覆盖值而非合并值——合并后的 llm.model 取自条目本身，两者不一定
      // 同名（超管可以把显示名起成任意值），混用会让部门"看到 A 却提交了 B"
      const deptLlm = (res.data as {
        dept?: { llm?: { model?: string; temperature?: number | null;
                         max_tokens?: number | null } } | null;
      }).dept?.llm;
      // 聊天识图两项同理：存的是**部门覆盖值**（留空 = 跟随全局）。用合并值
      // 回填的话，部门没选时会显示全局那份，一保存就固化成部门覆盖——以后
      // 超管改了全局，这个部门就不再跟随了
      const deptChat = (res.data as {
        dept?: { chat?: { image_template?: string;
                          image_prompt?: string } } | null;
      }).dept?.chat;
      llmForm.setFieldsValue({
        llm_model: deptLlm?.model ?? undefined,
        llm_temperature: deptLlm?.temperature ?? undefined,
        llm_max_tokens: deptLlm?.max_tokens ?? undefined,
      });
      const chat = res.data.chat;
      chatForm.setFieldsValue({
        chat_enable_multi_turn: chat?.enable_multi_turn ?? true,
        chat_history_rounds: chat?.history_rounds ?? 8,
        chat_system_prompt: chat?.system_prompt ?? '',
        chat_system_prompt_ref: chat?.system_prompt_ref ?? '',
        use_default_temperature: chat?.temperature == null,
        chat_temperature: chat?.temperature ?? 0.7,
        chat_top_p: chat?.top_p ?? 0.9,
        chat_max_tokens: chat?.max_tokens ?? null,
        chat_thinking_mode: (chat?.thinking_mode ?? 'disabled') as ThinkingMode,
        chat_kg_enhance: chat?.kg_enhance ?? true,
        chat_query_rewrite: chat?.query_rewrite ?? true,
        chat_query_rewrite_rounds: chat?.query_rewrite_rounds ?? 3,
        // 聊天识图：**模板**（与入库摘要共用的「看图策略」）+ 自定义提示词
        //（自定义填了优先于模板）。两者都填**部门覆盖值**，留空 = 跟随全局
        chat_image_template: deptChat?.image_template ?? '',
        chat_image_prompt: deptChat?.image_prompt ?? '',
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
      // 图片摘要（后端返回全局默认 + 本部门覆盖的合并值）
      const img = (res.data as unknown as {
        image_summary?: ImageSummaryConfig;
        vision_options?: Array<{ name: string; model: string }>;
      });
      setLlmOptions((res.data as {
        llm_options?: Array<{ name: string; model: string }>;
      }).llm_options ?? []);
      setImageTemplateOptions((res.data as {
        image_template_options?: Array<{ name: string; prompt: string;
                                         preview: string }>;
      }).image_template_options ?? []);
      setDefaultImagePrompt((res.data as {
        default_image_prompt?: string;
      }).default_image_prompt ?? '');
      setImageTemplateBody((res.data as {
        image_template_body?: string;
      }).image_template_body ?? '');
      setVisionOptions(img.vision_options ?? []);
      setPromptOptions((res.data as { prompt_options?: Array<{ name: string; content: string }> })
        .prompt_options ?? []);
      setDefaultSystemPrompt((res.data as { default_system_prompt?: string })
        .default_system_prompt ?? '');
      const is = img.image_summary ?? {};
      const opts = is.options ?? {};
      imgForm.setFieldsValue({
        img_model: is.model ?? '',
        img_output_format: is.output_format ?? 'fields',
        img_text_max_chars: is.text_max_chars ?? 200,
        img_max_images: is.max_images ?? 50,
        img_opt_label_type: opts.label_type ?? true,
        img_opt_read_text: opts.read_text ?? true,
        img_opt_describe_scene: opts.describe_scene ?? true,
        img_opt_describe_layout: opts.describe_layout ?? false,
        img_prompt: is.prompt ?? '',
      });
      // 部门没配过提示词（空串）→ 交给实时生成填默认模板；配过则视为已自定义
      setImgPromptTouched(!!(is.prompt ?? ''));
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
      // 只提交 llm 段白名单字段（条目名 + 温度/Token 微调）：
      // 空串/null = 不覆盖 → 跟随选中条目（没选则跟随全局激活模型）
      await updateChatSettings({
        llm: {
          model: vals.llm_model ?? '',
          temperature: vals.llm_temperature ?? null,
          max_tokens: vals.llm_max_tokens ?? null,
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
      // 提示词二选一：自定义模式清掉引用名并带上正文，引用/默认模式清掉正文
      // （后端解析顺序：引用 > system_prompt > 内置模板）
      const isPromptCustom =
        (vals.chat_system_prompt_ref ?? '') === CUSTOM_PROMPT;
      // 只提交 chat 段白名单字段（后端校验）；temperature 用 LLM 配置默认时提交 null
      await updateChatSettings({
        chat: {
          enable_multi_turn: vals.chat_enable_multi_turn,
          history_rounds: vals.chat_history_rounds,
          system_prompt: isPromptCustom ? (vals.chat_system_prompt ?? '') : '',
          system_prompt_ref: isPromptCustom
            ? ''
            : (vals.chat_system_prompt_ref ?? ''),
          temperature: vals.use_default_temperature ? null : vals.chat_temperature,
          top_p: vals.chat_top_p,
          max_tokens: vals.chat_max_tokens ?? null,
          thinking_mode: vals.chat_thinking_mode,
          kg_enhance: vals.chat_kg_enhance,
          query_rewrite: vals.chat_query_rewrite,
          query_rewrite_rounds: vals.chat_query_rewrite_rounds,
          // 聊天识图：模板名（空串 = 跟随全局）+ 自定义提示词
          //（自定义非空时优先于模板；两个都清空 = 回到跟随全局）
          image_template: vals.chat_image_template ?? '',
          image_prompt: vals.chat_image_prompt ?? '',
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

  const saveImgSummary = async () => {
    const vals = await imgForm.validateFields();
    setSavingImg(true);
    try {
      // 图片摘要段（部门可覆盖）：空 model = 用超管的默认模型，
      // 空 prompt = 用内置默认模板（后端 build_prompt 兜底）
      await updateChatSettings({
        image_summary: {
          model: vals.img_model ?? '',
          prompt: vals.img_prompt ?? '',
          output_format: vals.img_output_format ?? 'fields',
          text_max_chars: vals.img_text_max_chars ?? 200,
          max_images: vals.img_max_images ?? 50,
          options: {
            label_type: vals.img_opt_label_type ?? true,
            read_text: vals.img_opt_read_text ?? true,
            describe_scene: vals.img_opt_describe_scene ?? true,
            describe_layout: vals.img_opt_describe_layout ?? false,
          },
        },
      });
      message.success('图片摘要配置已保存');
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '保存失败');
    } finally {
      setSavingImg(false);
    }
  };

  const saveRetrieval = async () => {    const vals = await retrForm.validateFields();
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
          {/* 部门只**选**超管配好的模型条目（连接信息与密钥不下放），再按需
              微调温度/Token。原先让部门自由填 6 个字段，想换个模型就得整份
              抄一遍，抄漏一个就静默漂移——实测软件部漏了 thinking_control，
              用着思考模型 apex-quality 却继承了激活条目的 'none' */}
          <Row gutter={16}>
            <Col span={16}>
              <Form.Item name="llm_model" label="模型"
                extra="从超管配置的「LLM 模型管理」里选一个；留空 = 跟随全局激活模型">
                <Select allowClear showSearch placeholder="留空 = 跟随全局"
                  optionFilterProp="label"
                  options={llmOptions.map(m => ({
                    value: m.name,
                    label: m.model ? `${m.name}（${m.model}）` : m.name,
                  }))} />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="llm_temperature" label="温度" extra="覆盖选中条目">
                <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }}
                  placeholder="跟随条目" />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="llm_max_tokens" label="最大 Token" extra="覆盖选中条目">
                <InputNumber min={1} style={{ width: '100%' }}
                  placeholder="跟随条目" />
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
          {/* 系统提示词：可从超管维护的提示词库选一条（存名字 → 改库内容本部门
              立即生效），也可给本部门临时自定义一条 */}
          <Form.Item name="chat_system_prompt_ref" label="系统提示词"
            extra={
              <Space size={8}>
                <span>可引用超管维护的提示词库（改库内容本部门立即生效），或自定义一条；留空 = 用内置默认模板</span>
                {/* 自定义模式下文本框就在下方，无需"查看" */}
                {chatPromptRef !== CUSTOM_PROMPT && (
                  <Popover
                    title={chatPromptRef
                      ? `提示词库条目：${chatPromptRef}`
                      : '「跟随全局默认」实际使用的提示词'}
                    trigger="click"
                    content={
                      <div style={{
                        maxWidth: 460,
                        maxHeight: 320,
                        overflowY: 'auto',
                        whiteSpace: 'pre-wrap',
                        fontSize: 12,
                        lineHeight: 1.7,
                      }}>
                        {chatPromptRef
                          ? (promptOptions.find(p => p.name === chatPromptRef)?.content
                            ?? '（该条目已不存在，运行时将回退全局默认）')
                          : (defaultSystemPrompt || '加载中…')}
                      </div>
                    }
                  >
                    <Typography.Link style={{ fontSize: 12 }}>
                      查看提示词
                    </Typography.Link>
                  </Popover>
                )}
              </Space>
            }>
            <Select
              allowClear
              style={{ width: 380 }}
              placeholder="跟随全局默认"
              options={[
                { value: '', label: '跟随全局默认' },
                ...promptOptions.map(p => ({
                  value: p.name,
                  label: `提示词库：${p.name}`,
                })),
                { value: CUSTOM_PROMPT, label: '自定义…' },
              ]}
            />
          </Form.Item>
          {chatPromptRef === CUSTOM_PROMPT && (
            <Form.Item name="chat_system_prompt" label="自定义系统提示词"
              extra="可含 {refs} 占位符">
              <TextArea rows={4} placeholder="输入本部门专属的系统提示词…" />
            </Form.Item>
          )}
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
          {/* 聊天识图提示词（部门可覆盖，后端 whitelist chat.image_prompt）：
              各部门的图差别大（财务报表 / 运维报错截图 / 人事证照），一份
              全局提示词不可能都对，故允许本部门定制 */}
          {/* 读图策略：**选模板**（入库摘要与聊天识图共用同一套「看图策略」），
              自定义提示词是留给确实特殊的部门的出口——填了就优先于模板 */}
          <Form.Item
            name="chat_image_template"
            label="读图模板"
            extra="员工在聊天里发图、以及文档入库生成图片摘要时，共用这套「看图策略」——两边关注同样的东西、排除同样的东西，图文检索才容易互相对上。留空 = 用默认那套。"
          >
            <Select allowClear placeholder="用默认（通用）"
              options={imageTemplateOptions.map(t => ({
                value: t.name,
                label: t.preview ? `${t.name}（${t.preview}…）` : t.name,
              }))} />
          </Form.Item>
          <Form.Item
            name="chat_image_prompt"
            label={
              <Space size={8}>
                自定义读图提示词（高级）
                {/* 一键填入「当前实际会发」的那份——留空时输入框只有灰字
                    placeholder，想看清全文/在它基础上改都够不着 */}
                <Button type="link" size="small"
                  style={{ padding: 0, height: 'auto' }}
                  onClick={() => chatForm.setFieldsValue({
                    chat_image_prompt: defaultImagePrompt,
                  })}>
                  填入当前生效的提示词
                </Button>
              </Space>
            }
            extra="填了就优先于上面的模板，本部门不再用模板——仅当内置模板都不适用时才填。两个占位符可选：{max_chars} 替换为描述字数上限；{question} 替换为员工当前的问题，写上它才会带着问题读图（图里有箭头/红框/圈注时能定位到具体元素、报出准确名称），不写就是盲读，读图效果会明显变差。"
          >
            <TextArea rows={4} placeholder={defaultImagePrompt} />
          </Form.Item>
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
    {
      key: 'image_summary',
      label: (
        <Space>
          <Tag color="purple">图片摘要</Tag>
          图片摘要生成
          <span style={{ color: '#8c8c8c', fontSize: 12, fontWeight: 400 }}>
            解析时勾选才生效：把图里的文字读进正文，让证照/扫描件能被检索
          </span>
        </Space>
      ),
      extra: saveBtn(savingImg, () => void saveImgSummary()),
      children: (
        <Form form={imgForm} layout="vertical">
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="模型由超级管理员在「系统配置 → 图片解析模型」里维护，这里只选用哪个；提示词留空则用内置默认模板。"
          />
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item name="img_model" label="图片解析模型"
                extra="留空 = 用超管设的默认模型">
                <Select allowClear placeholder="跟随默认"
                  options={visionOptions.map(v => ({
                    value: v.name,
                    label: v.model ? `${v.name}（${v.model}）` : v.name,
                  }))} />
              </Form.Item>
            </Col>
            <Col span={imgFmt === 'brief' ? 8 : 6}>
              <Form.Item name="img_output_format" label="输出格式"
                extra="固定字段的检索命中率更高；简介不读图中文字">
                <Select options={[
                  { value: 'fields', label: '固定字段（类型/文字/画面）' },
                  { value: 'prose', label: '自然段' },
                  { value: 'brief', label: '一句话简介（不读图中文字）' },
                ]} />
              </Form.Item>
            </Col>
            {/* 「文字字段上限」是 fields 模式专有（截断"文字"字段）——
                简介模式不读文字、没有该字段，隐藏 */}
            {imgFmt !== 'brief' && (
              <Col span={6}>
                <Form.Item name="img_text_max_chars" label="文字字段上限"
                  extra="超出截断">
                  <InputNumber min={0} max={2000} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
            )}
            <Col span={imgFmt === 'brief' ? 8 : 6}>
              <Form.Item name="img_max_images" label="单文档图数上限"
                extra="0 = 不限">
                <InputNumber min={0} max={1000} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          {/* 「摘要内容」选项只服务 fields/prose（按选项拼提示词）；简介模式
              的输出由固定模板决定，勾选无意义，整块隐藏 */}
          {imgFmt !== 'brief' && (
            <Row gutter={16}>
              <Col span={24}>
                <Form.Item label="摘要内容（勾选后按选项生成提示词）"
                  style={{ marginBottom: 8 }}>
                  <Space size="large" wrap>
                    <Form.Item name="img_opt_label_type" valuePropName="checked" noStyle>
                      <Switch size="small" />
                    </Form.Item>
                    <span style={{ marginLeft: -14 }}>标注文件类型</span>
                    <Form.Item name="img_opt_read_text" valuePropName="checked" noStyle>
                      <Switch size="small" />
                    </Form.Item>
                    <span style={{ marginLeft: -14 }}>读出图中文字</span>
                    <Form.Item name="img_opt_describe_scene" valuePropName="checked" noStyle>
                      <Switch size="small" />
                    </Form.Item>
                    <span style={{ marginLeft: -14 }}>描述主体与场景</span>
                    <Form.Item name="img_opt_describe_layout" valuePropName="checked" noStyle>
                      <Switch size="small" />
                    </Form.Item>
                    <span style={{ marginLeft: -14 }}>描述布局与结构</span>
                  </Space>
                </Form.Item>
              </Col>
            </Row>
          )}
          <Row gutter={16}>
            <Col span={24}>
              {/* 提示词：标题行自己排（不用 Form.Item 的 label）——
                  label 容器宽度不受控，marginLeft:auto 推不到右边 */}
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
                <Space size={8}>
                  <span style={{ fontWeight: 500 }}>提示词</span>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {imgPromptTouched
                      ? '已自定义：改选项不再覆盖（点「恢复默认」还原）'
                      : '由上面的选项实时生成，可直接微调'}
                  </Text>
                </Space>
                <Button size="small" style={{ marginLeft: 'auto' }}
                  onClick={() => {
                    setImgPromptTouched(false);
                    imgForm.setFieldsValue({
                      img_opt_label_type: true, img_opt_read_text: true,
                      img_opt_describe_scene: true,
                      img_opt_describe_layout: false,
                    });
                    message.info('已按默认选项重新生成（保存后生效）');
                  }}>恢复默认</Button>
              </div>
              <Form.Item name="img_prompt" style={{ marginBottom: 0 }}>
                <Input.TextArea rows={6}
                  onChange={() => setImgPromptTouched(true)} />
              </Form.Item>
            </Col>
          </Row>
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
    </div>
  );
};

export default DeptConfig;
