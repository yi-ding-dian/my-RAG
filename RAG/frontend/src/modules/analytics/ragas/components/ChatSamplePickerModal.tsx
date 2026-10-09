/**
 * 从聊天历史导入测试集样本：勾选预览样本 → 追加到当前测试集。
 *
 * 从 RagasDetail 抽出来。**注意分工**（这是它和别的弹窗不同的地方）：
 * - 候选数据的**拉取**留在页面（`handleImportFromChat`）——它的触发按钮在
 *   「发起评估」弹窗里，且依赖那里的知识库选择，不属于本弹窗
 * - **去重与写回测试集表单**也留在页面——那要读 `evalForm` 里的现有样本
 * - 本组件只管「展示候选 + 勾选 + 把选中的问题文本交出去」
 *
 * 打开时默认全选（与原 `handleImportFromChat` 里的行为一致）。
 */
import { useEffect, useState } from 'react';
import {
  Alert, Button, Checkbox, List, Space, Tooltip, Typography,
} from 'antd';

import AppModal from '../../../../shared/components/common/AppModal';
import { RagasSampleInput } from '../../../../shared/api/client';

const { Text } = Typography;

export interface ChatSamplePickerModalProps {
  open: boolean;
  /** 候选样本（页面调 preview 接口拉好后传入） */
  list: RagasSampleInput[];
  onClose: () => void;
  /** 确定：把勾选的问题文本交给页面（去重与写回表单由页面负责） */
  onPick: (selectedQuestions: string[]) => void;
}

export const ChatSamplePickerModal = ({
  open, list, onClose, onPick,
}: ChatSamplePickerModalProps) => {
  const [selected, setSelected] = useState<string[]>([]);

  // 打开即全选（原逻辑在 handleImportFromChat 里设置，随 setChatPickOpen(true) 一起）
  useEffect(() => {
    if (open) setSelected(list.map(s => s.question));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, list]);

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 680, h: 480 }}
      rememberKey="ana-3"
      title={`从聊天历史导入（预览 ${list.length} 条）`}
      open={open}
      onCancel={onClose}
      onOk={() => onPick(selected)}
      okText={`追加勾选的 ${selected.length} 条`}
      cancelText="取消"
      width={680}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        <Alert
          type="info"
          showIcon
          message="将追加到当前测试集"
          description="勾选想要的问题后点确定：追加到测试集表单末尾（相同问题不重复添加）；点取消则不改动当前测试集。"
        />
        <Space>
          <Button size="small" onClick={() => setSelected(list.map(s => s.question))}>
            全选
          </Button>
          <Button size="small" onClick={() => setSelected([])}>
            全不选
          </Button>
          <Text type="secondary" style={{ fontSize: 12 }}>
            已选 {selected.length} / {list.length} 条
          </Text>
        </Space>
        <div style={{ maxHeight: 320, overflowY: 'auto', border: '1px solid #f0f0f0', borderRadius: 6, padding: '4px 8px' }}>
          <Checkbox.Group
            style={{ width: '100%' }}
            value={selected}
            onChange={(v) => setSelected(v as string[])}
          >
            <List
              size="small"
              dataSource={list}
              renderItem={(s, i) => (
                <List.Item style={{ padding: '6px 4px' }}>
                  <Checkbox value={s.question} style={{ width: '100%' }}>
                    <Tooltip title={s.question}>
                      <span
                        style={{
                          display: 'inline-block', maxWidth: 470,
                          overflow: 'hidden', textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap', verticalAlign: 'bottom',
                        }}
                      >
                        {i + 1}. {s.question}
                      </span>
                    </Tooltip>
                    <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                      会话问答
                    </Text>
                  </Checkbox>
                </List.Item>
              )}
            />
          </Checkbox.Group>
        </div>
      </Space>
    </AppModal>
  );
};
