import React from 'react';
import { Alert, App as AntApp, Button } from 'antd';
import AppModal from '../../shared/components/common/AppModal';
import { PRE_STYLE } from './shared';

/** 部门配置查看的数据（null = 不弹） */
export interface DeptConfigView {
  name: string;
  /** 部门显式覆盖的段（[段名, 字段dict]，只留有内容的） */
  filled: [string, unknown][];
  /** 当前生效配置（全局 + 部门覆盖的合并） */
  effective: Record<string, unknown>;
}

/**
 * 部门配置查看弹窗（超管只读）
 *
 * 展示**当前生效值**（全局 + 部门覆盖的合并），并列出本部门自行覆盖了哪些
 * 字段——超管据此判断"这个部门改过什么"。数据由调用方算好，这里只管渲染。
 */
const DeptConfigViewModal: React.FC<{
  data: DeptConfigView | null;
  onClose: () => void;
}> = ({ data, onClose }) => {
  const { message } = AntApp.useApp();
  if (!data) return null;

  const copyJson = (value: unknown, label: string) => {
    void navigator.clipboard
      .writeText(JSON.stringify(value, null, 2))
      .then(
        () => message.success(`${label}已复制到剪贴板`),
        () => message.error('复制失败（浏览器未授权剪贴板）'),
      );
  };

  return (
    <AppModal
      dimension="resizable"
      defaultSize={{ w: 780, h: 560 }}
      rememberKey="dept-config-view"
      open
      width={780}
      title={`部门配置：${data.name}`}
      footer={[
        <Button key="close" onClick={onClose}>关闭</Button>,
      ]}
      onCancel={onClose}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 8 }}
        message={data.filled.length > 0
          ? `该部门自行覆盖了 ${data.filled.length} 个段（${data.filled.map(([k]) => k).join(' / ')}），其余跟随全局`
          : '该部门未做任何覆盖，以下全部是全局默认值'}
      />
      <div style={{ fontWeight: 600, fontSize: 12, margin: '8px 0 4px' }}>
        当前生效配置
        <Button size="small" style={{ marginLeft: 8 }}
          onClick={() => copyJson(data.effective, '当前生效配置')}>
          复制
        </Button>
      </div>
      <pre style={PRE_STYLE}>
        {JSON.stringify(data.effective, null, 2)}
      </pre>
      {data.filled.length > 0 && (
        <>
          <div style={{ fontWeight: 600, fontSize: 12, margin: '12px 0 4px' }}>
            本部门覆盖的字段
            <Button size="small" style={{ marginLeft: 8 }}
              onClick={() => copyJson(
                Object.fromEntries(data.filled), '部门覆盖字段')}>
              复制
            </Button>
          </div>
          <pre style={{ ...PRE_STYLE, maxHeight: 240, background: '#fff7e6' }}>
            {JSON.stringify(Object.fromEntries(data.filled), null, 2)}
          </pre>
        </>
      )}
    </AppModal>
  );
};

export default DeptConfigViewModal;
