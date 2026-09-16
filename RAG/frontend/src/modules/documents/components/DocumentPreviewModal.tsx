import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Input, Spin, Typography } from 'antd';
import {
  DownloadOutlined,
  DownOutlined,
  SearchOutlined,
  UpOutlined,
} from '@ant-design/icons';
import type { DocumentItem } from '../../../shared/api/client';
import { downloadDocumentRaw, getDocument, getDocumentRaw } from '../../../shared/api/client';
import MdImages from '../../../shared/components/common/MdImages';
import AppModal from '../../../shared/components/common/AppModal';
import HeadingOutline from '../../../shared/components/common/HeadingOutline';
import { extractHeadings, type DocHeading } from '../../../shared/utils/docHeadings';

const { Text } = Typography;

/** 文本预览中本地图片引用（images/xxx 相对路径，raw 原文未改写）→ 鉴权代理 URL */
const rewriteRawImageRefs = (text: string, docId: string): string =>
  text.replace(
    /!\[([^\]]*)\]\((?:\.\/)?(images\/[^)]+)\)/g,
    (_m, alt: string, _prefix: string, path: string) => {
      const name = path.split('/').pop() || path;
      return `![${alt}](/api/files/images/${docId}/${name})`;
    },
  );

interface DocumentPreviewModalProps {
  open: boolean;
  /** 待预览文档（null 时弹窗不加载） */
  doc: DocumentItem | null;
  kbId?: string;
  onCancel: () => void;
}

/** 预览类型：pdf=iframe 原生渲染 / text=pre 等宽文本展示 /
 * spreadsheet=类 Excel HTML iframe（后端还原网格/合并/样式，见 spreadsheet_preview）/
 * docx=提供下载（doc 老二进制 Word 同走此分支，后端 raw 亦返回附件下载） */
type PreviewKind = 'pdf' | 'text' | 'spreadsheet' | 'docx' | 'unsupported';

const previewKindOf = (doc: DocumentItem | null): PreviewKind => {
  const ft = (doc?.file_type ?? '').toLowerCase();
  if (ft === 'pdf') return 'pdf';
  if (ft === 'txt' || ft === 'md' || ft === 'url') return 'text';
  if (ft === 'xlsx' || ft === 'xls' || ft === 'csv') return 'spreadsheet';
  if (ft === 'docx' || ft === 'doc') return 'docx';
  return 'unsupported';
};

/**
 * 文档在线预览弹窗（宽 900 / 高 80vh）：
 * - PDF：带鉴权头 fetch 原始字节 → Blob URL → iframe（浏览器原生渲染）
 * - TXT/MD/URL 网页：读取文本内容，pre 等宽字体白底黑字展示（MD 不渲染仅纯文本）
 * - DOCX/DOC：展示**解析后的全文**（data/parsed/{doc_id}.md）——浏览器渲染不了
 *   Word，而解析结果正是入库/切块/检索的依据，排查时对得上；另附"下载原文"入口。
 *   无解析产物（未解析/解析失败）→ 提示 + 下载
 * - 其他类型 / 无权限 / 404 / 超 50MB → Alert 友好提示
 * 关闭时 revoke Blob URL 释放内存。
 */
