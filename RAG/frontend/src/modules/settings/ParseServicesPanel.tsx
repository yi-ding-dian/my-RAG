import React from 'react';
import { Divider, Tooltip, theme } from 'antd';
import MineruPanel from './MineruPanel';
import DeepdocPanel from './DeepdocPanel';
import GotenbergPanel from './GotenbergPanel';
import type { TestItem } from './shared';

/** 本面板涉及的三个可探测段（key 与 SectionKey 一致） */
export type ParseSectionKey = 'mineru' | 'deepdoc' | 'gotenberg';

interface Props {
  /** 连接测试结果（当前编辑档案那份）；缺省时灯显示为灰色"未测试" */
  testItems?: Partial<Record<ParseSectionKey, TestItem>>;
}

/**
 * 连接状态灯：绿=连通 / 红=不通 / 黄=测试中 / 灰=未测
 *
 * 只反映**最近一次「测试连接」的结果**（含面板头 ⚡ 一次测三项），不做自动
 * 轮询——配置页不该常驻探测外部服务；想看最新状态点一下 ⚡ 即可。
 */
const StatusDot: React.FC<{ item?: TestItem }> = ({ item }) => {
  const { token } = theme.useToken();
  const status = item?.status ?? 'idle';
  const color = status === 'success' ? token.colorSuccess
    : status === 'failed' ? token.colorError
      : status === 'testing' ? token.colorWarning
        : token.colorTextQuaternary;
  const label = status === 'success' ? '连接正常'
    : status === 'failed' ? '连接失败'
      : status === 'testing' ? '测试中…'
        : '未测试（点右上角 ⚡ 测试）';
  return (
    <Tooltip title={item?.msg || label}>
      <span
        style={{
          display: 'inline-block',
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: color,
          marginLeft: 6,
          verticalAlign: 'middle',
        }}
      />
    </Tooltip>
  );
};

/** 分组小标题 + 状态灯 */
const SectionTitle: React.FC<{
  children: React.ReactNode;
  item?: TestItem;
}> = ({ children, item }) => (
  <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
    {children}
    <StatusDot item={item} />
  </div>
);

/**
 * 文档解析相关（配置档案弹窗）：MinerU / DeepDoc / Gotenberg 三段合一
 *
 * 三者都是**为文档解析服务**的外部依赖，原来各占一个折叠面板，配置时要来回
 * 展开对照；合并后在一个面板里从上到下列出，关系一眼可见：
 *
 *   MinerU       —— PDF 版面识别（标题层级还原的主力）
 *   DeepDoc      —— 另一条解析路线（表格输出为可检索 HTML）
 *   Gotenberg    —— 把 .doc/.docx 转 PDF 交给 MinerU（MinerU 直接吃 docx 会丢标题）
 *
 * 字段名互不冲突，合并后仍绑定同一个 Form；面板头的 ⚡ 一次测这三项
 * （见 shared.ts 的 PANEL_TEST_SECTIONS.parse），各段标题旁的状态灯随之变色。
 */
const ParseServicesPanel: React.FC<Props> = ({ testItems }) => (
  // className：组内字段间距由 CSS 收紧（见 styles/modals.css 的 .parse-services-panel）
  <div className="parse-services-panel">
    <SectionTitle item={testItems?.mineru}>MinerU 文档解析</SectionTitle>
    <MineruPanel />

    <Divider style={{ margin: '8px 0' }} />

    <SectionTitle item={testItems?.deepdoc}>DeepDoc 解析（RAGFlow）</SectionTitle>
    <DeepdocPanel />

    <Divider style={{ margin: '8px 0' }} />

    <SectionTitle item={testItems?.gotenberg}>文档转换（Gotenberg）</SectionTitle>
    <GotenbergPanel />
  </div>
);

export default ParseServicesPanel;
