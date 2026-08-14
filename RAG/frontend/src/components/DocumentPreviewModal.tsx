import React, { useEffect, useRef, useState } from 'react';
import { Alert, Button, Modal, Spin, Typography } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import type { DocumentItem } from '../api/client';
import { downloadDocumentRaw, getDocumentRaw } from '../api/client';
import MdImages from './MdImages';

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

/** 预览类型：pdf=iframe 原生渲染 / text=pre 等宽文本展示 / docx=提供下载 */
type PreviewKind = 'pdf' | 'text' | 'docx' | 'unsupported';

const previewKindOf = (doc: DocumentItem | null): PreviewKind => {
  const ft = (doc?.file_type ?? '').toLowerCase();
  if (ft === 'pdf') return 'pdf';
  if (ft === 'txt' || ft === 'md' || ft === 'url') return 'text';
  if (ft === 'docx') return 'docx';
  return 'unsupported';
};

/**
 * 文档在线预览弹窗（宽 900 / 高 80vh）：
 * - PDF：带鉴权头 fetch 原始字节 → Blob URL → iframe（浏览器原生渲染）
 * - TXT/MD/URL 网页：读取文本内容，pre 等宽字体白底黑字展示（MD 不渲染仅纯文本）
 * - DOCX：P1-5 不支持在线预览，提示"可下载后查看"并提供下载按钮（后端 raw
 *   接口对 docx 返回附件下载，文件名取 original_name）
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
  const blobUrlRef = useRef<string | null>(null);

  // 每次打开重新加载；关闭时释放 Blob URL
  useEffect(() => {
    if (!open || !doc || !kbId) return;
    const kind = previewKindOf(doc);
    if (kind === 'unsupported' || kind === 'docx') {
      // unsupported/docx 不请求 raw（docx 走下载分支，不设 error）
      setError(kind === 'unsupported' ? '该文件类型暂不支持在线预览' : null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setText('');
    setBlobUrl(null);
    getDocumentRaw(kbId, doc.id)
      .then(blob => {
        if (cancelled) return;
        if (kind === 'pdf') {
          const url = URL.createObjectURL(blob);
          blobUrlRef.current = url;
          setBlobUrl(url);
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
    <Modal
      title={doc ? `文档预览 - ${doc.original_name}` : '文档预览'}
      open={open}
      onCancel={onCancel}
      footer={null}
      width={900}
      styles={{ body: { height: '80vh', padding: 0, overflow: 'hidden' } }}
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
        <div style={{ padding: 24 }}>
          <Alert
            type="warning"
            showIcon
            message="暂不支持预览"
            description="该格式暂不支持在线预览，可下载后查看。"
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
    </Modal>
  );
};

export default DocumentPreviewModal;