const DocumentPreviewModal: React.FC<DocumentPreviewModalProps> = ({
  open,
  doc,
  kbId,
  onCancel,
}) => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [text, setText] = useState<string>('');
  /** docx/doc 的解析全文（浏览器渲染不了 Word，改展示解析结果，见 useEffect） */
  const [fullText, setFullText] = useState<string>('');
  /** 解析内容内的关键词搜索（docx 分支用） */
  const [searchText, setSearchText] = useState('');
  const [matchIdx, setMatchIdx] = useState(0);
  const currentMatchRef = useRef<HTMLElement | null>(null);
  const blobUrlRef = useRef<string | null>(null);

  // 每次打开重新加载；关闭时释放 Blob URL 并清空内容（防"标题变内容不变"残留）
  useEffect(() => {
    if (!open || !doc || !kbId) {
      if (blobUrlRef.current) {
        URL.revokeObjectURL(blobUrlRef.current);
        blobUrlRef.current = null;
      }
      setBlobUrl(null);
      setText('');
      return;
    }
    const kind = previewKindOf(doc);
    if (kind === 'unsupported') {
      setError('该文件类型暂不支持在线预览');
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setText('');
    setBlobUrl(null);
    setFullText('');
    setSearchText('');
    // docx/doc：浏览器无法渲染 Word，改为展示**解析后的全文**——入库/切块/检索
    // 都基于这份解析结果，排查"为什么召回不到、切块为什么怪"时对得上；想看原文
    // 排版用下载。解析产物不存在（未解析/解析失败）→ 兜底提示 + 下载入口
    if (kind === 'docx') {
      getDocument(kbId, doc.id)
        .then(res => {
          if (!cancelled) setFullText(res.data.full_text || '');
        })
        .catch(() => {
          if (!cancelled) setError('解析内容加载失败，可下载原文查看');
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
      return () => { cancelled = true; };
    }
    getDocumentRaw(kbId, doc.id)
      .then(blob => {
        if (cancelled) return;
        if (kind === 'pdf') {
          const url = URL.createObjectURL(blob);
          blobUrlRef.current = url;
          setBlobUrl(url);
        } else if (kind === 'spreadsheet') {
          // 类 Excel HTML：注入宿主主题标记（iframe 无主题上下文，
          // 后端 HTML 以 [data-theme] 属性切换亮/暗变量）
          blob.text().then(t => {
            if (cancelled) return;
            const theme = document.documentElement.getAttribute('data-theme') || 'light';
            // 注意: 后端 CSS 也含 "data-theme" 字样,须只检查 <html> 根标签
            const htmlTag = /<html[^>]*>/i.exec(t)?.[0] ?? '';
            const injected = htmlTag && !/data-theme/i.test(htmlTag)
              ? t.replace(htmlTag, `<html data-theme="${theme}"${htmlTag.slice(5)}`)
              : t;
            const url = URL.createObjectURL(new Blob([injected], { type: 'text/html' }));
            blobUrlRef.current = url;
            setBlobUrl(url);
          });
        } else {
          blob.text().then(t => {
            if (!cancelled) setText(t);
          });
        }
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message || '加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      if (blobUrlRef.current) {
        URL.revokeObjectURL(blobUrlRef.current);
        blobUrlRef.current = null;
      }
    };
  }, [open, doc, kbId]);

  // ---------- 解析内容内的关键词搜索（docx 分支） ----------
  // 命中上限：解析产物常见十万字级，全量高亮会拖垮渲染（与 ChunkCompareView
  // 的全文搜索同思路），超出部分不再标注
  const SEARCH_LIMIT = 500;

  /** 命中区间；跳过图片引用内部，免得搜 "http" 命中一堆图片地址 */
  const matches = useMemo<[number, number][]>(() => {
    const kw = searchText.trim().toLowerCase();
    if (!kw || !fullText) return [];
    const skip: [number, number][] = [];
    const imgRe = /!\[[^\]]*\]\([^)]*\)/g;
    let m: RegExpExecArray | null;
    while ((m = imgRe.exec(fullText)) !== null) {
      skip.push([m.index, m.index + m[0].length]);
    }
    const lower = fullText.toLowerCase();
    const out: [number, number][] = [];
    let from = 0;
    while (out.length < SEARCH_LIMIT) {
      const i = lower.indexOf(kw, from);
      if (i < 0) break;
      const end = i + kw.length;
      if (!skip.some(([s, e]) => i < e && end > s)) out.push([i, end]);
      from = i + Math.max(1, kw.length);
    }
    return out;
  }, [fullText, searchText]);

  /** 全文标题（目录与下面的分块共用同一份抽取结果，标题数量/层级与切块详情一致） */
  const headings = useMemo(() => extractHeadings(fullText), [fullText]);

  /**
   * 内容分块：Markdown 标题行独立成块（带锚点 id，供目录跳转），其余文本整块。
   * 只按标题切分——DOM 增量等于标题数（几十个），不会因为一份十万字文档炸出几千节点。
   * start 记录块在全文中的起始偏移，用于把全局命中区间映射到块内。
   */
  const blocks = useMemo(() => {
    const out: { text: string; start: number; heading?: DocHeading }[] = [];
    let last = 0;
    for (const h of headings) {
      if (h.pos > last) out.push({ text: fullText.slice(last, h.pos), start: last });
      out.push({ text: h.raw, start: h.pos, heading: h });
      last = h.end;
    }
    if (last < fullText.length) out.push({ text: fullText.slice(last), start: last });
    return out;
  }, [fullText, headings]);

  /**
   * 把一段文本按命中区间切成 [{text, idx}]（idx<0 = 普通段），供渲染层使用。
   * 全局 matches 的偏移减去块起始偏移即为块内局部偏移；跨块命中（搜索词正好
   * 横跨标题行）不参与高亮——计数仍算，仅不渲染，属罕见情形。
   */
  const splitByMatches = (text: string, start: number) => {
    const local = matches
      .map(([s, e], i) => [s - start, e - start, i] as [number, number, number])
      .filter(([s, e]) => s >= 0 && e <= text.length && s < e);
    if (!local.length) return [{ text, idx: -1 }];
    const out: { text: string; idx: number }[] = [];
    let pos = 0;
    local.forEach(([s, e, gi]) => {
      if (s > pos) out.push({ text: text.slice(pos, s), idx: -1 });
      out.push({ text: text.slice(s, e), idx: gi });
      pos = e;
    });
    if (pos < text.length) out.push({ text: text.slice(pos), idx: -1 });
    return out;
  };

  // 命中集合变化 → 回到第一处
  useEffect(() => {
    setMatchIdx(0);
  }, [matches.length]);

  // 当前命中的滚动定位（Enter / 上下按钮触发）
  useEffect(() => {
    currentMatchRef.current?.scrollIntoView({ block: 'center' });
  }, [matchIdx]);

  const gotoMatch = (delta: number) => {
    if (!matches.length) return;
    setMatchIdx(i => (i + delta + matches.length) % matches.length);
  };

  /** 渲染一个块的各段：命中段统一走 <mark>，普通段交给调用方的 renderPlain */
  const renderBlockSegs = (
    text: string,
    start: number,
    keyPrefix: string,
    renderPlain: (t: string, k: string) => React.ReactNode,
  ) => splitByMatches(text, start).map((seg, i) => (seg.idx < 0
    ? renderPlain(seg.text, `${keyPrefix}-${i}`)
    : (
      <mark
        key={`${keyPrefix}-${i}`}
        ref={seg.idx === matchIdx
          ? (el) => { currentMatchRef.current = el; }
          : undefined}
        style={{
          padding: 0,
          background: seg.idx === matchIdx ? '#ff9632' : '#ffe58f',
          color: 'inherit',
        }}
      >
        {seg.text}
      </mark>
    )));

  /** 目录跳转：标题锚点 id 与标题编号一一对应（doc-h-0、doc-h-1…，见 blocks 渲染） */
  const jumpToHeading = (h: DocHeading) => {
    document.getElementById(`doc-h-${h.index}`)
      ?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  };

  const kind = previewKindOf(doc);
  const isText = kind === 'text' && text !== '';

  /** DOCX 下载（P1-5）：后端 raw 返回附件字节，按 Content-Disposition 文件名保存 */
  const handleDownloadDocx = async () => {
    if (!kbId || !doc) return;
    try {
      await downloadDocumentRaw(kbId, doc.id);
    } catch (e) {
      setError((e as Error).message || '下载失败');
    }
  };

  return (
    <AppModal
      title={doc ? `文档预览 - ${doc.original_name}` : '文档预览'}
      open={open}
      onCancel={onCancel}
      footer={null}
      dimension="resizable"
      defaultSize={{ w: 1200, h: 800 }}
      rememberKey="doc-preview"
      destroyOnClose={false}
    >
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100%' }}>
          <Spin tip="加载中..." size="large">
            <div style={{ width: 200, height: 60 }} />
          </Spin>
        </div>
      ) : error ? (
        <div style={{ padding: 24 }}>
          <Alert type="error" showIcon message="预览失败" description={error} />
        </div>
      ) : blobUrl ? (
        <iframe
          key={`${doc?.id ?? ''}-${blobUrl}`}
          title={doc?.original_name}
          src={blobUrl}
          style={{ width: '100%', height: '100%', border: 0 }}
        />
      ) : isText ? (
        <pre
          style={{
            margin: 0,
            height: '100%',
            overflow: 'auto',
            padding: 16,
            fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
            fontSize: 13,
            lineHeight: 1.7,
            color: 'inherit',
            background: 'transparent',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {/* MD 文件：raw 文本中本地图片引用改写为鉴权代理 URL 并渲染为真实图片 */}
          {doc && ['md', 'markdown'].includes((doc.file_type ?? '').toLowerCase())
            ? <MdImages text={rewriteRawImageRefs(text, doc.id)} maxWidth={720} />
            : text}
        </pre>
      ) : kind === 'docx' ? (
        <div style={{ padding: 16 }}>
          {fullText ? (
            <>
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 10 }}
                message={
                  <span>
                    以下为文档解析后的内容
                    <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                      （入库、切块与检索都基于这份解析结果，与原文排版可能有差异；需要原文排版请下载）
                    </Text>
                  </span>
                }
                action={
                  <Button size="small" icon={<DownloadOutlined />} onClick={handleDownloadDocx}>
                    下载原文
                  </Button>
                }
              />
              {/* 内容内搜索：Enter 下一个 / Shift+Enter 上一个（与切块对比页同一手感）。
                  整条靠右对齐，与上方 Alert 的「下载原文」按钮同侧 */}
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  marginBottom: 8,
                  justifyContent: 'flex-end',
                }}
              >
                <Input
                  size="small"
                  allowClear
                  prefix={<SearchOutlined style={{ color: 'rgba(0, 0, 0, 0.35)' }} />}
                  placeholder="在解析内容中搜索"
                  style={{ width: 240 }}
                  value={searchText}
                  onChange={e => setSearchText(e.target.value)}
                  onPressEnter={e => gotoMatch(e.shiftKey ? -1 : 1)}
                />
                {searchText.trim() !== '' && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {matches.length ? `${matchIdx + 1} / ${matches.length}` : '无匹配'}
                  </Text>
                )}
                <Button
                  size="small"
                  icon={<UpOutlined />}
                  disabled={!matches.length}
                  onClick={() => gotoMatch(-1)}
                />
                <Button
                  size="small"
                  icon={<DownOutlined />}
                  disabled={!matches.length}
                  onClick={() => gotoMatch(1)}
                />
                {/* 目录：标题树（默认展开一级 = 可见前两级，三级及以下点箭头展开），
                    点击标题滚动到对应位置；放在整条工具栏最右侧（与切块详情同款组件） */}
                <HeadingOutline
                  headings={headings}
                  onJump={jumpToHeading}
                  resetKey={doc?.id}
                />
              </div>
              {/* 必须 pre-wrap：解析产物是 Markdown 纯文本，而 MdImages 输出的是
                  HTML 片段，浏览器默认折叠空白——不加这段换行会全挤成一坨
                  （MD 文件预览用 <pre> 包裹正是同一原因） */}
              <div
                style={{
                  maxHeight: '62vh',
                  overflowY: 'auto',
                  paddingRight: 4,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  lineHeight: 1.7,
                }}
              >
                {blocks.map((b, bi) => (b.heading ? (
                  <span
                    key={`h${bi}`}
                    id={`doc-h-${b.heading.index}`}
                    style={{ scrollMarginTop: 12 }}
                  >
                    {/* key 必须给：renderBlockSegs 返回的是数组，无搜索词时也走数组渲染 */}
                    {renderBlockSegs(b.text, b.start, `h${bi}`, (t, k) => <span key={k}>{t}</span>)}
                  </span>
                ) : renderBlockSegs(b.text, b.start, `t${bi}`,
                  (t, k) => <MdImages key={k} text={t} maxWidth={720} />)))}
              </div>
            </>
          ) : (
            <Alert
              type="warning"
              showIcon
              message="暂无可预览的内容"
              description="该文档尚未解析或解析失败，没有解析产物可展示；可下载原文查看。"
              action={
                <Button
                  size="small"
                  type="primary"
                  icon={<DownloadOutlined />}
                  onClick={handleDownloadDocx}
                >
                  下载文件
                </Button>
              }
            />
          )}
        </div>
      ) : kind === 'unsupported' ? (
        <div style={{ padding: 24 }}>
          <Alert type="warning" showIcon message="暂不支持预览" description="该文件类型暂不支持在线预览。" />
        </div>
      ) : (
        <div style={{ padding: 24 }}>
          <Text type="secondary">暂无内容</Text>
        </div>
      )}
    </AppModal>
  );
};

export default DocumentPreviewModal;
