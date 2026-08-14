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
| 400 | 参数不合法 / 旧密码不正确 | `部门不存在`、`旧密码不正确` |
| 401 | 未登录 / token 失效 / 登录失败 | `用户名或密码错误`、`登录已过期，请重新登录`（带 WWW-Authenticate 头） |
| 403 | 登录但角色无权 | `仅超级管理员可执行此操作` |
| 404 | 资源不存在 | `用户不存在`、`部门不存在` |
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

---

## 2. 用户管理 /api/users（全部仅 super_admin）

### 2.1 GET /api/users?department_id=xxx（可选过滤）

响应 `200`：`[UserPublic, ...]`（按创建时间升序，`department_name` 已 join）

### 2.2 POST /api/users

请求：`{"username": "zhangsan", "password": "123456", "display_name": "张三", "role": "dept_admin", "department_id": "dept_default"}`
- `201` 返回 UserPublic
- `400 {"detail": "部门不存在"}`（department_id 传了但不存在）
- `409 {"detail": "用户名已存在"}`
- `422`（role 不在 super_admin/dept_admin/user、必填缺失）

### 2.3 PUT /api/users/{id}（部分更新，传哪个改哪个）

请求：`{"display_name": "...", "role": "user", "department_id": "xxx" | null, "status": "disabled", "password": "newpass"}`
- `200` 返回 UserPublic（password 传了则重哈希，不回传）
- `400` 部门不存在 / `404 {"detail": "用户不存在"}`
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
| `top_k` | int? | 返回条数（默认取配置 RETRIEVAL_TOP_K） |
| `enable_hybrid` | bool? | 混合检索开关：None=用配置默认；true/false=强制开关 |
| `enable_rerank` | bool? | 重排开关：None=用配置默认；true/false=强制开关 |
| `similarity_threshold` | float? | 相似度阈值（0~1），低于阈值的命中被过滤 |

成功 `200`：`{sources: [{doc_id, doc_name, chunk_id, text, score, kb_id, kb_name}, ...]}`（多库时 Source 附带 kb_id/kb_name）
失败：`400`（query 为空 / kb 未传 / kb_ids>5）、`404`（任一库不存在或不可访问，伪装防探测）、`500`（检索链路异常）

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
