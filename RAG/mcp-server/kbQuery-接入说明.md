# 知识库查询工具（kb_query）接入说明

> 给 Agent（PiAgent）开发者：在 PiAgent 中接入企业知识库查询能力。
> 工具文件已就位于 `mcp/src/tools/kbQuery.js`，需完成**注册**与**配置接口**两步。

---

## 一、接入三步

### 1. 注册工具（mcp/src/index.js）

```javascript
import { registerKbQuery } from "./tools/kbQuery.js";

// 在现有工具注册处追加一行：
registerKbQuery(server);
```

注册后 Agent 自动发现工具：`kb_query`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | ✅ | 查询问题 |
| `top_k` | number | 选（默认 5） | 返回引用数量，1-10 |

### 2. 实现配置接口（方案 A）

工具运行时调用**配置接口**获取"知识库查询链接"（界面配置 → 存数据库 → 工具读取，适配镜像部署）：

```
GET /api/agent-config/kb-link
```

| 响应 | 说明 |
|---|---|
| `200 {"link": "http://<host>:8091/ext-query/<configId>?token=<token>"}` | 已配置，返回查询链接 |
| `404 {"detail": "未配置知识库查询链接"}` | 未配置，工具提示用户在设置中填写 |

- 工具默认请求 `http://127.0.0.1:3000`，如后端端口不同，用环境变量 `AGENT_CONFIG_BASE_URL` 覆盖
- 可选：工具也支持 `KB_QUERY_LINK` 环境变量直接提供链接（跳过接口）

### 3. 设置界面（可选增强）

- 配置项："知识库查询链接"（单个输入框，粘贴超管分享的链接）
- 校验：URL 格式 + 非空
- 可选"测试连接"：调 `GET {origin}/api/ext/{configId}/info?token=xxx`，200=可用 / 401=链接无效

---

## 二、知识库侧接口契约（已就绪，无需改动）

### 同步查询

```
POST {origin}/api/ext/{configId}/query
Authorization: Bearer {token}
Content-Type: application/json

{ "query": "接地线配置", "top_k": 3 }
```

响应：

```json
{
  "answer": "根据相关资料，接地线配置方法如下：…（≤6000 字）",
  "sources": [
    {
      "document_name": "CWBS-SCA调试说明书1-1.pdf",
      "text": "引用片段…（≤2000 字）",
      "image_urls": ["/api/files/images/xxx.jpg"]
    }
  ]
}
```

### 配置校验（测试连接用）

```
GET {origin}/api/ext/{configId}/info?token=xxx
→ 200 {"name": "配置名称", "kb_names": ["知识库A"]}
→ 401 链接无效或已失效
```

---

## 三、错误码表（工具已处理为中文提示）

| 场景 | 工具返回 |
|---|---|
| 未配置链接 | `知识库查询失败: 未配置知识库查询链接，请在设置中填写`（isError） |
| 链接格式错误 | `知识库查询链接格式不正确（应为 /ext-query/<配置ID>?token=xxx）` |
| 401（token 失效/停用/重置） | `知识库查询链接无效或已失效（请重新获取）` |
| 429（限流，20 次/分钟） | `知识库查询过于频繁，请稍后再试` |
| 网络/超时 | `知识库查询超时（30 秒）` / 网络错误原文 |
| 知识库服务错误 | `知识库查询失败 (502): <detail>` |

---

## 四、验证方式

1. 界面配置链接：`http://localhost:8091/ext-query/d29eb7564db3?token=32vAqKE3O5zgqEAMEIAxEBcbV4oB-84UP1GMk6nJ07w`（示例，超管在知识库"外部查询"创建/重置后获得）
2. 新会话提问："接地线配置"
3. 预期：返回知识库回答 + 引用来源（文档名+片段）

---

## 五、安全说明

- 链接内含访问凭证（token），妥善保管；知识库侧重置 token 后旧链接立即失效
- 知识库侧限流：每配置 20 次/分钟
- 所有外部查询记录在知识库侧审计日志（ext_query_logs.jsonl）
- 工具代码遵循"凭据不硬编码、错误 isError 返回"规范
