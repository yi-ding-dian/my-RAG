import React from 'react';
import { Button, Card, Col, Row, Space, Tag, Tooltip, Typography, theme } from 'antd';
import {
  CheckOutlined, CheckCircleFilled, CloseCircleFilled,
  DeleteOutlined, EditOutlined, LoadingOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import type { LLMModelItem, ServiceProfile } from '../../shared/api/client';
import { DOMAIN_CARDS, emptyTest, sectionLabel } from './shared';
import type { SectionKey, TestItem } from './shared';

const { Text } = Typography;

/** 单条连接测试结果（测试中 / 成功 / 失败） */
const TestLine: React.FC<{ item: TestItem; label: string }> = ({ item, label }) => {
  if (item.status === 'idle') return null;
  return (
    <div style={{ marginTop: 2, fontSize: 12 }}>
      <Text type="secondary" style={{ marginRight: 8 }}>{label}:</Text>
      {item.status === 'testing' ? (
        <Tag icon={<LoadingOutlined spin />} color="processing">测试中...</Tag>
      ) : item.status === 'success' ? (
        <Text type="success"><CheckCircleFilled /> {item.msg}</Text>
      ) : (
        <Text type="danger"><CloseCircleFilled /> {item.msg}</Text>
      )}
    </div>
  );
};

/** 档案卡片：域卡快捷导航（点击直达对应编辑折叠）+ 各段当前值摘要 + 连接测试结果 */
interface ProfileCardProps {
  profile: ServiceProfile;
  /** 本档案的连接测试状态（缺省 = 全 idle，不显示测试结果行） */
  testState?: Record<SectionKey, TestItem>;
  /** 只读（非超管）：隐藏编辑/删除/激活入口 */
  readOnly: boolean;
  /** 删除按钮是否禁用：当前使用中且只剩这一份时不可删 */
  deleteDisabled: boolean;
  /** 正在测试的域卡（`${profileId}:${domainKey}`） */
  domainTesting: string;
  onEdit: (panel?: string) => void;
  onActivate: () => void;
  onDelete: () => void;
  onTest: () => void;
  onDomainTest: (domainKey: string, sections: SectionKey[]) => void;
}

const ProfileCard: React.FC<ProfileCardProps> = ({
  profile: p, testState, readOnly, deleteDisabled, domainTesting,
  onEdit, onActivate, onDelete, onTest, onDomainTest,
}) => {
  const { token } = theme.useToken();
  const isActive = p.active;
  const tests = testState || emptyTest;
  // LLM 段摘要：激活模型（后端已统一为 {models, active} 结构）
  const llmSec = p.llm as unknown as {
    models?: LLMModelItem[]; active?: number;
  };
  const llmModelList = Array.isArray(llmSec?.models) ? llmSec.models : [];
  const activeLlm = llmModelList[llmSec?.active ?? 0] ?? null;

  return (
    <Card
      size="small"
      style={{
        marginBottom: 12,
        border: isActive ? '2px solid var(--brand-primary, #2563eb)' : '1px solid #eef2f7',
        background: isActive ? 'rgba(var(--brand-primary-rgb, 37, 99, 235), 0.06)' : undefined,
        boxShadow: '0 1px 3px rgba(16,24,40,0.04)',
        transition: 'border-color 0.2s ease, box-shadow 0.2s ease',
      }}
      title={
        <Space>
          {isActive && <Tag color="blue" icon={<CheckOutlined />}>当前使用</Tag>}
          <Text strong>{p.name}</Text>
        </Space>
      }
      extra={
        <Space size="small">
          {!isActive && !readOnly && (
            <Tooltip title="设为当前使用">
              <Button size="small" type="primary" icon={<CheckOutlined />}
                onClick={onActivate} />
            </Tooltip>
          )}
          {!readOnly && (
            <Tooltip title="编辑">
              <Button size="small" icon={<EditOutlined />}
                onClick={() => onEdit()} />
            </Tooltip>
          )}
          <Tooltip title="连接测试">
            <Button size="small" icon={<ThunderboltOutlined />} onClick={onTest}>
              测试连接
            </Button>
          </Tooltip>
          {!readOnly && (
            <Tooltip title="删除">
              <Button size="small" danger icon={<DeleteOutlined />}
                disabled={deleteDisabled}
                onClick={onDelete} />
            </Tooltip>
          )}
        </Space>
      }
    >
      {/* 配置域快捷导航（方案 A：概览在下、点域卡片直达对应编辑折叠，不再全量一张表） */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
        {DOMAIN_CARDS.map(d => (
          <div
            key={d.key}
            onClick={() => !readOnly && onEdit(d.key)}
            style={{
              display: 'flex', alignItems: 'center', gap: 8,
              padding: '6px 12px', borderRadius: 8,
              cursor: readOnly ? 'default' : 'pointer',
              background: 'rgba(var(--brand-primary-rgb, 37, 99, 235), 0.05)',
              border: '1px solid rgba(var(--brand-primary-rgb, 37, 99, 235), 0.18)',
              transition: 'all 0.2s',
            }}
          >
            <Text strong style={{ fontSize: 12, color: 'var(--brand-primary, #2563eb)' }}>
              {d.title}
            </Text>
            <Text type="secondary" style={{ fontSize: 11, maxWidth: 240 }}
              ellipsis={{ tooltip: d.summary(p) }}>
              {d.summary(p)}
            </Text>
            {d.sections && (
              <Tooltip title="测试连接（使用档案配置）">
                <Button
                  type="text"
                  size="small"
                  icon={<ThunderboltOutlined />}
                  loading={domainTesting === `${p.id}:${d.key}`}
                  onClick={e => {
                    e.stopPropagation();
                    onDomainTest(d.key, d.sections!);
                  }}
                />
              </Tooltip>
            )}
          </div>
        ))}
      </div>
      <Row gutter={[16, 4]}>
        <Col xs={24} md={12}>
          <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.llm}</Text>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            {activeLlm ? (
              <>
                <Tag color="blue" style={{ fontSize: 11 }}>激活: {activeLlm.name}</Tag>
                <Text code style={{ fontSize: 12 }}>{activeLlm.model || '-'}</Text>
                <Text style={{ fontSize: 12 }}>{activeLlm.base_url}</Text>
                <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>
                  {llmModelList.length > 1 ? `共 ${llmModelList.length} 个模型` : '单模型'}
                </Text>
              </>
            ) : (
              <Text style={{ fontSize: 12 }}>-</Text>
            )}
          </div>
        </Col>
        <Col xs={24} md={12}>
          <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.embedding}</Text>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Text code style={{ fontSize: 12 }}>{p.embedding?.model || '-'}</Text>
            <Text style={{ fontSize: 12 }}>{p.embedding?.base_url}</Text>
            <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>dim={p.embedding?.dimension}</Text>
          </div>
        </Col>
        <Col xs={24} md={12}>
          <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.mineru}</Text>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Text style={{ fontSize: 12 }}>{p.mineru?.url || '-'}</Text>
          </div>
        </Col>
        <Col xs={24} md={12}>
          <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.deepdoc}</Text>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Text style={{ fontSize: 12 }}>{p.deepdoc?.base_url || '-'}</Text>
          </div>
        </Col>
        <Col xs={24} md={12}>
          <Text type="secondary" style={{ fontSize: 12 }}>检索 / 切块</Text>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Text style={{ fontSize: 12 }}>top_k={p.retrieval?.top_k}</Text>
            <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>
              hybrid={p.retrieval?.enable_hybrid === false ? '关' : '开'}
            </Text>
            <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>
              rerank={p.retrieval?.rerank?.enabled
                ? `开(${p.retrieval?.rerank?.model || '-'})` : '关'}
            </Text>
            <Text style={{ fontSize: 12, color: token.colorTextTertiary }}>
              chunk={p.chunking?.chunk_size}/{p.chunking?.overlap}
            </Text>
          </div>
        </Col>
        <Col xs={24} md={12}>
          <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.mysql}</Text>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Text style={{ fontSize: 12 }}>
              {p.mysql
                ? p.mysql.url
                  ? String(p.mysql.url).slice(0, 60)
                  : `${p.mysql.host}:${p.mysql.port}/${p.mysql.database || ''}`
                : '-'}
            </Text>
          </div>
        </Col>
        <Col xs={24} md={12}>
          <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.minio}</Text>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Text style={{ fontSize: 12 }}>
              {p.minio ? `${p.minio.endpoint}/${p.minio.bucket}` : '-'}
            </Text>
          </div>
        </Col>
        <Col xs={24} md={12}>
          <Text type="secondary" style={{ fontSize: 12 }}>{sectionLabel.vector_store}</Text>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <Text style={{ fontSize: 12 }}>
              {p.vector_store
                ? p.vector_store.backend === 'milvus'
                  ? `Milvus: ${p.vector_store.milvus_uri || '-'}`
                  : 'Chroma（本地嵌入式）'
                : '-'}
            </Text>
          </div>
        </Col>
        <Col xs={24}>
          <TestLine item={tests.llm} label={sectionLabel.llm} />
          <TestLine item={tests.embedding} label={sectionLabel.embedding} />
          <TestLine item={tests.mineru} label={sectionLabel.mineru} />
          <TestLine item={tests.deepdoc} label={sectionLabel.deepdoc} />
          <TestLine item={tests.mysql} label={sectionLabel.mysql} />
          <TestLine item={tests.minio} label={sectionLabel.minio} />
          <TestLine item={tests.vector_store} label={sectionLabel.vector_store} />
        </Col>
      </Row>
    </Card>
  );
};

export default ProfileCard;
