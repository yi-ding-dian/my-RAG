import React, { useEffect, useMemo, useState } from 'react';
import AppModal from '../../../shared/components/common/AppModal';
import AppEmpty from '../../../shared/components/common/AppEmpty';
import { Alert, Spin, Tree, Typography } from 'antd';
import type { DocxOutlineItem } from '../../../shared/api/client';
import { asApiError, getDocxOutline } from '../../../shared/api/client';

const { Text } = Typography;

interface DocxOutlineModalProps {
  open: boolean;
  onCancel: () => void;
  /** 文档所属知识库 ID（缺失时不请求） */
  kbId?: string;
  /** 文档 ID（缺失时不请求） */
  docId?: string;
  /** 文档展示名（弹窗标题用） */
  fileName?: string;
}

/** 树节点（antd Tree treeData 形态） */
interface OutlineTreeNode {
  key: string;
  title: string;
  children: OutlineTreeNode[];
}

/**
 * 扁平标题列表（文档顺序）→ 层级树：按 level 维护祖先栈，level=N 的节点
 * 挂在栈顶（最近的更浅层级）之下并成为新的栈顶；层级跳跃（如一级标题后
 * 直接出现三级标题）挂到最近的上层节点下，标题不丢。
 */
const buildTree = (items: DocxOutlineItem[]): OutlineTreeNode[] => {
  const roots: OutlineTreeNode[] = [];
  const stack: OutlineTreeNode[] = [];
  items.forEach((item, index) => {
    const node: OutlineTreeNode = { key: `n${index}`, title: item.title, children: [] };
    const level = Math.max(1, Math.floor(item.level) || 1);
    stack.length = Math.min(stack.length, level - 1);
    const parent = stack[stack.length - 1];
    (parent ? parent.children : roots).push(node);
    stack.push(node);
  });
  return roots;
};

/**
 * 文档结构弹窗（「查看文档结构」）：预览结构解析（docx_struct）会把这份文档
 * 解析成什么样的标题层级树（层级 + 标题文本，Word 自动编号已还原），供用户
 * 在解析前确认；仅 docx/doc 文档可用（后端仅这两类支持）。
 *
 * 入口：文档画像「引擎建议」的"结构解析"标签（主）、解析配置弹窗选中
 * "结构解析"时的链接文字（副）。
 * 滚动结构复用 .chunk-detail-modal（头部固定，仅内容区内部滚动），不自造样式。
 */
const DocxOutlineModal: React.FC<DocxOutlineModalProps> = ({
  open,
  onCancel,
  kbId,
  docId,
  fileName,
}) => {
  const [items, setItems] = useState<DocxOutlineItem[]>([]);
  const [count, setCount] = useState(0);
  const [maxLevel, setMaxLevel] = useState(0);
  const [warning, setWarning] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !kbId || !docId) return;
    let cancelled = false;
    setItems([]);
    setCount(0);
    setMaxLevel(0);
    setWarning(null);
    setError(null);
    setLoading(true);
    getDocxOutline(kbId, docId)
      .then(res => {
        if (cancelled) return;
        setItems(res.data.items ?? []);
        setCount(res.data.count ?? 0);
        setMaxLevel(res.data.max_level ?? 0);
        setWarning(res.data.warning ?? null);
      })
      .catch(e => {
        if (cancelled) return;
        // 400=非 Word 文档 / 404=文档不存在或无权限 / 其他=服务异常
        setError(asApiError(e).response?.data?.detail || '加载文档结构失败，请稍后重试');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, kbId, docId]);

  const treeData = useMemo(() => buildTree(items), [items]);

  return (
    <AppModal
      dimension="resizable"
      defaultSize={{ w: 640, h: 620 }}
      rememberKey="docx-outline"
      className="chunk-detail-modal"
      title={`查看文档结构${fileName ? ` - ${fileName}` : ''}`}
      open={open}
      onCancel={onCancel}
      footer={null}
      destroyOnHidden
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 60 }}>
          <Spin />
        </div>
      ) : error ? (
        <Alert
          type="warning"
          showIcon
          message="无法加载文档结构"
          description={error}
          style={{ margin: 16 }}
        />
      ) : (
        <div style={{ padding: '4px 16px 12px' }}>
          {/* 概要行：共 N 个标题 / 最大层级 */}
          <div style={{ marginBottom: 12 }}>
            <Text strong>文档结构</Text>
            <Text type="secondary">
              {' · '}共 {count} 个标题 · {maxLevel} 级
            </Text>
          </div>
          {warning && (
            <Alert type="warning" showIcon message={warning} style={{ marginBottom: 12 }} />
          )}
          {items.length === 0 ? (
            <AppEmpty
              title="未检测到标题结构"
              description={
                warning
                  ? '文档结构提取异常，请检查文件后重试'
                  : '该文档没有可识别的标题层级（解析将按普通段落处理）'
              }
            />
          ) : (
            <Tree
              // 文档切换时重挂载：defaultExpandAll 只对首次渲染生效
              key={docId ?? 'outline'}
              defaultExpandAll
              selectable={false}
              treeData={treeData}
            />
          )}
        </div>
      )}
    </AppModal>
  );
};

export default DocxOutlineModal;
