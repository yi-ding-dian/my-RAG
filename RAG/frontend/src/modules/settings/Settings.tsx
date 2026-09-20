import React, { useCallback, useEffect, useState } from 'react';
import {
  App as AntApp,  Card,  Button,  Typography,  Space,  Tag,  Alert,  Skeleton,
} from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import {
  asApiError,
  listProfiles, deleteProfile, activateProfile,
  testProfileConnection, getEmbeddingDim, getSettingsReferences,
  ServiceProfile,
} from '../../shared/api/client';
import type { Department, SettingsReferences } from '../../shared/api/types';
import { listDepartments } from '../../shared/api/auth';
import { getDeptConfigView } from '../../shared/api/settings';
import { useAuth } from '../../shared/auth/AuthContext';
import PageHeader from '../../shared/components/layout/PageHeader';
import {
  DOMAIN_CARDS, allFailed, allTesting, emptyTest, sectionLabel, toTestItems,
} from './shared';
import type { SectionKey, TestItem } from './shared';
import ProfileCard from './ProfileCard';
import DeleteConfirmModal from './DeleteConfirmModal';
import type { DeleteTarget } from './DeleteConfirmModal';
import DeptConfigViewModal from './DeptConfigViewModal';
import ProfileEditorModal from './ProfileEditorModal';

const { Text } = Typography;

/** 系统配置页：配置档案列表 + 域卡快捷导航 + 新建/编辑弹窗（面板见同目录 *Panel.tsx） */

