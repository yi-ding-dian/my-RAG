# my-RAG API 契约（v1.2 · 多租户+团队协作 + 企业功能增强）

> 本文档由 Agent 1 冻结（后端基础与认证阶段），Agent 2（后端 RAG 改造）/ Agent 3（前端）必须以本文档为准。
> 变更需知会主 Agent 同步更新。
> v1.2 追加：第 7 章（检索多库/重排参数、文档回收站、标签、向量重建、质量统计、审计日志、图片代理、embedding 维度实测）。

## 0. 全局约定

- Base URL：`http://<host>:8091`，全部接口 `application/json`（除文档上传 multipart）
- 认证：除 `POST /api/auth/login`、`GET /api/health` 外**所有**接口要求
  `Authorization: Bearer <access_token>`
- 角色：`super_admin`（超级管理员）/ `dept_admin`（部门管理员）/ `user`（普通用户）
- 状态：`active`（正常）/ `disabled`（禁用，登录与鉴权均拒绝）
- 时间：字符串 `"%Y-%m-%d %H:%M:%S"`
- id：uuid4 hex 前 12 位；默认部门固定 id = `dept_default`；种子账号 `admin / admin123`

### 错误码约定（detail 均为中文文案）

| 状态码 | 场景 | detail 示例 |
|---|---|---|
| 400 | 参数不合法 / 旧密码不正确 / 密码强度不足 | `部门不存在`、`旧密码不正确`、`密码至少 8 位，且需同时包含字母和数字` |
| 401 | 未登录 / token 失效 / 登录失败 | `用户名或密码错误`、`登录已过期，请重新登录`（带 WWW-Authenticate 头） |
| 403 | 登录但角色无权（仅知识库写权限等资源级 403） | `仅超级管理员或本部门管理员可管理知识库` |
| 404 | 资源不存在 / 管理面越权伪装 | `用户不存在`、`部门不存在`、`资源不存在`（users/audit/settings/logs 等管理面普通用户访问统一 404 伪装，防接口探测） |
| 409 | 唯一性冲突 / 被引用 / 删除冲突 | `用户名已存在`、`部门名称已存在`、`不能删除当前登录账号`、`不能删除最后一个超级管理员`、`部门下存在 N 个用户，请先转移或删除用户` |
| 422 | pydantic 校验失败（字段类型/枚举） | 默认 FastAPI 格式（英文字段名） |

---

## 1. 认证 /api/auth

### 1.1 POST /api/auth/login（公开）

请求：
```json
{"username": "admin", "password": "admin123"}
```
成功 `200`：
```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "user": {
    "id": "abc123def456", "username": "admin", "display_name": "超级管理员",
    "role": "super_admin", "department_id": null, "department_name": null,
    "status": "active", "created_at": "2026-08-09 02:00:00"
  }
}
```
失败：`401 {"detail": "用户名或密码错误"}`（用户不存在/密码错/禁用统一此文案，防枚举）

### 1.2 GET /api/auth/me（登录）

响应：`200` 返回 `UserPublic`（同 login.user 结构，`department_name` 可为 null）
失败：`401`（无 token / 失效 / 用户已禁用）

### 1.3 POST /api/auth/change-password（登录）

请求：`{"old_password": "admin123", "new_password": "newpass888"}`
- `200 {"message": "密码修改成功"}`
- `400 {"detail": "旧密码不正确"}` / `400 {"detail": "新密码不能与旧密码相同"}`
- `400 {"detail": "密码至少 8 位，且需同时包含字母和数字"}`（新密码强度不足）

---

## 2. 用户管理 /api/users（全部仅 super_admin）

### 2.1 GET /api/users?department_id=xxx（可选过滤）

响应 `200`：`[UserPublic, ...]`（按创建时间升序，`department_name` 已 join）

### 2.2 POST /api/users

