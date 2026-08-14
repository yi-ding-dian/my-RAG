# 安全说明（Security Policy）

## 支持的版本

| 版本 | 支持状态 |
|------|----------|
| 0.1.x（当前） | 支持（维护中） |

## 报告漏洞（Responsible Disclosure）

请**不要**为安全问题创建公开 Issue。请通过以下渠道之一报告：

- **GitHub Security Advisory**（推荐）：仓库页 → `Security` → `Report a vulnerability`
  创建私有漏洞报告（仅维护者可见）；
- **邮件**：发送至维护者邮箱（见仓库 About 页面），邮件主题注明 `[SECURITY]`，
  内容包含：受影响版本、漏洞描述、复现步骤、影响范围与可选的修复建议。

我们会尽力在 48 小时内确认并回复，修复后通过 Security Advisory 发布公告。
在修复发布前，请勿公开披露细节。

## .env / 密钥泄漏处理指引

本项目所有敏感配置（LLM/Embedding API Key、ES 口令、JWT_SECRET、MySQL/MinIO 口令、
外部查询 token 等）均通过各子项目 `.env` 注入（已被 `.gitignore` 忽略，不入库）。

如果 `.env`（或任何真实口令）意外泄漏：

1. **立即吊销**：在 LLM/ES/MySQL/MinIO 等对应服务端重置密钥/口令；
2. **检查历史**：如果泄漏内容曾进入 git 历史（`git log -p` 可查），将受影响文件
   的密钥轮换后，用 `git filter-repo` 等工具清理历史，并注意 GitHub 侧缓存副本；
3. **检查环境**：确认 `.env`、`*.pem`、`*.key`、`credentials.json` 等未被打包进
   Docker 镜像（`.dockerignore` 已排除，勿改动）；CI 日志中不得出现真实口令；
4. **告知**：如确认已泄露到公开渠道，按"报告漏洞"渠道通知维护者。

### 日常防护清单

- 新环境首次部署：复制 `.env.example` 为 `.env`，**替换所有占位值**（`not-needed`、
  `changeme`、`sk-your-key-here` 等仅用于离线演示）；
- 不要将 `.env`、`data/`（运行时数据，含上传文档与向量库）提交或打包发布；
- 仓库内文档不得包含真实口令与内网 IP；截图发布前脱敏；
- 前端构建产物（`dist/`）不含密钥；密钥只存在于后端进程环境。
