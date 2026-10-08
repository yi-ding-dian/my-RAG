/**
 * 模型列表编辑（LLM 模型 / 图片解析模型共用）
 *
 * 这两组逻辑原先在 ProfileEditorModal 里是**逐字复制的两份**——保存 / 删除 /
 * 删除前确认 / 测连接激活，各 5 个函数 × 2 组，约 170 行，差异只有「条目名词、
 * 三处文案、测连接调用的 API」。复制粘贴的代价是漂移：给模型条目加一个字段、
 * 或改一次删除确认的口径，很容易只改一边。
 *
 * 把差异收敛进 options（entity / messages / testConnection），逻辑只留一份。
 */
import { useState, type Dispatch, type SetStateAction } from 'react';
import { App as AntApp } from 'antd';

import { asApiError } from '../../shared/api/client';
import type { DeleteTarget } from './DeleteConfirmModal';

/** 两组条目的公共形状（LLM 条目另有 temperature/max_tokens，此处用不到） */
export interface ModelItemLike {
  name: string;
  api_key?: string;
}

/** 测连接结果（两组 API 同形） */
export interface ConnResult {
  data: { ok: boolean; latency_ms?: number; reason?: string };
}

export interface ModelListEditorOptions<T extends ModelItemLike> {
  /** 条目名词，进删除确认标题：'LLM 模型' / '图片解析模型' */
  entity: string;
  models: T[];
  setModels: Dispatch<SetStateAction<T[]>>;
  active: number;
  setActive: Dispatch<SetStateAction<number>>;
  setDirty: (v: boolean) => void;
  /** 测连接（两组各调各的 API） */
  testConnection: (item: T) => Promise<ConnResult>;
  /** 该名字被谁引用（外部查询 / 部门配置按名字指定，删除前要提示） */
  referencesOf: (name: string) => DeleteTarget['references'];
  /** 确认框交给页面统一渲染（与档案删除共用同一个弹窗） */
  onRequestDelete: (target: DeleteTarget) => void;
  /** 文案差异（两组语序不同，故用回调而非拼字符串） */
  messages: {
    /** 激活成功；latencyMs 仅在连接测试通过时有值 */
    done: (name: string, latencyMs?: number) => string;
    /** 连接失败后强制激活成功 */
    doneForced: (name: string) => string;
    forceTitle: (name: string) => string;
    forceOkText: string;
  };
  /** 删除时的兜底说明（'会自动回退到全局激活模型' 等） */
  fallbackHint: string;
  /** 删除的正是当前激活项时的额外提示 */
  activeDeleteNote: string;
}

export function useModelListEditor<T extends ModelItemLike>(
  opts: ModelListEditorOptions<T>,
) {
  const { message, modal } = AntApp.useApp();
  const [editIdx, setEditIdx] = useState<number | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [testingIdx, setTestingIdx] = useState<number | null>(null);

  /** 打开编辑弹窗（表单回填在 ModelEditModals 内部做） */
  const openEdit = (idx: number | null) => {
    setEditIdx(idx);
    setModalOpen(true);
  };

  /** 弹窗保存：插入或替换（api_key 留空 = 保留原值，回填的是脱敏值） */
  const save = (item: T) => {
    opts.setModels(prev => {
      const next = [...prev];
      if (editIdx !== null && next[editIdx]) {
        next[editIdx] = {
          ...item, api_key: item.api_key || next[editIdx].api_key,
        };
      } else {
        next.push(item);
        if (next.length === 1) opts.setActive(0); // 从空添加第一个 → 自动激活
      }
      return next;
    });
    setModalOpen(false);
    opts.setDirty(true);
  };

  const remove = (idx: number) => {
    opts.setModels(prev => {
      if (prev.length <= 1) return prev; // 至少保留 1 个模型
      const next = prev.filter((_, i) => i !== idx);
      opts.setActive(a => (idx === a ? 0 : idx < a ? a - 1 : a));
      return next;
    });
    opts.setDirty(true);
  };

  /** 删除前确认：谁在按名字引用它 */
  const requestDelete = (idx: number) => {
    const m = opts.models[idx];
    if (!m) return;
    opts.onRequestDelete({
      title: `${opts.entity}「${m.name}」`,
      references: opts.referencesOf(m.name),
      fallbackHint: opts.fallbackHint,
      extraNote: idx === opts.active ? opts.activeDeleteNote : undefined,
      onConfirm: () => remove(idx),
    });
  };

  /** 激活：先测连接 → 成功直接激活；失败弹原因 + 可确认强制激活 */
  const activate = async (idx: number) => {
    if (idx === opts.active || testingIdx !== null) return;
    const item = opts.models[idx];
    if (!item) return;
    setTestingIdx(idx);
    const confirmForce = (reason: string) => {
      modal.confirm({
        title: opts.messages.forceTitle(item.name),
        content: reason,
        okText: opts.messages.forceOkText,
        cancelText: '取消',
        onOk: () => {
          opts.setActive(idx);
          opts.setDirty(true);
          message.success(opts.messages.doneForced(item.name));
        },
      });
    };
    try {
      const res = await opts.testConnection(item);
      if (res.data.ok) {
        opts.setActive(idx);
        opts.setDirty(true);
        message.success(opts.messages.done(item.name, res.data.latency_ms));
      } else {
        confirmForce(res.data.reason || '');
      }
    } catch (e: unknown) {
      confirmForce(asApiError(e).response?.data?.detail
        || '网络请求失败，请检查服务是否可达');
    } finally {
      setTestingIdx(null);
    }
  };

  return {
    editIdx, modalOpen, testingIdx,
    openEdit, closeModal: () => setModalOpen(false),
    save, remove, requestDelete, activate,
  };
}
