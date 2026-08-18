import React, { useCallback, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Button,
  Modal,
  Select,
  Skeleton,
  Space,
  Spin,
  Tabs,
  Tooltip,
  Typography,
} from 'antd';

const { Text } = Typography;
import {
  FullscreenExitOutlined,
  FullscreenOutlined,
  RobotOutlined,
} from '@ant-design/icons';
import {
  asApiError,
  AnalyzeResult,
  DocumentDetail,
  DocumentItem,
  KnowledgeGraph,
  ParserLlmModelItem,
  analyzeDocument,
  buildDocumentGraph,
  getDocument,
  getKnowledgeGraph,
  getLlmModelList,
  testLlmModelByName,
} from '../../api/client';
import ChunkCompareView from '../ChunkCompareView';
import KnowledgeGraphTab from '../KnowledgeGraphTab';
import DocumentPortrait from '../DocumentPortrait';
import DocumentPreviewModal from '../DocumentPreviewModal';
import ParseConfigModal from '../ParseConfigModal';
import SmartParseWizard from '../SmartParseWizard';
import RenameDocumentModal from '../RenameDocumentModal';
import UrlImportModal from '../UrlImportModal';
import BatchImportModal from '../BatchImportModal';

// ========== 图谱构建弹窗（LLM 模型选择状态机，自包含） ==========

export interface GraphBuildModalApi {
  /** 打开弹窗：加载模型列表，默认文档已配置模型（标"当前使用"），否则默认当前激活模型 */
  openGraphModal: (doc: DocumentItem) => Promise<void>;
  /** 弹窗节点（渲染在页面根部） */
  node: React.ReactNode;
}

/**
 * 图谱补建/重建弹窗状态机：模型列表/激活索引/选中模型/连接测试。
 * 切换模型即测连接，通过才可确认构建（本次构建生效，不写回文档配置）。
 * 原 Documents.tsx 的 graph* 状态与函数整体移入，行为不变。
 */
