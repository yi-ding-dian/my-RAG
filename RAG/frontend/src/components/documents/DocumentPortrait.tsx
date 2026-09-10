import React, { useState } from 'react';
import {
  Alert,
  Button,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  ReloadOutlined,
  FileTextOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ThunderboltOutlined,
  AlignLeftOutlined,
  DashboardOutlined,
  QuestionCircleOutlined,
  LinkOutlined,
  RocketOutlined,
  EyeOutlined,
} from '@ant-design/icons';
import type { AnalyzeResult } from '../../api/client';
import DocxOutlineModal from './DocxOutlineModal';

const { Text } = Typography;

/** 解析引擎显示名（画像卡片 + 向导 Step4 摘要共用） */
export const ENGINE_LABELS: Record<string, string> = {
  mineru: 'MinerU',
  deepdoc: 'DeepDOC',
  docx_struct: '结构解析',
  plain: '纯文本',
  auto: '自动',
  spreadsheet: '本地表格直读',
};

const DENSITY_TAG: Record<string, { color: string; label: string }> = {
  low: { color: 'default', label: '低' },
  mid: { color: 'warning', label: '中' },
  high: { color: 'volcano', label: '高' },
};

/** 画像卡片：图标 + 标签 + 值区（spw-* 样式类在 index.css，向导与画像弹窗共用） */
const PortraitCard: React.FC<{
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
  wide?: boolean;
  iconClass?: string;
}> = ({ icon, label, children, wide, iconClass }) => (
  <div className={`spw-portrait-card${wide ? ' spw-portrait-card--wide' : ''}`}>
    <span className={`spw-p-icon${iconClass ? ` ${iconClass}` : ''}`}>{icon}</span>
    <div className="spw-p-body">
      <div className="spw-p-label">{label}</div>
      <div className="spw-p-value">{children}</div>
    </div>
  </div>
);

interface DocumentPortraitProps {
  /** 画像数据（null 时仅展示加载/错误态） */
  analyze: AnalyzeResult | null;
  /** 加载中（Spin 包裹内容） */
  loading?: boolean;
  /** 加载失败信息（展示错误 Alert + 重试按钮） */
  error?: string | null;
  /** 重试回调（不传则不显示重试按钮） */
  onRetry?: () => void;
  /** 文档所属知识库 ID（「查看文档结构」弹窗用；与 docId 缺一不可） */
  kbId?: string;
  /** 文档 ID（「查看文档结构」弹窗用；缺失时引擎标签保持静态不可点） */
  docId?: string;
  /** 文档展示名（结构弹窗标题用） */
  fileName?: string;
}

/**
 * 文档画像只读展示（GET /analyze 结果）：文件类型/文本提取/引擎建议/
 * 标题结构/篇幅/QA 格式/指代密集度 + 推荐解析路径。
 * 智能解析向导 Step1 与「更多 → 查看文档画像」弹窗共用（抽取自 SmartParseWizard）。
 * 建议引擎为"结构解析"（docx_struct）时，引擎标签可点击打开「查看文档结构」
 * 弹窗——预览解析后的标题层级树（解析前就能确认结构解析的效果）。
 */