请求：`{"username": "zhangsan", "password": "user123456", "display_name": "张三", "role": "dept_admin", "department_id": "dept_default"}`
- `201` 返回 UserPublic
- `400 {"detail": "部门不存在"}`（department_id 传了但不存在）
- `400 {"detail": "密码至少 8 位，且需同时包含字母和数字"}`（密码强度不足：少于 8 位或未同时含字母和数字）
- `409 {"detail": "用户名已存在"}`
- `422`（role 不在 super_admin/dept_admin/user、必填缺失）

### 2.3 PUT /api/users/{id}（部分更新，传哪个改哪个）

请求：`{"display_name": "...", "role": "user", "department_id": "xxx" | null, "status": "disabled", "password": "newpass888"}`
- `200` 返回 UserPublic（password 传了则重哈希，不回传）
- `400` 部门不存在 / `400 {"detail": "密码至少 8 位，且需同时包含字母和数字"}`（重置密码强度不足） / `404 {"detail": "用户不存在"}`
- 禁用走 `status: "disabled"`，不做物理删除

### 2.4 DELETE /api/users/{id}

- `200 {"message": "用户已删除"}`
- `409 {"detail": "不能删除当前登录账号"}`（删自己）
- `409 {"detail": "不能删除最后一个超级管理员"}`
- `404 {"detail": "用户不存在"}`

---

## 3. 部门管理 /api/departments（全部仅 super_admin）

### 3.1 GET /api/departments

响应 `200`：`[{"id": "dept_default", "name": "默认部门", "description": "...", "created_at": "..."}, ...]`

### 3.2 POST /api/departments

请求：`{"name": "研发部", "description": "研发团队"}`
- `201` 返回 DepartmentPublic；`409 {"detail": "部门名称已存在"}`

### 3.3 PUT /api/departments/{id}

请求：`{"name": "...", "description": "..."}`（部分更新）
- `200` 返回 DepartmentPublic；`409` 重名；`404 {"detail": "部门不存在"}`

### 3.4 DELETE /api/departments/{id}

- `200 {"message": "部门已删除"}`
- `409`：`部门下存在 N 个用户，请先转移或删除用户` / `部门下存在 N 个知识库，请先迁移或删除知识库`
- `404 {"detail": "部门不存在"}`

---

## 4. KnowledgeBase 新字段（Agent 2 生效）

现有 `KnowledgeBase` 模型（`backend/models/rag_models.py`）在响应中**新增**：

```json
{
  "id": "...", "name": "...", "description": "...",
  "doc_count": 0, "chunk_count": 0, "created_at": "...",
  "department_id": "dept_default",   // 新增：所属部门，null=全局
  "owner_id": "abc123def456"          // 新增：创建人用户 id
}
```

`CreateKBRequest` 新增可选 `department_id`：
- super_admin 创建：可指定部门（缺省 = 当前用户部门或 null）
- dept_admin 创建：**后端强制覆盖为本人部门**（忽略 body）
- user 创建：403

---

## 5. 配置档案 profile 新段（Agent 2 生效）

`GET /api/settings/profiles` 返回的每个 profile 在现有 `llm/embedding/mineru/retrieval/chunking` 基础上**新增**两段：

```json
{
  "...现有字段...",
  "mysql": {
    "host": "127.0.0.1", "port": 5455,
    "user": "ragflow", "password": "******",   // GET 脱敏（endswith password 脱敏）
    "database": "my_rag"
  },
  "minio": {
    "endpoint": "127.0.0.1:9000",
    "access_key": "rag_flow", "secret_key": "******",   // GET 脱敏
    "bucket": "my-rag", "secure": false, "region": ""
  }
}
```

- 保存时传回脱敏值不覆盖原值（沿用 `****` 判定）；`JWT_SECRET` **不进**档案（仅 .env）
- `POST /api/settings/profiles/{id}/test` 响应新增 `mysql` 与 `minio` 两项，
  结构与现有 `{ok, latency_ms, message}` 一致
- mysql 配置变更 → `get_engine()` key 比对自动重建（无需重启）

---

## 6. 给 Agent 2/3 的说明

