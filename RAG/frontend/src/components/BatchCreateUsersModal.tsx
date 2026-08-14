/**
 * 批量建号弹窗（仅 super_admin 使用，入口在用户管理页工具栏）
 *
 * 输入格式：每行一个用户，英文逗号分隔：`用户名,显示名,部门名,角色`
 * - 角色：admin=部门管理员 / user=普通用户，缺省 user；部门缺省「默认部门」
 * - 空行与 # 注释行忽略；用户名须为 3-20 位字母/数字/下划线；同批次内用户名不可重复
 * - 初始密码统一为 123456（createUser 契约必传 password），创建后请用户尽快登录修改
 *
 * 执行：复用现有 listDepartments / createDepartment / createUser 接口，不新增后端接口；
 * 部门不存在时自动创建；逐个串行建号并展示进度（建号中 x/y）与结果汇总（成功 n / 失败 m + 原因列表）。
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  App as AntApp,
  Input,
  Modal,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  Department,
  createDepartment,
  createUser,
  listDepartments,
} from '../api/client';

const { Text } = Typography;

/** 默认初始密码（createUser 契约必传 password） */
const DEFAULT_PASSWORD = '123456';
/** 部门缺省值 */
const DEFAULT_DEPT = '默认部门';

const USERNAME_RE = /^[A-Za-z0-9_]{3,20}$/;

/** 解析后的一行用户（errors 阻止提交，warnings 仅提示不阻止） */
interface ParsedRow {
  /** 原始行号（从 1 起），便于定位输入问题 */
  line: number;
  username: string;
  displayName: string;
  deptName: string;
  role: 'dept_admin' | 'user';
  errors: string[];
  warnings: string[];
}

/** 解析批量输入：空行与 # 注释行忽略，逐行按英文逗号切分并校验 */
const parseBatch = (text: string, deptNames: Set<string>): ParsedRow[] => {
  const rows: ParsedRow[] = [];
  const seen = new Map<string, number>();
  text.split('\n').forEach((raw, idx) => {
    const line = idx + 1;
    const trimmed = raw.trim();
    if (!trimmed || trimmed.startsWith('#')) return;
    const parts = trimmed.split(',').map(p => p.trim());
    const username = (parts[0] ?? '').trim();
    const displayName = (parts[1] ?? '').trim();
    const deptName = (parts[2] ?? '').trim() || DEFAULT_DEPT;
    const roleRaw = (parts[3] ?? '').trim().toLowerCase();
    const errors: string[] = [];
    const warnings: string[] = [];
    if (!username) errors.push('用户名缺失');
    else if (!USERNAME_RE.test(username)) errors.push('用户名需为 3-20 位字母、数字或下划线');
    if (!displayName) errors.push('显示名缺失');
    let role: 'dept_admin' | 'user' = 'user';
    if (roleRaw) {
      if (roleRaw === 'admin') role = 'dept_admin';
      else if (roleRaw === 'user') role = 'user';
      else errors.push(`角色无效：「${roleRaw}」（仅支持 admin/user）`);
    }
    if (username) {
      if (seen.has(username)) errors.push(`与第 ${seen.get(username)} 行用户名重复`);
      else seen.set(username, line);
    }
    if (deptName && !deptNames.has(deptName)) {
      warnings.push(`部门「${deptName}」不存在，建号时将自动创建`);
    }
    rows.push({ line, username, displayName, deptName, role, errors, warnings });
  });
  return rows;
};

interface Props {
  open: boolean;
  onCancel: () => void;
  /** 建号完成后回调（父组件刷新用户列表） */
  onSuccess: () => void;
  /** 当前部门列表（预览校验用；执行时组件内部重新拉取最新列表） */
  departments: Department[];
}

