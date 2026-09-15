import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Badge, Button, Card, Col, Input, List, Row, Segmented, Space, Spin, Tag, Typography,
} from 'antd';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { ECharts, EChartsCoreOption } from 'echarts/core';

import { ackLogFaults, getLogOverview, LOG_HEALTH_REFRESH_EVENT } from '../../shared/api/client';
import type {
  LogDayStat,
  LogFaultEntry,
  LogHealth,
  LogOverview,
} from '../../shared/api/client';
import AppEmpty from '../../shared/components/common/AppEmpty';
import AppModal from '../../shared/components/common/AppModal';
import { levelColor } from './shared';
import type { AppInstance } from './shared';

echarts.use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const { Text } = Typography;

/** 总览刷新间隔（毫秒）：统计要扫日志文件，比实时流的 5s 轮询放慢 */
const REFRESH_INTERVAL = 60000;

const DAY_OPTIONS = [
  { label: '近 7 天', value: 7 },
  { label: '近 14 天', value: 14 },
  { label: '近 30 天', value: 30 },
];

/** 趋势图系列（颜色对齐 shared.levelColor：INFO 蓝 / WARNING 橙 / ERROR 红） */
const SERIES: { key: 'info' | 'warning' | 'error'; name: string; color: string }[] = [
  { key: 'info', name: 'INFO', color: '#1677ff' },
  { key: 'warning', name: 'WARNING', color: '#faad14' },
  { key: 'error', name: 'ERROR', color: '#ff4d4f' },
];

/** 半透明网格/文字色：亮色暗色主题下都不违和（与知识图谱一致的做法） */
const AXIS_COLOR = 'rgba(128, 128, 128, 0.85)';
const SPLIT_COLOR = 'rgba(128, 128, 128, 0.18)';

// ==================== Tab1：总览（7 天统计 + 红绿灯 + 最近系统故障） ====================

/**
 * 系统日志总览（仅 super_admin）：
 * - 顶部健康状态条：红灯 = 窗口内有**系统级**故障（LLM 崩溃/依赖连不上），
 *   绿灯 = 正常；副文案给出「系统故障 N · 操作失败 M」两个口径——用户级失败
 *   （文档超阈值这类）只影响单次操作，不进红灯，但仍计入 ERROR 计数
 * - 数字卡片：今日 INFO/WARNING/ERROR + 近 N 天系统故障
 * - 趋势图：近 N 天堆叠柱状图（echarts 按需引入），一眼看出哪天出过事
 * - 最近系统级故障列表：时间 + 级别 + 内容，最新在前
 * 仅当前 Tab 可见时轮询（60s），切走即停
 */