- 依赖注入：`get_current_user`（deps.py）→ `UserPublic`；`require_super_admin` 用于
  用户/部门/系统配置管理；`can_access_kb(kb, user)` / `can_manage_kb(kb, user)` 为纯函数
- kb 无权限时用 404 伪装防探测（权限矩阵见方案文档）
- 会话 JSON 增加 `user_id`；历史列表按用户过滤（super_admin 全量）
- 登录响应中的 `user` 即 UserPublic，前端可直接存入 localStorage 恢复会话

---

## 7. 功能增强接口（v1.2 新增）

### 7.1 聊天增强 /api/chat

#### POST /api/chat/retrieve（登录 + can_access_kb，无权限 404 伪装）

检索调试，v1.2 新增参数化检索：

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | str | 必填 |
| `kb_id` | str? | 单库检索（与 kb_ids 二选一） |
| `kb_ids` | list[str]? | 多库对比检索，1~5 个；每库独立 top_k 候选，合并按 score 降序取全局 top_k；与 kb_id 都传时**优先** |
| `top_k` | int? | 返回条数 1~50（越界 422；默认取配置 RETRIEVAL_TOP_K，与 stream 的 1~50 校验对齐） |
| `enable_hybrid` | bool? | 混合检索开关：None=用配置默认；true/false=强制开关 |
| `enable_rerank` | bool? | 重排开关：None=用配置默认；true/false=强制开关 |
| `similarity_threshold` | float? | 相似度阈值（0~1），低于阈值的命中被过滤 |

成功 `200`：`{sources: [{doc_id, doc_name, chunk_id, text, score, kb_id, kb_name}, ...]}`（多库时 Source 附带 kb_id/kb_name）
失败：`400`（query 为空 / kb 未传 / kb_ids>5）、`404`（任一库不存在或不可访问，伪装防探测）、`500`（检索链路异常）

#### POST /api/chat/stream（登录 + can_access_kb，无权限 404 伪装）

SSE 流式问答。请求体：`{query 或 message, kb_id, session_id?, top_k?}`。事件流（按顺序）：

| 顺序 | 事件名 | data | 说明 |
|---|---|---|---|
| 1 | `meta` | `{sources: [...]}` | 检索来源（无命中时为空数组） |
| 2 | `prompt` | `{prompt: [{role, content}, ...], retrieval_ms: int, kg_ms: int, rewrite_ms: int, rewritten_query: str?}` | 整块打包发给 LLM 的完整 messages 数组 + 召回耗时（毫秒）/ 图谱构建耗时（毫秒）/ 查询改写耗时（毫秒，未改写为 0）/ 改写后检索查询（未改写为 null）。仅在检索命中且调用 LLM 前发出；无命中 / 检索失败不发 |
| 3 | `delta` | `{text: str}` | 增量文本 |
| 4 | `done` | `{session_id, message_count, citation?}` | 生成完成，会话落盘。`citation`（来源非空时附带）：引用溯源统计——`refs` 有效引用标数 / `refs_invalid` 越界剥离数（模型编号 > 来源数，后端已从 delta 中剔除）/ `sentences` 句子总数 / `cited_sentences` 含引用句子数 / `coverage` 覆盖率（0~1）。前端据此显示引用提示 |

引用溯源保障：`delta` 文本经 CitationGuard 校验（规则与前端 renderCitationContent 同源——`[n]` 后须行尾/空白/标点，`见[3]附录` 类不识别），越界编号剥离（不送达前端），合法编号保留；回答完全无引用标且来源非空时前端提示"未标注引用来源"。
| 任意 | `error` | `{message: str}` | 检索失败 / LLM 调用失败等（HTTP 仍 200） |

- `prompt.prompt` 首条为 system（含引用），最后一条为 user（含"问题：{原问题}"），中间为历史轮次（多轮关闭时无）
- 无命中路径：`meta`（空 sources）→ `delta`（提示文本）→ `done`，不发 `prompt`、不调用 LLM
- 历史会话详情（GET /api/chat/history/{id}）的 messages 不含 prompt/耗时字段（仅前端本次会话内存态）

