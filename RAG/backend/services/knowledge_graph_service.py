"""知识图谱服务：入库时用 LLM 为每个切块抽取实体-关系，合并构建知识图谱

MVP 范围（方案已调研确认）：
- 构建：入库链路内同步构建（parser_config.knowledge_graph 开关，默认关），
  LLM（激活配置；parser_config.parse_llm_model 指定时改用该模型——从激活档案
  模型列表查完整配置覆盖，仅影响抽取，对话仍用激活模型；查不到/未指定回退
  激活模型）抽取实体-关系，强制 JSON 输出；存储为 JSON 文件
  data/storage/graphs/{kb_id}.json（无外部图数据库依赖）
- 不包含（下一轮）：检索增强（用图谱增强检索）、力导向图可视化、社区检测
- 数据模型：
    {
      "kb_id": "...", "updated_at": "...",
      "docs": {"{doc_id}": {"name": "...", "chunk_count": 58}},
      "entities": [{"id":"e1","name":"接地线","type":"设备","description":"…",
                    "count":12,
                    "chunk_refs":[{"doc_id":"...","chunk_index":5,
                                   "char_start":123,"char_end":140}]}],
      "relations": [{"id":"r1","source":"e1","target":"e5","type":"配置",
                     "description":"…","weight":1.0,"chunk_refs":[...]}]
    }
- 实体挂 chunks：chunk_index + char_start/char_end（相对文档解析全文，
  与 chunks_meta 偏移契约一致；用实体名在块内首次出现位置定位，
  定位失败回退整块区间）
- 幂等：文档重入库时先删该 doc 的实体/关系引用再合并；实体按 name+type
  规范化合并（count 累加、chunk_refs 追加去重、描述合并截断 200 字）；
  关系按 source+target+type 合并（weight 累加）
- 名称规范化：trim、全半角统一、数字间空格压缩
- 抽取失败/超时跳过该块，绝不阻塞入库（与 contextual_retriever 同策略）

LLM 客户端模式参照 contextual_retriever：独立 AsyncOpenAI（key 比对自动
重建）、并发限流 3（asyncio.Semaphore）、单调用超时 15s。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from openai import AsyncOpenAI

from backend.config import LLMConfig, STORAGE_DIR, get_active_config
from backend.models.rag_models import Source
from backend.services.settings_service import llm_cfg_for_parser
from backend.services.chat_service import _llm_to_dict
from backend.services.llm_client import (LLMRequestError, LLMTimeoutError,
                                         get_llm_client, llm_completion)
from backend.services.thinking_strategy import (
    build_thinking_extra_body, get_thinking_strategy,
)  # noqa: F401  （build_thinking_extra_body re-export：对外引用兼容，见模块注释）

logger = logging.getLogger(__name__)

# 图谱文件目录（data/storage/graphs/{kb_id}.json，目录自动创建）
GRAPH_DIR = STORAGE_DIR / "graphs"
# 块文本输入截断
_CHUNK_INPUT_CHARS = 1000
# 单次调用 max_tokens：实体-关系抽取输出比摘要长（可能 10+ 实体 + 若干关系），
# 推理模型（DeepSeek/Qwen3 等带 thinking）reasoning 部分会大量消耗 token——
# 真实测试（deepseek-v4-flash）：2048 时 23 块仅 6 块成功（reasoning 吃光
# → content 空/超时）；4096 时 11 块成功但仍有个别块 content 被截断
# （reasoning 超长 5-8K）；8192 顶到常见 API 上限给足 reasoning + 输出余量。
# 失败/超时仍跳过对应块、绝不阻塞入库
_MAX_TOKENS = 8192
# 并发调用上限（asyncio.Semaphore）
_CONCURRENCY = 3
# 单次 LLM 调用超时（秒）：实体-关系抽取比摘要重得多——推理模型
# （DeepSeek/Qwen3 带 thinking）单块 reasoning 实测 >15s（15s 超时下
# 真实测试 23 块仅 6 块成功，超时块占 12 块）；60s 给足推理余量，
# 失败/超时仍跳过对应块、绝不阻塞入库
_TIMEOUT = 60.0
# 实体/关系描述合并后长度上限（字符，防无限膨胀）
_DESC_MAX_CHARS = 200

# ---- 类型白名单（测试文档是 AI 发展史综述，按通用集设计，代码里可配置默认值）----
_VALID_ENTITY_TYPES = ("人物", "机构", "技术", "概念", "事件", "成果")
_VALID_RELATION_TYPES = ("提出", "开发", "发明", "启动", "导致", "影响", "属于", "相关")

# ---- 思考关闭策略 ----
# 按模型服务商/部署方式选择"关闭思考"的实现（模块化，见 thinking_strategy）：
# - 在线 DeepSeek 等 → ExtraBodyStrategy（extra_body 传 thinking
#   disabled/enabled + reasoning_effort，原 build_thinking_extra_body 逻辑）
# - 本地 LM Studio Qwen（内网 base_url）→ disabled 时 QwenPrefillStrategy
#   （messages 末尾注入空 <think> 块 + continue_assistant_turn 跳过思考，
#   LM Studio 0.4.x 忽略 extra_body 的 thinking 参数）；enabled* 时不注入
#   （本地无法控制思考强度，保持模型默认思考）
# - llm_cfg 无法判断 → NoopStrategy（不改请求兜底）
# build_thinking_extra_body 由 thinking_strategy 模块提供，此处 re-export
# 保持对外引用兼容（contextual_retriever/测试从本模块导入）

# 全角 → 半角映射（ASCII 可见字符 0xFF01~0xFF5E → 0x21~0x7E，含全角空格 　）
_FULL_TO_HALF = str.maketrans(
    "".join(chr(0xFF01 + i) for i in range(94)) + "　",
    "".join(chr(0x21 + i) for i in range(94)) + " ")
# 数字间空格压缩（"1 2"→"12"；"15 亿"这类数字后跟单位空格保留）
_DIGIT_SPACE_RE = re.compile(r"(?<=\d)\s+(?=\d)")
# ```json 代码块提取（兜底解析策略 1）
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.S)

# 注意：本 prompt 经 .format(chunk=...) 格式化，JSON 示例中的花括号必须
# 转义为 {{ }}（否则 format 会把 {"entities" 当作字段名 → KeyError）
_EXTRACT_PROMPT = (
    "你是知识图谱抽取助手。从下面的文本片段中抽取实体与实体间的关系。\n"
    "【实体类型白名单】只能从：人物、机构、技术、概念、事件、成果 中选择。\n"
    "【关系类型白名单】只能从：提出、开发、发明、启动、导致、影响、属于、相关 中选择。\n"
    "【规则】\n"
    "1. 仅基于文本显式内容抽取，禁止推断或编造；\n"
    "2. 实体名称保留文本中的完整表述（如“艾伦·图灵”“达特茅斯会议”）；\n"
    "3. 关系的 source/target 必须引用本片段中抽取出的实体名称，方向符合语义；\n"
    "4. 描述一句话即可（30 字以内）；\n"
    "5. 只输出 JSON，不要任何多余文字、解释或代码块标记。\n"
    "【输出格式】\n"
    '{{"entities":[{{"name":"实体名","type":"实体类型","description":"描述"}}],\n'
    ' "relations":[{{"source":"实体名","target":"实体名","type":"关系类型","description":"描述"}}]}}\n'
    '若片段没有可抽取内容，输出 {{"entities": [], "relations": []}}。\n\n'
    "文本片段：\n{chunk}"
)


# ==================== 名称规范化（纯函数） ====================

def normalize_name(name) -> str:
    """名称规范化：trim、全半角统一（全角字母数字/标点转半角）、数字间空格压缩

    - "　艾伦·图灵　" → "艾伦·图灵"（全角空格转半角 + trim）
    - "ＡＩ" → "AI"（全角字母转半角）
    - "15 亿参数" → "15 亿参数"（数字与单位间空格保留）；"1 9 4 3" → "1943"
    """
    s = str(name or "").strip()
    s = s.translate(_FULL_TO_HALF)
    # 数字间空格压缩：去掉两个数字之间的空格（"1 9 4 3"→"1943"）
    s = _DIGIT_SPACE_RE.sub("", s)
    return s.strip()


# ==================== 抽取响应解析（纯函数，多策略兜底） ====================

def _try_json_loads(text: str) -> Optional[dict]:
    """尝试把文本直接解析为 JSON dict（失败返回 None）"""
    try:
        obj = json.loads(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _try_balanced_from(text: str, start: int) -> Optional[dict]:
    """从 text[start]（'}' 处）开始做平衡括号匹配，返回完整 JSON 对象或 None"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _try_json_loads(text[start:i + 1])
    return None


