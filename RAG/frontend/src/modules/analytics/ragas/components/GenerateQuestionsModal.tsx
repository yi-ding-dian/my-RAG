/**
 * AI 出题弹窗（合成测试集）：读知识库切块自己出题 + 写参考答案。
 *
 * 从 RagasDetail 抽出来——10 个状态 + 4 个函数 + 弹窗 JSX 约 250 行，
 * 只服务"给还没人问过、也没人写题的新库造题"这一件事。
 *
 * 进出接口：
 * - `kbs`：知识库候选（页面已加载，避免组件内重复请求）
 * - `onCreated`：采用草稿建成评估集后回调（页面据此把新评估集插到列表最前）
 * - 关闭时内部自动丢弃草稿（resetGen），与原 `onCancel` 行为一致
 *
 * 状态名沿用页面里的 `gen*` 前缀：纯搬运，减少改名出错面。
 */
import { useState } from 'react';
import {
  Alert, App as AntApp, Button, Checkbox, Empty, Input, InputNumber, Select,
  Space, Typography,
} from 'antd';

import AppModal from '../../../../shared/components/common/AppModal';
import {
  createRagasDataset, generateRagasQuestions, listDocuments,
  DocumentItem, KnowledgeBase, RagasDataset, RagasGeneratedSample,
} from '../../../../shared/api/client';

const { Text } = Typography;

export interface GenerateQuestionsModalProps {
  open: boolean;
  /** 知识库候选（页面已加载） */
  kbs: KnowledgeBase[];
  onClose: () => void;
  /** 采用草稿建成评估集后回调（页面据此把新评估集插到列表最前） */
  onCreated: (ds: RagasDataset) => void;
}