const SettingsPage: React.FC = () => {
  const { message, modal } = AntApp.useApp();
  const { user } = useAuth();
  // 档案卡片只读模式：dept_admin 可查看配置与连接测试，修改档案仅 super_admin
  const readOnly = user?.role !== 'super_admin';
  // 部门管理员不应进入本页（菜单/路由已限 super_admin）；保留判断做防御
  const isDeptAdmin = user?.role === 'dept_admin';
  const [profiles, setProfiles] = useState<ServiceProfile[]>([]);
  const [loading, setLoading] = useState(false);
  // 部门配置查询（超管只读）：部门列表 + 查看弹窗（null = 关闭）
  const [departments, setDepartments] = useState<Department[]>([]);
  const [deptView, setDeptView] = useState<{
    name: string;
    /** 部门显式覆盖的段（[段名, 字段dict]，只留有内容的） */
    filled: [string, unknown][];
    /** 当前生效配置（全局 + 部门覆盖的合并） */
    effective: Record<string, unknown>;
  } | null>(null);

  // 编辑弹窗：表单、模型列表、指纹基线等状态都在 ProfileEditorModal 内部自持
  // （它们是"这次编辑"的一部分），这里只管开关与"编辑谁"
  const [editorOpen, setEditorOpen] = useState(false);
  /** 正在编辑的档案；null = 新建 */
  const [editingProfile, setEditingProfile] = useState<ServiceProfile | null>(null);
  /** 打开弹窗时默认展开哪个面板（从域卡点进来的） */
  const [focusPanel, setFocusPanel] = useState('ar');
  /** 配置引用关系：打开弹窗/进页面时拉一次，删除时本地查表查"谁在用它" */
  const [references, setReferences] = useState<SettingsReferences | null>(null);
  /** 待确认的删除（null = 没有正在确认的删除） */
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  // 域卡测试连接 loading 键（"profileId:domainKey"）
  const [domainTesting, setDomainTesting] = useState('');

  // 卡片连接测试状态（按档案 id）
  const [testStates, setTestStates] = useState<Record<string, Record<SectionKey, TestItem>>>({});
  // 当前激活 embedding 模型的实际输出维度（实测，供维度冲突核对）
  const [embeddingDim, setEmbeddingDim] = useState<number | null>(null);
  const [embeddingDimMsg, setEmbeddingDimMsg] = useState('');

  /**
   * 拉取档案列表（含当前 embedding 模型实测维度）。
   *
   * `silent` = 不亮骨架屏：保存/激活/删除后刷新时**必须**静默——那会把整页
   * （包括正开着的编辑弹窗）换成骨架屏再换回来，画面抽一下、弹窗里的滚动
   * 位置和展开的面板全丢。骨架屏只留给首次进页面。
   */
  const loadProfiles = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const res = await listProfiles();
      setProfiles(res.data);
    } catch {
      message.error('加载配置失败');
    } finally {
      if (!silent) setLoading(false);
    }
    // 当前激活 embedding 模型实际输出维度（实测；更换模型后此处用于核对冲突）
    try {
      const dimRes = await getEmbeddingDim();
      setEmbeddingDim(dimRes.data.dimension);
      setEmbeddingDimMsg(dimRes.data.ok ? '' : dimRes.data.message);
    } catch {
      // 非 fatal：维度检测失败不阻塞配置页
    }
  }, []);

  useEffect(() => {
    loadProfiles();
  }, [loadProfiles]);

  /**
   * 拉配置引用关系（谁在引用提示词 / LLM 模型 / 图片模型 / 激活档案）
   *
   * 拉不到就退化成"无引用"的简版确认——检测只是给删除加个提示，
   * 不该因为它失败就让人删不掉东西。
   */
  const loadReferences = useCallback(async () => {
    try {
      const res = await getSettingsReferences();
      setReferences(res.data);
    } catch {
      setReferences(null);
    }
  }, []);

  useEffect(() => {
    void loadReferences();
  }, [loadReferences]);

  // ---- 部门配置查询（超管只读；部门管理员在「部门配置」页自行修改） ----
  const loadDepartments = useCallback(async () => {
    if (isDeptAdmin) return;
    try {
      const res = await listDepartments();
      setDepartments(res.data ?? []);
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '加载部门列表失败');
    }
  }, [isDeptAdmin, message]);

  useEffect(() => {
    void loadDepartments();
  }, [loadDepartments]);

  /** 打开某部门配置查看弹窗（只读）：展示**当前生效值**（全局 + 部门覆盖的
      合并），并列出本部门自行覆盖了哪些字段——超管据此判断"这个部门改过什么"。
      数据在打开时算好，弹窗只负责渲染（统一走 AppModal，尺寸可记忆/拖拽）。 */
  const viewDeptConfig = async (deptId: string) => {
    try {
      const res = await getDeptConfigView(deptId);
      const d = res.data;
      const dept = (d.dept ?? {}) as Record<string, Record<string, unknown>>;
      setDeptView({
        name: d.name,
        filled: Object.entries(dept).filter(
          ([, v]) => v && typeof v === 'object' && Object.keys(v).length > 0),
        effective: {
          ...(d.chat ? { chat: d.chat } : {}),
          ...(d.retrieval ? { retrieval: d.retrieval } : {}),
          ...(d.agentic ? { agentic: d.agentic } : {}),
          ...(d.llm ? { llm: d.llm } : {}),
        },
      });
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '加载部门配置失败');
    }
  };
  // ---- 新建 / 编辑 ----
  /** 打开编辑弹窗（profile = null 表示新建；panel = 从哪张域卡点进来的） */
  const openEditor = (p: ServiceProfile | null, panel: string = 'ar') => {
    setEditingProfile(p);
    setFocusPanel(panel);
    setEditorOpen(true);
    // 引用关系可能在别处改过（新建了外链 / 部门换了提示词），每次开弹窗重拉
    void loadReferences();
  };

  /** 面板测试结果回传：只覆盖**本次被测的段**，其他段的状态灯不动 */
  const patchTestState = (
    profileId: string,
    items: Record<SectionKey, TestItem>,
    sections: SectionKey[],
  ) => {
    setTestStates(prev => {
      const next = { ...(prev[profileId] ?? emptyTest) };
      for (const k of sections) next[k] = items[k];
      return { ...prev, [profileId]: next };
    });
  };

  // ---- 激活 / 删除 ----
  /** 切换「当前使用」：二次确认后执行（一键切换影响所有用户的检索与对话，企业验收反馈） */
  const handleActivate = (p: ServiceProfile) => {
    modal.confirm({
      title: `确认切换为「${p.name}」？`,
      content: '切换后所有用户的检索与对话将立即使用该配置档案',
      okText: '确认切换',
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await activateProfile(p.id);
          message.success(res.data.message);
          await loadProfiles(true);
        } catch {
          message.error('切换失败');
        }
      },
    });
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteProfile(id);
      message.success('已删除');
      await loadProfiles(true);
    } catch {
      message.error('删除失败');
    }
  };

  /**
   * 删除档案前的确认
   *
   * 档案没有"按名字引用"一说，但删**激活中**的那份影响最大：后端会自动
   * 把激活切到剩余第一个，整套配置（LLM / 提示词 / 检索…）随之改变，
   * 所有部门和外部查询链接都受影响——所以这里报的是影响面，不是引用列表。
   */
  const requestDeleteProfile = (p: ServiceProfile) => {
    const ap = references?.active_profile;
    setDeleteTarget({
      title: `配置档案「${p.name}」`,
      // null：档案没有"引用方列表"这一说（见 DeleteTarget.references 注释），
      // 影响面全写在 extraNote 里
      references: null,
      extraNote: p.active ? (
        <>
          它正是<b>当前正在使用</b>的档案。删除后系统会自动切换到剩余的第一个档案，
          所有部门（{ap?.department_count ?? '—'} 个）与外部查询链接
          （{ap?.ext_query_count ?? '—'} 个）的配置都会跟着变。
        </>
      ) : (
        '它没有被激活，删除不影响正在运行的配置。'
      ),
      onConfirm: () => void handleDelete(p.id),
    });
  };

  // ---- 连接测试（卡片：按档案已保存值） ----
  const handleTest = async (p: ServiceProfile) => {
    setTestStates(prev => ({ ...prev, [p.id]: allTesting() }));
    try {
      const res = await testProfileConnection(p.id);
      setTestStates(prev => ({ ...prev, [p.id]: toTestItems(res.data) }));
    } catch (e: unknown) {
      const msg = asApiError(e).response?.data?.detail || '测试失败';
      setTestStates(prev => ({ ...prev, [p.id]: allFailed(msg) }));
    }
  };

  /** 域卡测试连接：按档案配置探测该域各段，结果以 toast 反馈成败 */
  const handleDomainTest = async (
    p: ServiceProfile, domainKey: string, sections: SectionKey[],
  ) => {
    setDomainTesting(`${p.id}:${domainKey}`);
    try {
      const res = await testProfileConnection(p.id);
      const results = sections
        .map(k => ({ key: k, r: (res.data as unknown as Record<string, { ok: boolean; message: string }>)[k] }))
        .filter(x => x.r);
      const fails = results.filter(x => !x.r.ok);
      if (fails.length === 0) {
        message.success(`${DOMAIN_CARDS.find(d => d.key === domainKey)?.title ?? '配置'}：连接测试通过`);
      } else {
        fails.forEach(x =>
          message.error(`${sectionLabel[x.key] ?? x.key}：${x.r.message}`));
        const okCount = results.length - fails.length;
        if (okCount > 0) {
          message.success(`其中 ${okCount} 项连接正常`);
        }
      }
    } catch (e: unknown) {
      message.error(asApiError(e).response?.data?.detail || '测试失败');
    } finally {
      setDomainTesting('');
    }
  };

  // 首次加载才显示骨架屏——后续刷新一律走 loadProfiles(true) 静默，
  // 否则会把正开着的弹窗一起换掉（见 loadProfiles 注释）
  if (loading) return <Skeleton active paragraph={{ rows: 12 }} />;

  return (
    <div>
      <PageHeader
        title="系统配置"
        description="管理服务配置档案：LLM / Embedding / 存储与检索参数"
        extra={
          !readOnly && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openEditor(null)}>
              新建配置档案
            </Button>
          )
        }
      />

      {readOnly && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message={isDeptAdmin
            ? '本页为超级管理员专用；部门管理员请使用左侧菜单的「部门配置」'
            : '您正在配置全局档案。LLM / 对话 / 检索等可由部门覆盖的配置，由各部门管理员在「部门配置」页自行设置；下方「部门配置查询」可查看各部门覆盖了什么。'}
        />
      )}
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 8 }}
        message="服务配置档案：修改并保存后即时生效（无需重启服务），切换「当前使用」即切换整套配置。"
        description="支持多套配置档案（例如：本地 Qwen、云端 DeepSeek），api_key 保存后仅显示脱敏值；「测试连接」可逐项验证 LLM / Embedding / MinerU / DeepDoc 是否可用。"
      />
      <Typography.Paragraph style={{ marginBottom: 16 }}>
        {embeddingDim != null ? (
          <Text>
            当前模型维度（已检测）：<Text strong>{embeddingDim} 维</Text>
            <Text type="secondary">
              {' '}
              —— 更换 Embedding 模型后若知识库出现「维度不匹配」，请在知识库管理页「重建向量」
            </Text>
          </Text>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>
            当前模型维度：{embeddingDimMsg || '检测中…'}
          </Text>
        )}
      </Typography.Paragraph>

      {/* 部门配置查询（仅超管，只读）：看各部门覆盖了什么——配置收口后超管
          不代改，修改由部门管理员在「部门配置」页自行完成 */}
      {!isDeptAdmin && (
        <Card
          size="small"
          style={{ marginBottom: 12 }}
          title={
            <Space>
              <Tag color="purple">部门配置查询</Tag>
              <Text strong>各部门覆盖的配置（只读）</Text>
            </Space>
          }
        >
          {departments.length === 0 ? (
            <Text type="secondary">暂无部门</Text>
          ) : (
            <Space wrap>
              {departments.map(d => (
                <Button key={d.id} size="small"
                  onClick={() => void viewDeptConfig(d.id)}>
                  {d.name}
                </Button>
              ))}
            </Space>
          )}
        </Card>
      )}

      {profiles.length === 0 ? (
        <Card><Text type="secondary">{readOnly ? '暂无配置档案' : '暂无配置档案，请新建'}</Text></Card>
      ) : (
        profiles.map(p => (
          <ProfileCard
            key={p.id}
            profile={p}
            testState={testStates[p.id]}
            readOnly={readOnly}
            deleteDisabled={p.active && profiles.length <= 1}
            domainTesting={domainTesting}
            onEdit={panel => openEditor(p, panel)}
            onActivate={() => handleActivate(p)}
            onDelete={() => requestDeleteProfile(p)}
            onTest={() => void handleTest(p)}
            onDomainTest={(key, sections) => void handleDomainTest(p, key, sections)}
          />
        ))
      )}

      {/* 编辑弹窗：表单、模型列表、指纹基线等状态都在它内部自持，
          这里只管开关、把引用数据给它、并接住它回传的测试结果 */}
      <ProfileEditorModal
        open={editorOpen}
        profile={editingProfile}
        focusPanel={focusPanel}
        readOnly={readOnly}
        references={references}
        testState={editingProfile ? testStates[editingProfile.id] : undefined}
        onRequestDelete={setDeleteTarget}
        onClose={() => setEditorOpen(false)}
        onSaved={() => void loadProfiles(true)}
        onTestResult={patchTestState}
      />

      {/* 删除确认：配置档案里所有删除（提示词 / LLM 模型 / 图片模型 / 档案本身）
          统一走它，会先列出"谁在引用"再让人决定 */}
      <DeleteConfirmModal
        target={deleteTarget}
        onClose={() => setDeleteTarget(null)}
      />

      <DeptConfigViewModal
        data={deptView}
        onClose={() => setDeptView(null)}
      />
    </div>
  );
};

export default SettingsPage;