export function useGraphBuildModal(
  kbId: string | undefined,
  onSuccess: () => Promise<void> | void,
): GraphBuildModalApi {
  const { message } = AntApp.useApp();
  const [graphDoc, setGraphDoc] = useState<DocumentItem | null>(null);
  const [graphModels, setGraphModels] = useState<ParserLlmModelItem[]>([]);
  const [graphActiveIdx, setGraphActiveIdx] = useState(0);
  const [graphSelected, setGraphSelected] = useState<string>();
  const [graphTesting, setGraphTesting] = useState(false);
  const [graphTestedOk, setGraphTestedOk] = useState(false);
  const [graphConfirmLoading, setGraphConfirmLoading] = useState(false);

  /** 测试指定模型连接（通过 → 可确认构建；失败 → 提示重选，确认保持禁用） */
  const testGraphModel = useCallback(
    async (name: string) => {
      setGraphTesting(true);
      setGraphTestedOk(false);
      try {
        const res = await testLlmModelByName(name);
        if (res.data.ok) {
          message.success(`「${name}」连接正常（${res.data.latency_ms}ms）`);
          setGraphTestedOk(true);
        } else {
          message.error(`模型连接失败：${res.data.reason}，请重新选择`);
        }
      } catch (e: unknown) {
        message.error(
          `模型连接失败：${asApiError(e).response?.data?.detail || '网络请求失败'}，请重新选择`,
        );
      } finally {
        setGraphTesting(false);
      }
    },
    [message],
  );

  /** 打开图谱构建弹窗：加载模型列表，默认文档已配置模型（标"当前使用"），
   * 否则默认当前激活模型；默认选中项同样先测连接，通过才可确认 */
  const openGraphModal = async (doc: DocumentItem) => {
    setGraphDoc(doc);
    setGraphSelected(undefined);
    setGraphTestedOk(false);
    setGraphTesting(true);
    try {
      const res = await getLlmModelList();
      const models = res.data.models ?? [];
      const activeIdx = res.data.active ?? 0;
      setGraphModels(models);
      setGraphActiveIdx(activeIdx);
      const docCfg = doc.parser_config?.parse_llm_model;
      const initial =
        typeof docCfg === 'string' && docCfg && models.some(m => m.name === docCfg)
          ? docCfg
          : models[activeIdx]?.name;
      setGraphSelected(initial);
      if (initial) {
        await testGraphModel(initial);
      }
    } catch {
      message.error('加载 LLM 模型列表失败');
    } finally {
      setGraphTesting(false);
    }
  };

  /** 切换模型：先测连接，通过才标记可确认（失败保持选中但确认禁用） */
  const handleGraphModelChange = (name: string) => {
    setGraphSelected(name);
    void testGraphModel(name);
  };

  /** 确认构建：以所选模型触发补建/重建（本次构建生效，不写回文档配置） */
  const handleGraphBuildConfirm = async () => {
    if (!kbId || !graphDoc) return;
    const isRebuild =
      graphDoc.graph_status === 'ready' || graphDoc.graph_status === 'failed';
    setGraphConfirmLoading(true);
    try {
      await buildDocumentGraph(kbId, graphDoc.id, { llm_model: graphSelected });
      message.success(`图谱${isRebuild ? '重建' : '补建'}任务已启动，完成后列表自动刷新`);
      setGraphDoc(null);
      void onSuccess();
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || `图谱${isRebuild ? '重建' : '补建'}失败`);
    } finally {
      setGraphConfirmLoading(false);
    }
  };

  const node = (
    <Modal
      title="构建知识图谱"
      open={!!graphDoc}
      onCancel={() => setGraphDoc(null)}
      onOk={() => void handleGraphBuildConfirm()}
      confirmLoading={graphConfirmLoading}
      okText="确认构建"
      okButtonProps={{ disabled: !graphTestedOk || graphTesting }}
      cancelText="取消"
    >
      {graphDoc && (
        <>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={`将使用所选模型构建知识图谱（复用「${graphDoc.original_name}」现有切块），对话模型不受影响`}
          />
          <div style={{ marginBottom: 8 }}>
            <Text strong>图谱构建模型</Text>
          </div>
          <Select
            value={graphSelected}
            onChange={handleGraphModelChange}
            style={{ width: '100%' }}
            loading={graphTesting && !graphModels.length}
            disabled={graphConfirmLoading}
            options={graphModels.map((m, i) => {
              // 默认选中项（文档已配置模型或激活模型）标注"当前使用"
              const docCfg = graphDoc.parser_config?.parse_llm_model;
              const useDocCfg =
                typeof docCfg === 'string' &&
                docCfg &&
                graphModels.some(x => x.name === docCfg);
              const isCurrent = useDocCfg ? m.name === docCfg : i === graphActiveIdx;
              return {
                value: m.name,
                label: `${m.name}${m.model && m.model !== m.name ? `（${m.model}）` : ''}${isCurrent ? ' — 当前使用' : ''}`,
              };
            })}
            placeholder="选择模型"
          />
          <div style={{ marginTop: 8, minHeight: 24 }}>
            {graphTesting ? (
              <Space size={4}>
                <Spin size="small" />
                <Text type="secondary">正在测试连接…</Text>
              </Space>
            ) : graphSelected && !graphTestedOk ? (
              <Text type="danger">模型连接未通过，请重新选择模型</Text>
            ) : null}
          </div>
        </>
      )}
    </Modal>
  );

  return { openGraphModal, node };
}

// ========== 文档画像弹窗（更多 → 查看文档画像，自包含加载） ==========

export interface PortraitModalApi {
  openPortrait: (doc: DocumentItem) => Promise<void>;
  node: React.ReactNode;
}

/**
 * 文档画像弹窗：复用 analyze 接口只读展示（与智能解析向导 Step1 同款画像
 * 卡片，DocumentPortrait 共享组件）。原 Documents.tsx portrait* 状态整体移入。
 */