export const GenerateQuestionsModal = ({
  open, kbs, onClose, onCreated,
}: GenerateQuestionsModalProps) => {
  const { message } = AntApp.useApp();
  const [genKbId, setGenKbId] = useState<string | undefined>(undefined);
  const [genCount, setGenCount] = useState(10);
  const [genLoading, setGenLoading] = useState(false);
  const [genSamples, setGenSamples] = useState<RagasGeneratedSample[]>([]);
  const [genChecked, setGenChecked] = useState<boolean[]>([]);
  const [genName, setGenName] = useState('');
  const [genSaving, setGenSaving] = useState(false);
  // 取材范围：选中的文档（空 = 全库）；只列已入库的（软删的块后端会排除）
  const [genDocs, setGenDocs] = useState<DocumentItem[]>([]);
  const [genDocIds, setGenDocIds] = useState<string[]>([]);

  const resetGen = () => {
    setGenSamples([]);
    setGenChecked([]);
    setGenName('');
  };

  /** 关闭并丢弃草稿（等价于原来的 onCancel={() => { setGenOpen(false); resetGen(); }}） */
  const handleClose = () => {
    onClose();
    resetGen();
  };

  // 换知识库 → 拉它的文档列表供"取材范围"多选（只列已入库的；
  // 软删的文档块会被后端直接排除，列出来反而误导）
  const handleGenKbChange = async (kbId: string | undefined) => {
    setGenKbId(kbId);
    setGenDocIds([]);
    setGenDocs([]);
    if (!kbId) return;
    try {
      const res = await listDocuments(kbId);
      const arr = Array.isArray(res.data) ? res.data : (res.data.items || []);
      setGenDocs(arr.filter(d => d.status === 'ingested'));
    } catch {
      setGenDocs([]);
    }
  };

  const handleGenerateQuestions = async () => {
    if (!genKbId) {
      message.error('请选择知识库');
      return;
    }
    setGenLoading(true);
    try {
      const res = await generateRagasQuestions(genKbId, genCount, genDocIds);
      const samples = res.data.samples || [];
      setGenSamples(samples);
      setGenChecked(samples.map(() => true));  // 默认全选，用户再取消不要的
      if (samples.length === 0) {
        message.warning(
          `抽了 ${res.data.picked} 个片段都没能出题（内容太少或模型判断不适合出题），`
          + '换个知识库或调大条数再试', 6);
      } else {
        message.success(`已生成 ${samples.length} 条草稿，请审核后采用`);
      }
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || 'AI 出题失败（检查 LLM 服务与知识库是否有切块）', 6);
    } finally {
      setGenLoading(false);
    }
  };

  // 采用勾选的草稿 → 建成正式评估集
  const handleAdoptGenerated = async () => {
    const picked = genSamples.filter((_, i) => genChecked[i]);
    if (picked.length === 0) {
      message.error('请至少勾选一条样本');
      return;
    }
    const name = genName.trim();
    if (!name) {
      message.error('请填写评估集名称');
      return;
    }
    if (!genKbId) return;
    setGenSaving(true);
    try {
      const res = await createRagasDataset({
        kb_id: genKbId, name, source: 'ai',
        samples: picked.map(s => ({
          question: s.question, ground_truth: s.ground_truth,
        })),
      });
      onCreated(res.data);
      handleClose();
      message.success(`评估集「${name}」已创建（${picked.length} 条）`);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '保存评估集失败');
    } finally {
      setGenSaving(false);
    }
  };

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 780, h: 640 }}
      rememberKey="ana-dataset-ai"
      title="AI 生成题集（草稿，需审核）"
      open={open}
      onCancel={handleClose}
      footer={genSamples.length === 0 ? [
        <Button key="cancel" onClick={handleClose}>取消</Button>,
        <Button key="gen" type="primary" loading={genLoading} onClick={handleGenerateQuestions}>
          开始生成
        </Button>,
      ] : [
        <Button key="regen" onClick={resetGen}>重新生成</Button>,
        <Button key="cancel2" onClick={handleClose}>取消</Button>,
        <Button key="adopt" type="primary" loading={genSaving} onClick={handleAdoptGenerated}>
          采用选中的 {genChecked.filter(Boolean).length} 条
        </Button>,
      ]}
      width={780}
    >
      {genSamples.length === 0 ? (
        <Space direction="vertical" style={{ width: '100%' }} size={14}>
          <Alert
            type="info"
            showIcon
            message="AI 读知识库的切块，自己出题 + 写参考答案"
            description="生成结果是**草稿**：你可以逐条改、删、勾选，确认后才存成评估集。
              参考答案是基于原文生成的，但仍建议对着「来源」核对一遍——AI 出的题和答案不能直接拿来就用。"
          />
          <div>
            <Text strong>知识库</Text>
            <Select
              style={{ width: '100%', marginTop: 4 }}
              placeholder="选择要出题的知识库"
              value={genKbId}
              onChange={handleGenKbChange}
              options={kbs.map(k => ({
                value: k.id,
                label: `${k.name}（${k.chunk_count} 个切块）`,
              }))}
              notFoundContent={<Empty description="暂无可用知识库" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            />
          </div>
          {genKbId && (
            <div>
              <Text strong>取材文档</Text>
              <Select
                style={{ width: '100%', marginTop: 4 }}
                mode="multiple"
                maxTagCount="responsive"
                allowClear
                placeholder="不选 = 全库（按文档分散抽样）"
                value={genDocIds}
                onChange={(v: string[]) => setGenDocIds(v)}
                // 用**原始文件名**：与出题来源显示的块 metadata.document_name 是同一个名字。
                // 若这里用 name（内部存储名 xxx.docx），下拉里选的与来源里看到的对不上
                options={genDocs.map(d => ({
                  value: d.id, label: d.original_name || d.name,
                }))}
                notFoundContent={<Empty description="该知识库暂无已入库文档"
                                           image={Empty.PRESENTED_IMAGE_SIMPLE} />}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                只从选中的文档取材；不选则整库。AI 读的是**切块**（被检索的正是它），
                不是原始文档——这样才能保证出的题"检索得到"，评估才公平。
                已删除的文档一律不参与
              </Text>
            </div>
          )}
          <div>
            <Text strong>生成条数</Text>
            <div style={{ marginTop: 4 }}>
              <InputNumber
                min={1}
                max={20}
                value={genCount}
                onChange={v => setGenCount(Number(v) || 10)}
              />
              <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                1~20 条；出题会分散到不同文档取材，每条一次 LLM 调用，越多越慢
              </Text>
            </div>
          </div>
        </Space>
      ) : (
        <Space direction="vertical" style={{ width: '100%' }} size={10}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            共 {genSamples.length} 条草稿，已默认全选。可直接编辑问题和参考答案，
            取消勾选不要的；「来源」是出题用的原文位置，方便核对答案准不准。
          </Text>
          <div style={{ maxHeight: 370, overflowY: 'auto' }}>
            {genSamples.map((s, i) => (
              <div
                key={i}
                style={{
                  padding: '8px 10px', marginBottom: 8,
                  border: '1px solid #f0f0f0', borderRadius: 8,
                }}
              >
                <Space align="start" style={{ width: '100%' }}>
                  <Checkbox
                    checked={genChecked[i]}
                    onChange={e => {
                      const next = [...genChecked];
                      next[i] = e.target.checked;
                      setGenChecked(next);
                    }}
                    style={{ marginTop: 6 }}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Input
                      value={s.question}
                      placeholder="问题"
                      onChange={e => {
                        const next = [...genSamples];
                        next[i] = { ...s, question: e.target.value };
                        setGenSamples(next);
                      }}
                    />
                    <Input.TextArea
                      style={{ marginTop: 6 }}
                      rows={2}
                      value={s.ground_truth}
                      placeholder="参考答案"
                      onChange={e => {
                        const next = [...genSamples];
                        next[i] = { ...s, ground_truth: e.target.value };
                        setGenSamples(next);
                      }}
                    />
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      来源：{s.source_doc_name
                        ? `${s.source_doc_name} · 块 ${s.source_chunk_index ?? '?'}`
                        : (s.source_doc
                          ? `${s.source_doc.slice(0, 12)}… · 块 ${s.source_chunk_index ?? '?'}`
                          : '（后端未能从块元数据取到）')}
                    </Text>
                  </div>
                </Space>
              </div>
            ))}
          </div>
          <div>
            <Text strong>存入哪个评估集</Text>
            <Input
              style={{ marginTop: 4 }}
              placeholder="评估集名称（1~50 字）"
              maxLength={50}
              value={genName}
              onChange={e => setGenName(e.target.value)}
            />
          </div>
        </Space>
      )}
    </AppModal>
  );
};
