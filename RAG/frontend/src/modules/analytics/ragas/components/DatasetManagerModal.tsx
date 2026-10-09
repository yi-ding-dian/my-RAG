/**
 * 评估集管理弹窗：已保存的题集列表（一键用它评估 / 删除）+ 两个入口。
 *
 * 从 RagasDetail 抽出来。它是评估集相关弹窗的 **hub**，三条跨弹窗连线：
 * - 「AI 生成题集」→ 开 GenerateQuestionsModal
 * - 「新建评估集」→ 开 NewDatasetModal
 * - 「用它评估」→ 关自己、开「发起评估」并带上这个评估集（含其绑定的知识库）
 * 三者的状态都由页面持有，本组件只负责"喊一声"（回调）。
 *
 * 列表数据 `datasets` 由页面传入而非组件自持：它还服务「发起评估」弹窗里的
 * 评估集下拉，两处必须同源。
 */
import {
  App as AntApp, Button, Empty, List, Popconfirm, Typography,
} from 'antd';
import {
  DeleteOutlined, PlayCircleOutlined, PlusOutlined, RobotOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';

import AppModal from '../../../../shared/components/common/AppModal';
import { deleteRagasDataset, RagasDataset } from '../../../../shared/api/client';

const { Text } = Typography;

export interface DatasetManagerModalProps {
  open: boolean;
  /** 评估集列表（页面持有——它也服务「发起评估」弹窗的下拉） */
  datasets: RagasDataset[];
  onClose: () => void;
  /** 删除成功后回调：页面清掉"当前选中"（若正是它）并从列表移除 */
  onDeleted: (id: string) => void;
  /** 「用它评估」：页面设好评估集与知识库后打开「发起评估」弹窗 */
  onUse: (ds: RagasDataset) => void;
  /** 打开 AI 生成题集弹窗 */
  onOpenGenerate: () => void;
  /** 打开新建评估集弹窗 */
  onOpenNewDataset: () => void;
}

export const DatasetManagerModal = ({
  open, datasets, onClose, onDeleted, onUse, onOpenGenerate, onOpenNewDataset,
}: DatasetManagerModalProps) => {
  const { message } = AntApp.useApp();

  // 删除评估集（已跑过的任务记录不受影响，仍可在任务列表回看）
  const handleDelete = async (d: RagasDataset) => {
    try {
      await deleteRagasDataset(d.id);
      message.success('评估集已删除');
      onDeleted(d.id);
    } catch {
      message.error('删除评估集失败');
    }
  };

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 720, h: 460 }}
      rememberKey="ana-dataset-mgr"
      title="评估集管理"
      open={open}
      onCancel={onClose}
      footer={[
        <Button
          key="ai"
          icon={<RobotOutlined />}
          onClick={onOpenGenerate}
        >
          AI 生成题集
        </Button>,
        <Button
          key="new"
          type="primary"
          icon={<PlusOutlined />}
          onClick={onOpenNewDataset}
        >
          新建评估集
        </Button>,
        <Button key="close" onClick={onClose}>关闭</Button>,
      ]}
      width={720}
    >
      <Text type="secondary" style={{ fontSize: 12 }}>
        评估集是一份固定的题集（问题 + 正确答案）。反复用同一份题集重跑，分数才有可比性
        ——改了切块 / 检索 / 提示词之后跑一次，就知道是变好还是变差。
      </Text>
      <List
        rowKey="id"
        style={{ marginTop: 12 }}
        dataSource={datasets}
        locale={{ emptyText: <Empty description="暂无评估集（在发起评估弹窗里可把测试集另存为评估集）"
                                         image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
        renderItem={d => (
          <List.Item
            actions={[
              <Button
                key="run"
                type="link"
                size="small"
                icon={<PlayCircleOutlined />}
                onClick={() => onUse(d)}
              >
                用它评估
              </Button>,
              <Popconfirm key="del" title="删除该评估集？" onConfirm={() => handleDelete(d)}>
                <Button type="link" size="small" danger icon={<DeleteOutlined />}>删除</Button>
              </Popconfirm>,
            ]}
          >
            <List.Item.Meta
              title={d.name}
              description={`${d.kb_name} · ${d.samples?.length ?? 0} 条样本 · 更新于 ${dayjs(d.updated_at).format('YYYY-MM-DD HH:mm')}`}
            />
          </List.Item>
        )}
      />
    </AppModal>
  );
};
