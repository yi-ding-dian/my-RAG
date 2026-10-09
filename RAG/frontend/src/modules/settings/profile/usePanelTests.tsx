/**
 * 折叠面板的「测试连接」（从 ProfileEditorModal 抽出）。
 *
 * 测的是表单里当前填的值，不是已保存的配置；档案未落库时走 draft 接口。
 */
import { useState, type ReactNode } from 'react';
import { Alert, App as AntApp, Button, Tooltip, Typography } from 'antd';
import { ThunderboltOutlined } from '@ant-design/icons';

import {
  asApiError, testDraftConnection, testProfileConnection,
} from '../../../shared/api/client';
import type { ServiceProfileInput } from '../../../shared/api/types';
import { PANEL_TEST_SECTIONS, sectionLabel, toTestItems } from '../shared';
import type { SectionKey, TestItem } from '../shared';

/** 面板最近一次测试结果（留在面板内持久展示，不像 toast 一闪而过） */
export interface PanelTestResult {
  ok: boolean;
  at: string;
  msg: string;
}

export interface UsePanelTestsOptions {
  /** 取当前表单 + 模型列表组装的后端入参（保存 与"按当前值测试"共用） */
  buildInput: () => ServiceProfileInput;
  /** 实际操作的档案 id：新建保存前为 null → 走 draft 接口 */
  editingId: string | null;
  /** 结果回传页面，驱动卡片上的状态灯（新建尚无档案，无处可回传） */
  onTestResult: (
    profileId: string,
    items: Record<SectionKey, TestItem>,
    sections: SectionKey[],
  ) => void;
}

export function usePanelTests({
  buildInput, editingId, onTestResult,
}: UsePanelTestsOptions) {
  const { message } = AntApp.useApp();
  const [panelResults, setPanelResults] =
    useState<Record<string, PanelTestResult>>({});
  /** 正在测试的面板 key（折叠面板标题上的按钮 loading） */
  const [panelTesting, setPanelTesting] = useState('');

  const handlePanelTest = async (panelKey: string, sections: SectionKey[]) => {
    setPanelTesting(panelKey);
    try {
      const body = buildInput();
      const res = editingId
        ? await testProfileConnection(editingId, body, sections)
        : await testDraftConnection(body, sections);
      if (editingId) onTestResult(editingId, toTestItems(res.data), sections);
      const failed = sections
        .map(k => ({
          key: k,
          r: (res.data as unknown as
            Record<string, { ok: boolean; message: string } | undefined>)[k],
        }))
        .filter((x): x is { key: SectionKey; r: { ok: boolean; message: string } } =>
          Boolean(x.r))
        .filter(x => !x.r.ok);
      setPanelResults(prev => ({
        ...prev,
        [panelKey]: {
          ok: failed.length === 0,
          at: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
          msg: failed.map(x =>
            `${sectionLabel[x.key] ?? x.key}：${x.r.message}`).join('；'),
        },
      }));
      if (failed.length === 0) {
        message.success(`${sectionLabel[sections[0]] ?? '配置'}：连接测试通过`);
      } else {
        failed.forEach(x =>
          message.error(`${sectionLabel[x.key] ?? x.key}：${x.r.message}`));
      }
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '测试失败');
    } finally {
      setPanelTesting('');
    }
  };

  /** 折叠面板内容顶部：最近一次测试的结果条（可关闭，不像 toast 一闪而过） */
  const withTestResult = (panelKey: string, node: ReactNode) => {
    const r = panelResults[panelKey];
    if (!r) return node;
    return (
      <>
        <Alert
          type={r.ok ? 'success' : 'error'}
          showIcon
          closable
          style={{ marginBottom: 12, padding: '5px 12px' }}
          onClose={() => setPanelResults(prev => {
            const next = { ...prev };
            delete next[panelKey];
            return next;
          })}
          message={
            <span style={{ fontSize: 12 }}>
              {r.ok ? '连接测试通过' : r.msg}
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {' '}· {r.at}
              </Typography.Text>
            </span>
          }
        />
        {node}
      </>
    );
  };

  /** 折叠面板标题：右侧可测面板夹带"测试"按钮（按当前填写值探测该段） */
  const panelLabel = (panelKey: string, title: string) => {
    const secList = PANEL_TEST_SECTIONS[panelKey];
    return (
      <div
        style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          width: '100%',
        }}
      >
        <span>{title}</span>
        {secList && (
          <Tooltip title="用当前填写的内容测试这一段（不必先保存）">
            <Button
              type="text"
              size="small"
              icon={<ThunderboltOutlined />}
              loading={panelTesting === panelKey}
              onClick={e => {
                e.stopPropagation();
                void handlePanelTest(panelKey, secList);
              }}
            >
              测试
            </Button>
          </Tooltip>
        )}
      </div>
    );
  };

  return {
    panelResults, setPanelResults, panelTesting,
    handlePanelTest, withTestResult, panelLabel,
  };
}
