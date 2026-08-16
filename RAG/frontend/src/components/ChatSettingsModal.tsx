import React, { useEffect, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Slider,
  Space,
  Spin,
  Switch,
} from 'antd';
import { getChatSettings, updateChatSettings } from '../api/client';
import { useAuth } from '../auth/AuthContext';

const { TextArea } = Input;

// 内置默认系统提示词概要（占位提示；完整模板在后端 chat_service._SYSTEM_PROMPT_TEMPLATE）
const DEFAULT_SYSTEM_PROMPT_SUMMARY =
  '留空使用系统默认提示词（只依据 [引用] 回答、句末用 [n] 标注来源、无相关内容时明确说明、简洁准确中文回答）；可包含 {knowledge} 占位符（检索原文逐字注入，无来源包装，适合要求逐字输出原文的场景）或 {refs}（带来源标注的引用内容）';

interface ChatSettingsFormValues {
  // 检索设置
  retrieval_similarity_threshold: number; // 0-1，默认 0（不过滤）
  retrieval_top_k: number; // 1-20，默认 5
  chat_kg_enhance: boolean; // 知识图谱增强，默认 true（有图谱才生效）
  // 对话设置
  chat_enable_multi_turn: boolean; // 多轮对话，默认 true
  chat_history_rounds: number; // 1-20，默认 8
  use_default_temperature: boolean; // true=温度跟随模型默认（保存 null）
  chat_temperature: number; // 0-2，step 0.1
  chat_top_p: number; // 0-1
  chat_max_tokens?: number | null; // 可空=跟随模型默认
  chat_system_prompt?: string; // 自定义系统提示词（空串=使用内置默认模板）
}

interface ChatSettingsModalProps {
  open: boolean;
  onCancel: () => void;
}

/**
 * 聊天设置弹窗：检索设置（相似度阈值/Top N）+ 对话设置（多轮/历史轮数/温度/Top P/最大 Token）。
 * 数据源 GET /api/settings/chat（登录即可读，返回当前用户视角合并值）；保存 POST
 * /api/settings/chat（白名单仅 chat/retrieval 段）：
 * - dept_admin：标题"本部门聊天配置"——保存强制写入本部门 chat_config（对本部门所有
 *   成员生效，不碰全局）；表单值为本部门合并结果（未设置的字段显示全局值，改动后仅
 *   该字段按部门覆盖，其余仍跟随全局）；
 * - super_admin：标题"聊天设置（全局）"——保存写入全局活跃档案。
 * 接口错误直接透传后端中文 detail 展示。
 */