export function usePortraitModal(kbId: string | undefined): PortraitModalApi {
  const [portraitDoc, setPortraitDoc] = useState<DocumentItem | null>(null);
  const [portraitData, setPortraitData] = useState<AnalyzeResult | null>(null);
  const [portraitLoading, setPortraitLoading] = useState(false);
  const [portraitError, setPortraitError] = useState<string | null>(null);

  const openPortrait = async (doc: DocumentItem) => {
    setPortraitDoc(doc);
    setPortraitData(null);
    setPortraitError(null);
    setPortraitLoading(true);
    try {
      const res = await analyzeDocument(kbId!, doc.id);
      setPortraitData(res.data);
    } catch (e: unknown) {
      setPortraitError(asApiError(e).response?.data?.detail || '画像分析失败，请重试');
    } finally {
      setPortraitLoading(false);
    }
  };

  const node = (
    <Modal
      className="parse-config-modal"
      title={
        <div className="spw-title">
          <span className="spw-title-icon">
            <RobotOutlined />
          </span>
          <div className="spw-title-texts">
            <span className="spw-title-text">文档画像</span>
            <span className="spw-title-sub">{portraitDoc?.original_name ?? ''}</span>
          </div>
        </div>
      }
      open={!!portraitDoc}
      onCancel={() => setPortraitDoc(null)}
      footer={null}
      width={760}
      style={{ top: '8vh' }}
      styles={{ body: { padding: '16px 20px', maxHeight: '72vh', overflow: 'auto' } }}
    >
      <DocumentPortrait
        analyze={portraitData}
        loading={portraitLoading}
        error={portraitError}
        onRetry={() => portraitDoc && void openPortrait(portraitDoc)}
      />
    </Modal>
  );

  return { openPortrait, node };
}

// ========== 切块详情弹窗（切块列表 + 知识图谱 Tab，自包含加载） ==========

export interface DetailModalApi {
  /** 打开切块详情：并行加载文档详情 + 知识图谱（若启用） */
  openDetail: (doc: DocumentItem) => Promise<void>;
  node: React.ReactNode;
}

/**
 * 切块详情弹窗（含知识图谱 Tab）：文档详情/图谱数据加载 + 放大还原。
 * 原 Documents.tsx detail* / graphData* 状态与 handleDetail 整体移入。
 */
export function useDetailModal(kbId: string | undefined): DetailModalApi {
  const { message } = AntApp.useApp();
  const [detail, setDetail] = useState<DocumentItem | null>(null);
  const [detailData, setDetailData] = useState<DocumentDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailFullscreen, setDetailFullscreen] = useState(false);
  // 知识图谱 Tab 数据（null=未启用/加载失败/接口 404）
  const [graphData, setGraphData] = useState<KnowledgeGraph | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);

  const openDetail = async (doc: DocumentItem) => {
    setDetail(doc);
    setDetailData(null);
    setDetailLoading(true);
    try {
      const res = await getDocument(kbId!, doc.id);
      setDetailData(res.data);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '加载详情失败');
    } finally {
      setDetailLoading(false);
    }
    // 知识图谱 Tab 数据（按当前文档过滤；未启用 → 404 → graph=null 显示空状态引导）
    setGraphData(null);
    setGraphLoading(true);
    try {
      const res = await getKnowledgeGraph(kbId!, doc.id);
      setGraphData(res.data);
    } catch {
      setGraphData(null); // 404（该知识库暂无知识图谱）/ 网络错误 → 空状态
    } finally {
      setGraphLoading(false);
    }
  };

  const node = (
    <Modal
      className="chunk-detail-modal"
      title={
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            paddingRight: 36,
          }}
        >
          <span>{detail ? `切块详情 - ${detail.original_name}` : '切块详情'}</span>
          <Tooltip title={detailFullscreen ? '还原' : '放大'}>
            <Button
              type="text"
              size="small"
              aria-label={detailFullscreen ? '还原窗口' : '放大窗口'}
              icon={detailFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
              onClick={() => setDetailFullscreen(v => !v)}
            />
          </Tooltip>
        </div>
      }
      open={!!detail}
      onCancel={() => setDetail(null)}
      footer={null}
      width={detailFullscreen ? '88vw' : 1150}
      // 高度用 min(固定vh, 视口高-120px) 兜底：小视口下 8vh+80vh+底部留白(padding 24px)
      // 也不会超过视口，全屏 .ant-modal-wrap 永不成为滚动容器
      style={
        detailFullscreen
          ? { top: '6vh', height: 'min(88vh, calc(100vh - 120px))' }
          : { top: '8vh', height: 'min(80vh, calc(100vh - 120px))' }
      }
      styles={{
        content: { display: 'flex', flexDirection: 'column', height: '100%' },
        header: { flexShrink: 0 },
        body: {
          padding: '16px 20px',
          flex: 1,
          minHeight: 0,
          overflow: 'auto',
          display: 'flex',
          flexDirection: 'column',
        },
      }}
    >
      {detailLoading ? (
        <Skeleton active paragraph={{ rows: 10 }} />
      ) : (
        <Tabs
          // 与日志页同款撑满规则（index.css .logs-page-tabs 链式 flex 撑满）
          className="logs-page-tabs"
          defaultActiveKey="chunks"
          items={[
            {
              key: 'chunks',
              label: '切块',
              children: (
                <ChunkCompareView
                  // 弹窗固定高度，内容区撑满剩余高度（头部/工具条固定，仅左右内容区内部滚动）
                  fillHeight
                  // key 绑定文档 id：切换文档时重挂载，重置选中态
                  key={detailData?.id}
                  chunks={
                    detailData?.chunks?.map(c => ({
                      index: c.index,
                      text: c.text,
                      char_start: c.char_start,
                      char_end: c.char_end,
                      context: c.context,
                      label: c.label,
                    })) ??
                    detailData?.chunk_preview?.map((text, i) => ({ index: i, text })) ??
                    []
                  }
                  fullText={detailData?.full_text}
                />
              ),
            },
            {
              key: 'graph',
              label: '知识图谱',
              children: (
                <KnowledgeGraphTab
                  graph={graphData}
                  loading={graphLoading}
                  chunks={
                    detailData?.chunks?.map(c => ({ index: c.index, text: c.text })) ??
                    detailData?.chunk_preview?.map((text, i) => ({ index: i, text })) ??
                    []
                  }
                />
              ),
            },
          ]}
        />
      )}
    </Modal>
  );

  return { openDetail, node };
}

