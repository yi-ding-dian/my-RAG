/**
 * 测试集导入/导出弹窗（txt / Excel）：导出下载文件，导入文件或文本回填（覆盖）。
 *
 * 从 RagasDetail 抽出来——5 个状态 + 6 个函数 + JSX，是弹窗里最大的一块。
 *
 * **分工**（与 ChatSamplePickerModal 同理）：
 * - 当前测试集的**读取**留在页面（`evalValidRows` 读 `evalForm`，别处也在用），
 *   以 `samples` prop 传进来供"打开时带出文本"和"导出"使用
 * - **写回**留在页面：导入解析出的样本经 `onImport` 交给页面写进 `evalForm`
 * - 解析与导出本身（纯逻辑 + 下载）都在本组件
 */
import { useEffect, useRef, useState } from 'react';
import {
  Alert, App as AntApp, Button, Input, Segmented, Space, Typography,
} from 'antd';
import {
  FileExcelOutlined, FileTextOutlined, ImportOutlined, UploadOutlined,
} from '@ant-design/icons';
import * as XLSX from 'xlsx';

import AppModal from '../../../../shared/components/common/AppModal';
import { EvalSampleRow, downloadBlob, evalFileStamp, parseTxtSamples, parseExcelSamples } from '../ragas-shared';

const { Text } = Typography;

export interface SampleTextImportModalProps {
  open: boolean;
  /** 当前测试集的有效行（页面从 evalForm 读出）：打开时带出成文本、导出时取用 */
  samples: EvalSampleRow[];
  onClose: () => void;
  /** 导入解析出的样本（覆盖语义）：页面负责写回 evalForm */
  onImport: (rows: EvalSampleRow[]) => void;
}

