import React, { useEffect, useRef, useState } from 'react';
import { App as AntApp, Button, Spin, Typography, Upload, theme } from 'antd';
import {
  FileAddOutlined,
  GlobalOutlined,
  InboxOutlined,
} from '@ant-design/icons';
import { asApiError, uploadDocument } from '../../../shared/api/client';
import AppModal, { AppModalFooter } from '../../../shared/components/common/AppModal';

const { Dragger } = Upload;
const { Text } = Typography;

/** 上传类型白名单（与后端 SUPPORTED_EXTS 保持一致） */
const ACCEPT_EXTS = ['.txt', '.md', '.pdf', '.docx', '.doc',
                     '.xlsx', '.xls', '.csv', '.ppt', '.pptx'];

/** **上传时**就会经文档转换服务（Gotenberg）转成 PDF 的格式（原文件不保留）
 *  —— 它们直接进解析链路会失败或丢标题层级，故在门口就统一成 PDF */
const UPLOAD_CONVERT_EXTS = ['.ppt', '.pptx'];

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
  /** 待确认转换的 ppt/pptx（统一弹窗确认后才入队上传） */
  const [pendingConvert, setPendingConvert] = useState<File | null>(null);
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
    // 类型白名单与后端 SUPPORTED_EXTS 保持一致
    const dot = file.name.lastIndexOf('.');
    const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : '';
    if (!ACCEPT_EXTS.includes(ext)) {
      message.warning(
        `不支持的文件类型：${file.name}（仅支持 ${ACCEPT_EXTS.join('/')}）`);
      return false;
    }
    // ppt/pptx：**上传时**就会经转换服务转成 PDF（原文件不保留），先让用户确认
    // —— 用项目统一的 AppModal（见 docs/弹窗规范(AppModal).md）
    if (UPLOAD_CONVERT_EXTS.includes(ext)) {
      setPendingConvert(file);
      return false;
    }
    // .doc 视解析引擎而定（选 MinerU 时也会先转 PDF），提示留有余地
    if (ext === '.doc') {
      message.info(
        `${file.name}：.doc 在部分解析方式下会先转成 PDF 再解析`, 6);
    }
    enqueue(file);
    return false;
  };

  /** 入队并触发聚合上传（multiple 时 antd 逐个回调 beforeUpload，先聚合成批
   *  （30ms 窗口）再统一并发上传） */
  const enqueue = (file: File) => {
    pendingFilesRef.current.push(file);
    if (!uploadTimerRef.current) {
      uploadTimerRef.current = window.setTimeout(() => {
        void flushUploads();
      }, 30);
    }
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
            accept={ACCEPT_EXTS.join(',')}
            multiple={true}
            showUploadList={false}
            beforeUpload={file => handleUpload(file)}
            disabled={uploadState.uploading || !kbId}
          >
            <div className="upload-inline__content">
              <InboxOutlined style={{ fontSize: 16, color: token.colorPrimary }} />
              <span>点击或拖拽文件到此处上传，支持 {ACCEPT_EXTS.join('/')}</span>
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

      {/* ppt/pptx 转换确认：用项目统一弹窗（见 docs/弹窗规范(AppModal).md） */}
      <AppModal
        title="该文件将先转换为 PDF"
        open={!!pendingConvert}
        dimension="auto"
        defaultSize={{ w: 520, h: 360 }}
        // 内容只有几行，放宽最小高度下限（组件注释里明确支持"内容很少的小弹窗"）
        minSize={{ w: 400, h: 180 }}
        onCancel={() => setPendingConvert(null)}
        footer={
          <AppModalFooter
            okText="转换并上传"
            onOk={() => {
              const f = pendingConvert;
              setPendingConvert(null);
              if (f) enqueue(f);
            }}
            onCancel={() => setPendingConvert(null)}
          />
        }
      >
        <div style={{ lineHeight: 1.8 }}>
          <b>{pendingConvert?.name}</b>
          <br />
          上传时会先通过文档转换服务（Gotenberg）转成 PDF，
          之后按 PDF 保存、解析与检索。
          <br />
          <Text type="secondary">原文件不保留。</Text>
        </div>
      </AppModal>
    </>
  );
};

export default UploadArea;
