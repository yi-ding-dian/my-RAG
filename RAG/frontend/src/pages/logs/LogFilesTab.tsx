import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Card, List, Space, Spin, Switch, Table, Tooltip, Typography } from 'antd';
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  DownloadOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import {
  LogFileInfo,
  LogLine,
  asApiError,
  deleteAllLogFiles,
  deleteLogFile,
  downloadLogFile,
  listLogFiles,
  tailSystemLogs,
} from '../../api/client';
import AppEmpty from '../../components/common/AppEmpty';
import ResizableTitle from '../../components/common/ResizableTitle';
import LeftPagination from '../../components/layout/LeftPagination';
import { useResizableColumns } from '../../hooks/useResizableColumns';
import { DETAIL_LIMIT, POLL_INTERVAL, formatBytes, renderLogItem } from './shared';
import type { AppInstance } from './shared';

const { Text } = Typography;

// ==================== Tab2：系统日志文件（文件表格 + 分页） ====================

/**
 * 日志文件 Tab（仅 super_admin）：只放文件本身
 * - 表格：日期 / 文件（点击查看内容）/ 大小 / 日志条数 / 修改时间 / 操作
 *   （下载、删除）；行数大文件为采样估算（显示「约 N 行」并悬浮说明）
 * - 5s 轮询文件列表（大小/行数随写入变化；「自动刷新」开关默认开）
 * - 右上角合计占用 + 清空全部（二次确认）；底部统一 LeftPagination（前端分页）
 */
