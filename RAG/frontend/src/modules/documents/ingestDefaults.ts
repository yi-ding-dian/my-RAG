/**
 * 入库参数默认值：**单一来源在后端**（GET /kbs/ingest-defaults）
 *
 * 此前这组数值（`800/100`、`512/50/1024/100`）在向导提交、批量智能、批量统一、
 * 失败兜底、解析配置弹窗里各抄了一份，且与后端活跃配置漂移——超管把 CHUNK_SIZE
 * 改成 1200 后，向导仍然发 800。现在只在这里拉一次、模块级缓存复用，组件按切块
 * 方式查表。
 *
 * 拉不到时的兜底是**不发参数**（后端会用活跃配置兜底），而不是在前端写死一份数值
 * ——写死就又制造了第二份默认值。
 */
import { useEffect, useState } from 'react';

import { getIngestDefaults } from '../../shared/api/client';
import type {
  IngestConfig,
  IngestDefaults,
  ParseMethod,
} from '../../shared/api/types';

/** 模块级缓存：同一页面内多次使用只请求一次 */
let cache: IngestDefaults | null = null;
let pending: Promise<IngestDefaults> | null = null;

export function loadIngestDefaults(): Promise<IngestDefaults> {
  if (cache) return Promise.resolve(cache);
  if (!pending) {
    pending = getIngestDefaults()
      .then(res => {
        cache = res.data;
        pending = null;
        return cache;
      })
      .catch(e => {
        pending = null;   // 允许下次重试
        throw e;
      });
  }
  return pending;
}

/** 测试用：清空缓存（业务代码不要调） */
export function __clearIngestDefaultsCache() {
  cache = null;
  pending = null;
}

/**
 * 取默认值表（组件用）。拉取失败返回 null，调用方按"无参数"处理即可
 * ——此时 buildConfig 不产生任何切块参数，后端用自己的默认值兜底。
 */
export function useIngestDefaults(): IngestDefaults | null {
  const [data, setData] = useState<IngestDefaults | null>(cache);
  useEffect(() => {
    let alive = true;
    loadIngestDefaults()
      .then(d => { if (alive) setData(d); })
      .catch(() => { /* 兜底见 buildConfig：不发参数 */ });
    return () => { alive = false; };
  }, []);
  return data;
}

/**
 * 按切块方式组装入库配置：默认值（后端表） + 用户覆盖。
 *
 * @param defaults useIngestDefaults() 的返回值，null = 未拉到（不发参数）
 * @param method   用户选定的切块方式
 * @param overrides 用户显式设置的项（增强开关、regex_pattern、思考模式等）
 */
export function buildConfig(
  defaults: IngestDefaults | null,
  method: ParseMethod,
  overrides: Partial<IngestConfig> = {},
): IngestConfig {
  const base = defaults?.methods?.[method] ?? {};
  return { method, ...base, ...overrides };
}

/**
 * 走解析器的文档类型（与后端 ingestion.params 的 _PARSED_FILE_TYPES 对应）。
 *
 * 用于"是否展示解析相关选项"这类纯展示判断；**不再用于决定要不要把
 * parser_engine 抄进请求**——那是 plan.config 已经定好的事（前端零加工）。
 */
export function isParserLike(fileType: string): boolean {
  return ['pdf', 'docx', 'doc'].includes(
    (fileType || '').toLowerCase().replace(/^\./, ''));
}
