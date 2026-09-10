import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Card, Input, List, Select, Space, Spin, Switch, Tag, Typography } from 'antd';
import { ClearOutlined, ReloadOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import {
  LogLine,
  LogRangeResult,
  LogSegment,
  listLogSegments,
  queryLogRange,
  tailSystemLogs,
} from '../../api/client';
import AppEmpty from '../../components/common/AppEmpty';
import {
  GAP_OPTIONS,
  LEVEL_OPTIONS,
  MAX_LOG_LINES,
  POLL_INTERVAL,
  RANGE_LIMIT,
  SEGMENT_POLL_EVERY,
  TAIL_LIMIT,
  inSegment,
  levelColor,
  recentDates,
  renderLogItem,
  shortTs,
} from './shared';
import type { AppInstance } from './shared';

const { Text } = Typography;

// ==================== Tab3：实时系统日志（实时流 + 条数 + 时间段） ====================

/**
 * 实时日志 Tab（仅 super_admin）：
 * - 实时日志流：按天文件 tail 字节游标增量 + 5s 轮询（「自动刷新」开关默认开）；
 *   倒序最新在上；关键字/级别过滤作用已加载行；「清空」清前端缓存并归位游标
 * - 条数记录：已加载/当前显示行数 + 分级别小计（INFO/WARNING/ERROR/DEBUG）
 * - 日志时间段：按「时间空档」把日志切成输出段（左侧列表：起止时间 + 条数 +
 *   ERROR 数），点击某段只看该区间日志（「全部日志」返回）；缓冲能完整覆盖时
 *   本地过滤，不够时调后端按时间段查询；时间段摘要降频刷新（每 3 个轮询周期）
 */
const LiveLogTab: React.FC<{ app: AppInstance; active: boolean }> = ({ app, active }) => {
  const { message } = app;

  const today = dayjs().format('YYYY-MM-DD');
  const [selectedDate, setSelectedDate] = useState(today);
  const [lines, setLines] = useState<LogLine[]>([]); // 正序（旧→新），渲染时倒序
  const offsetRef = useRef<number>(-1); // 字节游标（-1 = 尾部模式，首次加载/切换日期用）
  const [keyword, setKeyword] = useState('');
  const [levelFilter, setLevelFilter] = useState<string[]>([]);
  const [paused, setPaused] = useState(false); // true = 暂停自动刷新
  // 时间段（输出段）导航
  const [segments, setSegments] = useState<LogSegment[]>([]);
  const [segTruncated, setSegTruncated] = useState(false);
  const [gapSeconds, setGapSeconds] = useState(60);
  const [selectedSeg, setSelectedSeg] = useState<LogSegment | null>(null);
  // 选中时间段的日志：null = 用已加载缓冲本地过滤；非 null = 后端按区间查询结果
  const [rangeData, setRangeData] = useState<LogRangeResult | null>(null);
  const [rangeLoading, setRangeLoading] = useState(false);
  const tickRef = useRef(0); // 轮询计数（驱动时间段降频刷新）
  const gapInitedRef = useRef(false); // 空档选项首屏不重复拉取

  // 最近 7 天日期下拉（倒序：今天在最上）
  const dateOptions = useMemo(() => recentDates().reverse().map(d => ({ value: d, label: d })), []);

  const fetchTail = useCallback(async (initial = false) => {
    try {
      const res = await tailSystemLogs(selectedDate, initial ? -1 : offsetRef.current, TAIL_LIMIT);
      offsetRef.current = res.data.offset;
      if (res.data.lines.length > 0) {
        setLines(prev => {
          const merged = [...prev, ...res.data.lines];
          return merged.length > MAX_LOG_LINES
            ? merged.slice(merged.length - MAX_LOG_LINES)
            : merged;
        });
      }
    } catch {
      if (initial) message.error('加载系统日志失败');
    }
  }, [selectedDate, message]);

  // 时间段摘要（起止时间/条数/级别小计）；失败静默（不影响日志流）
  const fetchSegments = useCallback(async () => {
    try {
      const res = await listLogSegments(selectedDate, gapSeconds);
      setSegments(res.data.segments);
      setSegTruncated(res.data.truncated);
    } catch {
      // 静默
    }
  }, [selectedDate, gapSeconds]);

  // 挂载 + 切换日期：清缓存/选中态、游标归位、重新加载日志与时间段
  useEffect(() => {
    setLines([]);
    setSegments([]);
    setSelectedSeg(null);
    setRangeData(null);
    offsetRef.current = -1;
    void fetchTail(true);
    void fetchSegments();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate]);

  // 调整空档选项 → 重新切分时间段（首屏由上方 effect 负责）
  const fetchSegmentsRef = useRef(fetchSegments);
  useEffect(() => { fetchSegmentsRef.current = fetchSegments; }, [fetchSegments]);
  useEffect(() => {
    if (!gapInitedRef.current) {
      gapInitedRef.current = true;
      return;
    }
    void fetchSegmentsRef.current();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gapSeconds]);

  // 5s 轮询：拉新行；每 SEGMENT_POLL_EVERY 个周期刷新一次时间段摘要。
  // 暂停或切走本 Tab 即停，切回立即续拉一次（避免后台空转/数据过期）
  const fetchTailRef = useRef(fetchTail);
  useEffect(() => { fetchTailRef.current = fetchTail; }, [fetchTail]);
  useEffect(() => {
    if (paused || !active) return;
    void fetchTailRef.current(false);
    const timer = setInterval(() => {
      void fetchTailRef.current(false);
      tickRef.current += 1;
      if (tickRef.current % SEGMENT_POLL_EVERY === 0) void fetchSegmentsRef.current();
    }, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [paused, active]);

  // 清空日志行缓存：清前端缓存 + 游标归位（下次拉取回到尾部最近行）+ 退出区间视图
  const clearLines = () => {
    setLines([]);
    offsetRef.current = -1;
    setSelectedSeg(null);
    setRangeData(null);
  };

  // 点击时间段：只看该区间日志。缓冲能完整覆盖（本地过滤条数一致）时直接本地
  // 过滤，否则调后端按时间段查询（区间无标准行时后端无法过滤，退回本地）
  const openSegment = async (seg: LogSegment) => {
    const local = lines.filter(l => inSegment(l, seg));
    setSelectedSeg(seg);
    if (!seg.start_ts || !seg.end_ts || local.length === seg.count) {
      setRangeData(null);
      return;
    }
    setRangeLoading(true);
    setRangeData(null);
    try {
      const res = await queryLogRange(selectedDate, seg.start_ts, seg.end_ts, RANGE_LIMIT);
      setRangeData(res.data);
    } catch {
      message.error('加载该时间段日志失败');
      setSelectedSeg(null);
    } finally {
      setRangeLoading(false);
    }
  };

  // 返回全部（退出时间段筛选，回到实时流）
  const backToAll = () => {
    setSelectedSeg(null);
    setRangeData(null);
  };

  // 当前展示的日志行：实时流 / 时间段（后端区间结果或本地过滤）
  const displayLines = useMemo(() => {
    if (!selectedSeg) return lines;
    if (rangeData) return rangeData.lines;
    return lines.filter(l => inSegment(l, selectedSeg));
  }, [lines, selectedSeg, rangeData]);

  // 过滤（仅作用已展示行）+ 倒序（最新在上）
  const filteredLines = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    const out = displayLines.filter(l =>
      (!levelFilter.length || (l.level != null && levelFilter.includes(l.level)))
      && (!kw || l.line.toLowerCase().includes(kw)));
    return [...out].reverse();
  }, [displayLines, keyword, levelFilter]);

  // 分级别小计（当前展示行）
  const levelCounts = useMemo(() => {
    const m: Record<string, number> = {};
    for (const l of displayLines) {
      const k = l.level ?? 'OTHER';
      m[k] = (m[k] ?? 0) + 1;
    }
    return m;
  }, [displayLines]);

  const filtering = levelFilter.length > 0 || keyword.trim() !== '';
  // 时间段列表倒序展示（最新段在最上，与日志流方向一致；接口返回时间正序）
  const orderedSegments = useMemo(() => [...segments].reverse(), [segments]);
  // 选中时间段时后端结果被 limit 截断的提示（区间条数多时只回最后 RANGE_LIMIT 行）
  const rangeTruncated = !!selectedSeg && !!rangeData && rangeData.truncated;

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <Space wrap style={{ marginBottom: 16, flexShrink: 0 }}>
        <Select
          value={selectedDate}
          onChange={setSelectedDate}
          options={dateOptions}
          style={{ width: 130 }}
        />
        <Switch
          size="small"
          checked={!paused}
          onChange={v => setPaused(!v)}
          checkedChildren="自动刷新"
          unCheckedChildren="已暂停"
        />
        <Select
          value={gapSeconds}
          onChange={setGapSeconds}
          options={GAP_OPTIONS}
          style={{ width: 120 }}
        />
        <Input
          allowClear
          placeholder="关键字过滤（已加载行）"
          style={{ width: 190 }}
          value={keyword}
          onChange={e => setKeyword(e.target.value)}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder="级别过滤"
          style={{ minWidth: 180 }}
          value={levelFilter}
          onChange={setLevelFilter}
          options={LEVEL_OPTIONS}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void fetchTail(false)}>刷新</Button>
        <Button icon={<ClearOutlined />} onClick={clearLines}>清空</Button>
      </Space>

      {/* 条数记录：当前展示行数 + 分级别小计（过滤生效时附「显示 N 行」） */}
      <Space wrap size={8} style={{ marginBottom: 12, flexShrink: 0 }}>
        <Text type="secondary">
          {selectedSeg ? '当前时间' : '已加载'} {displayLines.length} 行
          {filtering ? `（过滤后显示 ${filteredLines.length} 行）` : ''}
          ，保留最近 {MAX_LOG_LINES} 行
        </Text>
        {LEVEL_OPTIONS.map(o => (
          <Tag key={o.value} color={levelColor(o.value)}>
            {o.value} {levelCounts[o.value] ?? 0}
          </Tag>
        ))}
        {(levelCounts.OTHER ?? 0) > 0 && <Tag>其他 {levelCounts.OTHER}</Tag>}
      </Space>

      {/* 主体：左侧时间段列表（点击只看该区间）；右侧日志流（flex 撑满、内部滚动） */}
      <div style={{ flex: 1, minHeight: 0, display: 'flex', gap: 12 }}>
        <Card
          size="small"
          title="日志时间段"
          style={{
            width: 260,
            flexShrink: 0,
            display: 'flex',
            flexDirection: 'column',
            minHeight: 0,
          }}
          styles={{ body: { flex: 1, minHeight: 0, overflowY: 'auto', padding: 0 } }}
          extra={<Text type="secondary" style={{ fontSize: 12 }}>共 {segments.length} 段</Text>}
        >
          {/* 「全部日志」入口（取消时间段筛选） */}
          <div
            onClick={backToAll}
            style={{
              cursor: 'pointer',
              padding: '6px 12px',
              background: selectedSeg ? undefined : 'rgba(22, 119, 255, 0.08)',
            }}
          >
            <Space size={6}>
              <Text strong={!selectedSeg}>全部日志</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>{lines.length} 行</Text>
            </Space>
          </div>
          <List
            size="small"
            dataSource={orderedSegments}
            locale={{
              emptyText: <Text type="secondary" style={{ fontSize: 12 }}>暂无时间段</Text>,
            }}
            renderItem={seg => {
              // 段的 start_ts 稳定（起点不变），用它判定选中（刷新后 end_ts/count 会变）
              const isSelected = !!selectedSeg && seg.start_ts === selectedSeg.start_ts;
              const errCount = seg.levels.ERROR ?? 0;
              return (
                <List.Item
                  onClick={() => void openSegment(seg)}
                  style={{
                    cursor: 'pointer',
                    padding: '6px 12px',
                    background: isSelected ? 'rgba(22, 119, 255, 0.08)' : undefined,
                  }}
                >
                  <div style={{ width: '100%' }}>
                    <Space size={6} style={{ width: '100%', justifyContent: 'space-between' }}>
                      <Text style={{ fontSize: 12 }}>
                        {shortTs(seg.start_ts)} ~ {shortTs(seg.end_ts)}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>{seg.count} 条</Text>
                    </Space>
                    {errCount > 0 && (
                      <Tag color="red" style={{ marginTop: 4 }}>ERROR {errCount}</Tag>
                    )}
                  </div>
                </List.Item>
              );
            }}
          />
          {segTruncated && (
            <div style={{ padding: '6px 12px' }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                文件较大，仅统计最近部分日志段（更早内容请下载文件查看）
              </Text>
            </div>
          )}
        </Card>

        <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          {/* 时间段筛选提示条：返回全部 + 区间条数/截断说明 */}
          {selectedSeg && (
            <Space wrap size={8} style={{ marginBottom: 8, flexShrink: 0 }}>
              <Tag color="blue">
                区间 {shortTs(selectedSeg.start_ts)} ~ {shortTs(selectedSeg.end_ts)}
              </Tag>
              <Text type="secondary">
                该时间段共 {rangeData?.total ?? selectedSeg.count} 条
                {rangeTruncated ? `，仅显示最后 ${rangeData?.lines.length ?? 0} 条` : ''}
              </Text>
              <Button type="link" size="small" onClick={backToAll}>返回全部</Button>
            </Space>
          )}
          <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
            {rangeLoading ? (
              <div style={{ display: 'flex', justifyContent: 'center', padding: '48px 0' }}>
                <Spin size="large" />
              </div>
            ) : (
              <List
                size="small"
                dataSource={filteredLines}
                locale={{
                  emptyText: (
                    <AppEmpty
                      title="暂无日志"
                      description={paused
                        ? '已暂停自动刷新，点击「刷新」手动拉取'
                        : `${selectedDate} 无日志内容`}
                    />
                  ),
                }}
                renderItem={renderLogItem}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default LiveLogTab;
