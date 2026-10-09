/**
 * 另存为评估集：把当前填写的测试集沉淀成可复用题集（之后可反复重跑对比分数）。
 *
 * 从 RagasDetail 抽出来。它是唯一"读别人状态"的弹窗，所以接口显式化：
 * - 归属知识库来自「发起评估」弹窗的 `evalKbIds`（多库取第一个）——以 `kbIds` 传入
 * - 测试集样本来自主表单 `evalForm`——以 `getSamples` 回调传入；
 *   校验失败时 antd 会自己高亮表单字段，所以这里不吞异常、直接返回
 */
import { useState } from 'react';
import { App as AntApp, Input, Typography } from 'antd';

import AppModal from '../../../../shared/components/common/AppModal';
import { createRagasDataset, RagasDataset } from '../../../../shared/api/client';

const { Text } = Typography;

export interface SaveAsDatasetModalProps {
  open: boolean;
  /** 归属知识库（多库时取第一个）；空数组 = 还没选库 */
  kbIds: string[];
  /** 取当前测试集：页面包装 `evalForm.validateFields`（校验失败直接抛出，antd 已高亮）。
   *  返回的每行问题与答案都已 trim、保证非空——故类型比 EvalSampleRow 更严，
   *  可直接喂给 createRagasDataset */
  getSamples: () => Promise<{ question: string; ground_truth: string }[]>;
  onClose: () => void;
  /** 保存成功：页面把新评估集插到列表最前 */
  onCreated: (ds: RagasDataset) => void;
}

export const SaveAsDatasetModal = ({
  open, kbIds, getSamples, onClose, onCreated,
}: SaveAsDatasetModalProps) => {
  const { message } = AntApp.useApp();
  const [name, setName] = useState('');

  const handleSave = async () => {
    const n = name.trim();
    if (!n) {
      message.error('请填写评估集名称');
      return;
    }
    if (kbIds.length === 0) {
      message.error('请先选择知识库');
      return;
    }
    let rows: { question: string; ground_truth: string }[];
    try {
      rows = await getSamples();
    } catch {
      return; // 表单校验失败：antd 已高亮对应字段，不必再提示
    }
    if (rows.length === 0) {
      message.error('请先填写至少一条有效测试样本（问题 + 正确答案）');
      return;
    }
    try {
      const res = await createRagasDataset({
        // 评估集绑定单个知识库：多库时取第一个作为归属库
        kb_id: kbIds[0], name: n, samples: rows, source: 'manual',
      });
      onCreated(res.data);
      onClose();
      setName('');
      message.success(`评估集「${n}」已保存（${rows.length} 条样本），以后可一键重跑对比`);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '保存评估集失败');
    }
  };

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 440, h: 280 }}
      rememberKey="ana-dataset-save"
      title="另存为评估集"
      open={open}
      onCancel={onClose}
      onOk={handleSave}
      okText="保存"
      cancelText="取消"
      width={440}
    >
      <Text type="secondary" style={{ fontSize: 12 }}>
        保存后可在「评估集」下拉里选中它重跑；同一份题集的历次分数可以直接对比。
      </Text>
      <Input
        style={{ marginTop: 12 }}
        placeholder="评估集名称（1~50 字）"
        maxLength={50}
        value={name}
        onChange={e => setName(e.target.value)}
        onPressEnter={handleSave}
        autoFocus
      />
    </AppModal>
  );
};
