"""聊天服务：检索增强问答 + SSE 流式 + 会话落盘

- 检索 top_k=5、相似度阈值 similarity_threshold（配置可调，低于阈值过滤）
  -> system prompt 强制"只依据[引用]回答、句末[n]标注、无答案明说"
- AsyncOpenAI 流式（LM Studio / vLLM OpenAI 兼容），qwen3.6-35b-a3b-apex-quality
- 生成参数（chat 段配置优先，None 回退 LLM 段默认）：
  temperature/top_p/max_tokens 覆盖 LLM 配置
- 多轮开关 enable_multi_turn=False 时不带历史（只发 system + 当前问题）
- 无命中直接告知，不调用 LLM
- history 截最近 8 轮（配置可调）；会话落盘 data/chat/{session_id}.json 含 sources 快照
- 标题取问题前 20 字；客户端断开时优雅收尾（已生成文本仍落盘）
- 运行时读取 get_active_config()（阶段2 配置档案即时生效）
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

from openai import AsyncOpenAI

from backend.config import CHAT_DIR, get_active_config
from backend.models.rag_models import (ChatHistoryItem, ChatMessage,
                                       ChatSession, Source)
from backend.services.retrieval_service import (RetrievalUnavailableError,
                                                get_retrieval_service)
from backend.services.settings_service import (merge_chat_config,
                                               merge_department_llm)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = (
    "你是一个严谨的知识库问答助手，回答用户问题时必须严格遵循以下规则：\n"
    "1. 只依据下方 [引用] 中的内容回答，禁止编造引用之外的信息；\n"
    "2. 回答中如需引用某条 [引用] 内容，请在该句句末紧贴句尾标注对应编号 [n]"
    "（n 为引用序号，如 [1]，标在句末标点前）；编号必须与 [引用] 中的编号一致，"
    "不要自造或改写编号；仅对确实来自 [引用] 的内容标注，不确定是否来自引用的内容"
    "不要标注；标注意图是让用户快速定位来源，[引用] 中的每一条（含“知识图谱”条目）"
    "都可被标注；\n"
    "3. 如果 [引用] 中没有与问题相关的信息，请直接说明“未检索到相关内容”，不要猜测或编造；\n"
    "4. 使用简洁、准确的中文回答。\n\n"
    "{refs}"
)

# 自定义 system_prompt（无占位符）自动追加引用段时一并追加的行内标注规则：
# 自定义模板覆盖内置规则，不追加标注指令则模型无 [n] 标注依据（行内引用功能失效）。
# 标注仅添加编号不改变引用原文，与"原样输出"类模板语义兼容。
# 引用文本单条长度上限（进 prompt 的 [引用] 段，兼顾回答依据完整性与 token 成本：
# 默认 naive 切块 800 字基本不触发，仅标题/父块等大块生效；5 源全满上限时
# 约 3 万字 ≈ 2.2 万 token 输入，qwen 长上下文可容纳；前端面板展示不受此限
# ——meta 下发的 sources.text/parent_text 为完整文本，面板另有"展开全文"交互）
_REF_TEXT_MAX_LEN = 6000


_CITATION_RULE = (
    "标注规则：回答中如需引用 [引用] 中的内容，请在引用句句尾紧贴句号前"
    "标注对应编号 [n]（n 必须与 [引用] 中的编号一致，如\"……成为历史上"
    "用户增长最快的消费级应用[2]。\"），禁止自造或改写编号；仅对确实来自"
    " [引用] 的内容标注，不确定是否来自引用的内容不要标注；[引用] 含"
    "\"知识图谱\"条目时同样可标注；标注只添加编号，不改变引用原文内容。"
)


def sse_event(event: str, data: dict) -> str:
    """格式化 SSE 事件"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_LLM_CONFIG_KEYS = ("base_url", "api_key", "model", "temperature",
                    "max_tokens", "timeout")


