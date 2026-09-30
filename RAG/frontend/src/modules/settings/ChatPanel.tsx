import React, { useEffect, useRef, useState } from 'react';
import { Col, Divider, Form, Input, InputNumber, Row, Select, Switch, Typography } from 'antd';
import type { LLMModelItem, VisionModelItem } from '../../shared/api/types';
import { buildDefaultImgPrompt } from './imageSummaryPrompt';

const { Text } = Typography;

/**
 * 内置读图提示词——**仅用于输入框的 placeholder，不与后端联动**
 *
 * 真值以后端 `chat_vision._CHAT_IMAGE_PROMPT` 为准（前端这份改了不影响实际
 * 调用）；贴在这里只是为了让"留空 = 用默认"不是个黑盒：用户得先看见默认长
 * 什么样，才知道该往哪儿改。改后端常量时同步改这里即可，不一致最多是提示
 * 文案过期，不会有行为差异。
 *
 * 两个占位符：`{max_chars}` 换成描述字数上限、`{question}` 换成用户当前问题。
 * 自定义提示词写了 `{question}` 才会带上问题读图（不写 = 盲读，行为同旧版）。
 */
const DEFAULT_IMAGE_PROMPT = `请描述这张图片，供知识库检索与问答使用。用户就这张图提的问题是：「{question}」。
要求：
1. **逐字抄录图中所有文字**：标题、单位名、编号、字段名、按钮名、型号、参数、报错信息、日期。这些是拿去知识库检索的关键词，必须一字不差，不得概括或改写。每项只列一次，不要重复；
2. 图中有**箭头、红框、圈注、高亮**等标记时，明确说明它指向哪个元素（报出该元素的准确名称）——用户的问题通常就针对这个元素；
3. 图中有**印章、签字、表格、图表**时，说明其内容与数量；若是界面或报错截图，说明是什么系统、什么页面、什么操作、什么提示；
4. **只描述真实看到的内容**：不推测、不补充常识、不回答图片之外的问题。文字模糊看不清时，明确说明看不清，**绝对不要猜测或编造**任何编号与数字；
5. **不要描述头像、昵称、时间戳、聊天气泡、背景装饰**等与内容无关的元素——它们不是检索线索，只会挤占篇幅；
6. 简洁中文，不超过 {max_chars} 字。`;

interface Props {
  /** LLM 模型列表：「识图模型」的首选来源——多模态模型通常就配在这份列表里 */
  llmModels: LLMModelItem[];
  /** 图片解析模型列表（配置档案 vision 段），后端候选池的另一半 */
  visionModels: VisionModelItem[];
  /** 通知外层"内容被改过"：切换提示词来源走的是程序化 setFieldsValue，
   *  不触发 Form.onValuesChange（脏标记唯一来源），不手动报到就会关窗不提示 */
  onEdit: () => void;
}

/** 识图模型下拉的选项：LLM 模型 + 图片解析模型，同名只留先出现的那份
 *
 *  按来源分组显示，是为了让人看清"这个模型是从哪配的"；后端的候选池同样是
 *  这两份合并（见 image_summary._vision_models），选哪一组都能解析到。
 */
const imageModelOptions = (
  llmModels: LLMModelItem[], visionModels: VisionModelItem[],
) => {
  const seen = new Set<string>();
  const pick = (name: string, model: string) => {
    if (!name || seen.has(name)) return null;
    seen.add(name);
    return {
      value: name,
      label: model && model !== name ? `${name}（${model}）` : name,
    };
  };
  const llm = llmModels.map(m => pick(m.name, m.model)).filter(Boolean);
  const vision = visionModels.map(m => pick(m.name, m.model)).filter(Boolean);
  const groups: { label: string; options: NonNullable<ReturnType<typeof pick>>[] }[] = [];
  if (llm.length) groups.push({ label: 'LLM 模型', options: llm as never });
  if (vision.length) groups.push({ label: '图片解析模型', options: vision as never });
  return groups;
};

/**
 * 聊天设置面板（配置档案弹窗）
 *
 * - 单条输入长度：问题/检索 query 的最大长度（原在「入库与限制」面板——
 *   它属于 chat 段、语义是聊天输入限制而非入库限制，故挪到此处）
 * - 引用设置：鼠标悬停在回答里的引用标 [n] 上时，浮层显示的字数上限。
 *   为什么是"窗口大小"而不是"截断长度"：回答实际用到的内容常落在块的中后段
 *   （表格块的有效数字几乎都在表格下方），浮层会先在全量文本上定位命中、
 *   再围绕命中开窗（见 MessageList 的 buildSnippet）。从头硬截会让命中整段
 *   落在窗口外，浮层里什么也标不出来——这正是此前"引用浮层看不到高亮"的原因。
 * - 聊天识图：发送图片 → 视觉模型读图 → 描述参与检索与回答（下方分组）
 */