const BatchCreateUsersModal: React.FC<Props> = ({ open, onCancel, onSuccess, departments }) => {
  const { message } = AntApp.useApp();
  const [text, setText] = useState('');
  // 执行中：done/total 用于「建号中 x/y」
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  // 完成汇总：成功数 + 失败明细
  const [summary, setSummary] = useState<{
    ok: number;
    failed: { username: string; reason: string }[];
  } | null>(null);

  // 每次打开弹窗重置输入与结果
  useEffect(() => {
    if (open) {
      setText('');
      setSummary(null);
      setProgress(null);
      setRunning(false);
    }
  }, [open]);

  const deptNames = useMemo(() => new Set(departments.map(d => d.name)), [departments]);
  const rows = useMemo(() => parseBatch(text, deptNames), [text, deptNames]);
  const validRows = rows.filter(r => r.errors.length === 0);
  const hasError = rows.some(r => r.errors.length > 0);

  const handleOk = async () => {
    if (validRows.length === 0 || running) return;
    setRunning(true);
    setSummary(null);
    setProgress({ done: 0, total: validRows.length });
    const ok: string[] = [];
    const failed: { username: string; reason: string }[] = [];
    try {
      // 执行前拉取最新部门列表，建 name→id 映射；新建的部门实时补充进映射
      const deptRes = await listDepartments();
      const deptIdByName = new Map<string, string>(deptRes.data.map(d => [d.name, d.id]));
      for (let i = 0; i < validRows.length; i++) {
        const row = validRows[i];
        try {
          let deptId = deptIdByName.get(row.deptName) ?? null;
          if (!deptId) {
            // 部门不存在：复用现有 createDepartment 接口自动创建
            const created = await createDepartment({ name: row.deptName });
            deptId = created.data.id;
            deptIdByName.set(row.deptName, deptId);
          }
          await createUser({
            username: row.username,
            password: DEFAULT_PASSWORD,
            display_name: row.displayName,
            role: row.role,
            department_id: deptId,
          });
          ok.push(row.username);
        } catch (e: any) {
          failed.push({ username: row.username, reason: e.response?.data?.detail || '创建失败' });
        }
        setProgress({ done: i + 1, total: validRows.length });
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || '加载部门列表失败，本次未建号');
    } finally {
      setRunning(false);
      setProgress(null);
      if (ok.length > 0 || failed.length > 0) {
        setSummary({ ok: ok.length, failed });
        if (failed.length === 0) {
          message.success(`批量建号完成：成功 ${ok.length} 个`);
        } else {
          message.warning(`批量建号完成：成功 ${ok.length} 个，失败 ${failed.length} 个`);
        }
      }
      onSuccess();
    }
  };

  const columns: ColumnsType<ParsedRow> = [
    { title: '行号', dataIndex: 'line', key: 'line', width: 60 },
    { title: '用户名', dataIndex: 'username', key: 'username' },
    { title: '显示名', dataIndex: 'displayName', key: 'displayName' },
    { title: '部门', dataIndex: 'deptName', key: 'deptName' },
    {
      title: '角色',
      key: 'role',
      width: 110,
      render: (_, r) =>
        r.role === 'dept_admin' ? <Tag color="blue">部门管理员</Tag> : <Tag>普通用户</Tag>,
    },
    {
      title: '校验',
      key: 'valid',
      width: 240,
      render: (_, r) => {
        if (r.errors.length > 0) {
          return (
            <Tooltip title={r.errors.join('；')}>
              <Tag color="red">错误</Tag>
            </Tooltip>
          );
        }
        if (r.warnings.length > 0) {
          return (
            <Tooltip title={r.warnings.join('；')}>
              <Tag color="orange">警告</Tag>
            </Tooltip>
          );
        }
        return <Tag color="green">通过</Tag>;
      },
    },
  ];

  return (
    <Modal
      title="批量建号"
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      okText={
        running && progress
          ? `建号中 ${progress.done}/${progress.total}`
          : validRows.length > 0
            ? `创建 ${validRows.length} 个用户`
            : '确定'
      }
      okButtonProps={{ disabled: hasError || validRows.length === 0 }}
      confirmLoading={running}
      cancelButtonProps={{ disabled: running }}
      width={820}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 8 }}
        message="每行一个用户，英文逗号分隔：用户名,显示名,部门名,角色"
        description={
          <span>
            角色缺省 user（admin=部门管理员）；部门缺省「默认部门」，不存在时自动创建；空行与 #
            注释行忽略。初始密码统一为 <Text strong>{DEFAULT_PASSWORD}</Text>，
            用户首次登录后必须立即修改密码（系统将强制要求）。
          </span>
        }
      />
      <Input.TextArea
        value={text}
        onChange={e => setText(e.target.value)}
        rows={6}
        disabled={running}
        placeholder={`每行一个用户，格式：用户名,显示名,部门名,角色\n示例：\nzhangsan,张三,研发部,admin\nlisi,李四,,user\n# 以 # 开头的行是注释，空行忽略`}
      />
      {summary && (
        <Alert
          style={{ marginTop: 8 }}
          type={summary.failed.length > 0 ? 'warning' : 'success'}
          showIcon
          message={`建号完成：成功 ${summary.ok} 个，失败 ${summary.failed.length} 个`}
          description={
            summary.failed.length > 0 ? (
              <div>
                {summary.failed.slice(0, 10).map(f => (
                  <div key={f.username}>
                    <Text type="danger">@{f.username}</Text>
                    ：{f.reason}
                  </div>
                ))}
                {summary.failed.length > 10 && (
                  <Text type="secondary">… 其余 {summary.failed.length - 10} 条省略</Text>
                )}
              </div>
            ) : undefined
          }
        />
      )}
      {rows.length > 0 && (
        <Table
          style={{ marginTop: 8 }}
          size="small"
          rowKey="line"
          columns={columns}
          dataSource={rows}
          pagination={false}
          scroll={{ y: 220 }}
        />
      )}
    </Modal>
  );
};

export default BatchCreateUsersModal;
