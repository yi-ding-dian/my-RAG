import React, { useEffect, useState } from 'react';
import { Select, Spin, Tag, Tooltip, Typography, theme } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import type {
  ImageSummaryPromptResult,
  ImgSummaryFormat,
} from '../../../shared/api/client';
import { getImageSummaryPrompt } from '../../../shared/api/client';

const { Text } = Typography;

/** 「跟随系统配置」的哨兵值：不覆盖部门/全局配的格式（语义=不传该参数） */
export const IMG_FMT_FOLLOW = '';

/** 格式标识 → 中文名（下拉选项与确认页回显同源，避免两处措辞漂移） */
export const IMG_FMT_LABELS: Record<string, string> = {
  [IMG_FMT_FOLLOW]: '跟随系统配置',
  fields: '固定字段',
  prose: '段落描述',
  brief: '一句话简介',
};

const FORMAT_OPTIONS = Object.entries(IMG_FMT_LABELS).map(([value, label]) => ({
  value,
  label,
}));

interface Props {
  /** 知识库 id：用于定位生效的部门配置（部门归属在知识库上，不在文档上） */
  kbId?: string;
  value?: ImgSummaryFormat | '';
  /** 可选：被 antd Form.Item 包裹时由表单注入 */
  onChange?: (v: ImgSummaryFormat | '') => void;
  /** small=解析弹窗紧凑排布（默认 middle） */
  size?: 'small' | 'middle';
  style?: React.CSSProperties;
}

/**
 * 图片摘要输出格式选择（三个解析入口共用）
 *
 * - 选项：跟随系统配置（默认，不覆盖部门/全局格式）+ 三种格式；
 * - 悬浮图标显示**本次实际会用的提示词**——文字由后端算好返回
 *   （GET /settings/image-summary/prompt），前端只展示、不自己拼模板：
 *   选了与部门配置不同的格式时，后端会把部门自定义提示词换成该格式的内置
 *   模板（那份是照旧格式写的，硬套会产出解析不了的内容），前端若自己拼就会
 *   显示得跟实际不一致，反而误导；
 * - 只读。要微调提示词去「系统配置 → 部门配置 → 图片摘要」（那里才有编辑与
 *   恢复默认）。
 */
const ImgSummaryFormatPicker: React.FC<Props> = ({
  kbId,
  value,
  onChange,
  size = 'middle',
  style,
}) => {
  const { token } = theme.useToken();
  const [preview, setPreview] = useState<ImageSummaryPromptResult | null>(null);
  const [loading, setLoading] = useState(false);

  // 格式变化就重取一次（"跟随系统配置"取的是部门当前配置的那份）
  useEffect(() => {
    let alive = true;
    setLoading(true);
    getImageSummaryPrompt(kbId, value || undefined)
      .then(res => {
        if (alive) setPreview(res.data);
      })
      .catch(() => {
        if (alive) setPreview(null);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kbId, value]);

  const tip = loading ? (
    <Spin size="small" />
  ) : preview ? (
    <div style={{ maxHeight: 280, overflowY: 'auto' }}>
      <div style={{ marginBottom: 6 }}>
        本次提示词
        <Tag
          color={preview.source === 'custom' ? 'blue' : 'default'}
          style={{ marginLeft: 6, fontSize: 11 }}
        >
          {preview.source === 'custom' ? '部门自定义' : '内置模板'}
        </Tag>
      </div>
      <div style={{ whiteSpace: 'pre-wrap', fontSize: 12, lineHeight: 1.65 }}>
        {preview.prompt}
      </div>
      <div style={{ marginTop: 8, fontSize: 11, opacity: 0.65 }}>
        只读。要微调请到「系统配置 → 部门配置 → 图片摘要」
      </div>
    </div>
  ) : (
    <span style={{ fontSize: 12 }}>提示词获取失败，可直接解析</span>
  );

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, ...style }}>
      <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
        输出格式
      </Text>
      <Select
        size={size}
        value={value ?? IMG_FMT_FOLLOW}
        onChange={v => onChange?.(v as ImgSummaryFormat | '')}
        options={FORMAT_OPTIONS}
        style={{ width: 150 }}
      />
      <Tooltip title={tip} overlayStyle={{ maxWidth: 460 }} placement="topLeft">
        <InfoCircleOutlined
          style={{ color: token.colorTextTertiary, cursor: 'help', fontSize: 14 }}
        />
      </Tooltip>
    </div>
  );
};

export default ImgSummaryFormatPicker;
