# 贡献指南（Contributing Guide）

感谢你愿意为 my-RAG 贡献力量。本仓库为单仓库双目录结构：

| 目录 | 内容 |
|------|------|
| `RAG/` | 企业知识库问答系统（FastAPI + React，Python 3.12 + Node 20+） |
| `RAGAS/` | 基于 RAGAS 的评估系统（FastAPI + React，Python 3.12 + Node 20+） |

## 一、开发流程（fork → 分支 → PR）

1. **Fork** 本仓库到你的账号；
2. **Clone** 并新建功能分支：
   ```bash
   git clone git@github.com:<your-account>/my-RAG.git
   git checkout -b feat/your-feature        # 或 fix/xxx、docs/xxx、chore/xxx
   ```
3. **开发**并本地验证（见下方"开发环境"与"测试要求"）；
4. **Commit**（提交信息规范见"提交信息"）；
5. **Push** 分支并创建 **Pull Request**，在 PR 描述中说明改动动机与验证结果；
6. 等待维护者 review，按反馈修改。

> 提交 PR 前请先同步上游最新代码，避免冲突：
> `git remote add upstream git@github.com:<org>/my-RAG.git && git fetch upstream`

## 二、开发环境

### RAG（知识库系统）

```bash
cd RAG
./deploy/install.sh                # 创建 .venv → 装后端依赖 → 装前端依赖（幂等）
./deploy/start.sh                  # 启动：后端 8091 + 前端 dev 3002
```

手动模式（Python 3.12，注意系统默认 python3 可能是旧版本）：

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. .venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8091 --reload
cd frontend && npm install && npm run dev
```

### RAGAS（评估系统）

```bash
cd RAGAS
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env              # 编辑 .env 填入 LLM / Embedding / ES 地址
.venv/bin/python -m backend.main  # 后端 8090
cd frontend && npm install && npm run dev   # 前端 dev 3000
```

或一键启动：`bash start.sh`。

## 三、测试要求

- **RAG 后端**：`cd RAG && .venv/bin/python -m pytest tests/`（从项目根目录跑，测试完全
  离线：conftest 注入 sqlite / mock LLM / mock Embedding）。新增功能必须附带测试；
- **RAGAS 后端**：`cd RAGAS && .venv/bin/python -m pytest tests/`；
- **前端**：`cd <子项目>/frontend && npm run build` 必须通过（TypeScript 严格编译）；
- PR 中请注明测试结果（如 `pytest 961 passed`、`npm run build 通过`）。

> 注意：测试需在子项目根目录用 `python -m pytest` 运行（测试通过 `backend.*`
> 导入，`python -m` 会把当前目录加入 sys.path）。

## 四、提交信息规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/v1.0.0/)：

```
<type>(<scope>): <subject>

类型: feat / fix / docs / style / refactor / perf / test / build / ci / chore / revert
scope（可选）: 涉及模块，如 ragas / ingestion / retrieval / frontend / ci
```

示例：

- `feat(retrieval): 支持多知识库合并检索`
- `fix(ingestion): 修复大文件解析超时问题`
- `docs: 补充架构文档 ARCHITECTURE.md`
- `ci: 增加 GitHub Actions 离线测试工作流`

提交前请自查：`git diff` 无调试残留；`.env` / 密钥 / 内网地址不入库；
改动范围与提交信息一致，不夹带无关变更。

## 五、代码风格

- Python：遵循 PEP 8；服务间通过 `get_xxx_service()` 单例模式访问，便于测试替换；
  新增服务请同步维护 `tests/conftest.py` 的 `reset_services()`。
- TypeScript：tsc 严格模式编译通过；新增页面在 `frontend/src/pages/` 下并注册路由。
- 文档：中文为主；涉及架构变更时同步更新 `RAG/ARCHITECTURE.md` 或 `RAGAS/ARCHITECTURE.md`。

## 六、其他

- Issue 请使用仓库内模板（bug_report / feature_request）；
- 安全相关问题不要开公开 Issue，请按 [SECURITY.md](./SECURITY.md) 上报。