const ChatSettingsModal: React.FC<ChatSettingsModalProps> = ({ open, onCancel }) => {
  const { message } = AntApp.useApp();
  const { user } = useAuth();
  const isDeptAdmin = user?.role === 'dept_admin';
  const [form] = Form.useForm<ChatSettingsFormValues>();
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const useDefaultTemperature = Form.useWatch('use_default_temperature', form) ?? true;

  // 打开时加载活跃档案聊天设置并回填表单
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    getChatSettings()
      .then(res => {
        if (cancelled) return;
        const { retrieval, chat } = res.data;
        setLoaded(true);
        form.setFieldsValue({
          retrieval_similarity_threshold: retrieval?.similarity_threshold ?? 0,
          retrieval_top_k: retrieval?.top_k ?? 5,
          chat_kg_enhance: chat?.kg_enhance ?? true,
          chat_enable_multi_turn: chat?.enable_multi_turn ?? true,
          chat_history_rounds: chat?.history_rounds ?? 8,
          use_default_temperature: chat?.temperature == null,
          chat_temperature: chat?.temperature ?? 0.7,
          chat_top_p: chat?.top_p ?? 0.9,
          chat_max_tokens: chat?.max_tokens ?? undefined,
          chat_system_prompt: chat?.system_prompt ?? '',
        });
      })
      .catch((e: any) => {
        if (cancelled) return;
        // 透传后端中文错误（如"没有激活的配置档案"），不再误显误导文案
        setLoadError(e.response?.data?.detail || '加载聊天设置失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, form]);

  const handleOk = async () => {
    let values: ChatSettingsFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    try {
      // 只提交白名单段：retrieval.top_k/similarity_threshold + chat 段（后端白名单校验）
      await updateChatSettings({
        retrieval: {
          top_k: values.retrieval_top_k,
          similarity_threshold: values.retrieval_similarity_threshold,
        },
        chat: {
          kg_enhance: values.chat_kg_enhance,
          enable_multi_turn: values.chat_enable_multi_turn,
          history_rounds: values.chat_history_rounds,
          // true=用 LLM 配置默认（保存 null）；false=保存滑条值
          temperature: values.use_default_temperature ? null : values.chat_temperature,
          top_p: values.chat_top_p,
          max_tokens: values.chat_max_tokens ?? null,
          // 空串=恢复内置默认模板（后端空串例外路径）
          system_prompt: values.chat_system_prompt ?? '',
        },
      });
      message.success('聊天设置已保存，即时生效');
      onCancel();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '保存聊天设置失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={isDeptAdmin ? '本部门聊天配置' : '聊天设置（全局）'}
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      confirmLoading={saving}
      okText="保存"
      cancelText="取消"
      okButtonProps={{ disabled: !loaded }}
      width={780}
    >
      {isDeptAdmin && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message="本部门配置对本部门所有成员生效"
          description="未修改的字段沿用全局配置；保存后本部门成员聊天即按此配置生效，不影响其他部门。"
        />
      )}
      {loading ? (
        <div style={{ textAlign: 'center', padding: 32 }}>
          <Spin />
        </div>
      ) : loaded ? (
        <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
          {/* 检索设置 */}
          <Card title="检索设置" size="small" style={{ marginBottom: 16 }}>
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item
                  name="retrieval_similarity_threshold"
                  label="相似度阈值"
                  extra="低于该阈值的检索片段将被过滤；0=不过滤"
                >
                  <Slider
                    min={0}
                    max={1}
                    step={0.05}
                    tooltip={{ formatter: v => `${Math.round((v ?? 0) * 100)}%` }}
                  />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item
                  name="retrieval_top_k"
                  label="Top N（返回片段数）"
                  rules={[
                    { required: true, message: '请输入 Top N' },
                    { type: 'number', min: 1, max: 20, message: '范围 1-20' },
                  ]}
                >
                  <InputNumber min={1} max={20} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
            </Row>
            <Form.Item
              name="chat_kg_enhance"
              label="知识图谱增强"
              valuePropName="checked"
              extra="查询时结合知识图谱实体关系增强回答，需文档构建过知识图谱（无图谱自动跳过）"
              style={{ marginBottom: 0 }}
            >
              <Switch />
            </Form.Item>
          </Card>

          {/* 对话设置 */}
          <Card title="对话设置" size="small">
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item name="chat_enable_multi_turn" label="多轮对话" valuePropName="checked" extra="开启后携带历史消息进行多轮问答">
                  <Switch />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item
                  name="chat_history_rounds"
                  label="历史轮数"
                  extra="对话时携带的历史轮数（1-20）"
                  rules={[
                    { required: true, message: '请输入历史轮数' },
                    { type: 'number', min: 1, max: 20, message: '范围 1-20' },
                  ]}
                >
                  <InputNumber min={1} max={20} style={{ width: '100%' }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item label="温度" required style={{ marginBottom: 8 }}>
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                      <span style={{ fontSize: 13, color: 'rgba(0,0,0,0.45)' }}>使用默认（跟随模型）</span>
                      <Form.Item name="use_default_temperature" valuePropName="checked" noStyle>
                        <Switch size="small" />
                      </Form.Item>
                    </div>
                    <Form.Item name="chat_temperature" noStyle>
                      <Slider
                        min={0}
                        max={2}
                        step={0.1}
                        disabled={useDefaultTemperature}
                        tooltip={{ formatter: v => v?.toFixed(1) }}
                      />
                    </Form.Item>
                  </Space>
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="chat_top_p" label="Top P" extra="核采样概率（0-1）">
                  <Slider min={0} max={1} step={0.05} tooltip={{ formatter: v => v?.toFixed(2) }} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="chat_max_tokens" label="最大 Token" extra="留空=跟随模型默认">
                  <InputNumber min={1} max={128000} placeholder="跟随模型默认" style={{ width: '100%' }} />
                </Form.Item>
              </Col>
            </Row>
            <Form.Item
              name="chat_system_prompt"
              label="系统提示词"
              extra="留空使用系统默认提示词；{knowledge}=检索原文逐字注入（无来源标注，模型可原样输出含图片/表格），{refs}=带来源标注的引用内容；两者可并存；不含任何占位符时自动在末尾追加引用段"
              style={{ marginBottom: 8 }}
            >
              <TextArea
                autoSize={{ minRows: 4, maxRows: 6 }}
                placeholder={DEFAULT_SYSTEM_PROMPT_SUMMARY}
              />
            </Form.Item>
            <div style={{ textAlign: 'right' }}>
              <Button
                type="link"
                size="small"
                style={{ padding: 0, height: 'auto' }}
                onClick={() => form.setFieldValue('chat_system_prompt', '')}
                title={isDeptAdmin
                  ? '清除本部门自定义提示词，跟随全局配置'
                  : '清空提示词，使用内置默认模板'}
              >
                {isDeptAdmin ? '跟随全局' : '恢复默认'}
              </Button>
            </div>
          </Card>
        </Form>
      ) : (
        <Alert
          message={loadError || '未找到活跃配置档案'}
          description={loadError ? undefined : '请到「系统配置」创建并激活配置档案后，再进行聊天设置'}
          type="warning"
          showIcon
          style={{ marginTop: 8 }}
        />
      )}
    </Modal>
  );
};

export default ChatSettingsModal;