const ChatPanel: React.FC<Props> = ({ llmModels, visionModels, onEdit }) => {
  const form = Form.useFormInstance();
  // ---- 读图提示词的来源：可以复用「图片摘要」那份 ----
  // 取「图片摘要」面板里配的那份；没配就用它的选项 + 格式算一份默认出来
  // （与后端 default_prompt 同逻辑，见 imageSummaryPrompt.ts）
  const sumPrompt = Form.useWatch('image_summary_prompt', form);
  const sumFmt = Form.useWatch('image_summary_output_format', form);
  const sumLabel = Form.useWatch('image_summary_opt_label_type', form);
  const sumText = Form.useWatch('image_summary_opt_read_text', form);
  const sumScene = Form.useWatch('image_summary_opt_describe_scene', form);
  const sumLayout = Form.useWatch('image_summary_opt_describe_layout', form);
  const chatPrompt = Form.useWatch('chat_image_prompt', form);
  const summaryPrompt = String(sumPrompt ?? '').trim() || buildDefaultImgPrompt({
    label_type: sumLabel ?? true, read_text: sumText ?? true,
    describe_scene: sumScene ?? true, describe_layout: sumLayout ?? false,
  }, sumFmt ?? 'fields');

  /**
   * 当前用的是哪一份提示词。
   *
   * 打开弹窗时**由值反推**（后端只认 `chat.image_prompt` 这一个字符串字段，
   * 空 = 内置；加"用哪一份"的模式位要动 schema 与部门覆盖逻辑，不值当）。
   * 但反推只够用在打开那一刻：用户切到「用图片摘要那份」之后，只要再改图片
   * 摘要的输出格式，那份文本就不再等于框里的快照——纯反推会误判成「自定义」、
   * 把来源下拉弹回去。所以推断出初值后，改由用户的选择驱动。
   */
  const [source, setSource] = useState<'builtin' | 'summary' | 'custom'>('builtin');
  /** summaryPrompt 的最新值（初始化 effect 只在挂载时跑一次，不便进依赖） */
  const summaryPromptRef = useRef('');
  summaryPromptRef.current = summaryPrompt;

  // 回填完成后按值推断一次来源。延到下一个宏任务，因为本面板的 effect 跑在
  // 父组件回填（fillForm）之前，此刻表单还是空的
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const v = String(form.getFieldValue('chat_image_prompt') ?? '').trim();
      setSource(!v ? 'builtin'
        : (v === summaryPromptRef.current ? 'summary' : 'custom'));
    }, 0);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 停在「用图片摘要那份」时，那份一变（换了输出格式/内容选项）就把快照跟上，
  // 否则框里还是旧格式的文本，而来源又会被反推逻辑判成「自定义」
  useEffect(() => {
    if (source !== 'summary') return;
    if (String(chatPrompt ?? '') === summaryPrompt) return;
    form.setFieldsValue({ chat_image_prompt: summaryPrompt });
    onEdit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, summaryPrompt, chatPrompt]);

  /**
   * 切换来源。
   *
   * - 内置：清空 → 后端回退内置读图提示词常量
   * - 摘要：填入「图片摘要」那份的文本（**快照**，不是引用——后端只认这一个
   *   字符串字段，引用要动 schema；摘要日后改了，这里会自动落回"自定义"并
   *   提示那份过期）
   * - 自定义：空框里先垫上内置那份当起点，否则选完还是"跟随内置"（来源是
   *   由值反推的，空值等于内置），用户会以为点了没反应
   */
  const switchSource = (src: string) => {
    const cur = String(form.getFieldValue('chat_image_prompt') ?? '').trim();
    setSource(src as 'builtin' | 'summary' | 'custom');
    if (src === 'builtin') form.setFieldsValue({ chat_image_prompt: '' });
    if (src === 'summary') form.setFieldsValue({ chat_image_prompt: summaryPrompt });
    if (src === 'custom' && !cur) {
      form.setFieldsValue({ chat_image_prompt: DEFAULT_IMAGE_PROMPT });
    }
    onEdit();
  };

  return (
  <>
    <Row gutter={12}>
      <Col span={8}>
        <Form.Item
          name="chat_max_query_len"
          label="单条输入长度（字）"
          tooltip="问题/检索 query 最大长度，超出返回友好提示（防超长粘贴耗尽上下文与费用）"
        >
          <InputNumber min={100} max={20000} step={100} style={{ width: '100%' }} />
        </Form.Item>
      </Col>
      <Col span={8}>
        <Form.Item
          name="chat_citation_snippet_chars"
          label="引用设置"
          tooltip={
            <div style={{ fontSize: 12, lineHeight: '18px' }}>
              鼠标悬停在回答里的引用标 [n] 上时，浮层显示的字数上限。
              <div style={{ marginTop: 6 }}>
                浮层会围绕回答实际用到的那段内容开窗——命中位置靠后时
                不会从头截断（否则命中会落在窗口外，浮层里看不到高亮）。
              </div>
              <div style={{ marginTop: 6 }}>
                范围 100~2000 字，默认 600。
              </div>
            </div>
          }
        >
          <InputNumber min={100} max={2000} step={50}
                       style={{ width: '100%' }} addonAfter="字" />
        </Form.Item>
      </Col>
      <Col span={8}>
        <Form.Item
          name="chat_prompt_total_max_tokens"
          label="上下文预算"
          tooltip={
            <div style={{ fontSize: 12, lineHeight: '18px' }}>
              进 prompt 的检索片段<b>总量</b>上限（token）。超出即停止追加后续
              片段；首条就超预算时截断它（有上下文总比一条都没有强）。
              <div style={{ marginTop: 6 }}>
                <b>只设总量、不设单条</b>：总量天然隐含单条约束，再配单条只会
                无谓截断大块——而大块往往正是最相关的那条。
              </div>
              <div style={{ marginTop: 6 }}>
                怎么定：<b>模型窗口 − 输出 max_tokens − 历史/系统提示的余量</b>。
                例：窗口 15000、输出 4096 → 输入留约 6000。
              </div>
              <div style={{ marginTop: 6 }}>
                token 数优先调模型服务的 /tokenize 精确计（vLLM 内置），
                服务不支持时按 字符数 × 0.62 估算。范围 500~200000，默认 6000。
              </div>
            </div>
          }
        >
          <InputNumber min={500} max={200000} step={500}
                       style={{ width: '100%' }} addonAfter="token" />
        </Form.Item>
      </Col>
    </Row>

    <Divider orientation="left" style={{ margin: '0 0 12px' }}>
      <Text type="secondary" style={{ fontSize: 12 }}>聊天识图</Text>
    </Divider>

    <Row gutter={12}>
      <Col span={6}>
        <Form.Item
          name="chat_image_enabled"
          label="发送图片"
          valuePropName="checked"
          tooltip="关闭后聊天界面不出现图片入口，后端也拒绝带图请求"
        >
          <Switch />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="chat_image_max_count"
          label="单次最多"
          tooltip="一条消息最多带几张图。前端超限直接拦（不发请求），后端再校验一次"
        >
          <InputNumber min={1} max={20} style={{ width: '100%' }} addonAfter="张" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="chat_image_max_mb"
          label="单张大小"
          tooltip="单张图片大小上限。前端选图时拦（提示更即时、不费上传往返），上传时后端再拦一次"
        >
          <InputNumber min={0.1} max={50} step={0.5}
                       style={{ width: '100%' }} addonAfter="MB" />
        </Form.Item>
      </Col>
      <Col span={6}>
        <Form.Item
          name="chat_image_desc_max_chars"
          label="描述长度上限"
          tooltip={
            <div style={{ fontSize: 12, lineHeight: '18px' }}>
              视觉模型读图产出的描述被<b>硬截断</b>到此长度。
              <div style={{ marginTop: 6 }}>
                <b>提示词里那句"不超过 N 字"只是软约束</b>——实测配置 800 字、
                模型输出 1498 字，所以这里有硬截断兜底。
              </div>
              <div style={{ marginTop: 6 }}>
                这段描述一次生成、两处用（并入检索词 + 注入回答），太长会把
                原问题淹没，故不是越大越好。
              </div>
              <div style={{ marginTop: 6 }}>
                <b>要模型多抄内容，就把它一起调大</b>：只改提示词不改这里，
                多抄的部分会被切掉，反而比不改更差。范围 100~4000，默认 800。
              </div>
            </div>
          }
        >
          <InputNumber min={100} max={4000} step={100}
                       style={{ width: '100%' }} addonAfter="字" />
        </Form.Item>
      </Col>
    </Row>

    <Row gutter={12}>
      <Col span={12}>
        <Form.Item
          name="chat_image_model"
          label="识图模型"
          tooltip={
            <div style={{ fontSize: 12, lineHeight: '18px' }}>
              读图用哪个多模态模型（选项来自「LLM 模型管理」与「图片解析模型」
              两份列表，多模态模型配在哪一份都能选）。
              <div style={{ marginTop: 6 }}>
                <b>留空 = 跟随「图片摘要」选的模型</b>。填了则识图与文档入库
                解耦：入库摘要要跑几十上百张图、求快求省，聊天识图是用户发完图
                实时等着的、求准，两者要求本就不同。
              </div>
              <div style={{ marginTop: 6 }}>
                指定的模型被删除后会自动回退到默认，不会让识图直接不可用。
              </div>
            </div>
          }
        >
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="跟随「图片摘要」选的模型"
            options={imageModelOptions(llmModels, visionModels)}
            notFoundContent="尚未配置多模态模型（先在「LLM 模型管理」里添加）"
          />
        </Form.Item>
      </Col>
    </Row>

    <Row gutter={12}>
      <Col span={8}>
        <Form.Item
          label="读图提示词来源"
          tooltip={
            <div style={{ fontSize: 12, lineHeight: '18px' }}>
              选「内置默认」或「图片摘要」时不必自己写。
              <div style={{ marginTop: 6 }}>
                两份提示词的用途不同：<b>图片摘要</b>那份是给入库用的三段式
                结构化输出（类型/文字/画面），套到聊天识图会让描述变成机械
                字段、丢掉「这是什么系统、报什么错」这类叙述；<b>内置读图</b>
                那份专为聊天设计（自然语言 + 高密度关键信息 + 长度占位符）。
                除非你确实想让两处共用一套措辞，否则建议保持内置。
              </div>
            </div>
          }
        >
          <Select
            value={source}
            onChange={switchSource}
            options={[
              { value: 'builtin', label: '跟随内置默认（推荐）' },
              { value: 'summary', label: '用「图片摘要」那份' },
              { value: 'custom', label: '自定义（在下方编辑）' },
            ]}
          />
        </Form.Item>
      </Col>
      {/* 选了「用图片摘要那份」时，把那份的输出格式也摆在这儿：
          提示词正文是跟着格式走的，换格式就等于换一份。
          与「图片摘要」面板里的 image_summary_output_format 是**同一个字段**，
          两处都绑 name 会共享同一份值——用户改任一边另一边跟着变，正是想要的 */}
      {source === 'summary' && (
        <Col span={8}>
          <Form.Item
            name="image_summary_output_format"
            label="图片摘要输出格式"
            tooltip={
              <div style={{ fontSize: 12, lineHeight: '18px' }}>
                这是<b>「图片摘要」的格式</b>（同一份配置）——在这里改，
                文档入库生成摘要时也会跟着变。
                <div style={{ marginTop: 6 }}>
                  提示词正文按格式生成，换格式就等于换一份，所以摆在旁边。
                </div>
              </div>
            }
          >
            <Select
              options={[
                { value: 'fields', label: '固定字段（类型/文字/画面）' },
                { value: 'prose', label: '自然段' },
                { value: 'brief', label: '一句话简介（不读图中文字）' },
              ]}
            />
          </Form.Item>
        </Col>
      )}
    </Row>

    <Form.Item
      name="chat_image_prompt"
      label="读图提示词"
      tooltip={
        <div style={{ fontSize: 12, lineHeight: '18px' }}>
          发给视觉模型的提示词。<b>留空 = 用下方 placeholder 里的内置默认。</b>
          <div style={{ marginTop: 6 }}>
            <b>支持 {'{max_chars}'} 占位符</b>，运行时替换为上面的「描述长度
            上限」。建议保留——不写它长度约束就只剩硬截断兜底。
          </div>
          <div style={{ marginTop: 6 }}>
            部门管理员可在「部门配置 → 对话增强」再覆盖一层；部门留空则跟随这里。
          </div>
          <div style={{ marginTop: 6 }}>
            调优提示：这段描述会<b>同时</b>并入检索词并注入回答，所以要
            「逐字抄录关键文字 + 简洁叙述」。若只想让模型回答得更细，改这里
            没用——主模型看到的是描述而非原图。
          </div>
        </div>
      }
    >
      <Input.TextArea
        rows={6}
        placeholder={DEFAULT_IMAGE_PROMPT}
        style={{ fontSize: 12, lineHeight: '18px' }}
      />
    </Form.Item>
    </>
  );
};

export default ChatPanel;
