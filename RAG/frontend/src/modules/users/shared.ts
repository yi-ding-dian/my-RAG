/**
 * 用户管理模块的常量与纯函数（Users 容器与两个 Tab 共用）。
 */
import type { Department, User } from '../../shared/api/client';

/** 密码强度校验规则（与后端 validate_password 一致：至少 8 位且同时包含字母和数字） */
export const passwordStrengthRules = (prefix = '密码') => [
  { min: 8, message: `${prefix}至少 8 位，且需同时包含字母和数字` },
  { pattern: /[A-Za-z]/, message: `${prefix}至少 8 位，且需同时包含字母和数字` },
  { pattern: /\d/, message: `${prefix}至少 8 位，且需同时包含字母和数字` },
];

export const roleMeta: Record<User['role'], { color: string; text: string }> = {
  super_admin: { color: 'red', text: '超级管理员' },
  dept_admin: { color: 'blue', text: '部门管理员' },
  user: { color: 'default', text: '普通用户' },
};

/** 部门选择项：空值表示未分配 */
export const deptOptions = (departments: Department[]) => [
  { value: '', label: '未分配' },
  ...departments.map(d => ({ value: d.id, label: d.name })),
];
