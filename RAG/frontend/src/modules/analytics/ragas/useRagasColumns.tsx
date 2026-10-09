/**
 * RAGAS 任务表的列定义（含列宽拖拽、分数对比、取消任务）。
 *
 * 从 RagasDetail 抽出来：这一坨（7 个普通列 + 分数列 + 操作列，约 130 行）
 * 是纯粹的"数据 → 列配置"，却占了页面文件里最长的一段，把弹窗、轮询、
 * 表单状态都挤到几百行之后。
 *
 * 依赖通过 options 注入，而不是让 hook 直接读页面状态：
 * - `tasks`：算"同一评估集上一次分数"（分数列的对比标签）
 * - `onOpenReport`：点「查看报告」——报告弹窗由页面持有
 * - `onRefresh`：取消任务成功后刷新列表
 *
 * 列宽拖拽的 `useResizableColumns` 也一并收进来：它的产物
 * （colWidths / handleResize / tableWidth）只服务这张表，留在页面里是多余的暴露。
 */
import { useMemo } from 'react';
import {
  App as AntApp, Button, Popconfirm, Space, Tag, Tooltip, Typography,
} from 'antd';
import { EyeOutlined, StopOutlined, SyncOutlined } from '@ant-design/icons';

import { cancelRagasEvaluation, RagasTask } from '../../../shared/api/client';
import { useAuth } from '../../../shared/auth/AuthContext';
import { useResizableColumns } from '../../../shared/hooks/useResizableColumns';
import { statusOf, scoreColor } from '../shared';
import { avgScore, metricLabel, metricLabelMap } from './ragas-shared';

const { Text } = Typography;

export interface RagasColumnsOptions {
  /** 任务列表（用于算"同一评估集上一次分数"） */
  tasks: RagasTask[];
  /** 点「查看报告」——报告弹窗由页面持有 */
  onOpenReport: (task: RagasTask) => void;
  /** 取消任务成功后刷新列表 */
  onRefresh: () => void;
}

