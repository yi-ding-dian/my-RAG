/**
 * 企业知识库外部查询客户端核心逻辑（可独立测试，不依赖 MCP SDK）
 *
 * 职责：
 * - 从环境变量读取配置（KB_EXT_URL / KB_EXT_ID / KB_EXT_TOKEN）
 * - 调用知识库同步问答接口 POST /api/ext/{configId}/query（Bearer token 鉴权），
 *   超时 30s（Node 18+ 原生 fetch，不依赖 axios）
 * - 错误映射为可读中文（401=链接无效/已失效、429=过于频繁、网络错误降级提示）
 * - 结果格式化：回答正文 + 引用编号 [n] + 来源文档名/片段，总长度 ≤6000 字符
 */
import process from "node:process";

const REQUEST_TIMEOUT_MS = 30_000; // 请求超时 30s
const MAX_ANSWER_CHARS = 6000; // 返回文本总长度上限（对齐知识库 6000 字规范）
const MAX_SOURCE_CHARS = 2000; // 单条引用片段长度上限

/** 读取环境变量配置 */
export function getKbConfig() {
  return {
    url: (process.env.KB_EXT_URL || "http://localhost:8091").replace(/\/+$/, ""),
    configId: process.env.KB_EXT_ID || "",
    token: process.env.KB_EXT_TOKEN || "",
  };
}

/** fetch 包装：30s 超时（AbortController）；网络错误/超时统一降级提示 */
async function fetchWithTimeout(url, options) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (err) {
    throw new Error("知识库服务不可达（网络错误或超时），请检查 KB_EXT_URL 或稍后再试");
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 同步查询企业知识库（基于知识库内容回答 + 引用来源）
 *
 * @param {object} params
 * @param {string} params.url      知识库服务地址（如 http://localhost:8091）
 * @param {string} params.configId 外部查询配置 id（管理端创建后获得）
 * @param {string} params.token    外部查询访问 token
 * @param {string} params.query    查询问题
 * @param {number} [params.top_k]  返回引用条数（1-10，默认 5）
 * @returns {Promise<{answer: string, sources: Array}>}
 *   answer: 基于知识库的回答文本（≤6000 字符）
 *   sources: [{document_name, text, image_urls}] 引用来源列表
 * @throws {Error} 配置缺失 / 鉴权失败 / 限流 / 业务错误（消息可直接展示给用户）
 */
export async function queryKnowledgeBase({ url, configId, token, query, top_k = 5 }) {
  if (!configId || !token) {
    throw new Error(
      "知识库「外部查询」未配置：请在知识库管理端创建外部查询配置，并设置环境变量 KB_EXT_ID / KB_EXT_TOKEN",
    );
  }
  const res = await fetchWithTimeout(`${url}/api/ext/${configId}/query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ query, top_k }),
  });

  if (res.status === 401) {
    throw new Error(
      "链接无效或已失效：请检查 KB_EXT_ID / KB_EXT_TOKEN 是否与知识库管理端「外部查询」配置一致（配置被停用/删除/重置 token 都会失效）",
    );
  }
  if (res.status === 429) {
    throw new Error("查询过于频繁：知识库外部查询每分钟有次数限制，请稍后再试");
  }
  if (!res.ok) {
    let detail = "";
    try {
      detail = JSON.parse(await res.text()).detail || "";
    } catch {
      /* 忽略响应体解析失败 */
    }
    throw new Error(`知识库查询失败 (${res.status})${detail ? `: ${detail}` : ""}`);
  }

  const data = await res.json();
  return {
    answer: typeof data.answer === "string" ? data.answer.trim() : "",
    sources: Array.isArray(data.sources) ? data.sources : [],
  };
}

/**
 * 格式化查询结果：回答正文 + 引用编号 [n] + 来源文档名/片段
 * 总长度 ≤6000 字符，超出自动截断并注明
 */
export function formatResult({ answer, sources }) {
  const lines = [];
  if (answer) {
    lines.push(answer);
  }
  if (Array.isArray(sources) && sources.length > 0) {
    lines.push("", "引用来源：");
    sources.forEach((s, i) => {
      const doc = s.document_name || "未知文档";
      const text = typeof s.text === "string" ? s.text.trim().slice(0, MAX_SOURCE_CHARS) : "";
      lines.push(`[${i + 1}] ${doc}：${text}`);
    });
  }
  let result = lines.join("\n").trim();
  const TRUNC_TIP = "……（结果过长，已截断）";
  if (result.length > MAX_ANSWER_CHARS) {
    result = result.slice(0, MAX_ANSWER_CHARS - TRUNC_TIP.length) + TRUNC_TIP;
  }
  return result;
}