def _try_balanced_json(text: str) -> Optional[dict]:
    """从文本中提取平衡的 JSON 对象（跳过 ```json 等前后缀/尾部文字）

    从左到右依次尝试每个 '{' 作为起点，用状态机（字符串内引号/转义感知）
    匹配配对的 '}'，提取子串后 json.loads——前缀文字里恰好含 '{'（如
    "结果如下：{"）时首个候选失败，继续尝试后续起点；全部失败返回 None。
    """
    start = 0
    while True:
        start = text.find("{", start)
        if start < 0:
            return None
        obj = _try_balanced_from(text, start)
        if obj is not None:
            return obj
        start += 1


def _validate_extraction(obj) -> Optional[dict]:
    """校验并规范化抽取结果结构：entities/relations 列表 + 字段白名单

    - 实体：name/type 必填且 type 在白名单；同名（规范化后）去重；
      description 仅 trim 截断（保留原文表述，不做全半角转换）
    - 关系：source/target 必须是本次抽取的实体名（规范化后比对）、
      type 在白名单；自环丢弃
    - entities 为空 → 返回空结构（合法空结果，如引言/结语块无可抽取内容，
      调用方跳过但不算解析失败）
    - 返回 {"entities": [...], "relations": [...]}（实体名已规范化）
    """
    if not isinstance(obj, dict):
        return None
    entities, relations = obj.get("entities"), obj.get("relations")
    if not isinstance(entities, list) or not isinstance(relations, list):
        return None
    out_entities: List[dict] = []
    name_map: Dict[str, dict] = {}
    for e in entities:
        if not isinstance(e, dict):
            continue
        name = normalize_name(e.get("name"))
        etype = str(e.get("type") or "").strip()
        if not name or not etype or etype not in _VALID_ENTITY_TYPES:
            continue
        if name in name_map:  # 同块重复实体去重（保留首条）
            continue
        desc = str(e.get("description") or "").strip()
        if len(desc) > _DESC_MAX_CHARS:
            desc = desc[:_DESC_MAX_CHARS]
        item = {"name": name, "type": etype, "description": desc}
        out_entities.append(item)
        name_map[name] = item
    out_relations: List[dict] = []
    for r in relations:
        if not isinstance(r, dict):
            continue
        source = normalize_name(r.get("source"))
        target = normalize_name(r.get("target"))
        rtype = str(r.get("type") or "").strip()
        if (not source or not target or source not in name_map
                or target not in name_map):
            continue  # 关系两端必须是本次抽取出的实体
        if rtype not in _VALID_RELATION_TYPES:
            continue
        if source == target:
            continue  # 自环无意义，丢弃
        desc = str(r.get("description") or "").strip()
        if len(desc) > _DESC_MAX_CHARS:
            desc = desc[:_DESC_MAX_CHARS]
        out_relations.append({
            "source": source, "target": target, "type": rtype,
            "description": desc})
    return {"entities": out_entities, "relations": out_relations}


