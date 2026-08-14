import React, { useEffect, useState } from 'react';
import {
  Table, Button, Upload, Card, Space, Typography, message, Modal,
  Descriptions, Tag, Empty, Popconfirm,
} from 'antd';
import { UploadOutlined, DeleteOutlined, EyeOutlined, InboxOutlined } from '@ant-design/icons';
import type { UploadFile } from 'antd/es/upload';
import { listDatasets, uploadDataset, getDataset, deleteDataset, DatasetListItem, DatasetPreview } from '../api/client';

const { Dragger } = Upload;
const { Text } = Typography;

const DatasetsPage: React.FC = () => {
  const [datasets, setDatasets] = useState<DatasetListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [preview, setPreview] = useState<DatasetPreview | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const res = await listDatasets();
      setDatasets(res.data);
    } catch (e: any) {
      message.error('加载数据集列表失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleUpload = async (file: File) => {
    setUploading(true);
    try {
      const res = await uploadDataset(file);
      message.success(res.data.message);
      await load();
    } catch (e: any) {
      message.error(e.response?.data?.detail || '上传失败');
    } finally {
      setUploading(false);
    }
    return false;
  };

  const handlePreview = async (id: string) => {
    try {
      const res = await getDataset(id, 20);
      setPreview(res.data);
      setPreviewOpen(true);
    } catch {
      message.error('加载预览失败');
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteDataset(id);
      message.success('已删除');
      await load();
    } catch {
      message.error('删除失败');
    }
  };

  const columns = [
    { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true },
    { title: '文件', dataIndex: 'file_name', key: 'file_name', width: 180 },
    { title: '记录数', dataIndex: 'row_count', key: 'row_count', width: 90 },
    {
      title: '列', dataIndex: 'columns', key: 'columns', width: 200,
      render: (cols: string[]) => cols.map(c => <Tag key={c}>{c}</Tag>),
    },
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 170 },
    {
      title: '操作', key: 'actions', width: 140,
      render: (_: any, row: DatasetListItem) => (
        <Space>
          <Button size="small" icon={<EyeOutlined />} onClick={() => handlePreview(row.id)}>预览</Button>
          <Popconfirm title="确定删除?" onConfirm={() => handleDelete(row.id)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Typography.Title level={4}>数据集管理</Typography.Title>

      <Card title="上传数据集" style={{ marginBottom: 24 }}>
        <Dragger
          accept=".csv,.json,.xlsx,.xls,.tsv"
          multiple={false}
          showUploadList={false}
          beforeUpload={handleUpload}
          disabled={uploading}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">点击或拖拽文件到此区域上传</p>
          <p className="ant-upload-hint">支持 CSV、JSON、Excel (.xlsx) 格式，需包含 question 列</p>
        </Dragger>
      </Card>

      <Card title="数据集列表">
        <Table
          dataSource={datasets}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={{ pageSize: 10 }}
          locale={{ emptyText: <Empty description="暂无数据集，请上传" /> }}
        />
      </Card>

      <Modal
        title="数据预览"
        open={previewOpen}
        onCancel={() => setPreviewOpen(false)}
        footer={null}
        width={800}
      >
        {preview && (
          <>
            <Descriptions size="small" column={2} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="总记录数">{preview.total}</Descriptions.Item>
              <Descriptions.Item label="列">{preview.columns.join(', ')}</Descriptions.Item>
            </Descriptions>
            <Table
              dataSource={preview.rows.map((r, i) => ({ ...r, _key: i }))}
              columns={preview.columns.map(c => ({
                title: c, dataIndex: c, key: c, ellipsis: true,
                render: (v: any) => typeof v === 'string' ? (v.length > 100 ? v.slice(0, 100) + '...' : v) : JSON.stringify(v),
              }))}
              rowKey="_key"
              size="small"
              scroll={{ x: 'max-content' }}
              pagination={false}
            />
          </>
        )}
      </Modal>
    </div>
  );
};

export default DatasetsPage;
