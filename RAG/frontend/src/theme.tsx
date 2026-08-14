/**
 * 全局主题模块（10 主题预设版）：
 * - THEME_PRESETS：10 种预设（5 浅 5 深，各带主色与配套的深/提亮档颜色）
 * - ThemeProvider + useTheme：暴露 { preset, isDark, setPreset }，业务组件零感知切换
 * - 偏好持久化：localStorage.myrag.theme = preset key 字符串；
 *   旧版存储值 'light'/'dark' 自动映射到 classic-blue / midnight-blue（兼容升级）
 * - 同步 documentElement[data-theme]（index.css 的 [data-theme='dark'] 规则依赖，两个深色预设都生效）
 *   以及 CSS 变量（--brand-primary 等 6 个，供 index.css 自绘区域/组件 inline 取色跟随主题）
 */
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { theme as antdTheme, type ThemeConfig } from 'antd';

const THEME_KEY = 'myrag.theme';

export type PresetKey =
  | 'classic-blue'
  | 'fresh-green'
  | 'elegant-purple'
  | 'sunny-orange'
  | 'lake-cyan'
  | 'midnight-blue'
  | 'night-purple'
  | 'forest-green'
  | 'dusk-red'
  | 'amber-gold';

export interface ThemePreset {
  key: PresetKey;
  /** 中文展示名（侧栏选择器 Tooltip / Profile 单选标签） */
  label: string;
  mode: 'light' | 'dark';
  /** 主色（AntD token colorPrimary + CSS 变量 --brand-primary） */
  colorPrimary: string;
  /** 主色深一档（浅色下 hover/激活文字、渐变终点） */
  colorPrimaryDeep: string;
  /** 主色提亮档（暗色下文字/描边提亮，保证深底可读） */
  colorPrimarySoft: string;
  /** 主色再提亮档（暗色下 hover 再亮一档） */
  colorPrimarySofter: string;
}

/** 10 种主题预设：浅色 5（经典蓝/清新绿/典雅紫/暖阳橙/湖光青）+ 深色 5（午夜蓝/暗夜紫/森林绿/暮色红/琥珀金），选择器按 5+5 两行展示 */
export const THEME_PRESETS: ThemePreset[] = [
  {
    key: 'classic-blue',
    label: '浅色 · 经典蓝',
    mode: 'light',
    colorPrimary: '#2563eb',
    colorPrimaryDeep: '#1d4ed8',
    colorPrimarySoft: '#93c5fd',
    colorPrimarySofter: '#bfdbfe',
  },
  {
    key: 'fresh-green',
    label: '浅色 · 清新绿',
    mode: 'light',
    colorPrimary: '#16a34a',
    colorPrimaryDeep: '#15803d',
    colorPrimarySoft: '#86efac',
    colorPrimarySofter: '#bbf7d0',
  },
  {
    key: 'elegant-purple',
    label: '浅色 · 典雅紫',
    mode: 'light',
    colorPrimary: '#7c3aed',
    colorPrimaryDeep: '#6d28d9',
    colorPrimarySoft: '#c4b5fd',
    colorPrimarySofter: '#ddd6fe',
  },
  {
    key: 'sunny-orange',
    label: '浅色 · 暖阳橙',
    mode: 'light',
    colorPrimary: '#ea580c',
    colorPrimaryDeep: '#c2410c',
    colorPrimarySoft: '#fdba74',
    colorPrimarySofter: '#fed7aa',
  },
  {
    key: 'lake-cyan',
    label: '浅色 · 湖光青',
    mode: 'light',
    colorPrimary: '#0891b2',
    colorPrimaryDeep: '#0e7490',
    colorPrimarySoft: '#67e8f9',
    colorPrimarySofter: '#a5f3fc',
  },
  {
    key: 'midnight-blue',
    label: '深色 · 午夜蓝',
    mode: 'dark',
    colorPrimary: '#3b82f6',
    colorPrimaryDeep: '#1d4ed8',
    colorPrimarySoft: '#93c5fd',
    colorPrimarySofter: '#bfdbfe',
  },
  {
    key: 'night-purple',
    label: '深色 · 暗夜紫',
    mode: 'dark',
    colorPrimary: '#8b5cf6',
    colorPrimaryDeep: '#6d28d9',
    colorPrimarySoft: '#c4b5fd',
    colorPrimarySofter: '#ddd6fe',
  },
  {
    key: 'forest-green',
    label: '深色 · 森林绿',
    mode: 'dark',
    colorPrimary: '#10b981',
    colorPrimaryDeep: '#059669',
    colorPrimarySoft: '#6ee7b7',
    colorPrimarySofter: '#a7f3d0',
  },
  {
    key: 'dusk-red',
    label: '深色 · 暮色红',
    mode: 'dark',
    colorPrimary: '#ef4444',
    colorPrimaryDeep: '#dc2626',
    colorPrimarySoft: '#fca5a5',
    colorPrimarySofter: '#fecaca',
  },
  {
    key: 'amber-gold',
    label: '深色 · 琥珀金',
    mode: 'dark',
    colorPrimary: '#f59e0b',
    colorPrimaryDeep: '#d97706',
    colorPrimarySoft: '#fcd34d',
    colorPrimarySofter: '#fde68a',
  },
];

const PRESET_MAP: Record<string, ThemePreset> = Object.fromEntries(
  THEME_PRESETS.map(p => [p.key, p]),
);

/**
 * 初始预设：localStorage 优先。
 * 旧版 myrag.theme 存的是 'light'/'dark' 字符串 → 映射 经典蓝/午夜蓝；
 * 新版存 preset key；缺失或异常回退默认（经典蓝）。
 * 注：不再提供"跟随系统"——10 种预设已显式覆盖浅/深外观，用户直接选具体配色；
 * 若叠加系统模式会造成语义冲突（如选"浅色·清新绿"后系统切暗，预设与模式打架），故移除。
 */