#### POST /api/chat/history/{session_id}/rename（owner 或 super_admin，否则 404 伪装）

请求：`{"title": "新标题（1~50 字）"}`
- `200 {"message": "会话已重命名", "session_id": "...", "title": "..."}`
- `400` 标题为空或超 50 字；`404` 会话不存在；重命名更新 updated_at，列表按最新活动置顶

#### GET /api/chat/history/{session_id}/export（owner 或 super_admin，否则 404 伪装）

导出会话为 Markdown 附件（记审计 `chat.export`，含消息数摘要）：
- `200`：`text/markdown; charset=utf-8` + `Content-Disposition: attachment; filename*=UTF-8''{标题}.md`
- 内容结构：`# 会话标题（kb_id、时间）` → 每条消息 `## 用户` / `## 助手`（回答含 [n] 引用标注）→ `### 引用 n：来源文档名`（来源片段前 500 字）；无消息时仅输出标题模板
- `404` 会话不存在

### 7.2 文档增强 /api/kbs/{kb_id}/documents（管理类 can_manage_kb 否则 403；读类 can_access_kb 无权限 404 伪装）

#### POST /{doc_id}/rename（can_manage_kb）

请求：`{"name": "新文件名（1~255 字符）"}`
- 只改展示名 `original_name`，内部存储名/向量/chunk 不变，**改名即时生效**（历史会话引用是落盘快照不回溯）
- `200` 返回 DocumentItem；`400` 长度非法 / 扩展名与原文件不一致 / 同库重名

#### POST /from-url（can_manage_kb）

URL 网页导入为文档（记审计 `doc.from-url`）：
- 请求：`{"url": "https://..."}`；约束：仅 http/https、超时 30s、响应体 ≤5MB
- 文件名取 `<title>`/首个 `<h1>`（截断 80、重名加序号），正文纯文本落盘为 .md，`file_type="url"`，状态 `uploaded` 待解析（与上传一致走 ingest）
- `200` 返回 DocumentItem；`400` 非 http/https / 抓取失败（超时、4xx、网络错误）

#### GET /{doc_id}/raw（can_access_kb，无权限 404 伪装）

文档原始内容预览：
- `pdf`：`application/pdf` 原始字节（浏览器原生渲染），≤50MB（超限 `413`）
- `txt` / `md` / `url`：`text/plain; charset=utf-8` 文本（url=导入时抓取的 md 文本）
- 其他类型 `400`（暂不支持在线预览）；回收站文档 `404` 伪装

#### DELETE /{doc_id}（软删语义，can_manage_kb）

- **不再物理删除**：标记 `deleted` + 向量 metadata `doc_active=False` + 失效 BM25，检索自动排除；向量/存储/切块全部保留，恢复无需重新解析
- `200 {"message": "文档已移入回收站（可恢复）", "doc_id": "..."}`；已在回收站 `409`；向量状态更新失败 `500`
- 彻底删除请走 `POST /{doc_id}/purge`（防误删）

#### POST /{doc_id}/restore（can_manage_kb）

恢复回收站文档：取消 deleted 标记 + 向量 `doc_active=True`，立即重新进入检索
- `200` 返回 DocumentItem；不在回收站 `409`

#### POST /{doc_id}/purge（can_manage_kb）

彻底删除：存储对象 + 向量 + 元数据 + 本地文件全清，**不可恢复**
- `200 {"message": "文档已彻底删除", "doc_id": "..."}`

#### GET /trash（can_manage_kb）

回收站列表：`200` 返回 `[DocumentItem, ...]`（`deleted=true`，含 `deleted_at` 删除时间；`/trash` 必须在 `/{doc_id}` 之前注册，避免路径捕获）

#### POST /trash/empty（can_manage_kb）

清空回收站：批量 purge（记审计 `doc.trash-empty` 含删除数量）
- `200 {"message": "回收站已清空", "count": N}`

### 7.3 知识库增强 /api/kbs

#### GET /api/kbs?tag=a&tag=b（登录；super_admin 全量，其余仅本部门）

