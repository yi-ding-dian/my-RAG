/**
 * 配置档案编辑器的「保存」（从 ProfileEditorModal 抽出）。
 * 组装入参、新建/更新、展开面板后校验。
 */
import { useState } from 'react';
import { App as AntApp } from 'antd';
import type { FormInstance } from 'antd';

import {
  asApiError, createProfile, updateProfile,
} from '../../../shared/api/client';
import type {
  LLMModelItem, ServiceProfile, ServiceProfileInput, VisionModelItem,
} from '../../../shared/api/types';
import { toProfileInput } from '../shared';
import type { ProfileFormValues } from '../shared';

export interface UseProfileSaveOptions {
  form: FormInstance;
  /** 当前编辑的档案（新建时为 null） */
  profile: ServiceProfile | null;
  /** 取两个模型列表的最新值（组装入参要含它们） */
  getModels: () => {
    llm: LLMModelItem[];
    llmActive: number;
    vision: VisionModelItem[];
    visionActive: number;
  };
  /**
   * 展开全部折叠面板并等一帧。折叠面板懒渲染，未展开则字段没注册进表单，
   * validateFields() 会直接跳过它们——必填项空着也能存进去。
   */
  expandAllPanels: () => Promise<void>;
  /** 保存成功后刷新页面上的档案列表 */
  onSaved: () => void;
  /** 保存成功后清掉脏标记（见 useProfileDirty） */
  markClean: () => void;
}

export function useProfileSave({
  form, profile, getModels, expandAllPanels, onSaved, markClean,
}: UseProfileSaveOptions) {
  const { message } = AntApp.useApp();
  const [saving, setSaving] = useState(false);
  /** 新建保存成功后的 id：让弹窗就地切成"编辑"态，不必回头去改页面状态 */
  const [createdId, setCreatedId] = useState<string | null>(null);
  /** 最后一次保存成功后的档案快照：「放弃改动」回到这里（新建保存后也能正确回退） */
  const [savedSnapshot, setSavedSnapshot] = useState<ServiceProfile | null>(null);

  /** 实际操作的档案 id：编辑取传入的，新建成功后用 createdId 顶上 */
  const editingId = profile?.id ?? createdId;

  /** 当前表单 + 模型列表组装成后端入参（保存 与"按当前值测试"共用） */
  const buildInput = (): ServiceProfileInput => {
    const m = getModels();
    return toProfileInput(
      form.getFieldsValue(true) as ProfileFormValues,
      m.llm.length ? { models: m.llm, active: m.llmActive } : undefined,
      m.vision.length ? { models: m.vision, active: m.visionActive } : undefined,
    );
  };

  const doSave = async (vals: ProfileFormValues) => {
    const m = getModels();
    setSaving(true);
    try {
      const llmSection = m.llm.length
        ? { models: m.llm, active: m.llmActive } : undefined;
      const visionSection = m.vision.length
        ? { models: m.vision, active: m.visionActive } : undefined;
      const data = toProfileInput(vals, llmSection, visionSection);
      let saved: ServiceProfile | null = null;
      if (editingId) {
        const res = await updateProfile(editingId, data);
        saved = res.data;
        message.success('配置档案已保存');
      } else {
        // 新建后留在弹窗里继续编辑，但必须记住 id——否则再点一次保存会又建一份
        const res = await createProfile(
          data as ServiceProfileInput & { name: string });
        setCreatedId(res.data.id);
        saved = res.data;
        message.success('配置档案已创建');
      }
      // 保存后**不关弹窗**：用户可以接着改、接着存，改完自己关
      onSaved();
      // 存过了：基线刷新、脏标记清掉，关窗时不再拦人
      markClean();
      // 「放弃改动」要能回到最近一次保存的状态（新建保存后 profile 仍是 null）
      if (saved) setSavedSnapshot(saved);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  /** 保存：先把所有面板展开再校验（理由见 expandAllPanels 注释） */
  const handleSave = async () => {
    await expandAllPanels();
    try {
      const vals = await form.validateFields();
      await doSave(vals);
    } catch (e: unknown) {
      // 校验没过：antd 会把出错的字段标红，这里再把它滚进视野——
      // 展开全部后表单很长，不滚的话用户看不到错在哪
      const first = (e as { errorFields?: { name: (string | number)[] }[] })
        ?.errorFields?.[0]?.name;
      if (first) {
        form.scrollToField(first as never, { behavior: 'smooth', block: 'center' });
      }
    }
  };

  /** 弹窗打开时重置新建态（基线/脏标记由 useProfileDirty 管） */
  const resetForOpen = () => {
    setCreatedId(null);
    setSavedSnapshot(null);
  };

  return {
    saving, editingId, savedSnapshot,
    buildInput, handleSave, resetForOpen,
  };
}
