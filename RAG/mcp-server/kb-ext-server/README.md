# kb-ext — 企业知识库 MCP server

把企业知识库（FastAPI，[`http://localhost:8091`](http://localhost:8091)）的**问答能力**包装成标准 MCP 工具 `kb_query`：Agent 提问 → 知识库完成「检索 + LLM 回答 + 引用来源」→ 一次返回完整答案。基于官方 `@modelcontextprotocol/sdk`，stdio 传输。

**功能概述**：`kb_query` 走知识库的**外部查询同步接口**（`/api/ext/{id}/query`），凭外部查询 token 鉴权，返回的是**基于知识库内容的完整回答**（带 [n] 引用编号与来源片段），而不是原始检索片段。仓库内另有同功能的 HTTP 直连方式（见 [`kbQuery-接入说明.md`](../kbQuery-接入说明.md) 与 `../kbQuery.js`），本 server 是其标准 MCP 封装，供外部 Agent 项目接入使用（如 my-Agent，属仓库外部项目，相关接入细节见该项目文档）。

## 用途

Agent 在对话中需要查阅企业知识库（制度、文档、FAQ）时，可调用 `kb_query` 工具：

- 传入问题（`query`）即可获得基于知识库的回答 + 引用来源（`top_k` 控制引用条数）
- 返回格式：回答正文 + `引用来源：` 列表（`[n] 文档名：片段`），总计 ≤6000 字符
- 引用片段中的图片链接（`/api/files/images/...`）可由 Agent 用于展示知识库图片
- 服务端不可达 / token 失效 / 限流均返回可读中文错误，不会抛异常

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
| ---- | ---- | ------ | ---- |
| `KB_EXT_URL` | 否 | `http://localhost:8091` | 知识库服务地址 |
| `KB_EXT_ID` | 是 | — | 外部查询配置 id（知识库超管在「外部查询」页创建后获得） |
| `KB_EXT_TOKEN` | 是 | — | 外部查询访问 token（创建/重置时返回明文；未配置时工具返回错误"KB_EXT_TOKEN 未配置：请在知识库「外部查询」创建配置后设置环境变量"） |

## 工具说明

### `kb_query`

| 参数 | 必填 | 类型 | 说明 |
| ---- | ---- | ---- | ---- |
| `query` | 是 | string | 查询问题 |
| `top_k` | 否 | number（1-10，默认 5） | 返回引用来源条数 |

### 返回说明

```
<回答正文（基于知识库内容，含 [n] 引用编号）>

引用来源：
[1] <文档名>：<引用片段（≤2000 字）>
[2] <文档名>：<引用片段>
```

- 回答全文 ≤6000 字符，超出自动截断并注明
- 知识库无命中时返回固定文案「未检索到相关内容，我无法回答该问题……」，不调 LLM

### 错误处理（优雅降级，均不抛异常）

| 场景 | 返回 |
| ---- | ---- |
| 未配置 `KB_EXT_ID` / `KB_EXT_TOKEN` | `KB_EXT_TOKEN 未配置：请在知识库「外部查询」创建配置后设置环境变量...` |
| token 错误 / 配置停用 / 删除 / 重置 token（401） | `链接无效或已失效：请检查 KB_EXT_ID / KB_EXT_TOKEN 是否与知识库管理端「外部查询」配置一致...` |
| 超过每分钟查询次数（429） | `查询过于频繁：知识库外部查询每分钟有次数限制，请稍后再试` |
| 知识库不可达（网络错误 / 超时 30s） | `知识库服务不可达（网络错误或超时），请检查 KB_EXT_URL 或稍后再试` |
| 服务端其他错误（检索失败 400 / LLM 失败 502 等） | `知识库查询失败 (<状态码>): <中文详情>` |

## 安装

```bash
cd mcp-server/kb-ext-server
npm install
node --check index.js && node --check query.js   # 语法检查
```

## 方式 A：在 my-Agent 设置界面添加（推荐）

> my-Agent 为仓库外部项目，以下步骤以其界面为例；其他 MCP 客户端（Claude/Cursor 等）配置方式同理，只是界面入口不同。

1. **知识库侧**：超管登录知识库管理端 →「外部查询」→ 新建配置（选择要暴露的知识库，可调温度/检索条数等）→ 复制返回的**配置 id** 与 **token**
2. **启动环境**：在启动 my-Agent 后端进程的环境中设置环境变量（MCP server 子进程继承后端进程环境，环境变量必须配置在启动进程的环境中，不能在客户端界面填写）：

   ```bash
   KB_EXT_URL=http://localhost:8091 KB_EXT_ID=<配置id> KB_EXT_TOKEN=<token> ./start.sh
   ```

3. **界面添加**：打开 my-Agent **⚙️ 设置 →「MCP 服务」tab** →「新增」：

   | 字段 | 值 |
   | ---- | -- |
   | 名称 | `kb_ext`（字母/数字/下划线/连字符，1-32 位） |
   | 命令 | `node` |
   | 参数 | `<本项目路径>/mcp-server/kb-ext-server/index.js` |
   | 描述 | 企业知识库问答（kb_query） |

4. **新会话生效**：保存后桥接自动重建，开新会话即可看到 `mcp__kb_ext__kb_query` 工具；Agent 对话中按需自动调用

> 依赖安装：脚本目录需先执行 `npm install`（93 个包，约 8s）。凭据必须配在进程环境变量中，不能在界面填写。

## 文件结构

```
mcp-server/kb-ext-server/
├── index.js        # MCP server 入口：注册 kb_query 工具，stdio 启动
├── query.js        # 业务核心：环境变量读取/同步问答调用/错误映射/结果格式化
├── package.json
└── README.md
```

## 验证

```bash
cd mcp-server/kb-ext-server
node --check index.js && node --check query.js
# 函数级冒烟（真实联调）：直接调用 query.js 查询一次
KB_EXT_URL=http://localhost:8091 KB_EXT_ID=<配置id> KB_EXT_TOKEN=<token> \
  node -e "import('./query.js').then(async m => { const r = await m.queryKnowledgeBase({ ...m.getKbConfig(), query: '你的问题' }); console.log(m.formatResult(r)); })"
```

- 知识库侧联调：`curl -X POST http://localhost:8091/api/ext/<配置id>/query -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{"query":"你的问题"}'`