const OverviewTab: React.FC<{
  app: AppInstance;
  active: boolean;
  /** 点趋势图柱子 → 跳到「实时系统日志」看那天的日志（父组件负责切 Tab） */
  onJumpToDate?: (date: string) => void;
}> = ({ app, active, onJumpToDate }) => {
  const { message } = app;
  const [days, setDays] = useState(7);
  const [data, setData] = useState<LogOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState('');
  const [acking, setAcking] = useState(false);
  // 确认弹窗：待确认的故障 + 备注输入（备注随确认存下，供日后回溯"当时为什么确认掉"）
  const [ackTarget, setAckTarget] = useState<{ ids: string[]; title: string } | null>(null);
  const [ackNote, setAckNote] = useState('');

  const fetchOverview = useCallback(async (initial = false) => {
    try {
      const res = await getLogOverview(days);
      setData(res.data);
      setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    } catch {
      if (initial) message.error('加载日志总览失败');
    } finally {
      setLoading(false);
    }
  }, [days, message]);

  // 非当前 Tab 不轮询（切走即停、切回立即续拉一次，与实时日志 Tab 同款）
  const fetchRef = useRef(fetchOverview);
  useEffect(() => { fetchRef.current = fetchOverview; }, [fetchOverview]);
  useEffect(() => {
    if (!active) return;
    setLoading(true);
    void fetchRef.current(true);
    const timer = setInterval(() => void fetchRef.current(false), REFRESH_INTERVAL);
    return () => clearInterval(timer);
  }, [active, days]);

  // 按条确认（ACK 消警）：确认后立即重拉总览与菜单灯色；全部确认完红灯才灭，
  // 之后新产生的故障是新 id、会重新亮灯（不是把信号一键按死）
  const ackFaults = async (ids: string[], note: string) => {
    if (!ids.length) return;
    setAcking(true);
    try {
      await ackLogFaults(ids, note);
      message.success(ids.length > 1
        ? `已确认 ${ids.length} 条；若再次出现系统级故障会重新亮红灯`
        : '已确认该条；全部确认完红灯才会熄灭');
      // 广播给菜单红绿灯立即重拉（否则它要等满一个 60s 轮询周期才变绿）
      window.dispatchEvent(new Event(LOG_HEALTH_REFRESH_EVENT));
      await fetchOverview(false);
      setAckTarget(null);
    } catch {
      message.error('确认失败，请重试');
    } finally {
      setAcking(false);
    }
  };

  // 打开确认弹窗（备注从空开始，避免把上一条的备注带到下一条）
  const openAck = (ids: string[], title: string) => {
    setAckTarget({ ids, title });
    setAckNote('');
  };

  // 单条故障：未确认的给「已解决」按钮，已确认的打标 + 显示处理备注（仍留在列表里可回溯）
  const renderFault = (item: LogFaultEntry) => (
    <List.Item
      style={{ padding: '6px 0' }}
      actions={
        item.acked
          ? [<Tag key="ok" color="success" style={{ marginInlineEnd: 0 }}>已解决</Tag>]
          : [
              <Button
                key="ack"
                size="small"
                type="link"
                danger
                style={{ padding: 0 }}
                onClick={() => openAck([item.id], '确认该故障已解决')}
              >
                未解决
              </Button>,
            ]
      }
    >
      <Space direction="vertical" size={2} style={{ width: '100%' }}>
        <Space size={10} align="start" style={{ width: '100%' }}>
          <Tag
            color={levelColor(item.level)}
            style={{ minWidth: 68, textAlign: 'center', marginInlineEnd: 0 }}
          >
            {item.level ?? 'LOG'}
          </Tag>
          <Text type="secondary" style={{ whiteSpace: 'nowrap', fontSize: 12 }}>
            {item.ts ?? ''}
          </Text>
          <Text style={{ wordBreak: 'break-all', fontSize: 13 }}>{item.message}</Text>
        </Space>
        {item.acked && item.ack_note && (
          <Text type="secondary" style={{ fontSize: 12, paddingLeft: 78 }}>
            备注：{item.ack_note}
          </Text>
        )}
      </Space>
    </List.Item>
  );

  if (loading && !data) {
    return (
      <div style={{ flex: 1, display: 'flex', justifyContent: 'center', padding: '64px 0' }}>
        <Spin size="large" />
      </div>
    );
  }
  if (!data) {
    return <AppEmpty title="暂无日志统计" description="指定日期范围内没有日志文件" />;
  }

  const isRed = data.health.level === 'red';
  // 用户级 ERROR = 全部 ERROR - 系统级 ERROR（两个口径并排给，避免「有 ERROR 却绿灯」的困惑）
  const userErrors = Math.max(0, data.totals.error - data.totals.fault_errors);
  // 未确认的故障（逐条 ACK 的对象）：确认完这些红灯才灭
  const unackedFaults = data.recent_faults.filter(f => !f.acked);
  const unackedCount = unackedFaults.length;
  const unackedIds = unackedFaults.map(f => f.id);

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <Space style={{ marginBottom: 12, flexShrink: 0 }} wrap>
        <Segmented
          value={days}
          onChange={v => setDays(v as number)}
          options={DAY_OPTIONS}
        />
        {updatedAt && (
          <Text type="secondary" style={{ fontSize: 12 }}>更新于 {updatedAt}</Text>
        )}
      </Space>

      <div style={{ flexShrink: 0 }}>
        <HealthBar health={data.health} userErrors={userErrors} totals={data.totals} />
      </div>

      <Row gutter={12} style={{ marginTop: 12, flexShrink: 0 }}>
        {[
          { title: '今日 INFO', value: data.today.info, color: '#1677ff' },
          { title: '今日 WARNING', value: data.today.warning, color: '#faad14' },
          { title: '今日 ERROR', value: data.today.error, color: '#ff4d4f' },
          {
            title: `近 ${days} 天系统故障`,
            value: data.totals.faults,
            color: isRed ? '#ff4d4f' : '#52c41a',
          },
        ].map(c => (
          <Col span={6} key={c.title}>
            <Card size="small" styles={{ body: { padding: '10px 16px' } }}>
              <Text type="secondary" style={{ fontSize: 12 }}>{c.title}</Text>
              <div style={{ fontSize: 24, fontWeight: 600, color: c.color, lineHeight: 1.35 }}>
                {c.value.toLocaleString()}
              </div>
            </Card>
          </Col>
        ))}
      </Row>

      <Card
        size="small"
        title={`近 ${days} 天日志趋势`}
        style={{ marginTop: 12, flexShrink: 0 }}
        extra={
          <Text type="secondary" style={{ fontSize: 12 }}>
            {onJumpToDate ? '点柱子可查看当天日志' : '堆叠柱：INFO / WARNING / ERROR'}
          </Text>
        }
      >
        <TrendChart days={data.days} onSelectDay={onJumpToDate} />
      </Card>

      {/* 故障列表：占满剩余高度、**内部**滚动——整页不滚，灯色与趋势始终在视野内 */}
      <Card
        size="small"
        title="最近系统级故障"
        style={{
          marginTop: 12,
          flex: 1,
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
        }}
        styles={{ body: { flex: 1, minHeight: 0, overflowY: 'auto', padding: '4px 16px' } }}
        extra={
          <Space size={10}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {`${unackedCount} 条未确认 / 共 ${data.recent_faults.length} 条`}
            </Text>
            {unackedCount > 0 && (
              <Button
                size="small"
                type="link"
                onClick={() => openAck(unackedIds, `确认这 ${unackedCount} 条故障已解决`)}
              >
                全部已解决
              </Button>
            )}
          </Space>
        }
      >
        <List
          size="small"
          dataSource={data.recent_faults}
          locale={{
            emptyText: (
              <AppEmpty
                title="暂无系统级故障"
                description={`近 ${days} 天 LLM 与依赖服务均正常`}
              />
            ),
          }}
          renderItem={renderFault}
        />
      </Card>

      {/* 确认弹窗（逐条/批量共用，项目统一 AppModal 门面、垂直居中、可拖拽调大小）：
          备注**必填**（写明处理方式，确认后在列表里可回看）。
          初始尺寸按内容定死（不用 auto：内容矮时实测高度会被容器自身撑住、底部留白） */}
      <AppModal
        dimension="resizable"
        defaultSize={{ w: 560, h: 210 }}
        minSize={{ w: 420, h: 210 }}
        maxSize={{ w: 900, h: 620 }}
        rememberKey="log-fault-ack"
        centered
        title={ackTarget?.title}
        open={!!ackTarget}
        okText="确认已解决"
        cancelText="取消"
        confirmLoading={acking}
        okButtonProps={{ disabled: !ackNote.trim() }}
        onOk={() => void ackFaults(ackTarget?.ids ?? [], ackNote)}
        onCancel={() => setAckTarget(null)}
        destroyOnHidden
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          备注必填：写清处理方式，确认后在故障列表里可回看
        </Text>
        {/* autoSize：随输入长高（3~4 行，够写 200 字）；超过 4 行由输入框自己滚，
            **不要 flex 撑满**——撑满会顶出容器、触发外层 overflow:auto 的滚动条 */}
        <Input.TextArea
          autoSize={{ minRows: 3, maxRows: 4 }}
          maxLength={200}
          showCount
          style={{ marginTop: 8 }}
          placeholder="例如：已重启 LLM 服务 / 压测产生的，非真实故障"
          value={ackNote}
          onChange={e => setAckNote(e.target.value)}
        />
      </AppModal>
    </div>
  );
};

