import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Empty, Input, Radio, Space, Spin, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
// ECharts 按需引入：仅 Graph 系列 + Tooltip/Legend 组件 + 标签防重叠 + Canvas 渲染器（控制包体，避免全量）
import * as echarts from 'echarts/core';
import { GraphChart } from 'echarts/charts';
import { TooltipComponent, LegendComponent } from 'echarts/components';
import { LabelLayout } from 'echarts/features';
import { CanvasRenderer } from 'echarts/renderers';
import type { ECharts, EChartsCoreOption } from 'echarts/core';
import type { GraphEntity, GraphRelation, KnowledgeGraph } from '../api/client';
import { useTheme } from '../theme';

echarts.use([GraphChart, TooltipComponent, LegendComponent, LabelLayout, CanvasRenderer]);

/**
 * ECharts 回调参数的最小收窄结构（formatter/click 参数为库内部联合类型，
 * 不做精确推断，仅取用到的字段；取代原 `(p: any)` 的 unknown + 守卫）。
 */
interface GraphCallbackParams {
  dataType?: string;
  data?: {
    entity?: GraphEntity;
    relation?: GraphRelation;
    sourceName?: string;
    targetName?: string;
  };
  name?: string;
}
const asGraphCallbackParams = (p: unknown): GraphCallbackParams =>
  typeof p === 'object' && p !== null ? (p as GraphCallbackParams) : {};

/**
 * 知识图谱 Tab（切块详情弹窗内）：
 * - 关系图（ECharts graph 力导向图）：节点 = 实体（大小按出现次数、颜色按类型），
 *   连线 = 关系（线宽按 weight、hover 连线显示关系类型），可拖拽 / 滚轮缩放；
 *   点击节点 → 弹出实体详情（描述 + count + 关联 chunk 文本，与实体表行展开共用渲染）
 * - 实体表：名称 / 类型 Tag / 描述 / 出现次数 / 关联块数，点击行展开关联 chunk 文本
 * - 关系表：来源实体 → 关系类型 → 目标实体（+ 描述 / 关联块数）
 * - 无图谱（后端 404 或空结构）→ 空状态引导"该文档未启用知识图谱，可在解析配置中开启"
 * - 搜索 + 中心子网视图：顶部搜索框按实体名包含匹配（不区分大小写），以命中实体为中心计算
 *   ego 子网（1/2 层可切换，多中心取并集），图与表同步切到子网；中心实体节点放大 + 品牌色
 *   描边高亮；子网节点超 200 自动降级（2 层回退 1 层，1 层仍超按 count 截断）防布局卡顿；
 *   清空搜索恢复全量图谱
 * - 规模预案：本项目图谱 100-200 节点，force 布局默认全部渲染实测可控；
 *   若超过 150 节点卡顿，再在顶部加"全部 / Top 50 高频实体"切换（按 count 排序截取），当前不预置
 */

const { Text } = Typography;

/** 品牌色（中心实体描边高亮） */
const BRAND_COLOR = '#2a78d6';

/** 子网节点数上限（超过则降级/截断，防 force 布局卡顿） */
const MAX_SUBNET_NODES = 200;

/** 切块条目（图谱 Tab 按 chunk_index 反查文本，来源详情接口 chunks） */
export interface GraphChunkItem {
  index: number;
  text: string;
}

interface KnowledgeGraphTabProps {
  /** 图谱数据（null=未启用/加载失败） */
  graph: KnowledgeGraph | null;
  /** 切块列表（chunks_meta，按 chunk_index 查文本） */
  chunks: GraphChunkItem[];
  /** 加载中 */
  loading?: boolean;
}

const entityTypeColor: Record<string, string> = {
  人物: 'blue',
  机构: 'purple',
  技术: 'green',
  概念: 'orange',
  事件: 'red',
  成果: 'cyan',
};

const relationTypeColor: Record<string, string> = {
  提出: 'blue',
  开发: 'green',
  发明: 'purple',
  启动: 'red',
  导致: 'volcano',
  影响: 'orange',
  属于: 'gold',
  相关: 'default',
};

