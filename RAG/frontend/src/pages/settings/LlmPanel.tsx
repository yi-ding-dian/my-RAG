import React from 'react';
import { Alert, Button, Empty, Popconfirm, Radio, Space, Tag, Tooltip, Typography, theme } from 'antd';
import {
  CheckOutlined,
  DeleteOutlined,
  EditOutlined,
  LoadingOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import type { LLMModelItem } from '../../api/client';

const { Text } = Typography;

/** 面板 props：模型列表状态与操作回调（状态由容器页持有，与表单同源） */
interface Props {
  llmModels: LLMModelItem[];
  llmActive: number;
  llmTestingIdx: number | null;
  openModelEdit: (idx: number | null) => void;
  deleteModel: (idx: number) => void;
  activateModel: (idx: number) => void;
}

/** LLM 对话模型面板（配置档案弹窗）：多模型列表 + 勾选激活 + 增删改 */
const LlmPanel: React.FC<Props> = ({ llmModels, llmActive, llmTestingIdx, openModelEdit, deleteModel, activateModel }) => {
  const { token } = theme.useToken();
  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 8 }}
        message="可添加多个模型；勾选激活的模型用于问答 / 评估等对话场景，以及未指定解析模型的上下文摘要 / 知识图谱抽取（解析配置弹窗可单独指定解析 LLM 模型）。激活时自动测试连接（GET {base_url}/models），连接失败可确认后仍激活。"
      />
      {llmModels.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          style={{ margin: '8px 0' }}
          description="未添加模型：保存档案后将使用系统出厂默认模型"
        />
      ) : (
        llmModels.map((m, i) => (
          <div
            key={i}
            style={{
              display: 'flex', alignItems: 'center', gap: 12,
              padding: '8px 12px', marginBottom: 8,
              borderRadius: 6,
              border: llmActive === i
                ? '1px solid var(--brand-primary, #2563eb)'
                : '1px solid #eef2f7',
              background: llmActive === i
                ? 'rgba(37, 99, 235, 0.05)' : undefined,
            }}
          >
            <Tooltip title={llmActive === i ? '已激活（勾选可切换）' : '勾选激活（先测试连接）'}>
              <Radio
                checked={llmActive === i}
                disabled={llmTestingIdx !== null}
                onClick={() => activateModel(i)}
              />
            </Tooltip>
            <div style={{ flex: 1, minWidth: 0 }}>
              <Space>
                <Text strong style={{ fontSize: 13 }}>{m.name}</Text>
                <Text code style={{ fontSize: 12 }}>{m.model}</Text>
                {llmActive === i && (
                  <Tag color="blue" icon={<CheckOutlined />}>激活中</Tag>
                )}
                {llmTestingIdx === i && (
                  <Tag icon={<LoadingOutlined spin />} color="processing">测试中</Tag>
                )}
              </Space>
              <div style={{
                fontSize: 12, color: token.colorTextTertiary,
                overflow: 'hidden', textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}>
                {m.base_url}　Key: {m.api_key || '-'}
              </div>
            </div>
            <Space size="small">
              <Button size="small" icon={<EditOutlined />}
                onClick={() => openModelEdit(i)} />
              <Popconfirm title="确定删除该模型?" onConfirm={() => deleteModel(i)}>
                <Tooltip title={llmModels.length <= 1 ? '至少保留 1 个模型' : '删除'}>
                  <Button size="small" danger icon={<DeleteOutlined />}
                    disabled={llmModels.length <= 1} />
                </Tooltip>
              </Popconfirm>
            </Space>
          </div>
        ))
      )}
      <Button size="small" icon={<PlusOutlined />}
        onClick={() => openModelEdit(null)}>
        添加模型
      </Button>
    </>
  );
};

export default LlmPanel;
