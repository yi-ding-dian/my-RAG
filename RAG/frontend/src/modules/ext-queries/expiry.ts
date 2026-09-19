/**
 * 外部查询链接的到期状态与展示文案（总览 / 配置 / 记录页共用）
 *
 * 后端存的是绝对时间 "YYYY-MM-DD HH:MM:SS"（空 = 永久有效），前端统一在这里
 * 换算成"剩余 N 天 / 已过期"这类人类可读状态，避免各页各写一套判断。
 */

/** 到期状态：永久 / 充裕 / 即将到期 / 已过期 */
export type ExpiryState = 'permanent' | 'ok' | 'expiring' | 'expired';

/** "即将到期"阈值（天）——与后端 EXPIRING_SOON_DAYS 保持一致 */
export const EXPIRING_SOON_DAYS = 7;

/** 后端时间串 → Date（空/非法返回 null，按"永久"处理） */
export const parseExpires = (value?: string | null): Date | null => {
  if (!value) return null;
  const d = new Date(value.replace(' ', 'T'));
  return Number.isNaN(d.getTime()) ? null : d;
};

/** 剩余毫秒（负值 = 已过期）；永久/非法 → null */
export const remainingMs = (value?: string | null): number | null => {
  const d = parseExpires(value);
  return d ? d.getTime() - Date.now() : null;
};

/** 到期状态 */
export const expiryState = (
  value?: string | null,
  soonDays: number = EXPIRING_SOON_DAYS,
): ExpiryState => {
  const left = remainingMs(value);
  if (left === null) return 'permanent';
  if (left <= 0) return 'expired';
  return left <= soonDays * 24 * 3600 * 1000 ? 'expiring' : 'ok';
};

/** 状态 → 展示文案 */
export const expiryText = (value?: string | null): string => {
  const state = expiryState(value);
  if (state === 'permanent') return '永久有效';
  if (state === 'expired') return '已过期';
  const left = remainingMs(value) as number;
  const days = Math.ceil(left / (24 * 3600 * 1000));
  return days <= 1 ? '今天到期' : `剩余 ${days} 天`;
};

/** 状态 → AntD Tag 颜色 */
export const expiryColor = (value?: string | null): string => {
  const state = expiryState(value);
  if (state === 'expired') return 'red';
  if (state === 'expiring') return 'orange';
  if (state === 'permanent') return 'default';
  return 'green';
};

/** Date → 后端时间串（与后端 %Y-%m-%d %H:%M:%S 一致） */
export const toExpiresString = (d: Date): string => {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

/** 到期快捷选项：值为天数，0 = 永久 */
export const EXPIRY_PRESETS = [
  { label: '永久有效', days: 0 },
  { label: '7 天', days: 7 },
  { label: '30 天', days: 30 },
  { label: '90 天', days: 90 },
  { label: '1 年', days: 365 },
] as const;

/** 续期可选天数（与后端 1~3650 校验一致） */
export const RENEW_PRESETS = [7, 30, 90, 365] as const;

/** 按天数算到期时间串（0 → null = 永久） */
export const expiresFromDays = (days: number): string | null => {
  if (!days) return null;
  return toExpiresString(new Date(Date.now() + days * 24 * 3600 * 1000));
};
