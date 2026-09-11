/**
 * API 层兼容入口（历史路径）。
 *
 * 业务已按领域拆分至 ./types ./auth ./kb ./chat ./settings ./other ./http，
 * 早期代码统一 `import { xxx } from '@/api/client'`，此处全量 re-export
 * 保持既有 import 路径零改动（新代码建议从 '@/api' 导入）。
 */
export * from './index';
export { default } from './http';
