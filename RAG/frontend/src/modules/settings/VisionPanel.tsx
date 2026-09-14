import React from 'react';
import { Alert, Button, Empty, Popconfirm, Radio, Space, Tag, Tooltip, Typography, theme } from 'antd';
import {
  CheckOutlined,
  DeleteOutlined,
  EditOutlined,
  LoadingOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import type { VisionModelItem } from '../../shared/api/types';

const { Text } = Typography;

/** 面板 props：模型列表状态与操作回调（状态由容器页持有，与表单同源） */
interface Props {
  visionModels: VisionModelItem[];
  visionActive: number;
  visionTestingIdx: number | null;
  openModelEdit: (idx: number | null) => void;
  deleteModel: (idx: number) => void;
  activateModel: (idx: number) => void;
}

/**
 * 图片解析模型面板（多模态模型，用于解析时生成图片摘要）。
 *
 * 与 LLM 面板的差异：条目只有 5 个字段（无 temperature/max_tokens——摘要用
 * 固定生成参数），且这里的"默认"是**部门没选模型时的兜底**：部门管理员在
 * 「部门配置」里选具体用哪个，超管在这里定推荐项。
 */
const VisionPanel: React.FC<Props> = ({ visionModels, visionActive, visionTestingIdx, openModelEdit, deleteModel, activateModel }) => {
  const { token } = theme.useToken();
  return (
    <>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 8 }}
        message="用于「图片摘要」：解析时把证照/扫描件里的文字读出来写进正文，让它们能被检索。可添加多个模型，第一个为默认；部门管理员在「部门配置 → 图片摘要」里挑用哪个。设为默认时自动测试连接（GET {base_url}/models）。"
      />
      {visionModels.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          style={{ margin: '8px 0' }}
          description="未添加模型：解析时勾选「图片摘要」会被拦下并提示先配置"
        />
      ) : (
        visionModels.map((m, i) => (
          <div
            key={i}
            style={{
              display: 'flex', alignItems: 'center', gap: 12,
              padding: '8px 12px', marginBottom: 8,
              borderRadius: 6,
              border: visionActive === i
                ? '1px solid var(--brand-primary, #2563eb)'
                : '1px solid #eef2f7',
              background: visionActive === i
                ? 'rgba(37, 99, 235, 0.05)' : undefined,
            }}
          >
            <Tooltip title={visionActive === i ? '当前默认（勾选可切换）' : '设为默认（先测试连接）'}>
              <Radio
                checked={visionActive === i}
                disabled={visionTestingIdx !== null}
                onClick={() => activateModel(i)}
              />
            </Tooltip>
            <div style={{ flex: 1, minWidth: 0 }}>
              <Space>
                <Text strong style={{ fontSize: 13 }}>{m.name}</Text>
                <Text code style={{ fontSize: 12 }}>{m.model}</Text>
                {visionActive === i && (
                  <Tag color="blue" icon={<CheckOutlined />}>默认</Tag>
                )}
                {visionTestingIdx === i && (
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
                <Tooltip title={visionModels.length <= 1 ? '至少保留 1 个模型' : '删除'}>
                  <Button size="small" danger icon={<DeleteOutlined />}
                    disabled={visionModels.length <= 1} />
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

export default VisionPanel;
