/**
 * kb-ext MCP server
 *
 * 将企业知识库（FastAPI 服务）的外部查询能力包装为 MCP 工具 kb_query，
 * 通过 stdio 传输与 Agent 通信。Agent 调用工具时，知识库完成
 * 检索 + LLM 回答 + 引用来源组装，一次返回完整答案。
 *
 * 配置（环境变量，MCP server 子进程继承宿主进程环境）：
 *   KB_EXT_URL    知识库服务地址，默认 http://localhost:8091
 *   KB_EXT_ID     外部查询配置 id（知识库管理端「外部查询」创建后获得）
 *   KB_EXT_TOKEN  外部查询访问 token（创建/重置时返回）
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { getKbConfig, queryKnowledgeBase, formatResult } from "./query.js";

const server = new McpServer(
  {
    name: "kb-ext",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  },
);

server.tool(
  "kb_query",
  "查询企业知识库，返回基于知识库内容的回答与引用来源",
  {
    query: z.string().describe("查询问题"),
    top_k: z
      .number()
      .int()
      .min(1)
      .max(10)
      .default(5)
      .describe("返回引用来源条数，默认 5，范围 1-10"),
  },
  async ({ query, top_k }) => {
    // 环境变量在 handler 内读取：缺失时返回明确提示（不抛异常）
    const { url, configId, token } = getKbConfig();
    if (!configId || !token) {
      return {
        content: [
          {
            type: "text",
            text: "KB_EXT_TOKEN 未配置：请在知识库「外部查询」创建配置后设置环境变量（KB_EXT_ID / KB_EXT_TOKEN / KB_EXT_URL）",
          },
        ],
        isError: true,
      };
    }
    try {
      const { answer, sources } = await queryKnowledgeBase({
        url,
        configId,
        token,
        query,
        top_k,
      });
      return { content: [{ type: "text", text: formatResult({ answer, sources }) }] };
    } catch (err) {
      // 所有错误（鉴权失败 / 限流 / 服务不可达 / 业务错误）统一转 MCP 工具错误
      return {
        content: [{ type: "text", text: err.message || "知识库查询失败" }],
        isError: true,
      };
    }
  },
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("kb-ext MCP 服务已通过 stdio 启动");
}

main().catch((err) => {
  console.error("致命错误：", err);
  process.exit(1);
});