const DocumentPortrait: React.FC<DocumentPortraitProps> = ({
  analyze,
  loading,
  error,
  onRetry,
  kbId,
  docId,
  fileName,
}) => {
  // 「查看文档结构」弹窗开关（仅引擎建议=结构解析且有文档定位信息时可打开）
  const [outlineOpen, setOutlineOpen] = useState(false);
  const suggestedEngine = analyze?.engine_suggestion.suggested;
  const canViewOutline = suggestedEngine === 'docx_struct' && !!kbId && !!docId;
  return (
    <>
    <Spin spinning={!!loading}>
    {loading ? (
      // 加载中：居中占满容器（Spin nest 模式下指示器居中显示；
      // 空 children 时 antd 将 spinner 渲染在左上角且容器坍缩，圈圈被弹窗
      // 头部/底部挡或显示不全——给撑高容器保证居中可见）
      <div style={{ minHeight: 320, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Spin size="large" />
      </div>
    ) : error ? (
      <Alert
        type="error"
        message="画像分析失败"
        description={error}
        action={
          onRetry ? (
            <Button size="small" icon={<ReloadOutlined />} onClick={onRetry}>
              重试
            </Button>
          ) : undefined
        }
      />
    ) : analyze ? (
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {analyze.warnings && analyze.warnings.length > 0 && (
          <Alert
            type="warning"
            message="画像分析部分失败"
            description={analyze.warnings.join('；')}
            showIcon
          />
        )}
        <div className="spw-grid">
          <PortraitCard icon={<FileTextOutlined />} label="文件类型">
            <Tag color="blue">{analyze.file_type}</Tag>
          </PortraitCard>
          <PortraitCard
            icon={analyze.extracted ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
            label="文本提取"
            iconClass={analyze.extracted ? 'spw-p-icon--success' : 'spw-p-icon--danger'}
          >
            {analyze.extracted ? (
              <Text type="success">成功</Text>
            ) : (
              <Text type="danger">{analyze.extract_warning ?? '提取失败'}</Text>
            )}
          </PortraitCard>
          <PortraitCard icon={<ThunderboltOutlined />} label="引擎建议">
            <Space direction="vertical" size={2}>
              {/* 建议引擎=结构解析时标签可点击（打开「查看文档结构」预览标题
                  层级树）；不可点场景保持原静态标签 */}
              {canViewOutline ? (
                <Tooltip title="点击查看这份文档解析后的标题层级树">
                  <Tag
                    color="geekblue"
                    className="spw-engine-tag--clickable"
                    icon={<EyeOutlined />}
                    onClick={() => setOutlineOpen(true)}
                  >
                    {ENGINE_LABELS[analyze.engine_suggestion.suggested] ?? analyze.engine_suggestion.suggested}
                    {' · 查看文档结构'}
                  </Tag>
                </Tooltip>
              ) : (
                <Tag
                  color={analyze.engine_suggestion.suggested === 'auto' ? 'default' : 'geekblue'}
                >
                  {ENGINE_LABELS[analyze.engine_suggestion.suggested] ?? analyze.engine_suggestion.suggested}
                </Tag>
              )}
              <Text type="secondary" style={{ fontSize: 12 }}>
                {analyze.engine_suggestion.reason}
              </Text>
            </Space>
          </PortraitCard>
          {analyze.spreadsheet ? (
            <>
              {/* 表格文档画像：Sheet 结构替代文本结构卡（后端 smart_parse 轻量统计） */}
              <PortraitCard icon={<DashboardOutlined />} label="表格结构" wide iconClass="spw-p-icon--violet">
                <Space direction="vertical" size={2}>
                  <span>
                    {analyze.spreadsheet.sheet_count} 个 Sheet /
                    共 {analyze.spreadsheet.total_rows} 行
                    {analyze.spreadsheet.merged_cells > 0 && (
                      <Tag color="orange" style={{ marginLeft: 8 }}>
                        合并单元格 {analyze.spreadsheet.merged_cells} 处
                      </Tag>
                    )}
                  </span>
                  <Space size={4} wrap>
                    {analyze.spreadsheet.sheets.slice(0, 4).map((s) => (
                      <Tag key={s.name} style={{ margin: 0 }}>
                        {s.name}（{s.rows} 行 × {s.cols} 列）
                      </Tag>
                    ))}
                    {analyze.spreadsheet.sheets.length > 4 && (
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        +{analyze.spreadsheet.sheets.length - 4} 个
                      </Text>
                    )}
                  </Space>
                </Space>
              </PortraitCard>
            </>
          ) : (
            <>
              <PortraitCard icon={<AlignLeftOutlined />} label="标题结构">
                {analyze.structure.has_headings ? (
                  <Space direction="vertical" size={2}>
                    <Tag color="green">
                      有标题（
                      {analyze.structure.heading_count + analyze.structure.numbered_headings} 个）
                    </Tag>
                    <div>
                      {analyze.structure.examples.slice(0, 2).map((t, i) => (
                        <Tag key={i} style={{ marginInlineEnd: 4, marginTop: 2 }}>
                          {t}
                        </Tag>
                      ))}
                    </div>
                    {/* 标题编号体系（检测结果自动用于入库切块层级推断）：
                        标签展示体系名，悬停看命中行数与示例标题 */}
                    {(analyze.structure.heading_systems ?? []).length > 0 && (
                      <div style={{ marginTop: 2 }}>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          编号体系：
                        </Text>
                        {(analyze.structure.heading_systems ?? []).map(s => (
                          <Tooltip
                            key={s.system}
                            title={
                              <div style={{ fontSize: 12, lineHeight: 1.7 }}>
                                <div style={{ fontWeight: 600 }}>{s.label}</div>
                                <div>命中 {s.hits} 个标题</div>
                                {s.examples.map((e, i) => (
                                  <div key={i} style={{ opacity: 0.85 }}>例：{e}</div>
                                ))}
                                <div style={{ marginTop: 4, opacity: 0.75 }}>
                                  入库时自动按此体系推断标题层级（章/节/小节）
                                </div>
                              </div>
                            }
                          >
                            <Tag color="blue" style={{ marginTop: 2, cursor: 'help' }}>
                              {s.label.split('（')[0]} ×{s.hits}
                            </Tag>
                          </Tooltip>
                        ))}
                      </div>
                    )}
                  </Space>
                ) : (
                  <Tag>无标题结构</Tag>
                )}
              </PortraitCard>
              <PortraitCard icon={<DashboardOutlined />} label="篇幅">
                <Space direction="vertical" size={2}>
                  <span>
                    {analyze.length.doc_label} / 段落 {analyze.length.paragraphs}
                    {analyze.length.over_threshold ? (
                      <Tag color="red" style={{ marginLeft: 8 }}>
                        超过阈值 {analyze.length.threshold_label}
                      </Tag>
                    ) : (
                      <Tag color="green" style={{ marginLeft: 8 }}>
                        ≤ 阈值 {analyze.length.threshold_label}
                      </Tag>
                    )}
                  </span>
                </Space>
              </PortraitCard>
              <PortraitCard
                icon={<QuestionCircleOutlined />}
                label="QA 格式"
                iconClass="spw-p-icon--violet"
              >
                {analyze.qa.is_qa ? (
                  <Tag color="purple">
                    QA 问答（{analyze.qa.qa_pairs} 对，占比 {Math.round(analyze.qa.ratio * 100)}%）
                  </Tag>
                ) : (
                  <Text type="secondary">非 QA 格式</Text>
                )}
              </PortraitCard>
              <PortraitCard
                icon={<LinkOutlined />}
                label="指代密集度"
                wide
                iconClass={
                  analyze.reference_density.level === 'high'
                    ? 'spw-p-icon--danger'
                    : analyze.reference_density.level === 'mid'
                      ? 'spw-p-icon--amber'
                      : undefined
                }
              >
                <Tag color={DENSITY_TAG[analyze.reference_density.level]?.color ?? 'default'}>
                  {analyze.reference_density.level_label}
                </Tag>
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                  {analyze.reference_density.count} 次（
                  {analyze.reference_density.per_1000_chars} 次/千字）
                </Text>
              </PortraitCard>
            </>
          )}
        </div>
        {analyze.recommendations && (
          <div className="spw-recommend">
            <div className="spw-recommend-title">
              <RocketOutlined />
              <span>推荐解析路径</span>
            </div>
            <div className="spw-recommend-body">
              <div className="spw-recommend-line">
                <span className="spw-recommend-chip">
                  {analyze.recommendations.chunk_method.label}
                  {analyze.recommendations.contextual_retrieval.recommended &&
                    ' + 上下文检索增强'}
                </span>
              </div>
              {analyze.recommendations.chunk_method.reason && (
                <div className="spw-recommend-reason">
                  {analyze.recommendations.chunk_method.reason}
                </div>
              )}
              {analyze.recommendations.contextual_retrieval.reason && (
                <div className="spw-recommend-reason">
                  {analyze.recommendations.contextual_retrieval.reason}
                </div>
              )}
            </div>
          </div>
        )}
      </Space>
    ) : null}
    </Spin>
    {/* 「查看文档结构」弹窗（引擎建议=结构解析时由上方标签点开） */}
    <DocxOutlineModal
      open={outlineOpen}
      onCancel={() => setOutlineOpen(false)}
      kbId={kbId}
      docId={docId}
      fileName={fileName}
    />
    </>
  );
};

export default DocumentPortrait;