/** 顶部健康状态条：灯色 + 摘要 + 双口径计数（系统故障 / 操作失败 / 日志总量）
 *  故障的确认在下方「最近系统级故障」列表里逐条进行（那里才看得到具体错什么） */
const HealthBar: React.FC<{
  health: LogHealth;
  userErrors: number;
  totals: LogOverview['totals'];
}> = ({ health, userErrors, totals }) => {
  const isRed = health.level === 'red';
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        flexWrap: 'wrap',
        padding: '14px 18px',
        borderRadius: 10,
        background: isRed
          ? 'linear-gradient(90deg, rgba(255, 77, 79, 0.10), transparent)'
          : 'linear-gradient(90deg, rgba(82, 196, 26, 0.10), transparent)',
        border: `1px solid ${isRed ? 'rgba(255, 77, 79, 0.35)' : 'rgba(82, 196, 26, 0.35)'}`,
      }}
    >
      <Badge status={isRed ? 'error' : 'success'} />
      <div style={{ lineHeight: 1.5 }}>
        <div style={{ fontSize: 16, fontWeight: 600 }}>
          {isRed ? '系统存在故障' : '系统运行正常'}
        </div>
        <Text type="secondary" style={{ fontSize: 12 }}>{health.summary}</Text>
      </div>
      <Space size={20} style={{ marginLeft: 'auto' }} wrap>
        <MiniStat label="系统故障" value={totals.faults} danger={isRed && totals.faults > 0} />
        <MiniStat label="操作失败" value={userErrors} />
        <MiniStat label="日志总量" value={totals.lines} />
      </Space>
    </div>
  );
};