export const SampleTextImportModal = ({
  open, samples, onClose, onImport,
}: SampleTextImportModalProps) => {
  const { message } = AntApp.useApp();
  const [evalText, setEvalText] = useState('');
  // 导入/导出格式（txt 文本 / Excel）；txt 走文本区与文件，Excel 走文件
  const [ioFormat, setIoFormat] = useState<'txt' | 'excel'>('txt');
  // 文件导入解析结果提示（有效/跳过/无效行明细），展示在弹窗内
  const [fileParseResult, setFileParseResult] =
    useState<{ ok: number; skipped: number; invalid: string[] } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 当前测试集 → 文本（每行一条：问题【Tab】正确答案；首行 # 注释说明，
  // 导入时自动忽略——txt 导出/导入闭环）
  const buildSamplesText = (): string => {
    if (samples.length === 0) return '';
    return `# 每行一条：问题【Tab 键】正确答案（# 开头为注释行，导入时忽略）\n`
      + samples.map(r => `${r.question}\t${r.ground_truth}`).join('\n');
  };

  // 打开时带出当前测试集文本（原 openTextModal 的两行设置）
  useEffect(() => {
    if (!open) return;
    setEvalText(buildSamplesText());
    setFileParseResult(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 导出（按所选格式）：txt 回填文本区 + 下载 .txt；Excel 生成 .xlsx 下载
  const handleExportByFormat = () => {
    if (samples.length === 0) {
      message.warning('当前测试集为空，无可导出内容');
      return;
    }
    if (ioFormat === 'excel') {
      // 生成 Excel（列：问题/正确答案，首行表头——导入时跳过）
      const ws = XLSX.utils.json_to_sheet(
        samples.map(r => ({ 问题: r.question, 正确答案: r.ground_truth })));
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, '测试集');
      XLSX.writeFile(wb, `测试集_${evalFileStamp()}.xlsx`);
      message.success(`已导出 Excel（${samples.length} 条），请选择保存位置`);
      return;
    }
    const text = buildSamplesText();
    setEvalText(text);
    downloadBlob(new Blob([text], { type: 'text/plain;charset=utf-8' }),
      `测试集_${evalFileStamp()}.txt`);
    message.success(`已导出 txt（${samples.length} 条），请选择保存位置`);
  };

  // 导入结果回填测试集（覆盖）+ 提示（有效/跳过；无效行明细展示在弹窗内）
  const applyImportResult = (
    rows: EvalSampleRow[], skipped: number, invalid: string[], closeModal: boolean) => {
    if (rows.length === 0) {
      message.error(invalid.length
        ? `未能解析出有效测试样本（${invalid[0]}）`
        : '未能解析出有效测试样本，请检查文件内容格式');
      return;
    }
    onImport(rows);
    setFileParseResult({ ok: rows.length, skipped, invalid });
    if (closeModal) onClose();
    message.success(
      `已导入 ${rows.length} 条测试样本（覆盖当前测试集）${skipped ? `，跳过 ${skipped} 条无效内容` : ''}`);
  };

  // 导入：解析文本框内容（txt：每行一条：问题【Tab】正确答案）覆盖回填
  const handleImportText = () => {
    const { rows, skipped, invalid } = parseTxtSamples(evalText);
    applyImportResult(rows, skipped, invalid, true);
  };

  // 导入：文件选择器按所选格式解析（txt 读文本；Excel 读 .xlsx/.csv）覆盖回填
  const handleFileImport = (file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = ioFormat === 'excel'
        ? parseExcelSamples(reader.result as ArrayBuffer)
        : parseTxtSamples(String(reader.result ?? ''));
      applyImportResult(result.rows, result.skipped, result.invalid, false);
    };
    reader.onerror = () => message.error('文件读取失败');
    if (ioFormat === 'excel') reader.readAsArrayBuffer(file);
    else reader.readAsText(file, 'utf-8');
  };

  // 文件选择器触发（按所选格式限定文件类型）；选择后清空 value 允许重选同文件
  const onFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    handleFileImport(file);
  };

  return (
    <AppModal
      dimension="auto"
      defaultSize={{ w: 680, h: 480 }}
      rememberKey="ana-2"
      title="测试集导入/导出"
      open={open}
      onCancel={onClose}
      width={680}
      footer={[
        <Button
          key="export"
          icon={ioFormat === 'excel' ? <FileExcelOutlined /> : <FileTextOutlined />}
          onClick={handleExportByFormat}
        >
          导出文件（{ioFormat === 'excel' ? 'Excel' : 'txt'}）
        </Button>,
        <Button key="file" icon={<UploadOutlined />} onClick={() => fileInputRef.current?.click()}>
          导入文件（{ioFormat === 'excel' ? 'Excel/csv' : 'txt'}）
        </Button>,
        ...(ioFormat === 'txt' ? [(
          <Button
            key="import"
            type="primary"
            icon={<ImportOutlined />}
            onClick={handleImportText}
          >
            从下方文本导入（覆盖测试集）
          </Button>
        )] : []),
      ]}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        <Space wrap>
          <Text strong>格式</Text>
          <Segmented
            options={[{ label: 'txt 文本', value: 'txt' }, { label: 'Excel(xlsx/csv)', value: 'excel' }]}
            value={ioFormat}
            onChange={(v) => setIoFormat(v as 'txt' | 'excel')}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {ioFormat === 'excel'
              ? 'Excel：导出两列（问题/正确答案，首行表头）；导入解析第一列为问题、第二列为正确答案'
              : 'txt：每行一条：问题【Tab 键】正确答案'}
          </Text>
        </Space>
        {ioFormat === 'txt' ? (
          <Alert
            type="info"
            showIcon
            message="txt 格式：每行一条：问题【Tab 键】正确答案"
            description={
              <span style={{ fontSize: 12 }}>
                先导出当前测试集（自动下载文件）→ 在外部按格式编辑 → 再导入回填（覆盖）。
                <br />空行与 <Text code>#</Text> 注释行忽略；问题或答案为空的行跳过；最多 100 条。
                <br />
                <Text code>在线式软地线和 IEC104 接地线配置有什么区别？&nbsp;&nbsp;两者不可同时配置</Text>
              </span>
            }
          />
        ) : (
          <Alert
            type="info"
            showIcon
            message="Excel 格式：两列（问题 / 正确答案）"
            description={
              <span style={{ fontSize: 12 }}>
                导出生成 .xlsx（首行表头，Excel/WPS 均可打开）；导入支持 .xlsx/.csv
                文件（首行表头跳过，其余每行取第一列为问题、第二列为正确答案）。
                <br />问题或答案为空的单元格所在行跳过；最多 100 条。
              </span>
            }
          />
        )}
        {fileParseResult ? (
          <Alert
            type={fileParseResult.invalid.length ? 'warning' : 'success'}
            showIcon
            message={`导入结果：有效 ${fileParseResult.ok} 条${fileParseResult.skipped ? `，跳过 ${fileParseResult.skipped} 条无效内容` : ''}（已覆盖当前测试集）`}
            description={fileParseResult.invalid.length > 0 ? (
              <span style={{ fontSize: 12 }}>
                无效内容（最多显示前 3 条）：<br />
                {fileParseResult.invalid.slice(0, 3).map((s, i) => (
                  <span key={i}>{s}<br /></span>
                ))}
              </span>
            ) : undefined}
          />
        ) : null}
        <Input.TextArea
          value={evalText}
          onChange={(e) => setEvalText(e.target.value)}
          rows={12}
          placeholder={'问题1\t正确答案1\n问题2\t正确答案2'}
          style={{ fontFamily: 'monospace', fontSize: 13 }}
        />
        <input
          ref={fileInputRef}
          type="file"
          accept={ioFormat === 'excel' ? '.xlsx,.xls,.csv' : '.txt'}
          style={{ display: 'none' }}
          onChange={onFileChange}
        />
      </Space>
    </AppModal>
  );
};