// ========== 现成弹窗编排（预览/解析配置/智能向导/重命名/URL 导入/批量导入） ==========

interface DocumentModalsProps {
  kbId?: string;
  previewDoc: DocumentItem | null;
  onPreviewClose: () => void;
  parseDoc: DocumentItem | null;
  onParseClose: () => void;
  smartDoc: DocumentItem | null;
  onSmartClose: () => void;
  renameDoc: DocumentItem | null;
  onRenameClose: () => void;
  urlImportOpen: boolean;
  onUrlImportClose: () => void;
  batchImportOpen: boolean;
  onBatchImportClose: () => void;
  /** 各弹窗成功后的列表刷新 */
  onSuccess: () => Promise<void> | void;
}

/**
 * 文档页受控弹窗编排（原 Documents.tsx 底部弹窗渲染整体移入）：
 * 在线预览 / 解析配置 / 智能解析向导 / 重命名 / URL 导入 / 批量导入并解析。
 */
const DocumentModals: React.FC<DocumentModalsProps> = ({
  kbId,
  previewDoc,
  onPreviewClose,
  parseDoc,
  onParseClose,
  smartDoc,
  onSmartClose,
  renameDoc,
  onRenameClose,
  urlImportOpen,
  onUrlImportClose,
  batchImportOpen,
  onBatchImportClose,
  onSuccess,
}) => (
  <>
    {/* 在线预览弹窗 */}
    <DocumentPreviewModal
      open={!!previewDoc}
      doc={previewDoc}
      kbId={kbId}
      onCancel={onPreviewClose}
    />

    {/* 解析配置弹窗 */}
    <ParseConfigModal
      open={!!parseDoc}
      doc={parseDoc}
      kbId={kbId}
      onCancel={onParseClose}
      onSuccess={onSuccess}
    />

    {/* 智能解析引导向导（独立模块：画像 → 切块方式 → 增强配置 → 确认解析） */}
    <SmartParseWizard
      open={!!smartDoc}
      doc={smartDoc}
      kbId={kbId}
      onCancel={onSmartClose}
      onSuccess={onSuccess}
    />

    {/* 文档重命名弹窗 */}
    <RenameDocumentModal
      open={!!renameDoc}
      doc={renameDoc}
      kbId={kbId}
      onCancel={onRenameClose}
      onSuccess={onSuccess}
    />

    {/* 批量导入并解析弹窗（多选 → 逐个上传+解析入库，智能/统一两种模式） */}
    <BatchImportModal
      open={batchImportOpen}
      kbId={kbId}
      onCancel={onBatchImportClose}
      onSuccess={onSuccess}
    />

    {/* URL 网页导入弹窗 */}
    <UrlImportModal
      open={urlImportOpen}
      kbId={kbId}
      onCancel={onUrlImportClose}
      onSuccess={onSuccess}
    />
  </>
);

export default DocumentModals;