const resolveInitialPreset = (): ThemePreset => {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light') return PRESET_MAP['classic-blue'];
    if (saved === 'dark') return PRESET_MAP['midnight-blue'];
    if (saved && PRESET_MAP[saved]) return PRESET_MAP[saved];
  } catch {
    // localStorage 不可用（隐私模式等）时回退默认
  }
  return PRESET_MAP['classic-blue'];
};

interface ThemeContextValue {
  /** 当前预设完整对象 */
  preset: ThemePreset;
  /** 当前是否暗色（旧调用方兼容；由 preset.mode 推导） */
  isDark: boolean;
  /** 切换预设（theme.tsx 导出 THEME_PRESETS 供 UI 遍历） */
  setPreset: (key: PresetKey) => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  preset: PRESET_MAP['classic-blue'],
  isDark: false,
  setPreset: () => {},
});

/** hex('#2563eb') → '37, 99, 235'（CSS 变量存 RGB 分量，供 rgba(var(--brand-primary-rgb), x) 使用） */
const hexToRgb = (hex: string): string => {
  const n = parseInt(hex.slice(1), 16);
  return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
};

/** hex + alpha → rgba() 字符串（AntD token 用） */
const hexToRgba = (hex: string, alpha: number): string => `rgba(${hexToRgb(hex)}, ${alpha})`;

export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [preset, setPresetState] = useState<ThemePreset>(resolveInitialPreset);

  // 偏好持久化 + 同步 data-theme 与 CSS 变量（index.css 自绘区域与 inline 取色依赖）
  useEffect(() => {
    try {
      localStorage.setItem(THEME_KEY, preset.key);
    } catch {
      // 忽略写入失败
    }
    const root = document.documentElement;
    root.setAttribute('data-theme', preset.mode === 'dark' ? 'dark' : 'light');
    root.style.setProperty('--brand-primary', preset.colorPrimary);
    root.style.setProperty('--brand-primary-rgb', hexToRgb(preset.colorPrimary));
    root.style.setProperty('--brand-primary-deep', preset.colorPrimaryDeep);
    root.style.setProperty('--brand-primary-soft', preset.colorPrimarySoft);
    root.style.setProperty('--brand-primary-soft-rgb', hexToRgb(preset.colorPrimarySoft));
    root.style.setProperty('--brand-primary-softer', preset.colorPrimarySofter);
  }, [preset]);

  const setPreset = useCallback((key: PresetKey) => {
    const next = PRESET_MAP[key];
    if (next) setPresetState(next);
  }, []);

  const value = useMemo(
    () => ({ preset, isDark: preset.mode === 'dark', setPreset }),
    [preset, setPreset],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
};

/** 读取主题上下文（须在 ThemeProvider 内使用） */
export const useTheme = (): ThemeContextValue => useContext(ThemeContext);

/** 明暗共用 token：圆角 8、字号 14、系统字体栈；主色类由预设注入 */
const baseToken = {
  colorSuccess: '#16a34a',
  colorWarning: '#f59e0b',
  colorError: '#ef4444',
  borderRadius: 8,
  fontSize: 14,
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Helvetica Neue', Helvetica, Arial, sans-serif",
  boxShadowTertiary: '0 1px 2px rgba(16, 24, 40, 0.04)',
};

/** 浅色组件微调：主色相关项（Menu 选中/悬停）跟随预设动态生成，其余固定 */
const lightComponents = (preset: ThemePreset): ThemeConfig['components'] => ({
  Card: {
    boxShadowTertiary: '0 1px 3px rgba(16, 24, 40, 0.06)',
    headerFontSize: 15,
  },
  Layout: {
    siderBg: '#ffffff',
    headerBg: '#ffffff',
  },
  Menu: {
    itemBorderRadius: 8,
    itemSelectedBg: hexToRgba(preset.colorPrimary, 0.1),
    itemSelectedColor: preset.colorPrimaryDeep,
    itemHoverBg: hexToRgba(preset.colorPrimary, 0.06),
  },
  Table: {
    headerBg: '#f8fafc',
    rowHoverBg: '#f8fafc',
    headerColor: 'rgba(15, 23, 42, 0.75)',
  },
  Tag: { borderRadiusSM: 6 },
});

/** 深色组件微调：darkAlgorithm 自动适配全部 AntD 组件，仅布局/菜单/表格做深色微调 */
const darkComponents = (preset: ThemePreset): ThemeConfig['components'] => ({
  Card: {
    boxShadowTertiary: '0 1px 3px rgba(0, 0, 0, 0.35)',
    headerFontSize: 15,
  },
  Layout: {
    siderBg: '#141821',
    headerBg: '#141821',
  },
  Menu: {
    itemBorderRadius: 8,
    itemSelectedBg: hexToRgba(preset.colorPrimary, 0.22),
    itemSelectedColor: preset.colorPrimarySoft,
    itemHoverBg: hexToRgba(preset.colorPrimary, 0.12),
  },
  Table: {
    headerBg: '#1c2129',
    rowHoverBg: '#1c2129',
  },
  Tag: { borderRadiusSM: 6 },
});

/** 按预设构建 ConfigProvider 主题：algorithm 由 mode 决定，主色 token 注入 */
export const buildTheme = (preset: ThemePreset): ThemeConfig => {
  const dark = preset.mode === 'dark';
  return {
    algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      ...baseToken,
      colorPrimary: preset.colorPrimary,
      colorInfo: preset.colorPrimary,
      colorLink: preset.colorPrimary,
      colorBgLayout: dark ? '#0f1116' : '#f5f7fa',
    },
    components: dark ? darkComponents(preset) : lightComponents(preset),
  };
};