/**
 * 实体类型 → 节点颜色（categorical 色板，dataviz 参考 8 色固定顺序）：
 * 已知 6 类型（人物/机构/技术/概念/事件/成果）固定取前 6 色；
 * 未知类型按在数据中的出现顺序从第 7 色起循环取色（类型多时避免撞色）
 */
const TYPE_PALETTE: string[] = [
  '#2a78d6', // 蓝
  '#eb6834', // 橙
  '#1baf7a', // 青绿
  '#eda100', // 黄
  '#e34948', // 红
  '#4a3aa7', // 紫
  '#e87ba4', // 品红
  '#008300', // 绿
];
const KNOWN_TYPE_COLOR: Record<string, string> = {
  人物: TYPE_PALETTE[0],
  机构: TYPE_PALETTE[1],
  技术: TYPE_PALETTE[2],
  概念: TYPE_PALETTE[3],
  事件: TYPE_PALETTE[4],
  成果: TYPE_PALETTE[5],
};

/** 实体类型 → 节点颜色（order 为类型在数据中首次出现顺序，未知类型循环取色） */
const typeColor = (type: string, order: number): string => {
  const fixed = KNOWN_TYPE_COLOR[type];
  if (fixed) return fixed;
  return TYPE_PALETTE[(order + TYPE_PALETTE.length - 6) % TYPE_PALETTE.length];
};

/** 关联 chunk 文本（按 chunk_index 反查；缺块显示占位） */
const refText = (chunks: GraphChunkItem[], chunkIndex: number): string => {
  const found = chunks.find(c => c.index === chunkIndex);
  if (!found) return '';
  const t = found.text.trim();
  return t.length > 200 ? `${t.slice(0, 200)}…` : t;
};

