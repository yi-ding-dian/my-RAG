import React from 'react';
import { Space } from 'antd';
import { ExclamationCircleFilled } from '@ant-design/icons';
import AppModal, { AppModalFooter } from '../../shared/components/common/AppModal';

/**
 * 「有未保存的改动」确认框（配置档案编辑弹窗关闭前弹）
 *
 * 用 AppModal 而非 antd 命令式 confirm，与项目其余弹窗保持同一套样式；
 * dimension=auto（项目里绝大多数弹窗的用法）：高度随内容走，不要用 fixed
 * 把两行字的确认框撑出大片空白。
 */
const CloseConfirmModal: React.FC<{
  open: boolean;
  /** 继续编辑：只关本框，保留编辑弹窗 */
  onKeepEditing: () => void;
  /** 放弃改动并关闭：连编辑弹窗一起关掉 */
  onDiscard: () => void;
}> = ({ open, onKeepEditing, onDiscard }) => (
  <AppModal
    dimension="auto"
    // minSize 必须显式给小：默认是 520×360（给大表单用的），拿来当这种小确认框的
    // 下限会**把宽度顶到 520**，而且拖拽也缩不下去
    minSize={{ w: 320, h: 140 }}
    defaultSize={{ w: 460, h: 200 }}
    title={
      <Space size={8}>
        <ExclamationCircleFilled style={{ color: '#faad14' }} />
        <span>有未保存的改动</span>
      </Space>
    }
    open={open}
    onCancel={onKeepEditing}
    footer={
      <AppModalFooter
        okText="放弃改动并关闭"
        cancelText="继续编辑"
        danger
        onOk={onDiscard}
        onCancel={onKeepEditing}
      />
    }
  >
    关闭后这些改动会丢失，确定关闭吗？
  </AppModal>
);

export default CloseConfirmModal;
