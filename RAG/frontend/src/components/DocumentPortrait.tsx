import React from 'react';
import {
  Alert,
  Button,
  Space,
  Spin,
  Tag,
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
} from '@ant-design/icons';
import type { AnalyzeResult } from '../api/client';

const { Text } = Typography;

/** 解析引擎显示名（画像卡片 + 向导 Step4 摘要共用） */
export const ENGINE_LABELS: Record<string, string> = {
  mineru: 'MinerU',
  deepdoc: 'DeepDOC',
  plain: '纯文本',
  auto: '自动',
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
}

/**
 * 文档画像只读展示（GET /analyze 结果）：文件类型/文本提取/引擎建议/
 * 标题结构/篇幅/QA 格式/指代密集度 + 推荐解析路径。
 * 智能解析向导 Step1 与「更多 → 查看文档画像」弹窗共用（抽取自 SmartParseWizard）。
 */
const DocumentPortrait: React.FC<DocumentPortraitProps> = ({
  analyze,
  loading,
  error,
  onRetry,
}) => (
  <Spin spinning={!!loading}>
    {error ? (
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
              <Tag
                color={analyze.engine_suggestion.suggested === 'auto' ? 'default' : 'geekblue'}
              >
                {ENGINE_LABELS[analyze.engine_suggestion.suggested] ?? analyze.engine_suggestion.suggested}
              </Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {analyze.engine_suggestion.reason}
              </Text>
            </Space>
          </PortraitCard>
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
);

export default DocumentPortrait;
