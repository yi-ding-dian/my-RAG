/**
 * 配置档案编辑器的「脏检查」（从 ProfileEditorModal 抽出）。
 *
 * 判据是"用户是否真的编辑过"（dirty），不是"内容指纹变了"——模型列表是打开时
 * 异步填进去的，用指纹当判据会导致"什么都没改直接关闭也报未保存"。
 * dirty 之后再比一次指纹，放过"改了又改回原值"。
 */
import { useRef, useState } from 'react';
import type { FormInstance } from 'antd';

import type { LLMModelItem, VisionModelItem } from '../../../shared/api/types';

export interface UseProfileDirtyOptions {
  form: FormInstance;
  /** 取模型列表最新值：指纹会被 setTimeout 旧闭包调用，直接读 state 会拿到过时快照 */
  getModels: () => {
    llm: LLMModelItem[];
    llmActive: number;
    vision: VisionModelItem[];
    visionActive: number;
  };
}

export function useProfileDirty({ form, getModels }: UseProfileDirtyOptions) {
  const [dirty, setDirty] = useState(false);
  /** 上次保存（或刚打开）时的内容指纹：脏了之后用它排除"改了又改回原值" */
  const baselineRef = useRef('');
  const [closeConfirmOpen, setCloseConfirmOpen] = useState(false);

  /** 当前编辑内容的指纹：表单全部字段 + 两个模型列表（后两者在独立 state 里，
   *  漏掉就会「改了模型列表却检测不出未保存」） */
  const editFingerprint = () => JSON.stringify({
    form: form.getFieldsValue(true),
    ...getModels(),
  });

  /** 记下当前为基线并清掉脏标记（保存成功后调用） */
  const markClean = () => {
    baselineRef.current = editFingerprint();
    setDirty(false);
  };

  /**
   * 弹窗打开时重置：清脏标记、收起可能残留的确认框，并把基线延到下一帧再记。
   *
   * 返回 cleanup（清定时器），供调用方从 effect 里返回。
   */
  const resetForOpen = () => {
    setDirty(false);
    // 显式收起"未保存确认框"：万一上次是被异常路径关掉的，残留的 open=true
    // 会让它在新一轮编辑刚开始就冒出来
    setCloseConfirmOpen(false);
    const timer = window.setTimeout(() => {
      baselineRef.current = editFingerprint();
    }, 0);
    return () => window.clearTimeout(timer);
  };

  /** 关闭前是否该弹"未保存"确认：dirty 且指纹确实与基线不同 */
  const shouldConfirmClose = () =>
    dirty && editFingerprint() !== baselineRef.current;

  return {
    dirty, setDirty,
    closeConfirmOpen, setCloseConfirmOpen,
    editFingerprint, markClean, resetForOpen, shouldConfirmClose,
  };
}
