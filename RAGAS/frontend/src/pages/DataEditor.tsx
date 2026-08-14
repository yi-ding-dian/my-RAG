import React, { useEffect, useState } from 'react';
import {
  Card, Form, Input, Button, Table, Space, Typography, message, Tag,
  Select, Modal, Popconfirm, Row, Col, Empty, Tooltip, Divider,
} from 'antd';
import {
  PlusOutlined, EditOutlined, DeleteOutlined, DownloadOutlined,
  EyeOutlined, InboxOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import {
  listDatasets, getDataset, createDataset, addSample, updateSample, deleteSample,
  deleteDataset, DatasetListItem, DatasetPreview,
} from '../api/client';

const { TextArea } = Input;
const { Text, Title } = Typography;

interface SampleFormData {
  question: string;
  answer: string;
  contexts: string;
  ground_truth: string;
}

const emptyForm: SampleFormData = { question: '', answer: '', contexts: '', ground_truth: '' };

const validateForm = (d: SampleFormData): string | null => {
  if (!d.question.trim()) return '问题不能为空';
  return null;
};

const sampleToForm = (s: any): SampleFormData => ({
  question: s.question || '',
  answer: s.answer || '',
  contexts: Array.isArray(s.contexts) ? s.contexts.join('\n') : (s.contexts || ''),
  ground_truth: s.ground_truth || '',
});

const DataEditorPage: React.FC = () => {
  const navigate = useNavigate();
  const [datasets, setDatasets] = useState<DatasetListItem[]>([]);
  const [loading, setLoading] = useState(false);

  // 当前编辑的数据集
  const [currentDsId, setCurrentDsId] = useState<string | null>(null);
  const [currentDsName, setCurrentDsName] = useState('');
  const [preview, setPreview] = useState<DatasetPreview | null>(null);
  const [rows, setRows] = useState<any[]>([]);

  // 新建弹窗
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);

  // 添加/编辑表单
  const [formOpen, setFormOpen] = useState(false);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [formData, setFormData] = useState<SampleFormData>(emptyForm);
  const [saving, setSaving] = useState(false);

  // 预览弹窗
  const [viewOpen, setViewOpen] = useState(false);
  const [viewData, setViewData] = useState<DatasetPreview | null>(null);

  const loadDatasets = async () => {
    setLoading(true);
    try {
      const res = await listDatasets();
      setDatasets(res.data);
    } catch { /* ignore */ }
    finally { setLoading(false); }
  };

  useEffect(() => { loadDatasets(); }, []);

  const loadPreview = async (id: string) => {
    try {
      const res = await getDataset(id, 200);
      setPreview(res.data);
      setRows(res.data.rows);
    } catch {
      message.error('加载数据失败');
    }
  };

  // 切换数据集
  const handleSelect = (id: string) => {
    setCurrentDsId(id);
    const ds = datasets.find(d => d.id === id);
    if (ds) setCurrentDsName(ds.name);
    loadPreview(id);
  };

  // 创建数据集
  const handleCreate = async () => {
    if (!newName.trim()) { message.warning('请输入数据集名称'); return; }
    setCreating(true);
    try {
      const res = await createDataset(newName.trim());
      message.success(res.data.message);
      setCreateOpen(false);
      setNewName('');
      await loadDatasets();
      setCurrentDsId(res.data.id);
      setCurrentDsName(newName.trim());
      await loadPreview(res.data.id);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '创建失败');
    } finally {
      setCreating(false);
    }
  };

  // 打开添加表单
  const openAddForm = () => {
    setEditingIdx(null);
    setFormData(emptyForm);
    setFormOpen(true);
  };

  // 打开编辑表单
  const openEditForm = (idx: number) => {
    const row = rows[idx];
    setEditingIdx(idx);
    setFormData(sampleToForm(row));
    setFormOpen(true);
  };

  // 保存样本（新增或更新）
  const handleSave = async () => {
    const err = validateForm(formData);
    if (err) { message.warning(err); return; }
    if (!currentDsId) return;
    setSaving(true);
    try {
      const payload = {
        question: formData.question.trim(),
        answer: formData.answer.trim(),
        contexts: formData.contexts.trim()
          ? formData.contexts.split('\n').filter(Boolean).map((s: string) => s.trim())
          : [],
        ground_truth: formData.ground_truth.trim() || undefined,
      };
      if (editingIdx !== null) {
        await updateSample(currentDsId, editingIdx, payload);
        message.success('已更新');
      } else {
        await addSample(currentDsId, payload);
        message.success('已添加');
      }
      setFormData(emptyForm);
      if (editingIdx !== null) {
        setEditingIdx(null);
        setFormOpen(false);
      }
      await loadPreview(currentDsId);
    } catch (e: any) {
      message.error(e.response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  // 删除样本
  const handleDeleteSample = async (idx: number) => {
    if (!currentDsId) return;
    try {
      await deleteSample(currentDsId, idx);
      message.success('已删除');
      await loadPreview(currentDsId);
    } catch {
      message.error('删除失败');
    }
  };

  // 批量删除
  const handleDeleteDataset = async (id: string) => {
    try {
      await deleteDataset(id);
      message.success('已删除');
      if (currentDsId === id) {
        setCurrentDsId(null);
        setPreview(null);
        setRows([]);
      }
      await loadDatasets();
    } catch {
      message.error('删除失败');
    }
  };

  // 预览数据集
  const handleView = async (id: string) => {
    try {
      const res = await getDataset(id, 200);
      setViewData(res.data);
      setViewOpen(true);
    } catch { message.error('加载失败'); }
  };

  // 导出 CSV
  const handleExport = () => {
    if (!preview || rows.length === 0) { message.warning('没有数据可导出'); return; }
    const header = 'question,answer,contexts,ground_truth';
    const csvRows = rows.map(r => {
      const q = `"${(r.question || '').replace(/"/g, '""')}"`;
      const a = `"${(r.answer || '').replace(/"/g, '""')}"`;
      const c = `"${(Array.isArray(r.contexts) ? r.contexts.join('; ') : r.contexts || '').replace(/"/g, '""')}"`;
      const g = `"${(r.ground_truth || '').replace(/"/g, '""')}"`;
      return `${q},${a},${c},${g}`;
    });
    const blob = new Blob([header + '\n' + csvRows.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${currentDsName}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const columns = [
    { title: '#', dataIndex: '_idx', key: '_idx', width: 60,
      render: (_: any, __: any, i: number) => (
        <Button type="link" size="small" onClick={() => openEditForm(i)}>
          {i + 1}
        </Button>
      ),
    },
    { title: '问题', dataIndex: 'question', key: 'question', ellipsis: true,
      render: (v: string) => <Text ellipsis={{ tooltip: v }} style={{ maxWidth: 250 }}>{v}</Text>,
    },
    { title: '答案', dataIndex: 'answer', key: 'answer', ellipsis: true,
      render: (v: string) => <Text ellipsis={{ tooltip: v }} style={{ maxWidth: 200 }}>{v}</Text>,
    },
    { title: '上下文', dataIndex: 'contexts', key: 'contexts', ellipsis: true, width: 200,
      render: (v: any) => {
        const text = Array.isArray(v) ? v.join(' | ') : (v || '');
        return <Text ellipsis={{ tooltip: text }} style={{ maxWidth: 180 }}>{text}</Text>;
      },
    },
    { title: '标准答案', dataIndex: 'ground_truth', key: 'ground_truth', ellipsis: true, width: 150,
      render: (v: any) => v ? <Text ellipsis={{ tooltip: v }}>{v}</Text> : <Tag color="default">-</Tag>,
    },
    { title: '操作', key: 'actions', width: 80,
      render: (_: any, __: any, i: number) => (
        <Space size="small">
          <Tooltip title="编辑">
            <Button size="small" icon={<EditOutlined />} onClick={() => openEditForm(i)} />
          </Tooltip>
          <Popconfirm title="确定删除这条?" onConfirm={() => handleDeleteSample(i)}>
            <Tooltip title="删除">
              <Button size="small" danger icon={<DeleteOutlined />} />
            </Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Title level={4}>数据编辑</Title>

      <Row gutter={24}>
        {/* 左侧：数据集列表 */}
        <Col xs={24} lg={6}>
          <Card
            title="数据集"
            size="small"
            extra={
              <Space size="small">
                <Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
                  新建
                </Button>
                <Button size="small" icon={<InboxOutlined />} onClick={() => navigate('/datasets')}>
                  上传
                </Button>
              </Space>
            }
          >
            {datasets.length === 0 ? (
              <Empty description="暂无数据集" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              datasets.map(ds => (
                <Card
                  key={ds.id}
                  size="small"
                  style={{
                    marginBottom: 6,
                    cursor: 'pointer',
                    background: currentDsId === ds.id ? '#e6f4ff' : undefined,
                    border: currentDsId === ds.id ? '1px solid #1677ff' : undefined,
                  }}
                  onClick={() => handleSelect(ds.id)}
                  hoverable
                >
                  <Space direction="vertical" size={2} style={{ width: '100%' }}>
                    <Text strong style={{ fontSize: 13 }}>{ds.name}</Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {ds.row_count} 条 | {ds.created_at?.slice(0, 10)}
                    </Text>
                    <Space size="small">
                      <Button size="small" type="link" icon={<EyeOutlined />}
                        onClick={(e) => { e.stopPropagation(); handleView(ds.id); }} />
                      <Popconfirm title="确定删除此数据集?" onConfirm={(e) => { e?.stopPropagation(); handleDeleteDataset(ds.id); }}>
                        <Button size="small" type="link" danger icon={<DeleteOutlined />}
                          onClick={(e) => e.stopPropagation()} />
                      </Popconfirm>
                    </Space>
                  </Space>
                </Card>
              ))
            )}
          </Card>
        </Col>

        {/* 右侧：数据编辑区 */}
        <Col xs={24} lg={18}>
          {!currentDsId ? (
            <Card>
              <Empty description={
                <Space direction="vertical">
                  <Text>请从左侧选择或新建一个数据集</Text>
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
                    新建数据集
                  </Button>
                </Space>
              } />
            </Card>
          ) : (
            <>
              {/* 工具栏 */}
              <Card size="small" style={{ marginBottom: 12 }}>
                <Space wrap>
                  <Button type="primary" icon={<PlusOutlined />} onClick={openAddForm}>
                    添加样本
                  </Button>
                  <Button icon={<DownloadOutlined />} onClick={handleExport}
                    disabled={rows.length === 0}>
                    导出 CSV
                  </Button>
                  <Button onClick={() => navigate('/evaluate')}>
                    去评估
                  </Button>
                  <Text type="secondary">
                    当前: {currentDsName} ({rows.length} 条)
                  </Text>
                </Space>
              </Card>

              {/* 添加/编辑表单 */}
              {formOpen && (
                <Card
                  size="small"
                  title={
                    <Space>
                      {editingIdx !== null
                        ? <span><EditOutlined /> 编辑第 {editingIdx + 1} 条</span>
                        : <span><PlusOutlined /> 添加第 {rows.length + 1} 条</span>
                      }
                    </Space>
                  }
                  style={{ marginBottom: 12, background: '#fafafa' }}
                  extra={
                    <Space>
                      <Button size="small" onClick={() => { setFormOpen(false); setFormData(emptyForm); setEditingIdx(null); }}>
                        取消
                      </Button>
                      <Button size="small" type="primary" loading={saving} onClick={handleSave}>
                        {editingIdx !== null ? '保存修改' : '添加'}
                      </Button>
                      {editingIdx === null && (
                        <Button size="small" loading={saving} onClick={async () => {
                          const err = validateForm(formData);
                          if (err) { message.warning(err); return; }
                          setSaving(true);
                          try {
                            const payload = {
                              question: formData.question.trim(),
                              answer: formData.answer.trim(),
                              contexts: formData.contexts.trim()
                                ? formData.contexts.split('\n').filter(Boolean).map((s: string) => s.trim())
                                : [],
                              ground_truth: formData.ground_truth.trim() || undefined,
                            };
                            await addSample(currentDsId!, payload);
                            message.success('已添加，继续添加下一条');
                            setFormData(emptyForm);
                            await loadPreview(currentDsId!);
                          } catch (e: any) {
                            message.error(e.response?.data?.detail || '保存失败');
                          } finally {
                            setSaving(false);
                          }
                        }}>
                          添加并继续
                        </Button>
                      )}
                    </Space>
                  }
                >
                  <Space direction="vertical" style={{ width: '100%' }} size="small">
                    <div>
                      <Text strong style={{ color: 'red' }}>* </Text>
                      <Text>问题 (question)</Text>
                    </div>
                    <TextArea
                      rows={2}
                      placeholder="输入问题"
                      value={formData.question}
                      onChange={e => setFormData({ ...formData, question: e.target.value })}
                    />
                    <Divider style={{ margin: '8px 0' }} />
                    <Text strong>答案 (answer)</Text>
                    <TextArea
                      rows={2}
                      placeholder="输入答案（可选）"
                      value={formData.answer}
                      onChange={e => setFormData({ ...formData, answer: e.target.value })}
                    />
                    <Divider style={{ margin: '8px 0' }} />
                    <Text strong>上下文 (contexts)</Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>多条上下文请换行分隔</Text>
                    <TextArea
                      rows={3}
                      placeholder="输入检索上下文，每行一条"
                      value={formData.contexts}
                      onChange={e => setFormData({ ...formData, contexts: e.target.value })}
                    />
                    <Divider style={{ margin: '8px 0' }} />
                    <Text strong>标准答案 (ground_truth)</Text>
                    <TextArea
                      rows={1}
                      placeholder="输入标准答案（可选）"
                      value={formData.ground_truth}
                      onChange={e => setFormData({ ...formData, ground_truth: e.target.value })}
                    />
                  </Space>
                </Card>
              )}

              {/* 数据表格 */}
              <Card size="small" title={`数据列表 (${rows.length} 条)`}>
                {rows.length === 0 ? (
                  <Empty description={
                    <Space direction="vertical">
                      <Text>暂无数据</Text>
                      <Button type="primary" icon={<PlusOutlined />} onClick={openAddForm}>
                        添加第一条样本
                      </Button>
                    </Space>
                  } />
                ) : (
                  <Table
                    dataSource={rows.map((r, i) => ({ ...r, _idx: i }))}
                    columns={columns}
                    rowKey="_idx"
                    size="small"
                    pagination={false}
                    scroll={{ x: 'max-content' }}
                  />
                )}
              </Card>
            </>
          )}
        </Col>
      </Row>

      {/* 新建数据集弹窗 */}
      <Modal
        title="新建数据集"
        open={createOpen}
        onCancel={() => { setCreateOpen(false); setNewName(''); }}
        onOk={handleCreate}
        confirmLoading={creating}
        okText="创建"
      >
        <div style={{ marginTop: 16 }}>
          <Text>数据集名称</Text>
          <Input
            style={{ marginTop: 8 }}
            placeholder="输入数据集名称"
            value={newName}
            onChange={e => setNewName(e.target.value)}
            onPressEnter={handleCreate}
            autoFocus
          />
        </div>
      </Modal>

      {/* 预览弹窗 */}
      <Modal
        title="数据预览"
        open={viewOpen}
        onCancel={() => setViewOpen(false)}
        footer={null}
        width={800}
      >
        {viewData && (
          <Table
            dataSource={viewData.rows.map((r, i) => ({ ...r, _key: i }))}
            columns={viewData.columns.map(c => ({
              title: c, dataIndex: c, key: c, ellipsis: true,
              render: (v: any) => typeof v === 'string'
                ? (v.length > 100 ? v.slice(0, 100) + '...' : v)
                : Array.isArray(v) ? v.join('; ').slice(0, 100) : JSON.stringify(v),
            }))}
            rowKey="_key"
            size="small"
            scroll={{ x: 'max-content' }}
            pagination={false}
          />
        )}
      </Modal>
    </div>
  );
};

export default DataEditorPage;