- 标签过滤：可重复传 tag 参数，**交集语义**（同时包含全部给定标签才返回）
- `200` 返回 `[KnowledgeBase, ...]`（含 doc_count/chunk_count/vector_status 摘要）

#### GET /api/kbs/tags（登录；可见范围内）

标签聚合（前端筛选条数据源）：
- `200 {"tags": [{"name": "...", "count": N}, ...]}`（count 降序、同 count 按名称升序）

#### PUT /api/kbs/{kb_id}/tags（can_manage_kb，否则 403）

覆盖式设置标签（记审计 `kb.tags-update`）：
- 请求：`{"tags": ["研发", "产品"]}`；≤10 个、每个 1~20 字符（自动去重去空白）；**空数组=清空**
- `200` 返回更新后 KnowledgeBase（含实时统计）；`400` 非法输入；`404` 库不存在

#### GET /api/kbs/{kb_id}/vector-status（can_access_kb，无权限 404 伪装）

向量维度一致性检测（P0）：
- `200 {"kb_id", "collection_vectors", "current_dim", "model_dim", "compatible", "message"}`
- `current_dim`：collection 内向量维度（空 collection 为 null）；`model_dim`：当前激活 embedding 模型实测维度（不可测为 null）
- `compatible`：维度相同 / collection 空 / 模型维度无法检测 → True；明确不匹配 → False

#### POST /api/kbs/{kb_id}/rebuild-vectors（can_manage_kb，否则 403）

一键重建向量：清空旧向量 → 逐个已入库文档重新 embedding（当前激活模型）→ 写回；后台串行任务
- `200 {"task_id": "..."}`（已有 running 任务时复用，幂等防重复触发）

#### GET /api/kbs/{kb_id}/rebuild-status（can_access_kb，无权限 404 伪装）

重建进度轮询：
- `200 {"kb_id", "task_id", "running", "done", "total", "failed", "current_doc", "finished_at", "errors": [{"doc_id", "doc_name", "error"}]}`
- 无任务历史时：task_id=null, running=false, done/total/failed=0

### 7.4 统计增强 /api/stats

#### GET /api/stats/quality?kb_id=（登录 + can_access_kb，无权限 404 伪装）

检索质量统计（近 30 天检索日志汇总）：
- `200 {"kb_id", "window_days": 30, "total_retrievals", "avg_hits_per_retrieval", "hit_docs": [{"doc_id", "doc_name", "hits"}]（top10 降序）, "zero_hit_docs": [{"doc_id", "doc_name", "chunks"}]（窗口期从未命中的已入库文档）, "daily": [{"date", "retrievals", "hit_rate"}]（日粒度，无数据的天 hit_rate=0）}`
- 无检索数据时返回空数组不报错

### 7.5 审计日志 /api/audit（全部仅 super_admin，否则 403）

#### GET /api/audit/logs（分页查询，created_at 倒序）

| 查询参数 | 说明 |
|---|---|
| `page` / `page_size` | 默认 1 / 20（page_size 1~200） |
| `action` | 操作类型精确过滤（如 kb.create / doc.upload / chat.export） |
| `target_type` | 目标类型（kb / doc / user / dept / config / chat） |
| `username` | 用户名模糊过滤 |
| `start_time` / `end_time` | 时间范围（含，`%Y-%m-%d %H:%M:%S`，字符串比较） |

`200 {"total", "page", "page_size", "items": [AuditLogPublic, ...]}`；AuditLogPublic 字段：`id, user_id, username, role, action, target_type, target_id, target_name, detail, ip, status, created_at`

#### GET /api/audit/actions

操作类型下拉数据源（前端筛选用）：`200 {"actions": [{"action", "label"}, ...]}`，含全部动作：登录/改密、用户/部门 CRUD、kb 增删改/标签/重建向量、文档 上传/重命名/网页导入/解析/删除/恢复/彻底删除/清空回收站、会话 删除/导出、配置档案 创建/修改/删除/激活/测试连接

### 7.6 文件代理 /api/files

