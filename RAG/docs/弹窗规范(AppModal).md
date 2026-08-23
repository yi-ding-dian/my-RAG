# 弹窗规范(AppModal)

> 本文档定义 my-RAG 全站统一弹窗的**视觉、行为、API 与落地方式**。
> 全项目弹窗已收敛到 `frontend/src/components/AppModal.tsx`(31 处全部迁移),
> 新弹窗一律使用。其他项目可整体照搬实现。
>
> 配套文件:`components/AppModal.tsx`(组件)、`utils/useResizable.tsx`(拖拽 hook)、
> `index.css` 中 `.rsz` / `.am-shell` 样式段。

---

## 一、视觉形态

```
┌──────────────────────────────────────────────────────┐
│                   标题文字                    ✕(可关) │
├──────────────────────────────────────────────────────┤
│                                                      │
│                   内容区(任意布局)                     │ ← 超长自动滚动
│                                                      │
├──────────────────────────────────────────────────────┤
│              [取消]        [确定(主题主色)]             │ ← 底部按钮(可自定义)
└──────────────────────────────────────────────────────┘
   ▲ 四周边缘:默认隐藏;鼠标悬到弹窗时浮现
     四边 8px 细条(内缩 16px 避让角)+ 四角 20×20 圆角块,
     颜色 = 主题品牌色(--brand-primary-rgb,CSS 变量)
```

要点:

| 项 | 规约 |
|---|---|
| 圆角/阴影/边框 | 跟随 antd v5 token(无需自绘) |
| 尺寸 | 内容自适应或可拖拽;**最小 520×360;最大 = 视口 − 48px(上下左右留白 24)**,绝不超出屏幕 |
| 手柄 | 平时隐藏(hover 弹窗浮现),拖拽中的边/角高亮——视觉不打扰,操作可感知 |
| 主题 | 手柄/高亮走 `--brand-primary-rgb`,亮/暗主题自动跟随 |

## 二、行为规约

1. **尺寸三档**
   - `auto`:高度跟随内容(ResizeObserver 实时测),超最大 → 内容区内部滚动;
   - `resizable`:初始固定尺寸 + 8 向自由拖拽;
   - `fixed`:固定尺寸,无手柄。
   - 拖拽 = 转为手动尺寸(记忆生效);西/北向拖动时窗口位置随左/上边缘移动。
2. **尺寸记忆**:`rememberKey` 存 localStorage(`am.{key}`),下次打开恢复(校验 ≥ 最小值)。
3. **关闭行为**
   - 右上角 ✕、Esc:正常关闭;
   - **点击遮罩:默认不关闭**(防误触丢表单/提交内容);
   - `busy`(提交/加载中):✕/遮罩/Esc 全部禁止,防忙时误关。
4. **底部按钮**:antd 原生(`okText/onOk/confirmLoading`)或统一 `AppModalFooter`。
5. **内容布局**:children 任意(表单/多列/步骤条/表格/嵌套弹窗),footer 自动吸底不随内容滚动。

## 三、API

```tsx
interface AppModalProps extends ModalProps {
  dimension?: 'auto' | 'resizable' | 'fixed';   // 默认 auto
  defaultSize?: { w: number; h: number };        // 默认 760×520
  minSize?: { w: number; h: number };            // 默认 520×360
  maxSize?: { w: number; h: number };            // 默认视口钳制(更强)
  rememberKey?: string;                          // 尺寸记忆键
  busy?: boolean;                                // 提交中全禁关
  maskClosable?: boolean;                        // 点遮罩关闭(默认 false)
  // 其余 antd ModalProps 全部透传:title/footer/onOk/okText/confirmLoading/
  // okButtonProps/closable/styles/destroyOnClose/className ...
}

// 统一底部按钮组(可选):
interface AppModalFooterProps {
  okText?: string;                 // 默认"确定"
  cancelText?: string | null;      // 默认"取消",null 不渲染
  okLoading?: boolean;             // 提交 loading + 全按钮禁用
  danger?: boolean;                // 危险操作(删除),确定按钮红色
  onOk?: () => void;
  onCancel?: () => void;
  extra?: React.ReactNode;         // 左侧附加区
}
```

示例:

```tsx
<AppModal
  title="导出报表"
  dimension="auto"
  rememberKey="export"
  busy={exporting}
  footer={<AppModalFooter okText="导出" okLoading={exporting}
            onOk={doExport} onCancel={close} extra={<a>导出格式说明</a>} />}
>
  <Form>…</Form>
</AppModal>
```

## 四、落地清单(其他项目)

| 文件 | 拷贝项 |
|---|---|
| `AppModal.tsx` | 组件本体(含 AppModalFooter;依赖 antd v5) |
| `useResizable.tsx` | 8 向拖拽 hook(pointer capture、clamp、位置回调、draggingDir) |
| CSS | `.rsz` 八向手柄 + `.am-shell:hover` 浮现 + hover/active 高亮(约 30 行) |

唯一主题依赖:CSS 变量 `--brand-primary-rgb`(形如 `59, 130, 246`),亮/暗主题各自注入即自动跟随;未注入时用 fallback 值(默认蓝)。

**适配非 antd 项目**:保持 `.am-shell` 包裹弹窗容器 + `.rsz` 手柄 DOM 结构即可,尺寸逻辑与框架无关(React hook 与原生事件均可)。

## 五、本项目的使用现状

- 全站 31 处弹窗已迁移(AppModal 内仅保留 antd `<Modal>` 一处实现);
- 代表用法:预览弹窗 `resizable`+记忆;智能解析向导 `auto`;URL 导入 `busy`+记忆;
- 迁移模式:替换 `<Modal>` → `<AppModal>`,按弹窗语义选 dimension/rememberKey,其余 props 透传。
