import React from 'react';
import { Card, Typography, Row, Col, Divider, Tag } from 'antd';
import {
  DatabaseOutlined, CheckSquareOutlined, FileTextOutlined,
  SettingOutlined, ThunderboltOutlined, BarChartOutlined,
} from '@ant-design/icons';

interface FlowStep {
  icon: React.ReactNode;
  title: string;
  desc: string;
  details: string[];
  color: string;
}

const steps: FlowStep[] = [
  {
    icon: <DatabaseOutlined style={{ fontSize: 28 }} />,
    title: '数据集准备',
    desc: '上传 CSV/JSON 格式的评估数据',
    details: ['支持 CSV、JSON 格式上传', '需包含 question、answer、contexts 等列', '支持手动添加和编辑样本'],
    color: '#1890ff',
  },
  {
    icon: <CheckSquareOutlined style={{ fontSize: 28 }} />,
    title: '选择评估指标',
    desc: '选择需要评估的 RAGAS 指标',
    details: ['忠实度：答案是否忠于上下文', '答案相关性：答案与问题的相关度', '上下文精确度/召回率等'],
    color: '#52c41a',
  },
  {
    icon: <FileTextOutlined style={{ fontSize: 28 }} />,
    title: '提示词配置',
    desc: '设置评估提示词语言和内容',
    details: ['支持中文 / English 自由切换', 'AI 自动翻译提示词', '可手动编辑和调整提示词'],
    color: '#722ed1',
  },
  {
    icon: <SettingOutlined style={{ fontSize: 28 }} />,
    title: 'LLM 参数',
    desc: '配置评估模型的运行参数',
    details: ['Temperature：评分确定性（建议 0）', 'Max Tokens：输出上限（建议 512）', '并发数：同时评分的请求数量'],
    color: '#fa8c16',
  },
  {
    icon: <ThunderboltOutlined style={{ fontSize: 28 }} />,
    title: '执行评估',
    desc: '后台运行 RAGAS 评估管道',
    details: ['线程池异步执行，不阻塞服务', '支持取消正在运行的任务', '实时日志追踪每步进度'],
    color: '#f5222d',
  },
  {
    icon: <BarChartOutlined style={{ fontSize: 28 }} />,
    title: '结果分析',
    desc: '查看评分详情与可视化报告',
    details: ['聚合评分：雷达图 + 柱状图', '逐条查看各指标得分', '导出 JSON / CSV / HTML 报告'],
    color: '#13c2c2',
  },
];

const FlowPage: React.FC = () => {
  return (
    <div>
      <Typography.Title level={4}>RAGAS 评估工作流程</Typography.Title>
      <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
        完整了解从数据准备到结果分析的评估全流程
      </Typography.Text>

      {/* 流程图 — 卡片式横向排列 */}
      <div style={{ overflowX: 'auto', padding: '24px 0' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', minWidth: 900 }}>
          {steps.map((step, idx) => (
            <React.Fragment key={step.title}>
              <div style={{ flex: 1, minWidth: 140 }}>
                <div style={{
                  display: 'flex', flexDirection: 'column', alignItems: 'center', textAlign: 'center',
                }}>
                  {/* 步骤圆 + 序号 */}
                  <div style={{
                    width: 64, height: 64, borderRadius: 32,
                    background: `linear-gradient(135deg, ${step.color}22, ${step.color}44)`,
                    border: `2px solid ${step.color}`,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: step.color, marginBottom: 12,
                  }}>
                    {step.icon}
                  </div>
                  {/* 步骤标题 */}
                  <Typography.Text strong style={{ fontSize: 15, marginBottom: 4 }}>
                    {step.title}
                  </Typography.Text>
                  {/* 步骤描述 */}
                  <Typography.Text type="secondary" style={{ fontSize: 12, marginBottom: 8, padding: '0 4px' }}>
                    {step.desc}
                  </Typography.Text>
                  {/* 序号标签 */}
                  <Tag color={step.color} style={{ borderRadius: 10 }}>{idx + 1}</Tag>
                </div>
              </div>
              {/* 连接箭头 */}
              {idx < steps.length - 1 && (
                <div style={{
                  flex: '0 0 40px', display: 'flex', alignItems: 'center', justifyContent: 'center',
                  paddingTop: 32,
                }}>
                  <svg width="40" height="24" viewBox="0 0 40 24">
                    <defs>
                      <marker id={`arrow-${idx}`} markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
                        <polygon points="0 0, 10 3.5, 0 7" fill="#bbb" />
                      </marker>
                    </defs>
                    <line x1="0" y1="12" x2="35" y2="12" stroke="#bbb" strokeWidth="2"
                      strokeDasharray="5,3" markerEnd={`url(#arrow-${idx})`} />
                  </svg>
                </div>
              )}
            </React.Fragment>
          ))}
        </div>
      </div>

      <Divider />

      {/* 详细说明卡片 */}
      <Typography.Title level={5} style={{ marginBottom: 16 }}>各步骤详解</Typography.Title>
      <Row gutter={[16, 16]}>
        {steps.map((step, idx) => (
          <Col xs={24} sm={12} lg={8} key={step.title}>
            <Card
              size="small"
              style={{ height: '100%', borderTop: `3px solid ${step.color}` }}
            >
              <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
                <Tag color={step.color} style={{ marginRight: 8 }}>{idx + 1}</Tag>
                <Typography.Text strong>{step.title}</Typography.Text>
              </div>
              <ul style={{ paddingLeft: 20, margin: 0, color: '#666', lineHeight: 2, fontSize: 13 }}>
                {step.details.map(d => <li key={d}>{d}</li>)}
              </ul>
            </Card>
          </Col>
        ))}
      </Row>
    </div>
  );
};

export default FlowPage;