#### GET /api/files/images/{doc_id}/{name}（登录；文档所属知识库可访问，无权限 404 伪装）

MinerU 解析图片鉴权代理（不暴露预签名 URL，图片存 MinIO/local）：
- **鉴权二选一**：`Authorization: Bearer <token>` header，或 `?token=<token>` query（img 标签无法带 header，前端渲染 markdown 图片时对 src 自动追加 JWT）
- `200` 图片字节（content_type 按扩展名 image/*，未知回退 octet-stream）；`401` 未鉴权；`404` 无权限/对象不存在/存储不可用

### 7.7 系统配置增强 /api/settings（仅 super_admin）

#### GET /api/settings/embedding-dim

当前激活 embedding 模型实际输出维度（**实测**，全局缓存；配置档案里的 dimension 字段是手填值，两者不同）：
- `200 {"dimension": 1024, "model": "bge-m3", "ok": true, "message": "..."}`；模型不可用/未配置时 `dimension: null, ok: false`
- 维度冲突检测（`/api/kbs/{id}/vector-status`）以此实测值为准

#### GET/POST /api/settings/chat（登录可读；super_admin / dept_admin 可写）

聊天设置接口（前端"聊天设置"弹窗数据源）。GET 返回当前用户视角合并值（全局活跃档案 + 本部门覆盖）；POST 白名单仅 `chat/retrieval/llm` 三段（未知字段/段 → 400）。

`chat` 段字段表：

| 字段 | 类型 | 值域 | 默认 | 说明 |
|---|---|---|---|---|
| `temperature` | float? | 0~2，null=跟随模型默认 | null | 温度 |
| `top_p` | float? | 0~1 | null | 核采样概率 |
| `max_tokens` | int? | null=跟随模型默认 | null | 最大输出 Token |
| `enable_multi_turn` | bool | - | true | 多轮对话开关 |
| `history_rounds` | int | 1~20 | 8 | 携带的历史轮数 |
| `system_prompt` | str | 空串=内置默认模板 | "" | 自定义系统提示词（可含 {refs}/{knowledge} 占位符） |
| `kg_enhance` | bool | - | true | 知识图谱增强开关 |
| `query_rewrite` | bool | - | true | 查询改写开关：LLM 把提问改写为正式独立检索查询——口语→书面（去除"帮我/查查/呀啦呢"等口头语）+ 指代消解（"它/上面那个"→实体）+ 省略补全；口语词不依赖历史直接触发，指代词需有对话历史（无历史消不掉），也可能多轮开关关闭但会话已有历史时触发（与多轮解耦）。每轮最多一次 LLM 调用（≤8s 超时，失败/无触发词自动回退原问题，不阻塞问答）。改写结果仅用于本轮检索（不落盘不入历史），SSE `prompt` 事件附 `rewritten_query`（改写后查询）/`rewrite_ms`（改写耗时），未改写时为 null/0 |
| `thinking_mode` | str | `disabled` / `enabled_low` / `enabled_high` / `enabled_max` | `disabled` | 聊天问答思考模式：`disabled`=关闭思考（更快更省 token，推荐）；`enabled_*`=开启思考并指定强度。注入方式按服务商区分：在线 API（api.deepseek.com 等）经请求 `extra_body` 控制（disabled → `{"thinking": {"type": "disabled"}}`；enabled → `{"thinking": {"type": "enabled"}, "reasoning_effort": low/high/max}`）；本地 Qwen 思考模型（base_url 含 localhost/127.0.0.1/192.168./10./172.16-31.）`disabled` 时在 messages 末尾注入空 `<think>` prefill 跳过思考，`enabled_*` 时不注入（保持模型默认思考，本地无法控制强度）。该注入为请求层变换，SSE `prompt` 事件内容仍为组装后原始 messages（不含注入/extra_body） |

`retrieval` 段：`top_k`（1~20）、`similarity_threshold`（0~1，0=不过滤）。`llm` 段：激活模型 `base_url/api_key/model/temperature/max_tokens/timeout` 6 字段（部门 LLM 覆盖）。
