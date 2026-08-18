/**
 * 用户画像/偏好记忆 API（/api/users/{user_id}/memory）。
 * 由 client.ts 全量 re-export，业务代码统一从 '@/api/client' 或 '@/api' 导入。
 *
 * 权限：GET 本人/超管/dept_admin 本部门；PUT/DELETE 仅本人（管理员只读）。
 */
import api from './http';
import type { UserMemory, UserMemoryUpdate } from './types';

/** 查看用户画像（本人 / 超管全量 / dept_admin 本部门；越权 404 伪装） */
export const getUserMemory = (userId: string) =>
  api.get<UserMemory>(`/users/${userId}/memory`);

/** 更新用户画像（仅本人）：enabled 开关 / items 条目全量替换 */
export const updateUserMemory = (userId: string, data: UserMemoryUpdate) =>
  api.put<UserMemory>(`/users/${userId}/memory`, data);

/** 删除画像条目（itemId 缺省=清空全部；仅本人） */
export const deleteUserMemory = (userId: string, itemId?: string) =>
  api.delete<{ message: string }>(`/users/${userId}/memory`, {
    params: itemId ? { item_id: itemId } : undefined,
  });