const LogFilesTab: React.FC<{ app: AppInstance; active: boolean }> = ({ app, active }) => {
  const { message, modal } = app;

  const [files, setFiles] = useState<LogFileInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  // 前端分页（文件数不多，一次拉全量本地分页；分页条固定左下角不随表格滚动）
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  // 详情视图：点击文件名进入，展示该天日志内容（tail 尾部模式最近 DETAIL_LIMIT 行）
  const [detailFile, setDetailFile] = useState<LogFileInfo | null>(null);
  const [detailLines, setDetailLines] = useState<LogLine[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  // 列宽拖拽（与 Users.tsx 同一套 hook/组件）
  const { colWidths, handleResize, tableWidth } = useResizableColumns<LogFileInfo>();

  const loadFiles = useCallback(async (silent = true) => {
    if (!silent) setLoading(true);
    try {
      const res = await listLogFiles();
      setFiles(res.data.files);
    } catch {
      if (!silent) message.error('加载日志文件列表失败');
    } finally {
      if (!silent) setLoading(false);
    }
  }, [message]);

  // 挂载：首次加载（非静默，显示 loading）
  useEffect(() => {
    void loadFiles(false);
  }, [loadFiles]);

  // 5s 轮询刷新文件列表（大小/条数随写入变化）；切走本 Tab 暂停，切回立即续拉
  const loadFilesRef = useRef(loadFiles);
  useEffect(() => { loadFilesRef.current = loadFiles; }, [loadFiles]);
  useEffect(() => {
    if (!autoRefresh || !active) return;
    void loadFilesRef.current(true);
    const timer = setInterval(() => { void loadFilesRef.current(true); }, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [autoRefresh, active]);

  // 删除文件后当前页可能越界（如第 2 页只剩 1 条）→ 页码回收
  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(files.length / pageSize));
    if (page > maxPage) setPage(maxPage);
  }, [files.length, page, pageSize]);

  // 删除指定天日志文件（删除前二次确认）
  const handleDeleteFile = (date: string) => {
    modal.confirm({
      title: `删除 ${date} 的日志文件？`,
      content: '删除后该天运行日志不可恢复。',
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await deleteLogFile(date);
          message.success(res.data.message);
          await loadFiles(false);
        } catch (e: unknown) {
          message.error(asApiError(e).response?.data?.detail || '删除失败');
        }
      },
    });
  };

  // 清空全部日志文件（删除前二次确认）
  const handleClearAll = () => {
    modal.confirm({
      title: '清空全部运行日志？',
      content: '将删除所有历史日志文件（今天的日志清空后继续写入），删除后不可恢复。',
      okText: '确认清空',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await deleteAllLogFiles();
          message.success(res.data.message);
          await loadFiles(false);
        } catch (e: unknown) {
          message.error(asApiError(e).response?.data?.detail || '删除失败');
        }
      },
    });
  };

  // 下载日志文件：fetch 带鉴权头取 Blob → 本地触发浏览器下载
  const handleDownload = async (file: LogFileInfo) => {
    try {
      const blob = await downloadLogFile(file.date);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = file.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      message.success(`已开始下载 ${file.filename}`);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || asApiError(e).message || '下载失败');
    }
  };

  // 点击文件名 → 详情视图：tail 尾部模式一次拉取该天最近 DETAIL_LIMIT 行
  // （满额即视为截断，提示下载完整文件）
  const loadDetail = useCallback(async (file: LogFileInfo) => {
    setDetailFile(file);
    setDetailLines([]);
    setDetailLoading(true);
    try {
      const res = await tailSystemLogs(file.date, -1, DETAIL_LIMIT);
      setDetailLines(res.data.lines);
    } catch {
      message.error('加载日志文件内容失败');
    } finally {
      setDetailLoading(false);
    }
  }, [message]);

  // 返回文件列表（详情数据清空，列表侧轮询数据不受影响）
  const backToList = () => {
    setDetailFile(null);
    setDetailLines([]);
  };

  const totalBytes = useMemo(
    () => files.reduce((s, f) => s + f.size_bytes, 0),
    [files],
  );
  // 前端分页：当前页数据
  const pageFiles = useMemo(
    () => files.slice((page - 1) * pageSize, page * pageSize),
    [files, page, pageSize],
  );

  const columns = useMemo<ColumnsType<LogFileInfo>>(() => [
    {
      title: '日期',
      dataIndex: 'date',
      key: 'date',
      width: colWidths.date ?? 120,
      onHeaderCell: () => ({
        width: colWidths.date ?? 120,
        onResize: handleResize('date'),
        title: '日期',
      }),
    },
    {
      title: '文件',
      dataIndex: 'filename',
      key: 'filename',
      width: colWidths.filename ?? 240,
      onHeaderCell: () => ({
        width: colWidths.filename ?? 240,
        onResize: handleResize('filename'),
        title: '文件',
      }),
      ellipsis: true,
      // 点击文件名 → 详情视图查看该天日志内容
      render: (v: string, row: LogFileInfo) => (
        <Typography.Link onClick={() => void loadDetail(row)}>{v}</Typography.Link>
      ),
    },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      key: 'size_bytes',
      width: colWidths.size_bytes ?? 110,
      onHeaderCell: () => ({
        width: colWidths.size_bytes ?? 110,
        onResize: handleResize('size_bytes'),
        title: '大小',
      }),
      render: (v: number) => formatBytes(v),
    },
    {
      title: '日志条数',
      dataIndex: 'line_count',
      key: 'line_count',
      width: colWidths.line_count ?? 130,
      onHeaderCell: () => ({
        width: colWidths.line_count ?? 130,
        onResize: handleResize('line_count'),
        title: '日志条数',
      }),
      align: 'right',
      // 大文件条数为采样估算（后端 line_count_estimated），显式标「约」防误读；
      // 字段缺失（后端旧版本）降级显示 '-'，不让整表渲染失败
      render: (v: number | undefined, row: LogFileInfo) => (
        v == null ? (
          <Text type="secondary">-</Text>
        ) : row.line_count_estimated ? (
          <Tooltip title="文件较大，条数为采样估算值（非精确计数）">
            <Text type="secondary">约 {v.toLocaleString()} 行</Text>
          </Tooltip>
        ) : (
          <Text>{v.toLocaleString()} 行</Text>
        )
      ),
    },
    {
      title: '修改时间',
      dataIndex: 'mtime',
      key: 'mtime',
      width: colWidths.mtime ?? 180,
      onHeaderCell: () => ({
        width: colWidths.mtime ?? 180,
        onResize: handleResize('mtime'),
        title: '修改时间',
      }),
      render: (v: string) => (v ? dayjs(v).format('YYYY-MM-DD HH:mm:ss') : '-'),
    },
    {
      title: '操作',
      key: 'actions',
      // 最后一列不提供拖拽手柄（避免拖出表格边界）
      width: 150,
      render: (_, row) => (
        <Space size={8}>
          <Button size="small" icon={<DownloadOutlined />} onClick={() => void handleDownload(row)}>
            下载
          </Button>
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => handleDeleteFile(row.date)}>
            删除
          </Button>
        </Space>
      ),
    },
  ], [colWidths, handleResize, loadDetail]);

  // ===== 详情视图：查看单天日志文件内容（正序时间从上到下） =====
  if (detailFile) {
    const file = detailFile; // 局部 const：闭包内保留非空窄化
    // tail 返回最近 DETAIL_LIMIT 行，拉满即视为文件更长被截断
    const truncated = detailLines.length >= DETAIL_LIMIT;
    return (
      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {/* 页头：返回 + 文件名 + 大小/行数（固定不随内容滚动） */}
        <Space wrap style={{ marginBottom: 16, flexShrink: 0 }} size={12}>
          <Button icon={<ArrowLeftOutlined />} onClick={backToList}>返回</Button>
          <Text strong style={{ fontSize: 14 }}>{file.filename}</Text>
          <Text type="secondary">大小 {formatBytes(file.size_bytes)}</Text>
          {typeof file.line_count === 'number' && (
            <Text type="secondary">
              {file.line_count_estimated ? '约 ' : ''}{file.line_count.toLocaleString()} 行
            </Text>
          )}
          <Text type="secondary">修改于 {dayjs(file.mtime).format('YYYY-MM-DD HH:mm:ss')}</Text>
          {truncated && (
            <Text type="secondary">
              该文件超过 {DETAIL_LIMIT} 行，仅显示最近 {DETAIL_LIMIT} 行，完整内容可点击「下载」
            </Text>
          )}
          <Button
            size="small"
            type="primary"
            icon={<DownloadOutlined />}
            onClick={() => void handleDownload(file)}
          >
            下载
          </Button>
        </Space>
        {/* 内容区：flex 撑满剩余高度，滚动条在内部（与列表视图一致） */}
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          {detailLoading ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '48px 0' }}>
              <Spin size="large" />
            </div>
          ) : (
            <List
              size="small"
              dataSource={detailLines}
              locale={{
                emptyText: (
                  <AppEmpty
                    title="暂无日志内容"
                    description={`${file.date} 无日志内容`}
                  />
                ),
              }}
              renderItem={renderLogItem}
            />
          )}
        </div>
      </div>
    );
  }

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <Space wrap style={{ marginBottom: 16, flexShrink: 0 }}>
        <Switch
          size="small"
          checked={autoRefresh}
          onChange={setAutoRefresh}
          checkedChildren="自动刷新"
          unCheckedChildren="已暂停"
        />
        <Button icon={<ReloadOutlined />} onClick={() => void loadFiles(false)}>刷新</Button>
        <Text type="secondary">
          日志按天轮转落盘，后端保留 14 天；点击文件名可查看该天日志内容
        </Text>
      </Space>

      <Card
        size="small"
        title="日志文件"
        style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}
        styles={{ body: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' } }}
        extra={
          <Space size={12}>
            <Text type="secondary">
              共 {files.length} 个文件，合计占用 {formatBytes(totalBytes)}
            </Text>
            <Button size="small" danger icon={<DeleteOutlined />} onClick={handleClearAll}>
              清空全部
            </Button>
          </Space>
        }
      >
        {/* Table 区：flex:1 撑满剩余，内部滚动，分页条固定左下不动 */}
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          <Table
            size="middle"
            rowKey="filename"
            columns={columns}
            dataSource={pageFiles}
            loading={loading}
            pagination={false}
            components={{ header: { cell: ResizableTitle } }}
            scroll={{ x: tableWidth(columns) }}
            sticky
            className="table-zebra"
            locale={{
              emptyText: <AppEmpty title="暂无日志文件" description="data/logs 下暂无按天日志文件" />,
            }}
          />
        </div>
        {/* 分页条：统一 LeftPagination（共 N 条 + 前端分页，固定左下） */}
        <LeftPagination
          page={page}
          pageSize={pageSize}
          total={files.length}
          onChange={(p, ps) => { setPage(p); setPageSize(ps); }}
        />
      </Card>
    </div>
  );
};

export default LogFilesTab;
