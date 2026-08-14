import { z } from "zod";

/**
 * 知识库查询工具（kb_query）
 *
 * 供 PiAgent 内置工具注册（mcp/src/tools/kbQuery.js），使用方式：
 *   import { registerKbQuery } from "./tools/kbQuery.js";
 *   registerKbQuery(server);   // index.js 注册一行
 *
 * 配置获取（方案 A）：
 *   工具运行时调 Agent 后端配置接口获取"知识库查询链接"（界面配置存数据库，
 *   天然适配镜像部署），接口契约：
 *     GET {AGENT_CONFIG_BASE_URL}/api/agent-config/kb-link
 *     → 200 { "link": "http://<host>:8091/ext-query/<configId>?token=<token>" }
 *     → 未配置 404 { "detail": "未配置知识库查询链接" }
 *   AGENT_CONFIG_BASE_URL 环境变量可选（默认 http://127.0.0.1:3000），
 *   或进程内直接提供 process.env.KB_QUERY_LINK（两者都无则报错提示）。
 */

const DEFAULT_CONFIG_BASE = "http://127.0.0.1:3000";
const TIMEOUT_MS = 30_000;
const ANSWER_MAX_LEN = 6000;
const SNIPPET_MAX_LEN = 2000;

/** 从 Agent 配置获取知识库查询链接 */
async function fetchKbLink() {
  // 1) 环境变量直接提供（Agent spawn MCP 时注入，可选）
  if (process.env.KB_QUERY_LINK) return process.env.KB_QUERY_LINK;
  // 2) 方案 A：调 Agent 后端配置接口
  const base = process.env.AGENT_CONFIG_BASE_URL || DEFAULT_CONFIG_BASE;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const resp = await fetch(`${base.replace(/\/$/, "")}/api/agent-config/kb-link`, {
      signal: ctrl.signal,
    });
    if (resp.status === 404) {
      throw new Error("未配置知识库查询链接，请在设置中填写");
    }
    if (!resp.ok) {
      throw new Error(`配置接口错误 (${resp.status})`);
    }
    const data = await resp.json();
    if (!data?.link) throw new Error("配置接口返回缺少 link 字段");
    return data.link;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("读取知识库配置超时");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/** 解析查询链接 → { origin, configId, token } */
export function parseKbLink(link) {
  const url = new URL(link);
  const seg = url.pathname.split("/").filter(Boolean); // ["ext-query", "<configId>"]
  if (seg[0] !== "ext-query" || !seg[1]) {
    throw new Error("知识库查询链接格式不正确（应为 /ext-query/<配置ID>?token=xxx）");
  }
  const token = url.searchParams.get("token");
  if (!token) throw new Error("知识库查询链接缺少 token 参数");
  return { origin: url.origin, configId: seg[1], token };
}

/** 调知识库同步查询接口，返回 { answer, sources } */
export async function queryKnowledgeBase(link, query, topK) {
  const { origin, configId, token } = parseKbLink(link);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const resp = await fetch(`${origin}/api/ext/${configId}/query`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ query, top_k: topK }),
      signal: ctrl.signal,
    });
    if (resp.status === 401) {
      throw new Error("知识库查询链接无效或已失效（请重新获取）");
    }
    if (resp.status === 429) {
      throw new Error("知识库查询过于频繁，请稍后再试");
    }
    if (!resp.ok) {
      const detail = await resp.text().catch(() => "");
      throw new Error(`知识库查询失败 (${resp.status})${detail ? `: ${detail}` : ""}`);
    }
    return await resp.json();
  } catch (e) {
    if (e.name === "AbortError") throw new Error("知识库查询超时（30 秒）");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/** 格式化输出：回答 + 引用来源 */
function formatResult(answer, sources) {
  let text = answer || "";
  if (text.length > ANSWER_MAX_LEN) text = `${text.slice(0, ANSWER_MAX_LEN)}…（已截断）`;
  if (sources?.length) {
    text += `\n\n引用来源：\n`;
    sources.forEach((s, i) => {
      const name = s.document_name || "未知文档";
      const snip = (s.text || "").slice(0, SNIPPET_MAX_LEN);
      text += `${i + 1}. ${name}\n   ${snip}\n`;
    });
  }
  return text;
}

/**
 * 在 MCP 服务上注册 kb_query 工具
 *
 * 查询企业知识库，返回基于知识库内容的回答与引用来源
 */
export function registerKbQuery(server) {
  server.tool(
    "kb_query",
    "查询企业知识库，返回基于知识库内容的回答和引用来源",
    {
      query: z.string().describe("查询问题"),
      top_k: z
        .number()
        .int()
        .min(1)
        .max(10)
        .default(5)
        .describe("返回引用数量，最多 10 条"),
    },
    async ({ query, top_k }) => {
      try {
        const link = await fetchKbLink();
        const data = await queryKnowledgeBase(link, query, top_k);
        return {
          content: [{ type: "text", text: formatResult(data.answer, data.sources) }],
        };
      } catch (error) {
        return {
          content: [{ type: "text", text: `知识库查询失败: ${error.message}` }],
          isError: true,
        };
      }
    },
  );
}
