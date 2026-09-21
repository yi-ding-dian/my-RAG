import React from 'react';
import { Collapse, Form, Select, Tooltip } from 'antd';
import type { MinerUBackend, MinerUEffort } from '../../../../shared/api/client';

interface MinerUBackendFieldProps {
  /** 表单字段名（默认 backend） */
  name?: string;
  /** 默认值 */
  initialValue?: MinerUBackend;
  /** effort 字段名（默认 effort；仅 backend=hybrid-engine 时提交） */
  effortName?: string;
}

/** 三个后端的对比（官方口径）：配置时直接看，不用去翻文档 */
const COMPARE_ROWS: [string, string, string, string][] = [
  ['核心原理',
    '多个专用小模型组成流水线，分步处理',
    'VLM 负责版面分析，结合 Pipeline 的原生文本提取',
    '端到端单个视觉大模型完成所有识别任务'],
  ['解析精度',
    '约 82%–86+，简单文档稳定',
    '约 90%–95+，兼顾精度与稳定',
    '约 90%–95+，复杂布局下精度最高'],
  ['幻觉风险',
    '无幻觉（基于规则和 OCR，输出稳定）',
    '低幻觉（通过原生文本提取约束，显著降低幻觉）',
    '可能产生幻觉（生成式模型，低质量图像上可能出错）'],
  ['硬件需求',
    '可纯 CPU 运行，使用GPU可加速解析',
    '最低 8GB 显存，推荐 10GB+',
    '最低 8GB 显存，官方建议 10GB+ 体验更佳'],
  ['处理速度',
    '最快（流水线并行，适合批量快速处理）',
    '中等（精度与速度的平衡选择）',
    '较慢（单模型端到端推理，计算量较大）'],
  ['适用场景',
    '纯文本 PDF、无需解析图片、简单文档、硬件资源有限、对速度要求高的场景',
    '通用场景（默认推荐）、企业级应用、对精度和稳定性都有要求的场景',
    '学术论文、多栏排版、复杂公式表格、高精度要求的复杂版面文档'],
];

const BACKEND_NAMES = ['pipeline（流水线）', 'hybrid-engine（混合引擎）', 'vlm-engine（视觉大模型）'];

const CompareTable: React.FC = () => (
  <div style={{ fontSize: 12, overflowX: 'auto' }}>
    <table style={{ borderCollapse: 'collapse', width: '100%', minWidth: 640 }}>
      <thead>
        <tr>
          <th style={thStyle(90)}>特性</th>
          {BACKEND_NAMES.map((n) => <th key={n} style={thStyle()}>{n}</th>)}
        </tr>
      </thead>
      <tbody>
        {COMPARE_ROWS.map(([feat, ...cells]) => (
          <tr key={feat}>
            <td style={{ ...tdStyle, fontWeight: 600, background: '#fafafa' }}>{feat}</td>
            {cells.map((c, i) => <td key={i} style={tdStyle}>{c}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

const thStyle = (width?: number): React.CSSProperties => ({
  border: '1px solid #f0f0f0', padding: '6px 8px', textAlign: 'left',
  background: '#fafafa', fontWeight: 600, width,
});
const tdStyle: React.CSSProperties = {
  border: '1px solid #f0f0f0', padding: '6px 8px', verticalAlign: 'top', lineHeight: 1.6,
};

/**
 * MinerU 解析后端选择（mineru-api /file_parse 的 backend 参数）。
 * 仅在解析引擎选择「MinerU 高精度」时显示（ParseConfigModal 条件渲染）。
 *
 * 取值以**服务端 OpenAPI 枚举**为准：pipeline / hybrid-engine / vlm-engine
 * （vlm-http-client、hybrid-http-client 需服务端开 --allow-public-http-client，
 * 默认关闭以防 SSRF，故不列出）。旧值 hybrid-auto-engine 服务端并不存在，
 * 后端已按 hybrid-engine 兼容。
 *
 * 默认值 pipeline：它在无 GPU 的环境也能跑（服务端用 CPU），是"总能出结果"的
 * 兜底档；有 GPU 的环境建议选 hybrid-engine。
 * effort 仅 hybrid-engine 有效——服务端默认 medium 会**关闭图片/图表分析**，
 * 实测会把表格说明行误标成标题、页眉混进正文，故前端默认给 high。
 */
const MinerUBackendField: React.FC<MinerUBackendFieldProps> = ({
  name = 'backend',
  initialValue = 'pipeline',
  effortName = 'effort',
}) => {
  // 只有 hybrid-engine 需要 effort（服务端标注 "Adapted only for hybrid backend"）
  const backend = Form.useWatch(name) as MinerUBackend | undefined;
  return (
    <>
      <Form.Item
        name={name}
        label={
          <span>
            MinerU 解析后端
            <Tooltip title="流水线最快、无幻觉但表格结构弱；混合引擎用 VLM 分析版面、精度与稳定性兼顾（推荐）；视觉大模型复杂版面最准但慢且可能幻觉。展开下方对比表看详细差异。">
              <span style={{ marginLeft: 6, color: '#999', cursor: 'help' }}>?</span>
            </Tooltip>
          </span>
        }
        initialValue={initialValue}
      >
        <Select
          options={[
            { value: 'auto', label: '自动（跟随服务端默认）' },
            { value: 'pipeline', label: '流水线 pipeline（最快，表格结构弱）' },
            { value: 'hybrid-engine', label: '混合引擎 hybrid-engine（推荐）' },
            { value: 'vlm-engine', label: '视觉大模型 vlm-engine（复杂版面最准，慢）' },
          ]}
        />
      </Form.Item>

      {backend === 'hybrid-engine' && (
        <Form.Item
          name={effortName}
          label={
            <span>
              解析力度
              <Tooltip title="仅混合引擎有效。medium 更快但会关闭图片/图表分析（实测会把表格说明行误标成标题、页眉混进正文）；high 开启图片分析、精度更高，推荐。">
                <span style={{ marginLeft: 6, color: '#999', cursor: 'help' }}>?</span>
              </Tooltip>
            </span>
          }
          initialValue={'high' as MinerUEffort}
        >
          <Select
            options={[
              { value: 'high', label: '高（开启图片/图表分析，推荐）' },
              { value: 'medium', label: '中（更快，但关闭图片分析）' },
            ]}
          />
        </Form.Item>
      )}

      <Collapse
        ghost
        style={{ marginBottom: 12, marginTop: -8 }}
        items={[{
          key: 'compare',
          label: '三种解析后端对比',
          children: <CompareTable />,
        }]}
      />
    </>
  );
};

export default MinerUBackendField;
