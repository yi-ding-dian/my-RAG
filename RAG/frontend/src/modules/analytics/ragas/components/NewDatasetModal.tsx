/**
 * 新建评估集弹窗：不经过"发起评估"，先把题集攒起来（之后可随时"用它评估"）。
 *
 * 从 RagasDetail 抽出来——5 个状态 + 1 个提交函数 + 弹窗 JSX，只服务这一件事。
 *
 * 进出接口：
 * - `kbs`：知识库候选（页面已加载）
 * - `onCreated`：创建成功后回调（页面把新评估集插到列表最前）
 *
 * 注意 `onCancel` 不重置表单：与原行为一致——只有创建成功才清空，
 * 中途关掉再打开还能接着编辑（用户可能只是去查个知识库名）。
 */
import { useState } from 'react';
import {
  App as AntApp, Empty, Input, Select, Space, Typography,
} from 'antd';

import AppModal from '../../../../shared/components/common/AppModal';
import { createRagasDataset, KnowledgeBase, RagasDataset } from '../../../../shared/api/client';
import { parseDatasetText } from '../ragas-shared';

const { Text } = Typography;

export interface NewDatasetModalProps {
  open: boolean;
  /** 知识库候选（页面已加载） */
  kbs: KnowledgeBase[];
  onClose: () => void;
  /** 创建成功后回调（页面把新评估集插到列表最前） */
  onCreated: (ds: RagasDataset) => void;
}

export const NewDatasetModal = ({
  open, kbs, onClose, onCreated,
}: NewDatasetModalProps) => {
  const { message } = AntApp.useApp();
  const [newDsName, setNewDsName] = useState('');
  const [newDsKbIds, setNewDsKbIds] = useState<string[]>([]);
  const [newDsText, setNewDsText] = useState('');
  const [newDsSubmitting, setNewDsSubmitting] = useState(false);

  // 直接新建评估集（不走发起评估）：题集可以先把问题攒起来，之后再跑
  const handleCreateDataset = async () => {
    const name = newDsName.trim();
    if (!name) {
      message.error('请填写评估集名称');
      return;
    }
    if (newDsKbIds.length === 0) {
      message.error('请选择知识库');
      return;
    }
    const rows = parseDatasetText(newDsText);
    if (rows.length === 0) {
      message.error('请至少填一条样本，每行格式：问题 | 参考答案（答案可留空）');
      return;
    }
    setNewDsSubmitting(true);
    try {
      const res = await createRagasDataset({
        kb_id: newDsKbIds[0], name, samples: rows, source: 'manual',
      });
      onCreated(res.data);
      onClose();
      setNewDsName('');
      setNewDsText('');
      setNewDsKbIds([]);
      message.success(`评估集「${name}」已创建（${rows.length} 条样本）`);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '新建评估集失败');
    } finally {
      setNewDsSubmitting(false);
    }
  };

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 560, h: 540 }}
      rememberKey="ana-dataset-new"
      title="新建评估集"
      open={open}
      onCancel={onClose}
      onOk={handleCreateDataset}
      okText="创建"
      cancelText="取消"
      confirmLoading={newDsSubmitting}
      width={560}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        <div>
          <Text strong>知识库</Text>
          <Select
            style={{ width: '100%', marginTop: 4 }}
            placeholder="选择题集归属的知识库"
            value={newDsKbIds[0]}
            onChange={(v: string) => setNewDsKbIds([v])}
            options={kbs.map(k => ({ value: k.id, label: k.name }))}
            notFoundContent={<Empty description="暂无可用知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          />
        </div>
        <div>
          <Text strong>名称</Text>
          <Input
            style={{ marginTop: 4 }}
            placeholder="评估集名称（1~50 字）"
            maxLength={50}
            value={newDsName}
            onChange={e => setNewDsName(e.target.value)}
          />
        </div>
        <div>
          <Space style={{ width: '100%', justifyContent: 'space-between' }}>
            <Text strong>样本</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>每行一条：问题 | 参考答案</Text>
          </Space>
          <Input.TextArea
            style={{ marginTop: 4 }}
            rows={9}
            placeholder={'这份文档主要讲什么？ | 讲的是……\n第二个问题？ |'}
            value={newDsText}
            onChange={e => setNewDsText(e.target.value)}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            共 {parseDatasetText(newDsText).length} 条。答案可以留空——配合「系统实时生成」
            跑评估时不需要标准答案（那种模式测的是端到端质量）
          </Text>
        </div>
      </Space>
    </AppModal>
  );
};
