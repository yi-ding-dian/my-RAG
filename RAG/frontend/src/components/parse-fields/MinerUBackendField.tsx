import React from 'react';
import { Form, Select, Tooltip } from 'antd';
import type { MinerUBackend } from '../../api/client';

interface MinerUBackendFieldProps {
  /** 表单字段名（默认 backend） */
  name?: string;
  /** 默认值 */
  initialValue?: MinerUBackend;
}

/**
 * MinerU 解析后端选择（mineru-api /file_parse backend 参数，实测对比见
 * 项目 mcp-server/kb-ext-server/record.md）。
 * 仅在解析引擎选择「MinerU 高精度」时显示（ParseConfigModal 条件渲染）。
 * 自动=不传（跟随 MinerU 服务端默认 hybrid-auto-engine）/ 混合自动引擎=质量优
 * （表格规范/OCR 准/流程图识别，速度稍慢）/ 管线=速度快（约快 20s，表格可能错乱）。
 */
const MinerUBackendField: React.FC<MinerUBackendFieldProps> = ({
  name = 'backend',
  initialValue = 'auto',
}) => {
  return (
    <Form.Item
      name={name}
      label={
        <span>
          MinerU 解析后端
          <Tooltip title="mineru-api 两种解析引擎实测：混合自动引擎（hybrid-auto-engine）质量优——表格规范/OCR 准/流程图识别，速度慢约 30%；管线（pipeline）快约 20s 但表格可能错乱。自动=跟随服务端默认（混合自动引擎）。">
            <span style={{ marginLeft: 6, color: '#999', cursor: 'help' }}>?</span>
          </Tooltip>
        </span>
      }
      initialValue={initialValue}
      extra="自动=跟随服务端默认（混合自动引擎）；混合自动引擎质量优（表格规范/OCR 准/流程图识别）；管线速度快约 20s，但表格可能错乱"
    >
      <Select
        options={[
          { value: 'auto', label: '自动（默认）' },
          { value: 'hybrid-auto-engine', label: '混合自动引擎（质量优）' },
          { value: 'pipeline', label: '管线（速度快）' },
        ]}
      />
    </Form.Item>
  );
};

export default MinerUBackendField;
