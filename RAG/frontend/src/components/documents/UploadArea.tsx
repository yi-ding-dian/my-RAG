import React, { useEffect, useRef, useState } from 'react';
import { App as AntApp, Button, Spin, Upload, theme } from 'antd';
import {
  FileAddOutlined,
  GlobalOutlined,
  InboxOutlined,
} from '@ant-design/icons';
import { asApiError, uploadDocument } from '../../api/client';

const { Dragger } = Upload;

interface UploadAreaProps {
  kbId?: string;
  canManage: boolean;
  onOpenBatchImport: () => void;
  onOpenUrlImport: () => void;
  /** 上传完成后的刷新（回第 1 页重拉，新文档在列表顶部） */
  onUploaded: () => Promise<void>;
}

/**
 * 上传条：列表卡片顶部的内嵌窄条（点击/拖拽上传 + 批量导入并解析 + 从 URL 导入）。
 * 自包含上传聚合逻辑：multiple 拖拽时 antd 逐个回调 beforeUpload，先聚合成批
 * （30ms 窗口）再统一并发上传（同目录 flushUploads 原逻辑整体移入）。
 */
const UploadArea: React.FC<UploadAreaProps> = ({
  kbId,
  canManage,
  onOpenBatchImport,
  onOpenUrlImport,
  onUploaded,
}) => {
  const { message, modal } = AntApp.useApp();
  const { token } = theme.useToken();
  // 批量上传状态：uploading=本批进行中；total/done=第 done+1 个；current=当前文件名
  const [uploadState, setUploadState] = useState<{
    uploading: boolean;
    total: number;
    done: number;
    current: string;
  }>({ uploading: false, total: 0, done: 0, current: '' });
  // multiple 拖拽时 antd 逐个回调 beforeUpload，先聚合成批再统一并发上传
  const pendingFilesRef = useRef<File[]>([]);
  const uploadTimerRef = useRef<number | null>(null);

  /** 并发池：以 concurrency 上限执行 fn（fn 内部已捕获异常，不会中断整池） */
  const runPool = async <T,>(
    items: T[],
    concurrency: number,
    fn: (item: T) => Promise<void>,
  ) => {
    const queue = [...items];
    const workers = Array.from(
      { length: Math.min(concurrency, queue.length) },
      async () => {
        while (queue.length > 0) {
          const item = queue.shift()!;
          await fn(item);
        }
      },
    );
    await Promise.all(workers);
  };

  const flushUploads = async () => {
    if (uploadTimerRef.current) {
      window.clearTimeout(uploadTimerRef.current);
      uploadTimerRef.current = null;
    }
    const files = pendingFilesRef.current.splice(0);
    if (files.length === 0) return;
    if (!kbId) {
      // 上传期间切换了知识库：丢弃滞留文件，避免传到错误的知识库
      pendingFilesRef.current = [];
      return;
    }
    setUploadState({ uploading: true, total: files.length, done: 0, current: '' });
    const failed: string[] = [];
    await runPool(files, 3, async file => {
      setUploadState(s => ({ ...s, current: file.name }));
      try {
        await uploadDocument(kbId, file);
      } catch (e: unknown) {
        // 同名文档检测：409 + detail 含"同名" → 确认后带 force=true 重传
        const detail = asApiError(e).response?.data?.detail;
        if (asApiError(e).response?.status === 409 && typeof detail === 'string' && detail.includes('同名')) {
          await new Promise<void>(resolve => {
            modal.confirm({
              title: '知识库中已存在同名文档',
              content: `知识库中已存在同名文档「${file.name}」，是否继续上传？`,
              okText: '继续上传',
              cancelText: '取消',
              onOk: async () => {
                try {
                  await uploadDocument(kbId, file, true);
                  message.success(`已继续上传「${file.name}」`);
                } catch (e2: unknown) {
                  failed.push(`${file.name}（${asApiError(e2).response?.data?.detail || '重传失败'}）`);
                  message.error(`继续上传「${file.name}」失败`);
                }
              },
              onCancel: () => {
                failed.push(`${file.name}（已取消：知识库已存在同名文档）`);
                message.info(`已取消上传「${file.name}」`);
              },
              afterClose: resolve,
            });
          });
          setUploadState(s => ({ ...s, done: s.done + 1 }));
          return;
        }
        failed.push(`${file.name}（${detail || '上传失败'}）`);
      }
      setUploadState(s => ({ ...s, done: s.done + 1 }));
    });
    setUploadState(s => ({ ...s, uploading: false, current: '' }));
    const ok = files.length - failed.length;
    if (failed.length === 0) {
      // 上传只上传不解析：由用户在文档列表手动选择解析方式后触发
      message.success(`批量上传完成：成功 ${ok} 个，请选择解析方式后点击解析`);
    } else {
      message.warning(`上传完成：成功 ${ok} 个，失败 ${failed.length} 个：${failed.join('、')}`);
    }
    // 上传后回第 1 页（新文档在列表顶部，避免停留在旧页码看不到新内容）
    await onUploaded();
  };

  const handleUpload = (file: File) => {
    if (!kbId) {
      message.warning('请先选择知识库');
      return false;
    }
    // L4: 拖拽/点击上传的类型预校验（Dragger accept 仅过滤文件选择器，拖拽不拦截）
    const dot = file.name.lastIndexOf('.');
    const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : '';
    if (!['.txt', '.md', '.pdf', '.docx'].includes(ext)) {
      message.warning(`不支持的文件类型：${file.name}（仅支持 .txt/.md/.pdf/.docx）`);
      return false;
    }
    // multiple 时 antd 逐个回调 beforeUpload，先聚合成批（30ms 窗口）再统一并发上传
    pendingFilesRef.current.push(file);
    if (!uploadTimerRef.current) {
      uploadTimerRef.current = window.setTimeout(() => {
        void flushUploads();
      }, 30);
    }
    return false;
  };

  // 组件卸载时清理批量上传聚合定时器
  useEffect(() => {
    return () => {
      if (uploadTimerRef.current) window.clearTimeout(uploadTimerRef.current);
    };
  }, []);

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'stretch', gap: 12, marginBottom: 12 }}>
        {canManage ? (
          <Dragger
            className="upload-zone upload-zone--inline"
            accept=".txt,.md,.pdf,.docx"
            multiple={true}
            showUploadList={false}
            beforeUpload={file => handleUpload(file)}
            disabled={uploadState.uploading || !kbId}
          >
            <div className="upload-inline__content">
              <InboxOutlined style={{ fontSize: 16, color: token.colorPrimary }} />
              <span>点击或拖拽文件到此处上传，支持 .txt/.md/.pdf/.docx</span>
            </div>
          </Dragger>
        ) : (
          <div className="upload-inline__denied">
            <InboxOutlined style={{ fontSize: 14 }} />
            <span>普通用户仅可查看与问答，如需上传请联系部门管理员</span>
          </div>
        )}
        {canManage && (
          <Button icon={<FileAddOutlined />} onClick={onOpenBatchImport}>
            批量导入并解析
          </Button>
        )}
        {canManage && (
          <Button icon={<GlobalOutlined />} onClick={onOpenUrlImport}>
            从 URL 导入
          </Button>
        )}
      </div>
      {uploadState.uploading && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginBottom: 12,
            color: token.colorTextSecondary,
            fontSize: 13,
          }}
        >
          <Spin size="small" />
          <span>
            上传中：第 {uploadState.done + 1}/{uploadState.total} 个
            {uploadState.current ? `（${uploadState.current}）` : ''}
          </span>
        </div>
      )}
    </>
  );
};

export default UploadArea;