def parse_extraction_response(content: str) -> Optional[dict]:
    """LLM 抽取响应的 JSON 兜底解析（多策略，全部失败返回 None）

    策略依次：
    1. 提取 ```json ... ``` 代码块内容解析（模型喜欢包代码块时兜底）；
    2. 全文直接 json.loads；
    3. 正则平衡括号：提取第一个完整 JSON 对象（尾部有多余文字时兜底）。
    任一策略产出通过 _validate_extraction 结构校验即返回。
    """
    if not content or not content.strip():
        return None
    candidates: List[str] = []
    for m in _JSON_BLOCK_RE.finditer(content):
        candidates.append(m.group(1).strip())
    candidates.append(content)
    for text in candidates:
        obj = _try_json_loads(text)
        if obj is not None:
            parsed = _validate_extraction(obj)
            if parsed is not None:
                return parsed
    obj = _try_balanced_json(content)
    if obj is not None:
        return _validate_extraction(obj)
    return None


# ==================== 图谱读写（JSON 文件，目录自动创建） ====================

def _empty_graph(kb_id: str) -> dict:
    return {
        "kb_id": kb_id,
        "updated_at": "",
        "docs": {},
        "entities": [],
        "relations": [],
    }


def graph_path(kb_id: str) -> Path:
    """图谱文件路径 data/storage/graphs/{kb_id}.json"""
    return GRAPH_DIR / f"{kb_id}.json"


def load_graph(kb_id: str) -> dict:
    """读取图谱文件；不存在/损坏返回空结构（不抛异常，幂等友好）"""
    path = graph_path(kb_id)
    graph = _empty_graph(kb_id)
    if not path.exists():
        return graph
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            graph["kb_id"] = data.get("kb_id", kb_id)
            graph["updated_at"] = data.get("updated_at", "")
            graph["docs"] = data.get("docs") if isinstance(data.get("docs"), dict) else {}
            graph["entities"] = (data.get("entities")
                                 if isinstance(data.get("entities"), list) else [])
            graph["relations"] = (data.get("relations")
                                  if isinstance(data.get("relations"), list) else [])
    except Exception as e:
        logger.warning("知识图谱文件损坏，按空图谱处理: %s err=%s",
                       path.name, str(e)[:150])
    return graph