/** 实体关联 chunk 文本渲染（实体表行展开 / 图节点详情面板共用） */
const renderChunkRefs = (entity: GraphEntity, chunks: GraphChunkItem[]) => {
  if (entity.chunk_refs.length === 0) {
    return <Text type="secondary">无关联切块</Text>;
  }
  return (
    <>
      {entity.chunk_refs.map((ref, i) => (
        <div key={i} style={{ marginBottom: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            块 {ref.chunk_index}（字符 {ref.char_start}~{ref.char_end}）
          </Text>
          <div
            style={{
              background: 'rgba(0,0,0,0.03)',
              borderRadius: 4,
              padding: '6px 8px',
              marginTop: 2,
              fontSize: 13,
              lineHeight: 1.6,
            }}
          >
            {refText(chunks, ref.chunk_index) || (
              <Text type="secondary">关联切块文本不可用</Text>
            )}
          </div>
        </div>
      ))}
    </>
  );
};

/** 实体详情面板（点击图节点弹出；与实体表行展开共用 chunk 文本渲染） */
const EntityDetailPanel: React.FC<{
  entity: GraphEntity;
  chunks: GraphChunkItem[];
  onClose: () => void;
}> = ({ entity, chunks, onClose }) => (
  <Card
    size="small"
    style={{ margin: '12px 0' }}
    title={
      <Space size={8}>
        <Text strong>{entity.name}</Text>
        <Tag color={entityTypeColor[entity.type] ?? 'default'}>{entity.type}</Tag>
      </Space>
    }
    extra={
      <Button type="text" size="small" onClick={onClose}>
        关闭
      </Button>
    }
  >
    <div style={{ marginBottom: 8 }}>
      <Text type="secondary">
        出现 {entity.count} 次 · 关联 {entity.chunk_refs.length} 个切块
      </Text>
    </div>
    <div style={{ marginBottom: 12 }}>
      {entity.description || <Text type="secondary">暂无描述</Text>}
    </div>
    <Text strong style={{ fontSize: 13 }}>
      关联切块
    </Text>
    <div style={{ marginTop: 8 }}>{renderChunkRefs(entity, chunks)}</div>
  </Card>
);

const KnowledgeGraphTab: React.FC<KnowledgeGraphTabProps> = ({ graph, chunks, loading }) => {
  const { isDark } = useTheme();
  const chartElRef = useRef<HTMLDivElement>(null);
  /** 点击图节点选中的实体（详情面板展示关联 chunk 文本） */
  const [activeEntity, setActiveEntity] = useState<GraphEntity | null>(null);
  /** 搜索关键字（实体名包含匹配，不区分大小写；空 = 全量视图） */
  const [searchKeyword, setSearchKeyword] = useState('');
  /** ego 子网层数（1 层 = 中心 + 直接邻居；2 层 = 再向外一层） */
  const [hop, setHop] = useState<1 | 2>(1);

  // 实体 id → 名称 映射（关系表展示用）
  const nameById = useMemo(() => {
    const m: Record<string, string> = {};
    for (const e of graph?.entities ?? []) m[e.id] = e.name;
    return m;
  }, [graph]);

  // 实体 id → 实体（子网截断排序用）
  const entityById = useMemo(() => {
    const m = new Map<string, GraphEntity>();
    for (const e of graph?.entities ?? []) m.set(e.id, e);
    return m;
  }, [graph]);

  // 是否处于搜索（子网）视图
  const isSearching = searchKeyword.trim() !== '';

  /**
   * 搜索 → ego 子网计算（纯函数 useMemo 防大图卡顿）：
   * - 匹配集合 = 名称包含关键字（不区分大小写）的实体，多实体命中取并集子网
   * - 沿关系边向外扩展 hop 层（无向），子网内关系 = 两端都在子网内的关系
   * - 防爆炸：节点 > 200 时 2 层回退 1 层；1 层仍超限按 count 排序截断保中心
   * - 无搜索关键字返回 null（表示全量视图，不参与过滤）
   */
  const subnet = useMemo(() => {
    if (!graph || graph.entities.length === 0) return null;
    const kw = searchKeyword.trim().toLowerCase();
    if (!kw) return null;

    const matched = graph.entities.filter(e => e.name.toLowerCase().includes(kw));
    if (matched.length === 0) {
      return { matched, nodeIds: new Set<string>(), nodes: [] as GraphEntity[], edges: [] as GraphRelation[], truncated: false };
    }
    const centerIds = new Set(matched.map(e => e.id));

    // 从中心集合沿无向关系向外扩展 layers 层
    const expand = (start: Set<string>, layers: number): Set<string> => {
      const ids = new Set<string>(start);
      for (let layer = 0; layer < layers; layer++) {
        const next = new Set<string>();
        for (const r of graph.relations) {
          if (ids.has(r.source) && !ids.has(r.target)) next.add(r.target);
          if (ids.has(r.target) && !ids.has(r.source)) next.add(r.source);
        }
        if (next.size === 0) break;
        next.forEach(id => ids.add(id));
      }
      return ids;
    };

    let nodeIds = expand(centerIds, hop);
    let truncated = false;
    if (nodeIds.size > MAX_SUBNET_NODES) {
      // 超限降级：2 层 → 回退 1 层；1 层仍超 → 保中心 + count 最高的邻居（截断）
      nodeIds = expand(centerIds, 1);
      truncated = true;
      if (nodeIds.size > MAX_SUBNET_NODES) {
        const sortedNeighbors = [...nodeIds]
          .filter(id => !centerIds.has(id))
          .sort((a, b) => (entityById.get(b)?.count ?? 0) - (entityById.get(a)?.count ?? 0));
        nodeIds = new Set<string>(centerIds);
        sortedNeighbors.slice(0, MAX_SUBNET_NODES - centerIds.size).forEach(id => nodeIds.add(id));
      }
    }

    return {
      matched,
      nodeIds,
      nodes: graph.entities.filter(e => nodeIds.has(e.id)),
      edges: graph.relations.filter(r => nodeIds.has(r.source) && nodeIds.has(r.target)),
      truncated,
    };
  }, [graph, searchKeyword, hop, entityById]);

  // 未启用/加载失败/空图谱 → 空状态引导
  const isEmpty = !graph || graph.entities.length === 0;

  // ===== 关系图 option（graph 系列力导向布局；搜索时只渲染子网） =====
  const graphOption = useMemo<EChartsCoreOption | null>(() => {
    if (!graph || graph.entities.length === 0) return null;
    // 数据视图：搜索无匹配 → null（图区域显示空态）；有匹配 → 子网；无搜索 → 全量
    const isSubnetView = subnet !== null;
    if (isSubnetView && subnet!.matched.length === 0) return null;
    const viewEntities = isSubnetView ? subnet!.nodes : graph.entities;
    const viewRelations = isSubnetView ? subnet!.edges : graph.relations;
    if (viewEntities.length === 0) return null;
    // 中心实体 id 集合（子网视图高亮用）
    const centerIds = isSubnetView ? new Set(subnet!.matched.map(e => e.id)) : new Set<string>();

    // 实体类型保序去重 → category 索引（图例按类型分组，子网视图只含子网内类型）
    const typeOrder: string[] = [];
    const typeIndex = new Map<string, number>();
    for (const e of viewEntities) {
      if (!typeIndex.has(e.type)) {
        typeIndex.set(e.type, typeOrder.length);
        typeOrder.push(e.type);
      }
    }

    const nodes = viewEntities.map(e => {
      const baseSize = Math.max(14, Math.min(36, 10 + Math.sqrt(Math.max(1, e.count)) * 4.2));
      const isCenter = centerIds.has(e.id);
      const node: Record<string, unknown> = {
        id: e.id,
        name: e.name,
        value: e.count,
        category: typeIndex.get(e.type),
        // 节点大小按出现次数：count 1 → 14px，count 36+ → 36px 封顶
        symbolSize: isCenter ? Math.max(44, Math.min(52, baseSize * 1.5)) : baseSize,
        isCenter,
        entity: e, // 点击节点时回传实体数据
      };
      if (isCenter) {
        // 中心实体高亮：品牌色描边 + 外发光 + 名称加粗
        node.itemStyle = {
          borderColor: BRAND_COLOR,
          borderWidth: 3,
          shadowBlur: 14,
          shadowColor: 'rgba(42,120,214,0.45)',
        };
        node.label = { fontWeight: 700 };
      }
      return node;
    });

    const edges = viewRelations.map(r => ({
      id: r.id,
      name: r.type, // 线标签显示关系类型（hover 时）
      source: r.source,
      target: r.target,
      value: r.weight,
      // 线宽按关系强度（weight 1-4 映射，封顶保持图面清爽）
      lineStyle: { width: Math.max(1, Math.min(4, r.weight)) },
      relation: r, // hover 连线时回传关系数据
    }));

    const n = viewEntities.length;
    return {
      // 力导向：全量视图斥力随节点数收紧（60 节点 ~270 → 200 节点 150）；
      // 子网视图节点少，斥力随节点数增大（紧凑不散开），边距收紧
      force: isSubnetView
        ? {
            repulsion: Math.min(240, 70 + n * 9),
            gravity: 0.08,
            edgeLength: [30, 70],
            friction: 0.6,
          }
        : {
            repulsion: Math.max(150, Math.round(270 - n * 0.7)),
            gravity: 0.08,
            edgeLength: [40, 90],
            friction: 0.6,
          },
      tooltip: {
        backgroundColor: isDark ? '#1f1f1f' : 'rgba(255,255,255,0.96)',
        borderColor: isDark ? '#434343' : '#d9d9d9',
        textStyle: { color: isDark ? '#e6e6e6' : '#333' },
        formatter: (p: unknown) => {
          const d = asGraphCallbackParams(p);
          if (d.dataType === 'node') {
            const e = d.data?.entity;
            if (!e) return '';
            return [
              `<b>${e.name}</b>`,
              `类型：${e.type} · 出现 ${e.count} 次 · 关联 ${e.chunk_refs.length} 个切块`,
              e.description ? `描述：${e.description}` : '',
            ]
              .filter(Boolean)
              .join('<br/>');
          }
          if (d.dataType === 'edge') {
            const r = d.data?.relation;
            if (!r) return '';
            return [
              `<b>${d.data?.sourceName} → ${r.type} → ${d.data?.targetName}</b>`,
              `强度：${r.weight}（关联 ${r.chunk_refs.length} 个切块）`,
              r.description ? `描述：${r.description}` : '',
            ]
              .filter(Boolean)
              .join('<br/>');
          }
          return '';
        },
      },
      legend: {
        type: 'scroll',
        top: 0,
        right: 4,
        data: typeOrder,
        textStyle: { color: isDark ? '#c9c9c9' : '#666' },
        itemWidth: 12,
        itemHeight: 12,
      },
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true, // 可拖拽 / 滚轮缩放
          draggable: true,
          data: nodes,
          links: edges,
          categories: typeOrder.map((t, i) => ({
            name: t,
            itemStyle: { color: typeColor(t, i) },
          })),
          // 连线统一灰色（可读优先），hover 高亮 + 显示关系类型；关系类型色不再叠加避免噪色
          lineStyle: {
            color: isDark ? 'rgba(255,255,255,0.28)' : 'rgba(0,0,0,0.3)',
            width: 1,
            curveness: 0,
          },
          // 线标签：常态隐藏（关系多时保持可读），hover 连线时显示关系类型
          edgeLabel: { show: false, fontSize: 10, color: isDark ? '#ddd' : '#555', formatter: '{b}' },
          label: {
            show: true,
            position: 'right',
            fontSize: 11,
            color: isDark ? '#d6d6d6' : '#3f3f3f',
            // 长名称截断，避免标签铺满图面
            formatter: (p: unknown) => {
              const name = asGraphCallbackParams(p).name;
              return name && name.length > 10 ? `${name.slice(0, 10)}…` : name ?? '';
            },
          },
          // 名称标签重叠时自动隐藏（LabelLayout，节点多时保持可读）
          labelLayout: { hideOverlap: true },
          emphasis: {
            focus: 'adjacency', // hover 高亮邻接子图，其余淡化
            lineStyle: { width: 2.5 },
            label: { show: true, fontWeight: 600 },
            edgeLabel: { show: true },
          },
        },
      ],
    } as EChartsCoreOption;
  }, [graph, isDark, subnet]);

  // 图实例生命周期：init / 点击节点回调 / ResizeObserver 自适应 / dispose（手写集成，仅依赖 echarts 本体）
  useEffect(() => {
    const el = chartElRef.current;
    if (!el || !graphOption) return;
    const chart: ECharts = echarts.init(el);
    chart.setOption(graphOption);
    const onClick = (p: unknown) => {
      const d = asGraphCallbackParams(p);
      if (d.dataType === 'node' && d.data?.entity) setActiveEntity(d.data.entity);
    };
    chart.on('click', onClick);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.off('click', onClick);
      chart.dispose();
    };
  }, [graphOption]);

  // 搜索切换子网后，若详情面板实体已不在子网内则关闭面板（防止展示子网外实体）
  useEffect(() => {
    if (activeEntity && subnet && !subnet.nodeIds.has(activeEntity.id)) {
      setActiveEntity(null);
    }
  }, [subnet, activeEntity]);

  const entityColumns: ColumnsType<GraphEntity> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: 180,
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '类型',
      dataIndex: 'type',
      key: 'type',
      width: 90,
      render: (v: string) => <Tag color={entityTypeColor[v] ?? 'default'}>{v}</Tag>,
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (v: string) => v || <Text type="secondary">-</Text>,
    },
    {
      title: '出现次数',
      dataIndex: 'count',
      key: 'count',
      width: 90,
    },
    {
      title: '关联块数',
      key: 'refs',
      width: 90,
      render: (_, row) => row.chunk_refs.length,
    },
  ];

  const relationColumns: ColumnsType<GraphRelation> = [
    {
      title: '来源',
      key: 'source',
      width: 180,
      render: (_, row) => <Text strong>{nameById[row.source] ?? row.source}</Text>,
    },
    {
      title: '关系',
      key: 'type',
      width: 90,
      render: (_, row) => <Tag color={relationTypeColor[row.type] ?? 'default'}>{row.type}</Tag>,
    },
    {
      title: '目标',
      key: 'target',
      width: 180,
      render: (_, row) => <Text strong>{nameById[row.target] ?? row.target}</Text>,
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (v: string) => v || <Text type="secondary">-</Text>,
    },
    {
      title: '关联块数',
      key: 'weight',
      width: 90,
      render: (_, row) => row.weight,
    },
  ];

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}>
        <Spin />
      </div>
    );
  }

  if (isEmpty) {
    return (
      <Empty
        style={{ padding: '32px 0' }}
        description={
          <Space direction="vertical" size={8}>
            <Text>该文档未启用知识图谱，可在解析配置中开启</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              开启后入库时将用 LLM 抽取实体与关系构建知识图谱（产生额外 token 费用）
            </Text>
          </Space>
        }
      />
    );
  }

  const viewEntities = subnet ? subnet.nodes : graph.entities;
  const viewRelations = subnet ? subnet.edges : graph.relations;

  return (
    <div>
      {/* 搜索 + 中心子网视图控制（搜索时显示层数切换，清空恢复全量） */}
      <Space style={{ marginBottom: 12 }} wrap>
        <Input.Search
          allowClear
          placeholder="搜索实体，查看以它为中心的关系网"
          value={searchKeyword}
          onChange={e => setSearchKeyword(e.target.value)}
          style={{ width: 320 }}
        />
        {isSearching && (
          <>
            <Radio.Group size="small" value={hop} onChange={e => setHop(e.target.value as 1 | 2)}>
              <Radio.Button value={1}>1 层</Radio.Button>
              <Radio.Button value={2}>2 层</Radio.Button>
            </Radio.Group>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {hop === 1 ? '中心 + 直接邻居' : '中心 + 2 层邻居'}
            </Text>
          </>
        )}
      </Space>
      {subnet && subnet.matched.length === 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="未找到匹配实体"
          description="没有名称包含该关键字的实体，请尝试其他关键字或清空搜索"
        />
      )}
      {subnet?.truncated && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={`子网规模超过 ${MAX_SUBNET_NODES} 节点，已回退显示 1 层邻居（${subnet.nodes.length} 个实体）`}
        />
      )}
      <Alert
        type={isSearching ? 'success' : 'info'}
        showIcon
        style={{ marginBottom: 12 }}
        message={
          isSearching && subnet && subnet.matched.length > 0
            ? `匹配 ${subnet.matched.length} 个中心实体，子网共 ${subnet.nodes.length} 个实体、${subnet.edges.length} 条关系（${hop} 层）`
            : `共 ${graph.entities.length} 个实体、${graph.relations.length} 条关系（构建于 ${graph.updated_at}）`
        }
      />
      {/* 关系图（力导向，~420px） */}
      <div style={{ margin: '4px 0 8px' }}>
        <Text strong>关系图</Text>
        <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
          可拖拽 / 滚轮缩放，点击节点查看详情
        </Text>
      </div>
      {/* key 区分两分支：ECharts dispose 会清空 dom 内容，无 key 时 React 复用节点会把
          后插入的 Empty 一并清掉（空态不显示）；key 强制重建，dispose 只作用于被移除的旧节点 */}
      {graphOption ? (
        <div
          key="chart"
          ref={chartElRef}
          style={{
            height: 420,
            width: '100%',
            borderRadius: 8,
            border: `1px solid ${isDark ? '#303030' : '#eee'}`,
            background: isDark ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.015)',
          }}
        />
      ) : (
        <div
          key="empty"
          style={{
            height: 420,
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            borderRadius: 8,
            border: `1px dashed ${isDark ? '#303030' : '#eee'}`,
            background: isDark ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.015)',
          }}
        >
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未找到匹配实体" />
        </div>
      )}
      {activeEntity && (
        <EntityDetailPanel
          entity={activeEntity}
          chunks={chunks}
          onClose={() => setActiveEntity(null)}
        />
      )}
      <Table<GraphEntity>
        size="small"
        rowKey="id"
        dataSource={viewEntities}
        columns={entityColumns}
        pagination={{ pageSize: 8, showTotal: (t: number) => `共 ${t} 个实体` }}
        scroll={{ x: 700 }}
        // 点击行展开：显示该实体关联的 chunk 文本（与图节点详情共用渲染）
        expandable={{
          expandedRowRender: row => (
            <div style={{ padding: '4px 8px' }}>{renderChunkRefs(row, chunks)}</div>
          ),
        }}
      />
      <div style={{ margin: '16px 0 8px', fontWeight: 500 }}>关系</div>
      <Table<GraphRelation>
        size="small"
        rowKey="id"
        dataSource={viewRelations}
        columns={relationColumns}
        pagination={{ pageSize: 8, showTotal: (t: number) => `共 ${t} 条关系` }}
        scroll={{ x: 700 }}
      />
    </div>
  );
};

export default KnowledgeGraphTab;
