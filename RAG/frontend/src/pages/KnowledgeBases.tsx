import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  App as AntApp,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Progress,
  Skeleton,
  Space,
  Tag,
  Typography,
} from 'antd';
import {
  LinkOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  asApiError,
  KnowledgeBase,
  RebuildTaskStatus,
  TagCount,
  createKb,
  deleteKb,
  getRebuildStatus,
  listKbTags,
  listKbs,
  rebuildVectors,
  updateKb,
  updateKbTags,
} from '../api/client';
import AppEmpty from '../components/AppEmpty';
import KbCard from '../components/KbCard';
import PageHeader from '../components/PageHeader';
import { useAuth } from '../auth/AuthContext';

const KnowledgeBasesPage: React.FC = () => {
  const { message } = AntApp.useApp();
  const { user } = useAuth();
  const navigate = useNavigate();
  // 普通用户仅问答：新建/删除/重建仅 dept_admin 与 super_admin 可用（列表数据后端已按权限过滤）
  const canManage = user?.role !== 'user';

  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<{ name: string; description?: string }>();

  // 标签：筛选条数据源 + 选中标签（多选交集过滤）+ 弹窗草稿
  const [allTags, setAllTags] = useState<TagCount[]>([]);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [editingKb, setEditingKb] = useState<KnowledgeBase | null>(null);
  const [draftTags, setDraftTags] = useState<string[]>([]);
  const [tagInput, setTagInput] = useState('');

  // 名称搜索：前端过滤（与后端标签交集过滤叠加）
  const [searchKeyword, setSearchKeyword] = useState('');

  // 重建向量：rebuildKb 非空时打开进度 Modal，轮询 rebuild-status 直至完成
  const [rebuildKb, setRebuildKb] = useState<KnowledgeBase | null>(null);
  const [rebuildStatus, setRebuildStatus] = useState<RebuildTaskStatus | null>(null);

  // 列表加载：有选中标签时按标签过滤（多选=后端交集过滤）
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listKbs(selectedTags.length ? { tag: selectedTags } : undefined);
      setKbs(res.data);
    } catch {
      message.error('加载知识库列表失败');
    } finally {
      setLoading(false);
    }
  }, [message, selectedTags]);

  // 标签筛选条数据源（当前用户可见范围内聚合）
  const loadTags = useCallback(async () => {
    try {
      const res = await listKbTags();
      setAllTags(res.data.tags);
    } catch {
      // 标签加载失败不阻塞页面（筛选条留空即可）
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    loadTags();
  }, [loadTags]);

  // 名称搜索过滤（标签过滤由后端完成，此处仅按名称前端过滤）
  const filteredKbs = useMemo(() => {
    const kw = searchKeyword.trim().toLowerCase();
    if (!kw) return kbs;
    return kbs.filter(kb => kb.name.toLowerCase().includes(kw));
  }, [kbs, searchKeyword]);

  const openCreate = () => {
    setEditingKb(null);
    form.resetFields();
    setDraftTags([]);
    setTagInput('');
    setModalOpen(true);
  };

  const openEdit = (kb: KnowledgeBase) => {
    setEditingKb(kb);
    form.setFieldsValue({ name: kb.name, description: kb.description });
    setDraftTags(kb.tags ?? []);
    setTagInput('');
    setModalOpen(true);
  };

  // 弹窗内回车添加标签（去空白、去重、≤10 个）
  const addDraftTag = () => {
    const t = tagInput.trim();
    setTagInput('');
    if (!t) return;
    setDraftTags(prev => {
      if (prev.includes(t)) return prev;
      if (prev.length >= 10) {
        message.warning('最多 10 个标签');
        return prev;
      }
      return [...prev, t];
    });
  };

  const toggleTag = (name: string) => {
    setSelectedTags(prev =>
      prev.includes(name) ? prev.filter(x => x !== name) : [...prev, name],
    );
  };

  // 卡片内移除单个标签：移除后立即 PUT 保存
  const handleRemoveTag = async (kb: KnowledgeBase, tag: string) => {
    try {
      await updateKbTags(kb.id, (kb.tags ?? []).filter(t => t !== tag));
      await load();
      await loadTags();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '移除标签失败');
    }
  };

  // 新建 / 编辑共用提交（编辑模式 PUT 名称/描述/标签）
  const handleSubmit = async () => {
    let values: { name: string; description?: string };
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      if (editingKb) {
        await updateKb(editingKb.id, {
          name: values.name,
          description: values.description || undefined,
          tags: draftTags,
        });
        message.success('知识库已更新');
      } else {
        await createKb({
          name: values.name,
          description: values.description || undefined,
          tags: draftTags.length ? draftTags : undefined,
        });
        message.success('知识库创建成功');
      }
      setModalOpen(false);
      setEditingKb(null);
      form.resetFields();
      setDraftTags([]);
      await load();
      await loadTags();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || (editingKb ? '更新失败' : '创建失败'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (kb: KnowledgeBase) => {
    try {
      await deleteKb(kb.id);
      message.success(`知识库「${kb.name}」已删除`);
      await load();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '删除失败');
    }
  };

  // 一键重建：启动后台任务 → 轮询进度
  const handleRebuild = async (kb: KnowledgeBase) => {
    try {
      const res = await rebuildVectors(kb.id);
      setRebuildStatus({
        kb_id: kb.id,
        task_id: res.data.task_id,
        running: true,
        done: 0,
        total: 0,
        failed: 0,
        current_doc: null,
        finished_at: null,
        errors: [],
      });
      setRebuildKb(kb);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '启动重建失败');
    }
  };

  // 重建进度轮询（1.5s 间隔；running=false 或出错时结束并提示）
  useEffect(() => {
    if (!rebuildKb) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const res = await getRebuildStatus(rebuildKb.id);
        if (cancelled) return;
        setRebuildStatus(res.data);
        if (!res.data.running) {
          const { done, failed, errors } = res.data;
          if (failed > 0) {
            const first = errors[0];
            message.warning(
              `向量重建完成：成功 ${done} 个，失败 ${failed} 个` +
                (first && first.doc_name ? `（${first.doc_name}: ${first.error.slice(0, 80)}）` : ''),
            );
          } else {
            message.success(`向量重建完成：${done} 个文档已重新向量化`);
          }
          setRebuildKb(null);
          await load();
          return;
        }
        timer = setTimeout(poll, 1500);
      } catch {
        if (!cancelled) {
          message.error('查询重建进度失败');
          setRebuildKb(null);
        }
      }
    };
    timer = setTimeout(poll, 500);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [rebuildKb, load, message]);

  const rebuildPercent = (() => {
    if (!rebuildStatus) return 0;
    if (rebuildStatus.total <= 0) return rebuildStatus.running ? 0 : 100;
    return Math.round(((rebuildStatus.done + rebuildStatus.failed) / rebuildStatus.total) * 100);
  })();

  return (
    <div>
      <PageHeader
        title="知识库"
        description="点击卡片进入知识库的文档管理；更换 Embedding 模型后若出现「维度不符」，点「重建向量」重新生成"
        extra={
          <>
            <Input
              prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
              placeholder="搜索知识库名称"
              value={searchKeyword}
              onChange={e => setSearchKeyword(e.target.value)}
              allowClear
              style={{ width: 220 }}
            />
            <Button icon={<ReloadOutlined />} onClick={() => load()}>
              刷新
            </Button>
            {canManage && (
              <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                新建知识库
              </Button>
            )}
            {/* 外部查询（仅超管）：将知识库开放给外部人员查询 */}
            {user?.role === 'super_admin' && (
              <Button icon={<LinkOutlined />} onClick={() => navigate('/ext-queries')}>
                外部查询
              </Button>
            )}
          </>
        }
      />

      {/* 标签筛选条：点击多选（交集过滤），再点取消，选中蓝色高亮 */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space size={[8, 8]} wrap>
          <Typography.Text type="secondary">按标签筛选：</Typography.Text>
          <Tag
            style={{ cursor: 'pointer' }}
            color={selectedTags.length === 0 ? 'blue' : undefined}
            onClick={() => setSelectedTags([])}
          >
            全部
          </Tag>
          {allTags.map(t => (
            <Tag
              key={t.name}
              style={{ cursor: 'pointer' }}
              color={selectedTags.includes(t.name) ? 'blue' : undefined}
              onClick={() => toggleTag(t.name)}
            >
              {t.name}（{t.count}）
            </Tag>
          ))}
          {allTags.length === 0 && (
            <Typography.Text type="secondary">暂无标签</Typography.Text>
          )}
        </Space>
      </Card>

      {/* 首屏加载：卡片网格形态骨架（匹配 kb-grid 布局） */}
      {loading ? (
        <div className="kb-grid">
          {Array.from({ length: 4 }).map((_, i) => (
            <Card key={i} className="kb-card" styles={{ body: { padding: 16 } }}>
              <Skeleton
                active
                avatar={{ size: 38, shape: 'square' }}
                title={{ width: '60%' }}
                paragraph={{ rows: 2 }}
              />
            </Card>
          ))}
        </div>
      ) : (
        <>
          {/* 卡片网格：addCard（canManage）+ 知识库卡片 */}
          <div className="kb-grid">
            {canManage && (
              <div className="kb-add-card" onClick={openCreate}>
                <PlusOutlined style={{ fontSize: 30 }} />
                <Typography.Text strong>新建知识库</Typography.Text>
              </div>
            )}
            {filteredKbs.map(kb => (
              <KbCard
                key={kb.id}
                kb={kb}
                canManage={canManage}
                onEdit={openEdit}
                onRebuild={handleRebuild}
                onDelete={handleDelete}
                onRemoveTag={handleRemoveTag}
              />
            ))}
          </div>

          {/* 空态：无库引导创建（保留）；搜索/筛选无结果时给出提示 */}
          {kbs.length === 0 && !loading && (
            <AppEmpty
              title={selectedTags.length > 0 ? '没有符合所选标签的知识库' : '暂无知识库'}
              description={
                selectedTags.length > 0 ? undefined : '创建后即可上传文档构建问答知识'
              }
              action={
                canManage && selectedTags.length === 0 ? (
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                    新建知识库
                  </Button>
                ) : undefined
              }
            />
          )}
          {kbs.length > 0 && filteredKbs.length === 0 && (
            <AppEmpty title="没有匹配的知识库" />
          )}
        </>
      )}

      {/* 重建进度 Modal（后台任务轮询，无取消按钮防中断） */}
      <Modal
        title={`重建向量 - ${rebuildKb?.name ?? ''}`}
        open={!!rebuildKb}
        footer={null}
        closable={false}
      >
        {rebuildStatus && (
          <div>
            <Progress
              percent={rebuildPercent}
              status={rebuildStatus.running ? 'active' : rebuildStatus.failed > 0 ? 'exception' : 'success'}
            />
            <Typography.Paragraph style={{ marginTop: 12, marginBottom: 4 }}>
              已完成 <b>{rebuildStatus.done}</b> / {rebuildStatus.total} 个文档
              {rebuildStatus.failed > 0 && (
                <>
                  {' '}
                  ，失败 <b style={{ color: '#cf1322' }}>{rebuildStatus.failed}</b> 个
                </>
              )}
              {rebuildStatus.current_doc && (
                <span style={{ color: '#94a3b8' }}>（正在处理：{rebuildStatus.current_doc}）</span>
              )}
            </Typography.Paragraph>
            {rebuildStatus.errors.length > 0 && (
              <Typography.Paragraph type="danger" style={{ marginBottom: 0, fontSize: 12 }}>
                {rebuildStatus.errors.slice(0, 5).map((e, i) => (
                  <div key={i}>
                    {e.doc_name || e.doc_id}: {e.error}
                  </div>
                ))}
              </Typography.Paragraph>
            )}
          </div>
        )}
      </Modal>

      <Modal
        title={editingKb ? '编辑知识库' : '新建知识库'}
        open={modalOpen}
        onOk={handleSubmit}
        onCancel={() => {
          setModalOpen(false);
          setEditingKb(null);
          form.resetFields();
          setDraftTags([]);
          setTagInput('');
        }}
        confirmLoading={submitting}
        okText={editingKb ? '保存' : '创建'}
        cancelText="取消"
      >
        <Form form={form} layout="vertical" initialValues={{ name: '', description: '' }}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入知识库名称' }]}>
            <Input placeholder="例如：公司制度文档" maxLength={50} showCount />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea placeholder="选填，简单描述该知识库的内容" maxLength={200} rows={3} />
          </Form.Item>
          <Form.Item label="标签">
            <Input
              value={tagInput}
              placeholder="输入标签后回车添加（最多 10 个，每个 ≤20 字符）"
              maxLength={20}
              allowClear
              onChange={e => setTagInput(e.target.value)}
              onPressEnter={e => {
                e.preventDefault();
                addDraftTag();
              }}
            />
            {draftTags.length > 0 && (
              <div style={{ marginTop: 8 }}>
                <Space size={[0, 4]} wrap>
                  {draftTags.map(t => (
                    <Tag
                      key={t}
                      closable
                      onClose={() => setDraftTags(prev => prev.filter(x => x !== t))}
                    >
                      {t}
                    </Tag>
                  ))}
                </Space>
              </div>
            )}
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default KnowledgeBasesPage;