def save_graph(kb_id: str, graph: dict) -> Path:
    """图谱落盘（目录自动创建）；返回文件路径"""
    path = graph_path(kb_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kb_id": kb_id,
        "updated_at": graph.get("updated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "docs": graph.get("docs") or {},
        "entities": graph.get("entities") or [],
        "relations": graph.get("relations") or [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


# ==================== 图合并 / 幂等（纯函数） ====================

def _append_ref(refs: List[dict], ref: dict) -> bool:
    """引用追加去重：同一 doc_id+chunk_index 只保留最新一条（覆盖式）

    返回是否新增（True=新引用；False=覆盖了旧引用）
    """
    for i, r in enumerate(refs):
        if r.get("doc_id") == ref.get("doc_id") \
                and r.get("chunk_index") == ref.get("chunk_index"):
            refs[i] = ref
            return False
    refs.append(ref)
    return True


def _merge_desc(old: str, new: str) -> str:
    """描述合并：已有时拼接（"；"分隔），总长截断 _DESC_MAX_CHARS"""
    if not old:
        return new
    if not new or new in old:
        return old
    merged = f"{old}；{new}"
    return merged[:_DESC_MAX_CHARS]


def _next_id(items: List[dict], prefix: str) -> str:
    """分配递增 id（e1/e2... r1/r2...）：扫描现有 id 取最大序号 + 1（删除后不撞车）"""
    max_no = 0
    for it in items:
        iid = str(it.get("id") or "")
        if iid.startswith(prefix):
            try:
                max_no = max(max_no, int(iid[len(prefix):]))
            except ValueError:
                pass
    return f"{prefix}{max_no + 1}"


def remove_doc_refs(graph: dict, doc_id: str) -> int:
    """清除某文档在图谱中的所有实体/关系引用（文档重入库幂等第一步）

    - 实体：过滤掉该 doc 的 chunk_refs，count 重算为引用块数；无引用删除
    - 关系：同上（weight 重算）；source/target 实体已不存在的删除
    - 返回清除的引用条数
    """
    removed = 0
    kept_entities: List[dict] = []
    for e in graph.get("entities", []):
        refs = [r for r in e.get("chunk_refs", []) if r.get("doc_id") != doc_id]
        removed += len(e.get("chunk_refs", [])) - len(refs)
        if refs:
            e["chunk_refs"] = refs
            e["count"] = len(refs)
            kept_entities.append(e)
    graph["entities"] = kept_entities
    kept_ids = {e["id"] for e in kept_entities}
    kept_relations: List[dict] = []
    for r in graph.get("relations", []):
        if r.get("source") not in kept_ids or r.get("target") not in kept_ids:
            removed += len(r.get("chunk_refs", []))
            continue
        refs = [x for x in r.get("chunk_refs", []) if x.get("doc_id") != doc_id]
        removed += len(r.get("chunk_refs", [])) - len(refs)
        if refs:
            r["chunk_refs"] = refs
            r["weight"] = float(len(refs))
            kept_relations.append(r)
    graph["relations"] = kept_relations
    return removed


def merge_into_graph(graph: dict, doc_id: str, chunk_index: int,
                     chunk_text: str, char_start: int, char_end: int,
                     extraction: dict) -> None:
    """把一次抽取结果合并进图谱（纯合并，不清旧引用——文档级幂等清除
    由调用方在整篇合并前执行一次 remove_doc_refs）

    - extraction: parse_extraction_response 产物（实体/关系名已规范化）
    - 实体按 name+type 规范化合并：count=引用块数、chunk_refs 追加去重、
      描述合并截断；关系按 source+target+type 合并：weight=引用块数
    - 实体引用区间：实体名在块内首次出现位置定位（相对全文偏移，
      char_start 起点 + 块内偏移），定位失败回退整块区间
    """
    ref = {"doc_id": doc_id, "chunk_index": chunk_index,
           "char_start": char_start, "char_end": char_end}

    # 实体合并（name+type 匹配，id 保持稳定）
    key_map: Dict[tuple, dict] = {}
    for e in graph.get("entities", []):
        key_map[(e.get("name"), e.get("type"))] = e
    for ent in extraction.get("entities", []):
        key = (ent["name"], ent["type"])
        existing = key_map.get(key)
        if existing:
            s, epos = _locate_ref(chunk_text, ent["name"], char_start, char_end)
            _append_ref(existing["chunk_refs"], {**ref, "char_start": s, "char_end": epos})
            existing["count"] = len(existing["chunk_refs"])
            existing["description"] = _merge_desc(existing["description"], ent["description"])
        else:
            s, epos = _locate_ref(chunk_text, ent["name"], char_start, char_end)
            eid = _next_id(graph["entities"], "e")
            item = {"id": eid, "name": ent["name"], "type": ent["type"],
                    "description": ent["description"], "count": 1,
                    "chunk_refs": [{**ref, "char_start": s, "char_end": epos}]}
            graph["entities"].append(item)
            key_map[key] = item

    # 关系合并：source/target 名称 → 实体 id（同名多类型取首个）
    name_to_id: Dict[str, str] = {}
    for e in graph.get("entities", []):
        if e.get("name") not in name_to_id:
            name_to_id[e["name"]] = e["id"]
    rel_key_map: Dict[tuple, dict] = {}
    for r in graph.get("relations", []):
        rel_key_map[(r.get("source"), r.get("target"), r.get("type"))] = r
    for rel in extraction.get("relations", []):
        s_id = name_to_id.get(rel["source"])
        t_id = name_to_id.get(rel["target"])
        if not s_id or not t_id or s_id == t_id:
            continue
        key = (s_id, t_id, rel["type"])
        existing = rel_key_map.get(key)
        if existing:
            _append_ref(existing["chunk_refs"], ref)
            existing["weight"] = float(len(existing["chunk_refs"]))
            existing["description"] = _merge_desc(existing["description"], rel["description"])
        else:
            rid = _next_id(graph["relations"], "r")
            item = {"id": rid, "source": s_id, "target": t_id,
                    "type": rel["type"], "description": rel["description"],
                    "weight": 1.0, "chunk_refs": [ref]}
            graph["relations"].append(item)
            rel_key_map[key] = item


def _locate_ref(chunk_text: str, entity_name: str,
                char_start: int, char_end: int) -> tuple:
    """实体在块内的位置 → 全文偏移区间；找不到回退整块区间"""
    pos = chunk_text.find(entity_name)
    if pos >= 0:
        return char_start + pos, char_start + pos + len(entity_name)
    return char_start, char_end


# ==================== LLM 抽取 ====================

# 独立 LLM 客户端（key 比对自动重建：配置变化即重建，无需重启；实现统一
# 在 llm_client.get_llm_client，缓存为模块级 key→client 字典）


def _get_client(llm_cfg: Optional[dict] = None) -> AsyncOpenAI:
    """按 LLM 配置 key 比对自动重建客户端（委托统一工厂 get_llm_client）

    保留模块级函数名（test_parse_llm_model 等测试 monkeypatch 依赖）。
    """
    return get_llm_client(llm_cfg)


async def _extract_one(sem: asyncio.Semaphore, index: int, chunk_text: str,
                       llm_cfg: dict, strategy=None,
                       timeout: float = _TIMEOUT) -> Optional[dict]:
    """单个块的实体-关系抽取：成功返回 {index, extraction}；失败/超时/空 → None

    strategy: 思考关闭策略（thinking_strategy 模块产物，见上）——apply(payload)
    统一处理 extra_body（在线 API）与 messages prefill 注入（本地 Qwen）
    """
    async with sem:
        try:
            # 消费处类型化：dict → LLMConfig（扩展字段忽略）；_get_client
            # 调用点仍传原 dict（测试 recorder 断言 dict 结构兼容）
            cfg = LLMConfig.from_dict(llm_cfg)
            client = _get_client(llm_cfg)
            payload = {
                "messages": [
                    {"role": "system", "content": "你是知识图谱抽取助手。"},
                    {"role": "user", "content": _EXTRACT_PROMPT.format(
                        chunk=chunk_text)},
                ],
            }
            if strategy is not None:
                strategy.apply(payload)
            # 统一调用包装：超时 → LLMTimeoutError；调用失败 → LLMRequestError
            resp = await llm_completion(
                client, model=cfg.model, messages=payload["messages"],
                max_tokens=_MAX_TOKENS, temperature=0.1,
                extra_body=payload.get("extra_body"), timeout=timeout,
            )
            try:
                content = (resp.choices[0].message.content or "").strip()
            except Exception:
                content = ""
            if not content:
                logger.warning("知识图谱抽取响应为空，跳过: chunk#%d", index)
                return None
            extraction = parse_extraction_response(content)
            if extraction is None:
                # 失败诊断：记录响应开头片段（区分 reasoning 泄露/格式错乱/
                # 内容空等；推理模型思考过长吃光 max_tokens 时 content 为空）
                logger.warning(
                    "知识图谱抽取 JSON 解析失败，跳过: chunk#%d 响应开头: %r",
                    index, content[:200] if content else "")
                return None
            if not extraction.get("entities"):
                # 合法空结果（引言/结语等块无可抽取内容），跳过不算失败
                logger.info("知识图谱抽取为空结果，跳过: chunk#%d", index)
                return None
            return {"index": index, "extraction": extraction}
        except LLMTimeoutError:
            logger.warning("知识图谱抽取超时（>%.0fs），跳过: chunk#%d",
                           timeout, index)
            return None
        except LLMRequestError as e:
            # 可预期失败（网络/限流/HTTP 错误）：warning，单块失败跳过不阻塞
            logger.warning("知识图谱抽取失败，跳过: chunk#%d err=%s",
                           index, str(e)[:150])
            return None
        except Exception as e:
            # 兜底（未知异常）：同样跳过不阻塞，信息保留
            logger.warning("知识图谱抽取失败，跳过: chunk#%d err=%s",
                           index, str(e)[:150])
            return None


# 空统计（开关关/无块/LLM 未配置时返回）
_EMPTY_STATS = {"chunks": 0, "extracted": 0, "entities": 0, "relations": 0}


async def build_graph_for_doc(kb_id: str, doc_id: str, doc_name: str,
                              chunks, raw_texts: Optional[List[str]] = None,
                              cfg: Optional[dict] = None,
                              timeout: float = _TIMEOUT,
                              cancel_event: Optional[asyncio.Event] = None) -> dict:
    """为文档构建/合并知识图谱，返回统计 {chunks, extracted, entities, relations}

    - chunks: List[Chunk]（切块结果，取 .char_start/.char_end；文本取
      raw_texts[i] 若传——父标题前缀只用于展示，实体偏移以原文为准——
      否则取 .text，与 chunks_meta 偏移契约一致）
    - cfg: parser_config（knowledge_graph 开关；关/缺省直接返回空统计，
      不调用 LLM——与 ingestion 层判断双保险）
    - cancel_event（可选）：中断信号——抽取开始前/进行中置位 → 取消尚未
      开始的块调用（已发起的 LLM 请求随 task cancel 传播中断），不加载/
      不清除/不落盘本次结果，直接返回空统计（调用方区分"中断"与"失败"，
      入库链路不传 = 无取消行为）
    - 幂等：合并前清除该文档旧引用（重入库不产生重复实体/关系）
    - 失败/超时跳过对应块，绝不抛异常；保存失败仅 warning（不阻塞入库）
    """
    if not cfg or not cfg.get("knowledge_graph"):
        return dict(_EMPTY_STATS)
    if not chunks:
        return dict(_EMPTY_STATS)
    # 解析 LLM 模型：parser_config.parse_llm_model 指定（图谱抽取专用模型，
    # 从激活档案模型列表查完整配置）→ 覆盖；未指定/查不到 → 激活模型
    # （调用点保持 dict 传递：_get_client 的测试 recorder 断言 dict 结构）
    llm_cfg = _llm_to_dict(get_active_config().llm)
    override = llm_cfg_for_parser(cfg.get("parse_llm_model"))
    if override:
        llm_cfg = {**llm_cfg, **override}
    llm_cfg_obj = LLMConfig.from_dict(llm_cfg)
    if not (llm_cfg_obj.base_url and llm_cfg_obj.model):
        logger.warning("LLM 未配置（base_url/model 为空），跳过知识图谱构建")
        return dict(_EMPTY_STATS)
    # 思考关闭策略：按模型服务商/部署方式选择（在线 DeepSeek → extra_body
    # 关闭思考；本地 LM Studio Qwen → messages 末尾注入空 <think> 块跳过思考，
    # 见 thinking_strategy）
    strategy = get_thinking_strategy(llm_cfg, cfg.get("thinking_mode"))
    sem = asyncio.Semaphore(_CONCURRENCY)
    tasks = [
        asyncio.create_task(
            _extract_one(sem, i, (raw_texts[i] if raw_texts else c.text)[:_CHUNK_INPUT_CHARS],
                         llm_cfg, strategy, timeout=timeout))
        for i, c in enumerate(chunks)
    ]
    # 中断：抽取与取消信号竞争——信号先到 → 取消全部块任务（httpx/anyio
    # 等在途连接随 task.cancel 传播立即中断），不等待调用自然完成，本次
    # 不合并、不落盘，立即返回空统计
    if cancel_event is None:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    else:
        if cancel_event.is_set():
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=0.5)
            logger.info("知识图谱构建已中断: %s (%s)", doc_name, doc_id)
            return dict(_EMPTY_STATS)
        main_fut = asyncio.ensure_future(
            asyncio.gather(*tasks, return_exceptions=True))
        cancel_fut = asyncio.ensure_future(cancel_event.wait())
        done, _ = await asyncio.wait(
            {main_fut, cancel_fut}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_fut in done:
            # 取消传播有短暂窗口（真实客户端 1s 级）；不等待完成也安全
            # （残留调用结果丢弃，函数已返回）
            for t in tasks:
                t.cancel()
            await asyncio.wait({main_fut}, timeout=2)
            logger.info("知识图谱构建已中断: %s (%s)", doc_name, doc_id)
            return dict(_EMPTY_STATS)
        results = main_fut.result()
    # 防御：仅保留正常抽取结果（task.cancel 的 CancelledError 等异常对象过滤）
    results = [r for r in results if isinstance(r, dict)]
    # 中断兜底：信号与 gather 同时完成时丢弃结果（本次不合并、不落盘）
    if cancel_event and cancel_event.is_set():
        logger.info("知识图谱构建已中断: %s (%s)", doc_name, doc_id)
        return dict(_EMPTY_STATS)

    # 合并进图谱（文档级幂等：整篇合并前清一次该文档旧引用——旧图谱对应旧
    # 文本，重入库先清再合并不产生重复实体；注意必须在循环外执行一次，
    # 若在每块合并内清除则只有最后一块的实体被保留）
    graph = load_graph(kb_id)
    remove_doc_refs(graph, doc_id)
    graph["docs"][doc_id] = {"name": doc_name or doc_id, "chunk_count": len(chunks)}
    extracted = 0
    for r in results:
        if not r:
            continue
        extracted += 1
        chunk = chunks[r["index"]]
        merge_into_graph(
            graph, doc_id, r["index"],
            (raw_texts[r["index"]] if raw_texts else chunk.text),
            chunk.char_start, chunk.char_end, r["extraction"])
    graph["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        save_graph(kb_id, graph)
    except Exception as e:
        logger.warning("知识图谱落盘失败（不阻塞入库）: %s err=%s",
                       doc_id, str(e)[:150])
    return {
        "chunks": len(chunks),
        "extracted": extracted,
        "entities": len(graph.get("entities", [])),
        "relations": len(graph.get("relations", [])),
    }


# ==================== 图谱查询（GraphRAG 检索增强，Local Search 思路） ====================
# 查询链路：LLM 抽查询实体 → 图谱实体匹配（精确/包含）→ 1-hop 邻接扩展 →
# 组装"知识图谱"来源引用注入回答，与向量/BM25 检索并行；任何环节失败
# 静默降级（warning + 返回空/None），绝不阻塞查询。

# 查询实体抽取超时（秒）：查询链路延迟敏感，短超时快速失败返回 []，
# 绝不让图谱通道拖慢问答
_QUERY_TIMEOUT = 8.0
# 单次调用 max_tokens：输出是短 JSON 数组（最多 5 个实体名），
# thinking disabled 下无需 reasoning 余量，512 足够
_QUERY_MAX_TOKENS = 512

# 注意：本 prompt 经 .format(query=...) 格式化，JSON 示例中的方括号
# 不涉及 format 字段解析（仅花括号需转义），可直接书写
_QUERY_ENTITY_PROMPT = (
    "你是实体抽取助手。从下面的用户问题中抽取关键实体名称"
    "（人名、技术名词、概念、机构名等），用于在知识图谱中检索匹配。\n"
    "【规则】\n"
    "1. 只抽取问题中明确提到的实体，最多 5 个；\n"
    "2. 保留问题中的完整表述（如“艾伦·图灵”，不要简化为“图灵”）；\n"
    "3. 若问题中没有可抽取的实体，输出空数组。\n"
    "【输出格式】\n"
    '只输出 JSON 数组，如 ["实体1","实体2"]，不要任何多余文字、解释或代码块标记。\n\n'
    "问题：{query}"
)


def _try_balanced_array(text: str) -> Optional[list]:
    """从文本中提取第一个平衡的 JSON 数组（字符串/转义感知），失败返回 None

    与 _try_balanced_from 同款状态机，起点改为 '['、终止为匹配的 ']'，
    用于模型输出带 ```json 前缀/尾部多余文字时的兜底解析。
    """
    start = text.find("[")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except Exception:
                    return None
                return obj if isinstance(obj, list) else None
    return None


def parse_query_entities(content: str) -> List[str]:
    """LLM 查询实体响应的 JSON 数组解析（多策略，全部失败返回 []）

    - 策略：```json 代码块提取 → 直接 json.loads → 平衡数组提取（尾部文字兜底）
    - 校验：仅字符串项、trim、规范化去重、每项 ≤30 字、最多 5 个
    """
    if not content or not content.strip():
        return []
    candidates: List[str] = []
    for m in _JSON_BLOCK_RE.finditer(content):
        candidates.append(m.group(1).strip())
    candidates.append(content)
    seen: set = set()
    out: List[str] = []

    def _collect(arr) -> None:
        for item in arr:
            if not isinstance(item, str):
                continue
            name = normalize_name(item)
            if not name:
                continue
            if len(name) > 30:
                name = name[:30]
            if name not in seen:
                seen.add(name)
                out.append(name)

    for text in candidates:
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, list):
            _collect(obj)
            return out[:5]  # 首个成功解析即返回
    arr = _try_balanced_array(content)
    if arr is not None:
        _collect(arr)
    return out[:5]


async def extract_query_entities(query: str) -> List[str]:
    """LLM 从问题抽取实体名（GraphRAG 检索增强第一步）

    - 独立 LLM 客户端复用 _get_client（激活配置，key 变化自动重建）；
    - thinking disabled 加速（简单任务，关闭思考省时省 token）；
    - 短超时（_QUERY_TIMEOUT=8s）；LLM 未配置/超时/失败/空响应一律返回
      []（绝不抛异常、绝不阻塞查询——图谱通道失败静默降级）
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        llm_cfg = _llm_to_dict(get_active_config().llm)
        llm_cfg_obj = LLMConfig.from_dict(llm_cfg)
        if not (llm_cfg_obj.base_url and llm_cfg_obj.model):
            logger.warning("LLM 未配置（base_url/model 为空），跳过查询实体抽取")
            return []
        client = _get_client(llm_cfg)
        # 思考关闭策略：查询实体抽取固定关闭思考（短时延迟敏感任务）——
        # 在线 DeepSeek → extra_body disabled；本地 LM Studio Qwen → prefill
        strategy = get_thinking_strategy(llm_cfg, "disabled")
        payload = {
            "messages": [
                {"role": "system", "content": "你是实体抽取助手。"},
                {"role": "user",
                 "content": _QUERY_ENTITY_PROMPT.format(query=query)},
            ],
        }
        strategy.apply(payload)
        # 统一调用包装：超时 → LLMTimeoutError；调用失败 → LLMRequestError
        resp = await llm_completion(
            client, model=llm_cfg_obj.model, messages=payload["messages"],
            max_tokens=_QUERY_MAX_TOKENS, temperature=0.1,
            extra_body=payload.get("extra_body"), timeout=_QUERY_TIMEOUT,
        )
        try:
            content = (resp.choices[0].message.content or "").strip()
        except Exception:
            content = ""
        return parse_query_entities(content)
    except LLMTimeoutError:
        logger.warning("查询实体抽取超时（>%.0fs），图谱通道跳过", _QUERY_TIMEOUT)
        return []
    except LLMRequestError as e:
        # 可预期失败（网络/限流/HTTP 错误）：warning，图谱通道静默降级
        logger.warning("查询实体抽取失败，图谱通道跳过: %s", str(e)[:150])
        return []
    except Exception as e:
        # 兜底（未知异常）：同样静默降级，信息保留
        logger.warning("查询实体抽取失败，图谱通道跳过: %s", str(e)[:150])
        return []


def match_entities(graph: dict, query_entities: List[str]) -> List[dict]:
    """查询实体 → 图谱实体匹配（纯函数，名称规范化后精确/包含匹配）

    规则（控制误匹配）：
    - 精确：规范化后完全相等；
    - 包含（图谱名含查询名）：图谱实体名包含查询实体名，且查询实体名
      ≥2 字（"图灵"能匹配"艾伦·图灵"；单字如"图"不参与包含匹配，
      防过于宽泛的误匹配）；
    - 包含（查询名含图谱名）：查询实体名包含图谱实体名，且图谱实体名
      ≥2 字（问题带全称"图灵测试的提出者"时仍能命中短名实体"图灵"）；
    - 返回去重的匹配实体列表（保持图谱顺序）
    """
    qnames = [normalize_name(q) for q in query_entities]
    qnames = [q for q in qnames if q]
    if not qnames:
        return []
    matched: List[dict] = []
    seen = set()
    for e in graph.get("entities", []):
        name = normalize_name(e.get("name"))
        if not name:
            continue
        for q in qnames:
            hit = (q == name
                   or (len(q) >= 2 and name.find(q) >= 0)
                   or (len(name) >= 2 and q.find(name) >= 0))
            if hit:
                if e.get("id") not in seen:
                    seen.add(e.get("id"))
                    matched.append(e)
                break
    return matched


def expand_neighbors(graph: dict, matched_entities: List[dict],
                     hop: int = 1) -> dict:
    """邻接扩展：取匹配实体的邻居实体 + 连接关系 + 关联 chunk_refs

    - hop=1（当前支持范围）：任何关系一端是匹配实体 → 对端即邻居；
      关系只保留"实体集合内两端相连"的（匹配+邻居全图 1-hop 子图）
    - 返回 {"entities": [...], "relations": [...], "source_chunks": [...]}
      - entities：匹配实体 + 邻居实体（图谱顺序，去重）
      - relations：子图内关系（保留原字段：id/source/target/type/...）
      - source_chunks：实体/关系 chunk_refs 按 doc_id+chunk_index 去重，
        带 char_start/char_end 偏移（供引用溯源）
    """
    if not matched_entities:
        return {"entities": [], "relations": [], "source_chunks": []}
    entity_map: Dict[str, dict] = {}
    for e in graph.get("entities", []):
        if e.get("id") is not None:
            entity_map[e["id"]] = e
    matched_ids = {e.get("id") for e in matched_entities}
    node_ids = set(matched_ids)
    for r in graph.get("relations", []):
        s, t = r.get("source"), r.get("target")
        if s in matched_ids or t in matched_ids:
            if s in entity_map:
                node_ids.add(s)
            if t in entity_map:
                node_ids.add(t)
    entities = [entity_map[i] for i in node_ids if i in entity_map]
    relations = [r for r in graph.get("relations", [])
                 if r.get("source") in node_ids and r.get("target") in node_ids]
    chunks: Dict[tuple, dict] = {}
    for e in entities:
        for ref in e.get("chunk_refs", []) or []:
            if isinstance(ref, dict):
                chunks.setdefault((ref.get("doc_id"), ref.get("chunk_index")), ref)
    for r in relations:
        for ref in r.get("chunk_refs", []) or []:
            if isinstance(ref, dict):
                chunks.setdefault((ref.get("doc_id"), ref.get("chunk_index")), ref)
    source_chunks = sorted(
        chunks.values(),
        key=lambda x: (str(x.get("doc_id") or ""), int(x.get("chunk_index") or 0)))
    return {"entities": entities, "relations": relations,
            "source_chunks": source_chunks}


# 图谱上下文组装限制（防 context_text 膨胀——引用文本有 2000 字符截断）
_MAX_CTX_ENTITIES = 30
_MAX_CTX_RELATIONS = 40
_CTX_DESC_CHARS = 60


def build_graph_context(graph: dict, query_entities: List[str]) -> dict:
    """组装图谱上下文（纯函数）：匹配 → 1-hop 扩展 → CSV 文本组装

    文本形式（参照 KnowFlow KGSearch）：
      【知识图谱实体】
      {name}({type}|{description}|{count})
      【知识图谱关系】
      {source}|{type}|{target}|{description}
    - 实体/关系超限截断（30/40 条），描述截断 60 字
    - 返回 {"entities": [...], "relations": [...], "context_text": str,
      "source_chunks": [...]}；无匹配实体 → context_text="" 其余空结构
    """
    matched = match_entities(graph, query_entities)
    if not matched:
        return {"entities": [], "relations": [], "context_text": "",
                "source_chunks": []}
    expanded = expand_neighbors(graph, matched, hop=1)
    entities = expanded["entities"][:_MAX_CTX_ENTITIES]
    relations = expanded["relations"][:_MAX_CTX_RELATIONS]
    lines: List[str] = []
    if entities:
        lines.append("【知识图谱实体】")
        for e in entities:
            desc = str(e.get("description") or "").strip().replace("\n", " ")
            if len(desc) > _CTX_DESC_CHARS:
                desc = desc[:_CTX_DESC_CHARS] + "…"
            lines.append(f"{e.get('name')}({e.get('type')}|{desc}|{e.get('count', 0)})")
    if relations:
        lines.append("【知识图谱关系】")
        name_of = {e.get("id"): e.get("name")
                   for e in expanded["entities"]}  # 全量实体映射，防截断丢名
        for r in relations:
            s_name = name_of.get(r.get("source"), r.get("source"))
            t_name = name_of.get(r.get("target"), r.get("target"))
            desc = str(r.get("description") or "").strip().replace("\n", " ")
            if len(desc) > _CTX_DESC_CHARS:
                desc = desc[:_CTX_DESC_CHARS] + "…"
            lines.append(f"{s_name}|{r.get('type')}|{t_name}|{desc}")
    return {
        "entities": entities,
        "relations": relations,
        "context_text": "\n".join(lines),
        "source_chunks": expanded["source_chunks"],
    }


async def build_kg_source(kb_id: str, query: str,
                          enabled: bool = True) -> Optional[Source]:
    """图谱检索增强通道：抽实体 → 匹配/扩展 → 组装"知识图谱"引用

    - enabled=False（开关关）→ None；无图谱文件/无实体 → None（零成本跳过）
    - 无匹配实体 → None（查询完全照旧）；LLM 抽实体失败/超时 → [] → None
    - 任何异常 → warning 不抛出（绝不阻塞查询）
    - 返回 Source：document_name="知识图谱"、text=context_text（独立引用
      条目，与普通检索引用并列；score=0 表示图谱上下文无相似度评分，
      不参与 rerank——rerank 只处理 retrieval_service 内的普通候选）
    """
    if not enabled:
        return None
    try:
        graph = load_graph(kb_id)
        if not graph.get("entities"):
            return None  # 无图谱（未构建/空图谱）自动跳过
        query_entities = await extract_query_entities(query)
        if not query_entities:
            return None
        ctx = build_graph_context(graph, query_entities)
        if not ctx["context_text"]:
            return None
        logger.info("图谱增强命中: kb=%s query=%r 实体=%d 子图=%d 关系=%d",
                    kb_id, query[:30], len(query_entities),
                    len(ctx["entities"]), len(ctx["relations"]))
        return Source(
            id=f"kg:{kb_id}",
            text=ctx["context_text"],
            score=0.0,
            document_id="",
            document_name="知识图谱",
            kb_id=kb_id,
            chunk_index=-1,
            char_start=-1,
            char_end=-1,
        )
    except Exception as e:
        logger.warning("知识图谱增强失败（跳过，不影响查询）: %s", str(e)[:150])
        return None
