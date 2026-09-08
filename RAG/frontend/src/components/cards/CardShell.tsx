import React from 'react';
import { Card, Typography } from 'antd';

/**
 * 通用卡片底座（卡片库第 1 层）：视觉骨架复用，业务内容 props 注入。
 *
 * 用法（与知识库卡片 KbCard 同构，后续可迁移复用）：
 *   <CardShell
 *     iconText="文"
 *     title="花花的旅游计划.xlsx"
 *     actions={<Button .../>}          // hover 时显示的右上操作区
 *     tags={<Tag>已入库</Tag>}          // 标题下标签行
 *     desc={<.../>}                     // 中部描述区（可省）
 *     meta={<FileTextOutlined /> 17 切块}  // footer 统计行
 *     footerExtra={<Button type="link">查看详情</Button>}    // footer 常显入口
 *     time="2026-08-23"                 // footer 日期（dayjs 格式化后传入）
 *     onClick={...}
 *   />
 *
 * 样式（.gen-card-*）与 kb-card 同构，见 index.css：radius-lg、hover
 * 上浮 + 主色边框 + 加深阴影；图标按名称 hash 取 8 组品牌渐变。
 */

/** 名称 hash → 渐变配色（与知识库卡片同 8 组品牌系） */
export const CARD_GRADIENTS = [
  'linear-gradient(135deg, var(--brand-primary, #2563eb) 0%, var(--brand-primary-deep, #1d4ed8) 100%)',
  'linear-gradient(135deg, #0ea5e9 0%, #0284c7 100%)',
  'linear-gradient(135deg, #10b981 0%, #047857 100%)',
  'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)',
  'linear-gradient(135deg, #ef4444 0%, #b91c1c 100%)',
  'linear-gradient(135deg, #8b5cf6 0%, #6d28d9 100%)',
  'linear-gradient(135deg, #ec4899 0%, #be185d 100%)',
  'linear-gradient(135deg, #14b8a6 0%, #0f766e 100%)',
];

/** 字符串 hash（名称 → 稳定取色） */
export const hashString = (s: string): number => {
  let h = 0;
  for (let i = 0; i < s.length; i += 1) {
    h = (h * 31 + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
};

export interface CardShellProps {
  /** 图标文字（取名称首字即可；内部按 hash 取渐变） */
  iconText: string;
  /** 卡片标题（1 行省略） */
  title: string;
  /** hover 式操作区（右上角，触屏常显）；点击请自行 stopPropagation */
  actions?: React.ReactNode;
  /** 标题下标签行（Tag 序列），缺省不渲染 */
  tags?: React.ReactNode;
  /** 中部描述区（2 行 clamp），缺省不渲染 */
  desc?: React.ReactNode;
  /** footer 统计行（flex 左排列），缺省不渲染 */
  meta?: React.ReactNode;
  /** footer 常显入口（如"查看详情"链接按钮），缺省不渲染 */
  footerExtra?: React.ReactNode;
  /** footer 日期（已格式化字符串），缺省不渲染 */
  time?: string;
  /** 整卡点击（操作区外部） */
  onClick?: () => void;
  /** 自定义卡片类名（业务做尺寸微调等） */
  className?: string;
  style?: React.CSSProperties;
}

/**
 * 通用实体卡片骨架：渐变图标 + 标题 + hover 动作区 + 标签 + 描述 + footer。
 * 纯视觉/布局复用，不绑定任何业务数据；业务卡片（KbCard / DocumentCard…）
 * 组合 props 即可，不再各自复制样式。
 */
const CardShell: React.FC<CardShellProps> = ({
  iconText,
  title,
  actions,
  tags,
  desc,
  meta,
  footerExtra,
  time,
  onClick,
  className,
  style,
}) => {
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  return (
    <Card
      className={`gen-card ${className ?? ''}`.trim()}
      style={style}
      onClick={onClick}
    >
      <div className="gen-card__head">
        <div
          className="gen-card__icon"
          style={{ background: CARD_GRADIENTS[hashString(title) % CARD_GRADIENTS.length] }}
        >
          {iconText}
        </div>
        <Typography.Text strong ellipsis className="gen-card__name" title={title}>
          {title}
        </Typography.Text>
        {actions && (
          <div className="gen-card__actions" onClick={stop}>
            {actions}
          </div>
        )}
      </div>

      {tags && <div className="gen-card__tags">{tags}</div>}

      {desc && <div className="gen-card__desc">{desc}</div>}

      {(meta || footerExtra || time) && (
        <div className="gen-card__footer">
          {meta && <div className="gen-card__footerTop">{meta}</div>}
          {footerExtra}
          {time && (
            <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
              {time}
            </Typography.Text>
          )}
        </div>
      )}
    </Card>
  );
};

export default CardShell;
