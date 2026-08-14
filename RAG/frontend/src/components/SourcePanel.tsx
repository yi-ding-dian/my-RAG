import React, { useLayoutEffect, useRef, useState } from 'react';
import { Button, Collapse, Typography, Tag, theme } from 'antd';
import type { Source } from '../api/client';
import MdImages from './MdImages';

const { Text } = Typography;

/** 命中片段预览限高（超出显示"展开/收起"） */
const MAX_PREVIEW_HEIGHT = 120;
/** 父块上下文预览限高 */
const MAX_PARENT_HEIGHT = 160;

interface SourcePanelProps {
  sources: Source[];
  /** 点击"查看原文"打开溯源弹窗（可选） */
  onViewOriginal?: (source: Source) => void;
  /** 显示来源序号角标 [n]（与回答正文引用编号对应，从 1 开始） */
  numbered?: boolean;
  /** 弹窗内变体：圆角 10、浅底描边、间距 12、hover 提亮（背景/描边走 CSS class，可被 hover 覆盖） */
  variant?: 'default' | 'modal';
}

/**
 * 图片感知的引用预览块：MdImages 全文渲染（markdown 图片引用显示为真实图片；
 * 字符截断会切坏 ![alt](src) 引用，故不做字符截断），容器限高隐藏溢出，
 * 内容超出时提供"展开/收起"按钮。
 *
 * 溢出判断：overflow:hidden 下 scrollHeight 仍为完整内容高度，可靠对比限高。
 * 图片加载完成后重测（图片加载前高度为 0 会漏判溢出，导致图片被裁且无展开入口）。
 */
const PreviewBlock: React.FC<{
  text: string;
  maxHeight?: number;
  imgMaxWidth?: number | string;
  color?: string;
}> = ({ text, maxHeight = MAX_PREVIEW_HEIGHT, imgMaxWidth = 120, color }) => {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const [imgLoads, setImgLoads] = useState(0);
  const innerRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = innerRef.current;
    setOverflows(!!el && el.scrollHeight > maxHeight + 2);
  }, [text, maxHeight, imgLoads]);

  return (
    <div style={{ fontSize: 12, lineHeight: 1.7 }}>
      <div style={expanded ? undefined : { maxHeight, overflow: 'hidden' }}>
        <div
          ref={innerRef}
          style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color }}
        >
          <MdImages
            text={text}
            maxWidth={imgMaxWidth}
            onImageLoad={() => setImgLoads((n) => n + 1)}
          />
        </div>
      </div>
      {overflows && (
        <Button
          type="link"
          size="small"
          style={{ padding: 0, height: 'auto', fontSize: 12 }}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? '收起' : '展开'}
        </Button>
      )}
    </div>
  );
};

/**
 * 引用来源面板：文档名 Tag + 相似度 + 片段预览 + 查看原文。
 * 父子分块命中（Source.parent_text 存在）时：折叠面板展示父块上下文 + 命中子块预览小字；
 * 否则保持原有纯片段预览。片段与父块均经 MdImages 渲染（图片引用显示为真实图片）。
 */
const SourcePanel: React.FC<SourcePanelProps> = ({
  sources,
  onViewOriginal,
  numbered,
  variant = 'default',
}) => {
  const { token } = theme.useToken();
  const isModal = variant === 'modal';
  if (!sources || sources.length === 0) return null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: isModal ? 12 : 6 }}>
      {sources.map((s, i) => (
        <div
          key={s.id || `${s.document_id}-${s.chunk_index}-${i}`}
          className={isModal ? 'source-card--modal' : undefined}
          style={{
            // modal 变体背景/描边由 CSS class 控制（hover 提亮需要覆盖），default 用 token 浅底
            background: isModal ? undefined : token.colorFillQuaternary,
            borderRadius: isModal ? 10 : 6,
            padding: isModal ? '10px 12px' : '6px 10px',
            border: isModal ? undefined : `1px solid ${token.colorBorderSecondary}`,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            {numbered && (
              <span
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  lineHeight: '16px',
                  color: 'var(--brand-primary, #2563eb)',
                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                  flexShrink: 0,
                }}
              >
                [{i + 1}]
              </span>
            )}
            <Tag color="blue" style={{ marginRight: 0 }}>
              {s.document_name || s.document_id}
            </Tag>
            <Text type="secondary" style={{ fontSize: 12, flex: 1 }}>
              相似度 {s.score != null ? s.score.toFixed(3) : '-'}
            </Text>
            {onViewOriginal && (
              <Button
                size="small"
                type="link"
                style={{ padding: '0 4px', fontSize: 12 }}
                onClick={() => onViewOriginal(s)}
              >
                查看原文
              </Button>
            )}
          </div>
          {s.parent_text ? (
            <>
              <Collapse
                ghost
                size="small"
                style={{ padding: 0, marginBottom: 2 }}
                defaultActiveKey={['parent']}
                items={[
                  {
                    key: 'parent',
                    label: (
                      <Text strong style={{ fontSize: 12 }}>
                        父块上下文
                      </Text>
                    ),
                    children: (
                      <PreviewBlock
                        text={s.parent_text}
                        maxHeight={MAX_PARENT_HEIGHT}
                        imgMaxWidth="100%"
                      />
                    ),
                  },
                ]}
              />
              <div style={{ fontSize: 12, lineHeight: 1.7, display: 'flex', gap: 4 }}>
                <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
                  命中片段：
                </Text>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <PreviewBlock text={s.text} color={token.colorTextSecondary} />
                </div>
              </div>
            </>
          ) : (
            <PreviewBlock text={s.text} color={token.colorTextSecondary} />
          )}
        </div>
      ))}
    </div>
  );
};

export default SourcePanel;
