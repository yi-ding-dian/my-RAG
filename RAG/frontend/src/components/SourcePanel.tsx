import React, { useEffect, useRef, useState } from 'react';
import { Button, Collapse, Typography, Tag, theme } from 'antd';
import type { Source } from '../api/client';
import MdImages from './MdImages';
import {
  computeHighlightRanges,
  splitByHighlights,
  type HighlightRange,
} from '../utils/sourceHighlight';

const { Text } = Typography;

/** 命中片段预览限高（超出显示"展开全文/收起"） */
const MAX_PREVIEW_HEIGHT = 240;
/** 父块上下文预览限高 */
const MAX_PARENT_HEIGHT = 240;

interface SourcePanelProps {
  sources: Source[];
  /** 点击"查看原文"打开溯源弹窗（可选） */
  onViewOriginal?: (source: Source) => void;
  /** 显示来源序号角标 [n]（与回答正文引用编号对应，从 1 开始） */
  numbered?: boolean;
  /** 弹窗内变体：圆角 10、浅底描边、间距 12、hover 提亮（背景/描边走 CSS class，可被 hover 覆盖） */
  variant?: 'default' | 'modal';
  /**
   * 对应回答文本（引用面板相关文本高亮的匹配基准）：对每条引用的展示文本
   * 计算与回答的重叠区间并高亮；缺省/图谱引用（无自然文本）/无命中时原样显示
   */
  answerText?: string;
}

/**
 * 图片感知的引用预览块：MdImages 全文渲染（markdown 图片引用显示为真实图片；
 * 字符截断会切坏 ![alt](src) 引用，故不做字符截断），容器限高隐藏溢出，
 * 内容超出时提供"展开/收起"按钮。
 *
 * 相关片段模式（"引用来源面板只显示与回答相关的片段"）：调用方给出与回答
 * 重叠的高亮区间时，默认只渲染高亮段拼接的"相关片段"（splitByHighlights 取
 * 高亮段，段间用省略号"…"连接，未命中的无关正文/图片不展示），并始终提供
 * "展开全文/收起"切换（展开后渲染完整文本，高亮仍在）；高亮区间为空/全部
 * 无效（如图谱引用、无命中）时回退原有完整文本截断显示（溢出驱动按钮），
 * 绝不显示空白。
 *
 * 溢出判断：overflow:hidden 下 scrollHeight 仍为完整内容高度，可靠对比限高。
 * 图片加载完成后重测（图片加载前高度为 0 会漏判溢出，导致图片被裁且无展开入口）。
 */
const PreviewBlock: React.FC<{
  text: string;
  maxHeight?: number;
  imgMaxWidth?: number | string;
  color?: string;
  /** 相关文本高亮区间（相对 text 坐标），透传给 MdImages；非空时进入相关片段模式 */
  highlights?: HighlightRange[];
}> = ({ text, maxHeight = MAX_PREVIEW_HEIGHT, imgMaxWidth = 120, color, highlights }) => {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const [imgLoads, setImgLoads] = useState(0);
  const innerRef = useRef<HTMLDivElement>(null);

  // 相关片段模式：高亮段（splitByHighlights 过滤普通段）。空数组 → 无有效高亮 → 回退完整文本。
  // 注意 computeHighlightRanges 保证高亮区间不覆盖图片标签，故高亮段内无图片，可直接 <mark> 渲染。
  const snippetParts = highlights && highlights.length > 0
    ? splitByHighlights(text, highlights).filter(s => s.highlighted)
    : [];
  const hasSnippet = snippetParts.length > 0;
  const showSnippet = hasSnippet && !expanded;

  // 溢出检测用 ResizeObserver 而非一次性判定：antd Collapse/Modal 挂载有
  // 高度动画，useLayoutEffect 在动画中执行时 scrollHeight 不完整，会漏判
  // 溢出导致"展开全文"按钮不出现（引用文本被裁剪却无法展开）。RO 在元素
  // 尺寸变化（Collapse 展开动画、图片加载、字体加载）时持续重算。
  useEffect(() => {
    const el = innerRef.current;
    if (!el) return;
    const update = () => setOverflows(el.scrollHeight > maxHeight + 2);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text, maxHeight, imgLoads]);

  return (
    <div
      /* 测试钩子：snippet 模式（相关片段）时附加 --snippet（E2E 断言用，无行为影响） */
      className={`preview-block${showSnippet ? ' preview-block--snippet' : ''}`}
      style={{ fontSize: 12, lineHeight: 1.7 }}
    >
      <div style={expanded ? undefined : { maxHeight, overflow: 'hidden' }}>
        <div
          ref={innerRef}
          style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color }}
        >
          {showSnippet ? (
            // 相关片段模式：只展示与回答重叠的高亮段，段间用省略号连接（无关正文/图片被省略）
            snippetParts.map((seg, i) => (
              <React.Fragment key={i}>
                {i > 0 && <span style={{ opacity: 0.5 }}>…</span>}
                <mark className="citation-highlight">{seg.text}</mark>
              </React.Fragment>
            ))
          ) : (
            <MdImages
              text={text}
              maxWidth={imgMaxWidth}
              onImageLoad={() => setImgLoads((n) => n + 1)}
              highlights={highlights}
            />
          )}
        </div>
      </div>
      {/* 相关片段模式恒显切换按钮（默认有裁剪必须有全文入口）；否则由溢出检测驱动（现有行为） */}
      {(hasSnippet || overflows) && (
        <Button
          type="link"
          size="small"
          style={{
            padding: '2px 0 0',
            height: 'auto',
            fontSize: 12,
            marginTop: 2,
            borderTop: '1px dashed rgba(var(--brand-primary-soft-rgb, 147, 197, 253), 0.5)',
          }}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? '收起' : '展开全文'}
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
  answerText,
}) => {
  const { token } = theme.useToken();
  const isModal = variant === 'modal';
  if (!sources || sources.length === 0) return null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: isModal ? 12 : 6 }}>
      {sources.map((s, i) => {
        // 相关文本高亮：图谱引用（无自然文本）跳过；answerText 缺失/无命中返回 []
        const isGraph = s.document_name === '知识图谱';
        const parentHighlights = !isGraph && answerText
          ? computeHighlightRanges(answerText, s.parent_text || '')
          : [];
        const textHighlights = !isGraph && answerText
          ? computeHighlightRanges(answerText, s.text)
          : [];
        return (
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
                        highlights={parentHighlights}
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
                  <PreviewBlock
                    text={s.text}
                    color={token.colorTextSecondary}
                    highlights={textHighlights}
                  />
                </div>
              </div>
            </>
          ) : (
            <PreviewBlock
              text={s.text}
              color={token.colorTextSecondary}
              highlights={textHighlights}
            />
          )}
        </div>
        );
      })}
    </div>
  );
};

export default SourcePanel;
