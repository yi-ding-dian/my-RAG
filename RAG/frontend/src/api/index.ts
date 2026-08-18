/**
 * API 层统一出口：类型 + 各领域 API 方法 + axios 实例。
 * 业务代码可从 '@/api/client'（历史路径，兼容 re-export）或 '@/api' 导入。
 */
export * from './types';
export * from './auth';
export * from './kb';
export * from './chat';
export * from './settings';
export * from './other';
export * from './http';
export { default } from './http';
