/**
 * 用户反馈汇总小卡：总数/好评/差评 + 最近反馈（原因）列表。
 *
 * 从 `../shared`（原 analytics-shared.tsx）挪来：它只服务反馈领域
 * （概览摘要卡内嵌展示，数据同反馈详情页），不占着"公共资源"的位置。
 * 数据由本组件自己拉（getChatFeedbackStats），外部直接用即可。
 */
import React, { useEffect, useState } from 'react';
import { Skeleton, Space, Typography, theme } from 'antd';
import type { ChatFeedbackStats } from '../../../shared/api/other';
import { getChatFeedbackStats } from '../../../shared/api/client';
import { useVisibleCount } from '../shared';

const { Text } = Typography;

/** 「最近反馈」列表显示的条数由卡片实际高度决定（见 useVisibleCount），不接外部参数 */
export const FeedbackSummary: React.FC = () => {
  const { token } = theme.useToken();
  const [stats, setStats] = useState<ChatFeedbackStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    getChatFeedbackStats()
      .then(res => {
        if (!cancelled) setStats(res.data);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 列表条数按卡片剩余高度算：填满且不滚动（见 useVisibleCount）。
  // 必须在下面的早退 return **之前**调用，否则违反 hooks 规则
  const list = useVisibleCount<HTMLDivElement>(28);

  if (loading) return <Skeleton active paragraph={{ rows: 3 }} />;
  if (error || !stats) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        反馈数据加载失败或暂无权限查看
      </Text>
    );
  }
  const rate = stats.total > 0 ? Math.round((stats.up / stats.total) * 100) : 0;
  const recent = stats.recent.slice(0, list.count);
  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <Space size={24} wrap>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>总反馈</Text>
          <div style={{ fontSize: 20, fontWeight: 600 }}>{stats.total}</div>
        </div>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>好评</Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: '#52c41a' }}>{stats.up}</div>
        </div>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>差评</Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: '#ff4d4f' }}>{stats.down}</div>
        </div>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>好评率</Text>
          <div style={{ fontSize: 20, fontWeight: 600, color: token.colorPrimary }}>
            {rate}%
          </div>
        </div>
      </Space>
      {stats.recent.length > 0 && (
        <div style={{
          marginTop: 12, flex: 1, minHeight: 0,
          display: 'flex', flexDirection: 'column',
        }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            最近反馈（最新 {recent.length} 条）
          </Text>
          <div ref={list.ref} style={{ flex: 1, minHeight: 0, overflow: 'hidden' }}>
            {recent.map(r => (
            <div
              key={r.id}
              style={{
                padding: '6px 0',
                borderBottom: `1px solid ${token.colorBorderSecondary}`,
                fontSize: 12,
              }}
            >
              <span
                style={{
                  color: r.rating === 'up' ? '#52c41a' : '#ff4d4f',
                  marginRight: 6,
                }}
              >
                {r.rating === 'up' ? '👍' : '👎'}
              </span>
              {r.reason || <Text type="secondary">（无补充说明）</Text>}
              <Text type="secondary" style={{ marginLeft: 8, fontSize: 11 }}>
                {r.created_at}
              </Text>
              </div>
            ))}
          </div>
          {stats.recent.length > recent.length ? (
            <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 6 }}>
              共 {stats.recent.length} 条反馈，其余见详情页
            </Text>
          ) : null}
        </div>
      )}
      {stats.total === 0 && (
        <Text type="secondary" style={{ fontSize: 12 }}>暂无用户反馈</Text>
      )}
    </div>
  );
};