export function useRagasColumns({
  tasks, onOpenReport, onRefresh,
}: RagasColumnsOptions) {
  const { message } = AntApp.useApp();
  const { user } = useAuth();
  const { colWidths, handleResize, tableWidth } = useResizableColumns<RagasTask>();

  // 同一评估集 + **同一评委模型** + **同一回答来源**的"上一次"平均分：按这三者分组，
  // 组内按创建时间升序，每个任务取它前一个的分数。
  // 为什么三项都要进分组键：换评委换的是评分标准、换 answer 来源换的是被测对象
  // （题集答案→测检索 / 实时生成→测端到端），任一不同分数都不可直接比——
  // 混在一起算差值会把"测法变了"误读成"质量变了"，那正是评估集要避免的事
  const prevScoreByTask = useMemo(() => {
    const byKey = new Map<string, RagasTask[]>();
    for (const t of tasks) {
      const ds = t.local_dataset_id;
      if (!ds) continue;
      const key = `${ds}|${t.eval_model || ''}|${t.answer_source || 'dataset'}`;
      const arr = byKey.get(key);
      if (arr) arr.push(t);
      else byKey.set(key, [t]);
    }
    const out = new Map<string, number>();
    for (const arr of byKey.values()) {
      const sorted = [...arr].sort(
        (a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
      for (let i = 1; i < sorted.length; i++) {
        const prev = avgScore(sorted[i - 1].scores);
        if (prev !== null) out.set(sorted[i].id, prev);
      }
    }
    return out;
  }, [tasks]);

  // 取消按钮显隐（与后端权限一致）：仅运行中/排队中任务，且为发起人本人或
  // super_admin（dept_admin 本人发起同样命中 user_id 比对；本部门其他用户
  // 发起的任务后端放行、前端从简不显示——权限由后端兜底校验）
  const canCancelTask = (t: RagasTask) =>
    (t.status === 'running' || t.status === 'queued') &&
    (t.user_id === user?.id || user?.role === 'super_admin');

  // 取消评估任务（Popconfirm 确认后调用；成功刷新列表，失败显示后端中文错误）
  const handleCancelTask = async (task: RagasTask) => {
    try {
      await cancelRagasEvaluation(task.id);
      message.success('评估任务已取消');
      onRefresh();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '取消失败，请稍后重试');
    }
  };

  const columns = [
    {
      title: '任务名称', dataIndex: 'name', key: 'name', ellipsis: true,
      width: colWidths.name ?? 260,
      onHeaderCell: () => ({ width: colWidths.name ?? 260, onResize: handleResize('name'), title: '任务名称' }),
      render: (v: string) => <Text strong>{v}</Text>,
    },
    {
      title: '知识库', dataIndex: 'kb_name', key: 'kb_name', ellipsis: true,
      width: colWidths.kb_name ?? 130,
      onHeaderCell: () => ({ width: colWidths.kb_name ?? 130, onResize: handleResize('kb_name'), title: '知识库' }),
      render: (v?: string) => v || <Text type="secondary">—</Text>,
    },
    {
      title: '样本来源', dataIndex: 'source', key: 'source',
      width: colWidths.source ?? 100,
      onHeaderCell: () => ({ width: colWidths.source ?? 100, onResize: handleResize('source'), title: '样本来源' }),
      render: (v?: string) => (v === 'chat'
        ? <Tag color="blue">会话问答</Tag>
        : v === 'logs' ? <Tag color="green">检索日志</Tag>
        : v === 'manual' ? <Tag color="purple">手动填写</Tag>
        : v === 'dataset' ? <Tag color="orange">评估集重跑</Tag>
        : <Text type="secondary">—</Text>),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status',
      width: colWidths.status ?? 110,
      onHeaderCell: () => ({ width: colWidths.status ?? 110, onResize: handleResize('status'), title: '状态' }),
      render: (s: string) => {
        const cfg = statusOf(s);
        return (
          <Tag color={cfg.color}>
            {s === 'running' ? <SyncOutlined spin /> : null} {cfg.label}
          </Tag>
        );
      },
    },
    {
      title: '指标', dataIndex: 'metrics', key: 'metrics',
      width: colWidths.metrics ?? 220,
      onHeaderCell: () => ({ width: colWidths.metrics ?? 220, onResize: handleResize('metrics'), title: '指标' }),
      render: (ms: string[] | undefined) =>
        ms?.length ? ms.map(m => (
          <Tooltip key={m} title={metricLabelMap[m] ? m : undefined}>
            <Tag>{metricLabel(m)}</Tag>
          </Tooltip>
        )) : <Text type="secondary">—</Text>,
    },
    {
      title: '创建时间', dataIndex: 'created_at', key: 'created_at',
      width: colWidths.created_at ?? 170,
      onHeaderCell: () => ({ width: colWidths.created_at ?? 170, onResize: handleResize('created_at'), title: '创建时间' }),
      render: (v?: string) => v || '—',
    },
    {
      title: '完成时间', dataIndex: 'completed_at', key: 'completed_at',
      width: colWidths.completed_at ?? 170,
      onHeaderCell: () => ({ width: colWidths.completed_at ?? 170, onResize: handleResize('completed_at'), title: '完成时间' }),
      render: (v?: string) => v || '—',
    },
    {
      // 平均分 + 与"同一评估集上一次"的对比标签。
      // 这一列才是评估集的价值兑现处：改了检索/切块/提示词后，有无进步一眼可见
      title: '分数', key: 'score', width: 160,
      render: (_: unknown, t: RagasTask) => {
        const cur = avgScore(t.scores);
        // 悬停显示本次评委模型：换过评委的任务，分数不与其它任务直接比
        const tip = t.eval_model ? `评委模型：${t.eval_model}` : undefined;
        if (cur === null) {
          return <Text type="secondary" title={tip}>—</Text>;
        }
        const prev = prevScoreByTask.get(t.id);
        const diff = prev === undefined ? null : cur - prev;
        return (
          <Space size={4} title={tip}>
            <span style={{ color: scoreColor(cur), fontWeight: 600 }}>{cur.toFixed(4)}</span>
            {diff !== null && (
              <Tag color={diff >= 0 ? 'green' : 'red'} style={{ marginInlineEnd: 0 }}>
                {diff >= 0 ? '↑' : '↓'}{Math.abs(diff).toFixed(3)}
              </Tag>
            )}
          </Space>
        );
      },
    },
    {
      // 行尾操作："查看报告"常显；"取消"仅运行中/排队中且有权限时显示
      // （Popconfirm 确认，点击不触发整行报告跳转）
      title: '操作', key: 'action', width: 170,
      render: (_: unknown, record: RagasTask) => (
        <>
          <Button
            size="small"
            type="link"
            icon={<EyeOutlined />}
            style={{ padding: 0, fontSize: 12 }}
            onClick={e => {
              e.stopPropagation();
              onOpenReport(record);
            }}
          >
            查看报告
          </Button>
          {canCancelTask(record) ? (
            <Popconfirm
              title="取消该评估任务？"
              description="取消后任务停止运行，本次评估结果将不可用"
              okText="确认取消"
              okButtonProps={{ danger: true }}
              cancelText="保留"
              onConfirm={() => handleCancelTask(record)}
            >
              <Button
                size="small"
                type="link"
                danger
                icon={<StopOutlined />}
                style={{ padding: 0, fontSize: 12, marginLeft: 8 }}
                onClick={e => e.stopPropagation()}
              >
                取消
              </Button>
            </Popconfirm>
          ) : null}
        </>
      ),
    },
  ];

  return { columns, tableWidth };
}
