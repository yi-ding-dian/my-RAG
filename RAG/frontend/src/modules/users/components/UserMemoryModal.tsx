/**
 * 用户画像只读查看弹窗（管理员视角：本人可编辑，管理员只读）。
 *
 * 从 Users.tsx 抽出来。数据加载从页面的 `openMemoryView` 挪进本组件
 * （open 且 user 变化时拉），页面只负责"看谁的画像"。
 */
import { useEffect, useState } from 'react';
import {
  App as AntApp, Button, Empty, List, Space, Tag, Typography,
} from 'antd';
import dayjs from 'dayjs';

import AppModal from '../../../shared/components/common/AppModal';
import {
  asApiError, getUserMemory, User, UserMemory, UserMemoryItem,
} from '../../../shared/api/client';

const { Text } = Typography;

export interface UserMemoryModalProps {
  open: boolean;
  /** 要查看的用户（null = 尚未选择） */
  user: User | null;
  onClose: () => void;
}

export const UserMemoryModal = ({ open, user, onClose }: UserMemoryModalProps) => {
  const { message } = AntApp.useApp();
  const [memoryView, setMemoryView] = useState<UserMemory | null>(null);
  const [loading, setLoading] = useState(false);

  // 打开（或换用户）时拉画像；失败则关闭弹窗（与原 openMemoryView 行为一致）
  useEffect(() => {
    if (!open || !user) return;
    let cancelled = false;
    setMemoryView(null);
    setLoading(true);
    getUserMemory(user.id)
      .then(res => { if (!cancelled) setMemoryView(res.data); })
      .catch((e: unknown) => {
        if (cancelled) return;
        message.error(asApiError(e).response?.data?.detail || '加载用户画像失败');
        onClose();
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, user]);

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 560, h: 440 }}
      rememberKey="user-3"
      title={`用户画像 - ${user?.username ?? ''}`}
      open={open}
      onCancel={onClose}
      footer={
        <Button onClick={onClose}>关闭</Button>
      }
      width={560}
    >
      {loading ? (
        <div style={{ padding: 24, textAlign: 'center' }}>
          <Text type="secondary">加载中…</Text>
        </div>
      ) : memoryView ? (
        <div>
          <Space style={{ marginBottom: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              个性化记忆：
            </Text>
            <Tag color={memoryView.memory_enabled ? 'success' : 'default'}>
              {memoryView.memory_enabled ? '已开启' : '已关闭'}
            </Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {memoryView.updated_at
                ? `最近更新 ${dayjs(memoryView.updated_at).format('YYYY-MM-DD HH:mm:ss')}`
                : '暂无更新记录'}
            </Text>
          </Space>
          {memoryView.items.length > 0 ? (
            <List
              size="small"
              dataSource={memoryView.items}
              rowKey="id"
              renderItem={(item: UserMemoryItem) => (
                <List.Item>
                  <Space wrap>
                    <Tag color={item.type === 'profile' ? 'blue' : 'purple'}>
                      {item.type === 'profile' ? '画像' : '偏好'}
                    </Tag>
                    <Text>{item.content}</Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      把握度 {Math.round(item.confidence * 100)}%
                    </Text>
                  </Space>
                </List.Item>
              )}
            />
          ) : (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="暂无画像条目"
            />
          )}
          <Text type="secondary" style={{ fontSize: 12 }}>
            管理员仅可查看（只读）；编辑/删除需用户本人登录个人设置页操作
          </Text>
        </div>
      ) : null}
    </AppModal>
  );
};
