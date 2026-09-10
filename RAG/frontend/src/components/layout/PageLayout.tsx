import React from 'react';
import PageHeader from './PageHeader';

/**
 * 页面布局器（父类骨架）：标题 + 描述 + 面包屑 + 右上按钮区 + 子布局容器。
 *
 * 统一页面骨架，子页面用 children 填充内容区；配合 TableSectionLayout
 * 子布局即可获得「页头固定、分页贴屏幕最下方、中间滚动」的完整页面体验。
 * 页面结构（与 PageHeader 职责区分）：
 * ┌─────────── PageHeader（title/description/breadcrumb/extra）───────────┐
 * │ 文档管理（全部部门）                    [按钮1][按钮2]                 │
 * │ 描述可选                                                               │
 * └────────────────────────────────────────────────────────────────────────┘
 * ┌────────────────────────────────────────────────────────────────────────┐
 * │                     children —— 子布局填充区（flex:1）                  │
 * └────────────────────────────────────────────────────────────────────────┘
 *
 * 用法：
 *   <PageLayout
 *     title="文档管理（全部部门）"
 *     description="超管跨部门视图…"
 *     breadcrumb={<Breadcrumb items={...} />}
 *     extra={<><Button>刷新</Button></>}
 *     footer={...}                    // 可选：固定屏幕最下方（分页等）
 *   >
 *     <TableSectionLayout ...>…</TableSectionLayout>
 *   </PageLayout>
 */

export interface PageLayoutProps {
  /** 页面大标题（可选：纯面包屑/导航场景可不传，如文档管理三级下钻页） */
  title?: React.ReactNode;
  /** 副标题（灰字小字，可选） */
  description?: React.ReactNode;
  /** 面包屑：渲染在标题上方（可选） */
  breadcrumb?: React.ReactNode;
  /** 右上操作区（flex 布局，可选：按钮1/按钮2 等任意元素） */
  extra?: React.ReactNode;
  /** 内容区（子布局），占剩余高度 */
  children?: React.ReactNode;
  /** 固定屏幕最下方的区域（如分页条；可选） */
  footer?: React.ReactNode;
  /** 整体上下留白（默认 0——外层 Content 已有 padding 24；如需 8/16 传入） */
  gap?: number;
}

/**
 * 页面级骨架：PageHeader 固定顶部 + children 撑满剩余 + footer 贴屏幕最下方。
 * - 高度:100%（父链 App Content height:100vh → page-fade height:100% 已保证）
 * - flex column：header 固定，children flex:1 占中，footer 贴底
 */
const PageLayout: React.FC<PageLayoutProps> = ({
  title,
  description,
  breadcrumb,
  extra,
  children,
  footer,
}) => (
  <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
    <PageHeader
      title={title}
      description={description}
      breadcrumb={breadcrumb}
      extra={extra}
    />
    {/* 子布局区：占剩余高度（内部自滚动由子布局决定；无子布局时弹性占位） */}
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {children}
    </div>
    {footer && (
      <div style={{ flexShrink: 0, marginTop: 12 }}>{footer}</div>
    )}
  </div>
);

export default PageLayout;
