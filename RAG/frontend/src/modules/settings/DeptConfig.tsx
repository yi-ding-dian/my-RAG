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

const { TextArea } = Input;
const { Text } = Typography;

/** 系统提示词下拉里的"自定义"标记（取不可能撞上条目名的串） */
const CUSTOM_PROMPT = '__custom__';

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

// ---- 默认提示词模板（与后端 backend/services/image_summary.py 的
//      default_prompt 保持同措辞；前端这份只用于「选项一变就实时显示」，
//      真正生效的默认值仍由后端兜底——两边不一致时以后端为准）----
const IMG_OPTION_LINES: Record<string, string> = {
  label_type: '类型：这是什么（文件类型或场景）',
  read_text: '文字：图中可见的关键文字（标题、单位名称、编号、日期、金额、规格等）',
  describe_scene: '画面：画面主要对象与特征（设备、场地、人物、签章等）',
  describe_layout: '版式：版式结构（表格行列、签章位置、分区布局等）',
};
const IMG_OPTION_ORDER = ['label_type', 'read_text', 'describe_scene', 'describe_layout'];
const IMG_FIXED_TAIL = `
要求：
- 只描述你确实看到的内容，不要推测、不要评价、不要补充常识
- 某个字段确实没有内容时，写"无"
- 不要开场白，不要总结
`;

/** 按选项 + 输出格式拼默认提示词（与后端 default_prompt 同逻辑）
 *  brief 格式不读图中文字——适合"整页全是文字但不需要理解含义"的图，
 *  代价是图里的字检索不到，故不适合证照类。 */
function buildDefaultImgPrompt(opts: Record<string, boolean>, fmt: string): string {
  if (fmt === 'brief') {
    return '用一句话说明这张图片大致是什么、用来做什么的'
      + '（如「某公司生产厂房外景照片」「设备接线示意图」）。\n\n'
      + '要求：\n'
      + '- 只描述你确实看到的，不要推测、不要评价、不要陈述图中的具体内容与文字\n'
      + '- 不超过 30 字，写成一句话\n'
      + '- 直接输出这句话，不要加「这张图片」之类的开场白，不要换行';
  }
  const lines = IMG_OPTION_ORDER.filter(k => opts[k]).map(k => IMG_OPTION_LINES[k]);
  const use = lines.length ? lines : [IMG_OPTION_LINES.read_text];
  if (fmt === 'prose') {
    return '请查看这张图片，用中文写一段 2~4 句的客观描述，用于文档检索。\n\n'
      + '要点：\n' + use.map(l => `- ${l}`).join('\n') + '\n' + IMG_FIXED_TAIL;
  }
  return '请查看这张图片，按下面的字段输出中文描述，用于文档检索。\n\n'
    + '每行一个字段，只输出这几行，不要加其他说明：\n'
    + use.join('\n') + '\n' + IMG_FIXED_TAIL;
}

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
  const [visionOptions, setVisionOptions] = useState<Array<{ name: string; model: string }>>([]);
  /** 超管配的系统提示词库条目（部门只"选"用哪条，不能编辑库本身） */
  const [promptOptions, setPromptOptions] = useState<Array<{ name: string; content: string }>>([]);
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

  /** 上一次的输出格式：用来识别"格式变了"（格式与提示词是配套的，变了必须重算） */
  const prevImgFmtRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    // **输出格式变了就强制重算**（并解除"已自定义"）：旧提示词是照旧格式写的，
    // 留着会让模型按旧格式作答、而代码按新格式解析——与解析入口的处理同一道理。
    // 其余情况（勾选项变化 / 手动微调）仍尊重 touched：用户的微调不该被选项覆盖。
    // 首次（prev 为 undefined，含加载回填）不算"变化"，避免把存过的提示词冲掉。
    const fmtChanged = prevImgFmtRef.current !== undefined
      && prevImgFmtRef.current !== imgFmt;
    prevImgFmtRef.current = imgFmt;
    if (!fmtChanged && imgPromptTouched) return;
    imgForm.setFieldsValue({
      img_prompt: buildDefaultImgPrompt({
        label_type: imgOptLabel ?? true,
        read_text: imgOptText ?? true,
        describe_scene: imgOptScene ?? true,
        describe_layout: imgOptLayout ?? false,
      }, imgFmt ?? 'fields'),
    });
    if (fmtChanged) setImgPromptTouched(false);
  }, [imgPromptTouched, imgFmt, imgOptLabel, imgOptText, imgOptScene,
      imgOptLayout, imgForm]);

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
        chat_system_prompt_ref: chat?.system_prompt_ref ?? '',
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
      // 图片摘要（后端返回全局默认 + 本部门覆盖的合并值）
      const img = (res.data as unknown as {
        image_summary?: ImageSummaryConfig;
        vision_options?: Array<{ name: string; model: string }>;
      });
      setVisionOptions(img.vision_options ?? []);
      setPromptOptions((res.data as { prompt_options?: Array<{ name: string; content: string }> })
        .prompt_options ?? []);
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
          {/* 系统提示词：可从超管维护的提示词库选一条（存名字 → 改库内容本部门
              立即生效），也可给本部门临时自定义一条 */}
          <Form.Item name="chat_system_prompt_ref" label="系统提示词"
            extra="可引用超管维护的提示词库（改库内容本部门立即生效），或自定义一条；留空 = 用内置默认模板">
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
              extra="可含 {knowledge} / {refs} 占位符">
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
