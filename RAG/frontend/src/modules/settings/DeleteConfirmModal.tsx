import React from 'react';
import { Space } from 'antd';
import { ExclamationCircleFilled } from '@ant-design/icons';
import AppModal, { AppModalFooter } from '../../shared/components/common/AppModal';
import type { ConfigReference } from '../../shared/api/types';

/** 待确认的一次删除 */
export interface DeleteTarget {
  /** 删的是什么，拼进标题，如 `提示词「工程文档提示词」` */
  title: string;
  /**
   * 正在引用它的地方。
   * - 数组：列出引用方；空数组 = 没人引用，显示"不影响正在运行的配置"；
   * - `null`：这类对象**没有"引用方"这一说**（如配置档案本身），
   *   只说 extraNote 里那句影响说明——否则会和空数组的提示重复一遍。
   */
  references: ConfigReference[] | null;
  /** 引用失效后会自动回退到哪（有引用时才展示），如「会自动回退到全局默认提示词」 */
  fallbackHint?: string;
  /** 额外说明（如删激活档案：会切到哪一份、影响多少部门） */
  extraNote?: React.ReactNode;
  /** 确认后真正执行的删除动作 */
  onConfirm: () => void;
}

/**
 * 删除前的二次确认（配置档案里的删除统一走它）
 *
 * 与普通确认框的差别：先列出**谁在引用**待删的东西，并说清引用失效后会
 * 自动回退到哪儿——删之前让人知道影响面，而不是删完才发现某条链接变了。
 *
 * 文案口径依据后端行为：引用失效一律**回退默认、不报错**
 * （见 services/settings/references.py 顶部说明），所以这里说"回退"，
 * 不说"会报错"。
 */
const DeleteConfirmModal: React.FC<{
  target: DeleteTarget | null;
  onClose: () => void;
}> = ({ target, onClose }) => {
  if (!target) return null;
  const refs = target.references;
  // 英文/数字开头的名字前面留一个空格，免得和"删除"粘成"删除LLM 模型…"；
  // 中文开头不留，否则是"删除 提示词…"这种多余空隙
  const pad = /^[A-Za-z0-9]/.test(target.title) ? ' ' : '';

  return (
    <AppModal
      // auto：高度随内容走（引用列表长短不一）。auto 模式下 bodyFloor 固定
      // 96，不会被 minSize.h 撑出空白——small 确认框用它是安全的
      dimension="auto"
      defaultSize={{ w: 520, h: 240 }}
      title={
        <Space size={8}>
          <ExclamationCircleFilled style={{ color: '#faad14' }} />
          <span>删除{pad}{target.title}？</span>
        </Space>
      }
      open
      onCancel={onClose}
      footer={
        <AppModalFooter
          okText="仍要删除"
          danger
          onOk={() => {
            target.onConfirm();
            onClose();
          }}
          onCancel={onClose}
        />
      }
    >
      {refs === null ? null : refs.length === 0 ? (
        <div>没有其他地方引用它，删除不影响正在运行的配置。</div>
      ) : (
        <>
          <div>
            以下 <b>{refs.length}</b> 处正在引用它，删除后
            {target.fallbackHint ? `${target.fallbackHint}：` : '会影响它们：'}
          </div>
          <ul
            style={{
              margin: '8px 0 0',
              paddingLeft: 20,
              maxHeight: 160,
              overflowY: 'auto',
            }}
          >
            {refs.map((r, i) => (
              <li key={`${r.kind}-${r.id}-${i}`}>{r.label}</li>
            ))}
          </ul>
        </>
      )}
      {target.extraNote && <div style={{ marginTop: 12 }}>{target.extraNote}</div>}
    </AppModal>
  );
};

export default DeleteConfirmModal;