def _llm_to_dict(llm_cfg) -> dict:
    """LLM 配置对象 → dict（merge_department_llm 入参；dict 直接透传）"""
    if isinstance(llm_cfg, dict):
        return {k: llm_cfg.get(k) for k in _LLM_CONFIG_KEYS}
    try:
        return llm_cfg.model_dump()  # pydantic v2 BaseModel
    except AttributeError:
        return {k: getattr(llm_cfg, k, None) for k in _LLM_CONFIG_KEYS}


class ChatService:

    def __init__(self):
        self._lock = threading.Lock()
        self._client: Optional[AsyncOpenAI] = None
        self._client_key: str | None = None

    # ---------- 客户端 ----------

    def _get_client(self, llm_cfg: Optional[dict] = None) -> AsyncOpenAI:
        """按 LLM 配置 key 比对自动重建：配置（地址/密钥/模型）变化即重建，无需重启

        llm_cfg：合并后的 LLM 配置 dict（base_url/api_key/model/timeout，
        merge_department_llm 输出）；None = 使用全局活跃配置。缓存 key 为
        合并配置的 JSON 序列化——部门配置变化（含 api_key）即重建独立 client。
        """
        if llm_cfg is None:
            llm_cfg = _llm_to_dict(get_active_config().llm)
        key = json.dumps(llm_cfg, sort_keys=True, ensure_ascii=False)
        if self._client is None or self._client_key != key:
            self._client = AsyncOpenAI(
                base_url=llm_cfg.get("base_url", ""),
                api_key=llm_cfg.get("api_key", ""),
                timeout=float(llm_cfg.get("timeout") or 60),
            )
            self._client_key = key
        return self._client

    # ---------- 会话持久化 ----------

    def _get_session_path(self, session_id: str) -> Path:
        return CHAT_DIR / f"{session_id}.json"

    def _save_session(self, session: ChatSession):
        session.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._get_session_path(session.id).write_text(
                json.dumps(session.model_dump(mode="json"),
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _load_or_create(self, session_id: Optional[str], kb_id: str,
                        message: str, user_id: Optional[str] = None) -> ChatSession:
        """加载已有会话（kb 不一致则新建）或创建新会话（注入 user_id）"""
        if session_id:
            session = self.get_session(session_id)
            if session and session.kb_id == kb_id:
                return session
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return ChatSession(
            id=uuid.uuid4().hex[:12],
            kb_id=kb_id,
            user_id=user_id,
            title=message.strip()[:20] or "新会话",
            messages=[],
            created_at=now,
            updated_at=now,
        )

    def list_sessions(self, user_id: Optional[object] = None,
                      kb_id: Optional[str] = None) -> List[ChatHistoryItem]:
        """会话历史列表（updated_at 倒序）

        - user_id: None 或 "all" → 全部（super_admin）；str → 仅本人；
          集合（list/set/tuple）→ 用户集合内（dept_admin 统计本部门用）；
          空集合也按过滤处理（部门无成员 → 返回空列表，防全系统会话泄露）
        - kb_id 可选过滤（现有实现忽略该参数，本次补上）
        - 旧会话 JSON 无 user_id → 视为归属 super_admin（普通用户不可见）
        """
        items: List[ChatHistoryItem] = []
        if not CHAT_DIR.exists():
            return items
        for f in CHAT_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                s_user_id = data.get("user_id")
                # 显式判 None：空集合（set()）也必须走过滤分支，
                # 否则 falsy 落入"不过滤全部"，部门无用户时会泄露全系统会话
                if user_id is not None and user_id != "all":
                    if isinstance(user_id, (list, set, tuple)):
                        if s_user_id not in user_id:
                            continue
                    elif s_user_id != user_id:
                        continue
                if kb_id and data.get("kb_id") != kb_id:
                    continue
                items.append(ChatHistoryItem(
                    id=data["id"],
                    kb_id=data.get("kb_id", ""),
                    user_id=s_user_id,
                    title=data.get("title", ""),
                    message_count=len(data.get("messages", [])),
                    created_at=data.get("created_at", ""),
                    updated_at=data.get("updated_at", ""),
                ))
            except Exception as e:
                logger.warning("加载会话 %s 失败: %s", f.name, e)
        return sorted(items, key=lambda x: x.updated_at, reverse=True)

    def get_session(self, session_id: str) -> Optional[ChatSession]:
        path = self._get_session_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ChatSession(**data)
        except Exception as e:
            logger.warning("读取会话 %s 失败: %s", session_id, e)
            return None

    def delete_session(self, session_id: str) -> bool:
        path = self._get_session_path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def rename_session(self, session_id: str, title: str) -> Optional[ChatSession]:
        """重命名会话（读 JSON → 改 title → 写回）；文件不存在或读取失败返回 None"""
        path = self._get_session_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            session = ChatSession(**data)
        except Exception as e:
            logger.warning("读取会话 %s 失败: %s", session_id, e)
            return None
        session.title = title
        self._save_session(session)
        logger.info("会话重命名: %s -> %s", session_id, title[:30])
        return session

    # ---------- 流式问答 ----------

    async def stream_chat(self, kb_id: str, message: str,
                          session_id: Optional[str] = None,
                          user_id: Optional[str] = None,
                          top_k: Optional[int] = None,
                          dept_config: Optional[dict] = None) -> AsyncIterator[str]:
        """SSE 流：meta(sources) -> delta(文本) -> done(session_id, message_count) / error

        top_k: 检索条数覆盖（None=取配置 retrieval.top_k，聊天页选择器透传）
        dept_config: 当前用户所在部门的完整配置（{"llm": {...},
          "chat": {...}, "retrieval": {...}}；None/空 = 纯全局活跃档案，
          现状行为；只含 chat/retrieval 段时同样兼容——llm 段缺省=用全局）。
          字段级合并：部门只覆盖它设置的字段（非 None 且 system_prompt
          非空串），其余用全局；检索 top_k/similarity_threshold 同步覆盖；
          llm 段 base_url/api_key/model/temperature/max_tokens/timeout
          字段级覆盖全局 LLM 配置（客户端按合并配置独立缓存）。
        """
        session = self._load_or_create(session_id, kb_id, message, user_id)
        answer_parts: List[str] = []
        saved = False
        dept = dept_config or {}
        dept_retrieval = (dept.get("retrieval")
                          if isinstance(dept.get("retrieval"), dict) else {})
        dept_llm = dept.get("llm") if isinstance(dept.get("llm"), dict) else {}

        try:
            # 0) 聊天配置字段级合并（提前计算：知识图谱增强开关在此读取；
            #    纯函数无副作用，第 4 步直接复用，避免重复合并）
            cfg = get_active_config()
            merged_chat = merge_chat_config(
                {
                    "chat": {
                        "temperature": cfg.chat.temperature,
                        "top_p": cfg.chat.top_p,
                        "max_tokens": cfg.chat.max_tokens,
                        "enable_multi_turn": cfg.chat.enable_multi_turn,
                        "history_rounds": cfg.chat.history_rounds,
                        "system_prompt": cfg.chat.system_prompt,
                        "kg_enhance": cfg.chat.kg_enhance,
                    },
                    "retrieval": {
                        "top_k": cfg.retrieval.top_k,
                        "similarity_threshold": cfg.retrieval.similarity_threshold,
                    },
                },
                dept,
            )["chat"]

            # 1) 检索（P1-2：Embedding 服务不可用等 RetrievalUnavailableError
            # 直接透传"检索服务不可用：..."，其余异常统一"检索失败: ..."前缀）
            #    部门配置覆盖检索参数：top_k（路由层选择器优先）与相似度阈值
            eff_top_k = top_k
            if eff_top_k is None and dept_retrieval.get("top_k") is not None:
                eff_top_k = int(dept_retrieval["top_k"])
            eff_min_score = dept_retrieval.get("similarity_threshold")
            try:
                sources = await get_retrieval_service().retrieve(
                    kb_id, message, top_k=eff_top_k, min_score=eff_min_score)
            except Exception as e:
                logger.exception("检索失败: %s", e)
                if isinstance(e, RetrievalUnavailableError):
                    yield sse_event("error", {"message": str(e)})
                else:
                    yield sse_event("error", {"message": f"检索失败: {e}"})
                return

            # 2) 知识图谱增强通道（与普通检索并行注入：LLM 抽实体 → 图谱匹配
            #    → 1-hop 邻接扩展 → 组装"知识图谱"来源引用；开关关/无图谱/
            #    失败一律跳过不阻塞查询；不参与 rerank——rerank 只处理检索
            #    服务内的普通候选）
            #
            #    引用顺序规则（全链路编号 = 本列表顺序，1..N 连续无跳跃）：
            #    普通检索引用保持相关度降序（retrieval_service 内排好），
            #    图谱引用（score=0，无相似度语义）固定追加在末尾作为补充引用，
            #    不参与任何分数排序。meta 事件与 _build_refs 均按本列表顺序
            #    编号，前端行内 [n]（sources[n-1]）与面板角标（index+1）同源，
            #    任何地方不得对 sources 重排。
            from backend.services.knowledge_graph_service import build_kg_source
            kg_source = await build_kg_source(
                kb_id, message, merged_chat.get("kg_enhance", True))
            if kg_source:
                sources.append(kg_source)

            # 3) meta
            yield sse_event("meta", {
                "sources": [s.model_dump(mode="json") for s in sources],
            })

            # 3) 无命中：直接告知，不调用 LLM
            if not sources:
                tip = ("未检索到相关内容，我无法回答该问题。"
                       "请尝试换一种问法，或先在知识库中上传相关文档。")
                answer_parts.append(tip)
                yield sse_event("delta", {"text": tip})
                self._finalize(session, message, answer_parts, sources)
                saved = True
                yield sse_event("done", {
                    "session_id": session.id,
                    "message_count": len(session.messages),
                })
                return

            # 4) 组装 prompt（引用放在 system；history 截最近 N 轮，
            #    多轮开关 enable_multi_turn=False 时不带历史；
            #    合并后的聊天配置已在第 0 步计算（部门字段级覆盖），直接复用）
            sys_prompt = merged_chat["system_prompt"]
            enable_multi_turn = merged_chat["enable_multi_turn"]
            history_rounds = merged_chat["history_rounds"]
            temperature = merged_chat["temperature"]
            top_p = merged_chat["top_p"]
            max_tokens = merged_chat["max_tokens"]
            refs = self._build_refs(sources)
            knowledge = self._build_knowledge(sources)
            system_content = self._build_system_content(
                sys_prompt, refs, knowledge)
            messages = [{"role": "system", "content": system_content}]
            if enable_multi_turn:
                rounds = int(history_rounds)
                history = session.messages[-(rounds * 2):]
                messages.extend(
                    {"role": m.role, "content": m.content} for m in history)
            # 行内引用标注指令追加到 user 消息（system 指令部分模型遵循弱，
            # user 侧紧邻问题遵循度高；完整示例 few-shot 强化；不含占位符，
            # 不受自定义 system_prompt 影响——自定义模板用户自行负责标注规则）
            cite_note = (
                "【回答标注要求（最高优先级，覆盖其他输出要求）：\n"
                "1. 回答中每个事实性陈述，若内容来自上方 [引用]，"
                "必须在该句句尾紧贴句号前标注对应编号 [n]，编号与 [引用]"
                "中的编号一致，禁止自造或改写编号；\n"
                "2. 示例：\"2022年11月，OpenAI发布了基于GPT-3.5的ChatGPT[1]。"
                "它成为历史上用户增长最快的消费级应用[2]。\"；连续多句引用"
                "同一编号时合并标注为 [1,2] 形式；\n"
                "3. 不确定是否来自 [引用] 的内容不要标注；[引用] 含"
                "\"知识图谱\"条目时同样可标注；\n"
                "4. 用简洁中文转述引用内容，不要原样复制 [引用] 中的"
                "【知识图谱实体】等标记性原文；回答正文不要输出 Markdown"
                "格式符号（#、*、- 等标题或列表符号）。】\n\n"
                f"问题：{message}"
            )
            messages.append({"role": "user", "content": cite_note})

            # 5) LLM 流式（生成参数：chat 段配置非 None 时覆盖 LLM 段默认值；
            #    部门 llm 段字段级覆盖全局 LLM——地址/密钥/模型/生成参数，
            #    _get_client 按合并配置独立缓存，部门切换即重建）
            llm_cfg = get_active_config().llm
            merged_llm = merge_department_llm(_llm_to_dict(llm_cfg), dept_llm)
            client = self._get_client(merged_llm)
            request_kwargs: dict = {
                "model": merged_llm["model"],
                "messages": messages,
                "temperature": (temperature
                                if temperature is not None
                                else merged_llm["temperature"]),
                "max_tokens": (max_tokens
                               if max_tokens is not None
                               else merged_llm["max_tokens"]),
                "stream": True,
            }
            if top_p is not None:
                request_kwargs["top_p"] = top_p
            try:
                stream = await client.chat.completions.create(**request_kwargs)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = delta.content if delta else None
                    if content:
                        answer_parts.append(content)
                        yield sse_event("delta", {"text": content})
            except asyncio.CancelledError:
                logger.info("客户端中断流式问答: %s", session.id)
                if answer_parts:
                    self._finalize(session, message, answer_parts, sources)
                    saved = True
                raise
            except Exception as e:
                logger.exception("LLM 流式调用失败: %s", e)
                err_msg = f"LLM 调用失败: {e}"
                answer_parts.append(err_msg)
                self._finalize(session, message, answer_parts, sources)
                saved = True
                yield sse_event("error", {"message": err_msg})
                return

            # 6) done
            self._finalize(session, message, answer_parts, sources)
            saved = True
            yield sse_event("done", {
                "session_id": session.id,
                "message_count": len(session.messages),
            })
        finally:
            # 兜底：异常路径下只要已有文本也落盘（优雅收尾）
            if not saved and answer_parts:
                try:
                    self._finalize(session, message, answer_parts, [])
                except Exception:
                    logger.exception("兜底落盘失败: %s", session.id)

    @staticmethod
    def _build_system_content(system_prompt: str, refs: str,
                              knowledge: str = "") -> str:
        """组装 system 内容（配置档案 chat.system_prompt 支持自定义）

        占位符规则（{knowledge} 与 {refs} 可并存，各自替换）：
        - 含 {knowledge} → 替换为纯知识文本（检索片段原文逐字拼接，
          无 "[引用 n]（来源：xxx）" 包装——用户模板要求原文逐字输出）
        - 含 {refs} → 替换为带来源标注的引用内容（现有格式）
        - 含任一占位符 → 模板其余原样返回（自定义覆盖全部规则：
          内置模板的"句末 [n] 标注"等由用户自己负责），不追加内容
        - 不含任何占位符 → 末尾自动追加 "\n\n[引用]\n{refs}"
          （保证检索引用必达，防止用户忘写占位符导致模型无引用可依据）
        - 空 / 纯空白 → 内置默认模板（现有行为零变化，{refs} 在末尾）
        """
        raw = (system_prompt or "").strip()
        if not raw:
            return _SYSTEM_PROMPT_TEMPLATE.format(refs=refs)
        # 先判定再替换：knowledge 内容本身即使含 "{refs}" 字样也不误判
        has_knowledge = "{knowledge}" in raw
        has_refs = "{refs}" in raw
        if has_knowledge:
            # 用 str.replace 而非 str.format：用户模板中其他花括号不会触发 KeyError
            raw = raw.replace("{knowledge}", knowledge)
        if has_refs:
            raw = raw.replace("{refs}", refs)
        if has_knowledge or has_refs:
            return raw
        # 无占位符：末尾自动追加引用段 + 行内标注规则
        # （保证检索引用必达，防止用户忘写占位符导致模型无引用可依据；
        #   标注规则保证行内 [n] 指令送达——自定义模板已覆盖内置规则）
        return f"{raw}\n\n{_CITATION_RULE}\n[引用]\n{refs}"

    @staticmethod
    def _build_knowledge(sources: List[Source]) -> str:
        """构建纯知识文本（{knowledge} 占位符注入内容）

        - 多片段按序拼接，片段间空行（\n\n）分隔
        - 每片段用 (s.parent_text or s.text)（父块优先，上下文更完整）
        - 不加任何 "[引用 n]（来源：xxx）" 包装：用户模板要求原文逐字输出，
          图片标签 ![]()、表格等 Markdown 结构原样保留
        - 纯空白片段跳过（无内容可输出）；其余逐字不截断（截断会破坏图片标签）
        """
        parts = []
        for s in sources:
            text = s.parent_text or s.text
            if text.strip():
                parts.append(text)
        return "\n\n".join(parts)

    @staticmethod
    def _build_refs(sources: List[Source]) -> str:
        # 引用编号规则：编号 = sources 列表位置（1..N 连续），与 meta 事件
        # 下发的 sources 顺序完全一致（stream_chat 中 meta 与 _build_refs 都
        # 以同一列表为源）；前端行内 [n] 按 sources[n-1] 映射、面板角标按
        # index+1 渲染，全链路同序。列表顺序约定：普通检索引用按相关度降序
        # （retrieval_service 内排好），图谱引用（score=0）追加在末尾。
        # 调用方不得对 sources 重排/去重后传给本方法，否则编号与前端错位。
        parts = []
        for i, s in enumerate(sources, start=1):
            name = s.document_name or s.document_id
            head = f"[引用 {i}]（来源：{name}）"
            # 引用文本优先用父块全文（上下文更完整，parent_child 模式），无父块用子块
            text = (s.parent_text or s.text)[:_REF_TEXT_MAX_LEN]  # 单块保护截断
            # 上下文摘要：有 context 且文本未含摘要前缀时拼到引用头部——
            # 父块全文本身无摘要（摘要是对子块生成的），补前缀让引用也显示；
            # s.text 为向量化增强文本（已含【上下文】前缀）时不重复拼接
            if s.context and not text.startswith("【上下文】"):
                text = f"【上下文】{s.context}\n{text}"
            parts.append(f"{head}\n{text}")
        return "\n\n".join(parts)

    def _finalize(self, session: ChatSession, message: str,
                  answer_parts: List[str], sources: List[Source]):
        """落盘会话（追加 user 消息 + assistant 消息，含 sources 快照）"""
        session.messages.append(ChatMessage(role="user", content=message))
        session.messages.append(ChatMessage(
            role="assistant",
            content="".join(answer_parts),
            sources=sources,
        ))
        self._save_session(session)

    # ---------- 会话导出 ----------

    @staticmethod
    def build_export_markdown(session: ChatSession) -> str:
        """会话导出为 Markdown（问答正文 + [n] 引用与来源片段）

        格式：
        # 会话标题（kb_id、时间）
        ## 用户
        问题
        ## 助手
        回答正文（含 [n] 引用标）
        ### 引用 n：来源文档名
        引用片段（前 500 字）

        引用编号与回答内 [n] 标注一致：每条助手消息内从 1 重新编号，
        对应其 sources 快照顺序；无消息时仅输出标题模板。
        """
        lines = [
            f"# {session.title or '未命名会话'}（kb_id: {session.kb_id or '—'}，"
            f"时间: {session.updated_at or session.created_at}）",
            "",
        ]
        for m in session.messages:
            if m.role == "user":
                lines.append("## 用户")
                lines.append("")
                lines.append(m.content.strip() or "（空）")
                lines.append("")
            elif m.role == "assistant":
                lines.append("## 助手")
                lines.append("")
                lines.append(m.content.strip() or "（无回答）")
                lines.append("")
                for i, s in enumerate(m.sources or [], start=1):
                    name = s.document_name or s.document_id or "未知文档"
                    snippet = (s.text or "").strip()[:500]
                    lines.append(f"### 引用 {i}：{name}")
                    lines.append("")
                    lines.append(snippet or "（无引用片段）")
                    lines.append("")
        return "\n".join(lines).rstrip() + "\n"


_chat_service: Optional[ChatService] = None


def get_chat_service() -> ChatService:
    global _chat_service
    if _chat_service is None:
        _chat_service = ChatService()
    return _chat_service