/** 状态条上的小计数（不用 Statistic：大字号会把状态条撑高） */
const MiniStat: React.FC<{ label: string; value: number; danger?: boolean }> = ({
  label, value, danger,
}) => (
  <div style={{ textAlign: 'right', minWidth: 62 }}>
    <div style={{ fontSize: 18, fontWeight: 600, color: danger ? '#ff4d4f' : undefined }}>
      {value.toLocaleString()}
    </div>
    <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text>
  </div>
);

/**
 * 近 N 天日志趋势：堆叠柱状图（手写 echarts 集成，仅依赖 echarts 本体，
 * 与知识图谱 Tab 同款：init / ResizeObserver 自适应 / dispose）。
 * 点柱子 → onSelectDay(date)，父组件负责切到「实时系统日志」并选中该天。
 */
const TrendChart: React.FC<{
  days: LogDayStat[];
  onSelectDay?: (date: string) => void;
}> = ({ days, onSelectDay }) => {
  const elRef = useRef<HTMLDivElement>(null);
  // chart 放 state（不放 ref）：事件绑定的 effect 需要能依赖它，实例重建时才会重绑
  const [chart, setChart] = useState<ECharts | null>(null);
  // 回调与数据放 ref：父组件传的 onSelectDay 多是内联函数、每次渲染都是新引用，
  // 直接进依赖数组会让点击事件被反复解绑重绑（实测点击会丢），这里只绑一次
  const daysRef = useRef(days);
  const selectRef = useRef(onSelectDay);
  useEffect(() => { daysRef.current = days; }, [days]);
  useEffect(() => { selectRef.current = onSelectDay; }, [onSelectDay]);

  useEffect(() => {
    if (!elRef.current) return;
    const instance = echarts.init(elRef.current);
    // 事件跟实例生命周期绑死（不经过 state）：StrictMode 下会 mount→unmount→mount，
    // 用 state 传实例时 cleanup 的 setChart(null) 可能盖掉新实例，导致事件丢失
    const onClick = (params: { dataIndex: number }) => {
      const day = daysRef.current[params.dataIndex];
      if (day) selectRef.current?.(day.date);
    };
    instance.on('click', onClick);
    setChart(instance);
    const ro = new ResizeObserver(() => instance.resize());
    ro.observe(elRef.current);
    return () => {
      ro.disconnect();
      instance.off('click', onClick);
      instance.dispose();
      setChart(null);
    };
  }, []);

  useEffect(() => {
    if (!chart) return;
    const option: EChartsCoreOption = {
      grid: { left: 8, right: 12, top: 34, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: {
        right: 0,
        top: 0,
        icon: 'roundRect',
        itemWidth: 10,
        itemHeight: 10,
        textStyle: { color: AXIS_COLOR },
      },
      xAxis: {
        type: 'category',
        data: days.map(d => d.date.slice(5)),
        axisTick: { show: false },
        axisLine: { lineStyle: { color: SPLIT_COLOR } },
        axisLabel: { color: AXIS_COLOR },
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: SPLIT_COLOR } },
        axisLabel: { color: AXIS_COLOR },
      },
      series: SERIES.map(s => ({
        name: s.name,
        type: 'bar',
        stack: 'total',
        barMaxWidth: 30,
        cursor: 'pointer',                    // 提示柱子可点（跳当天日志）
        emphasis: { focus: 'series' },
        itemStyle: { color: s.color },
        data: days.map(d => d[s.key]),
      })),
    };
    chart.setOption(option, true);
  }, [chart, days]);

  // 高度压到 150：整页不滚动的前提下，把剩余高度让给下方故障列表（那才是要操作的地方）
  return <div ref={elRef} style={{ width: '100%', height: 150 }} />;
};

export default OverviewTab;
