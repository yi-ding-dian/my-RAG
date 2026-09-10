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
 * MinerU 解析后端选择（mineru-api /file_parse backend 参数）。
 * 仅在解析引擎选择「MinerU 高精度」时显示（ParseConfigModal 条件渲染）。
 * 默认值 pipeline（2026-09-09 起，后端 resolve_parser_config 兜底同值）：
 * 本环境 MinerU 服务无 GPU 配置，混合自动引擎（hybrid-auto-engine）不可用，
 * 管线（pipeline）CPU 可跑且实测正常；若以后服务端补 GPU，可改回混合引擎。
 */
const MinerUBackendField: React.FC<MinerUBackendFieldProps> = ({
  name = 'backend',
  initialValue = 'pipeline',
}) => {
  return (
    <Form.Item
      name={name}
      label={
        <span>
          MinerU 解析后端
          <Tooltip title="混合自动引擎：先识别页面结构再分块处理，表格/OCR/流程图更准，速度快慢取决于服务端硬件；管线：标准流水线，速度快，但复杂表格可能错乱。当前默认管线。">
            <span style={{ marginLeft: 6, color: '#999', cursor: 'help' }}>?</span>
          </Tooltip>
        </span>
      }
      initialValue={initialValue}
    >
      <Select
        options={[
          { value: 'auto', label: '自动（跟随服务端）' },
          { value: 'hybrid-auto-engine', label: '混合自动引擎（质量优）' },
          { value: 'pipeline', label: '管线（速度快）' },
        ]}
      />
    </Form.Item>
  );
};

export default MinerUBackendField;
