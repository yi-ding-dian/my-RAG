"""查询改写服务：多轮对话检索前，LLM 把（历史 + 当前问题）改写为独立查询

背景：检索阶段此前完全不用对话历史——chat_service 只把 history 注入
LLM 对话，检索用的是用户**原问题**。多轮对话中"它""上面那个""这个方案"
等指代/省略会直接进向量检索，命中率显著下降（查询改写是 Dify/FastGPT 等
平台的标配检索增强）。

功能：
- 触发条件（见 _needs_rewrite）：含口语词（帮我/查查/呀/呢…）必触发——
  正式化不依赖历史；含指代词（它/这/那个…）需有历史（无历史指代无从
  消解，直接 None 白费调用）。触发由配置开关（chat.query_rewrite，默认开）
  先决定入口，本函数再按启发式精判
- 一次 LLM 调用（复用激活模型，thinking disabled 加速，短超时）；
  失败/超时/空 → None，调用方回退原问题（绝不阻塞问答，与图谱通道
  静默降级风格一致）；改写有输出长度上限与"不得引入原文与历史中
  不存在的实体或数字"约束（防改写写歪——写歪比不改写更伤检索）
- 改写结果仅本轮回调使用：不落盘、不进历史、不出现在对话中
  （prompt 事件附 rewritten_query 供前端"请求详情"展示，便于调试）
- 输出清洗：取首个非空行、剥离最常见前缀/引号、长度截断；空 → None
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from backend.config import LLMConfig, get_active_config
from backend.services.llm_client import (LLMRequestError, LLMTimeoutError,
                                         get_llm_client, llm_completion,
                                         llm_to_dict)
from backend.services.thinking_strategy import get_thinking_strategy

logger = logging.getLogger(__name__)

# 指代词：含任一即是"提问依赖上下文才能理解"的信号（必须有历史才有改写价值——没有历史消不掉）；
# 宁可多触发（改写结果与原问题无差时检索行为等价，代价一次小调用），不可漏触发（指代直接进向量检索 = 零命中）
_REFERENCE_INDICATORS = (
    "它", "他", "她", "他们", "她们", "它们的",
    "这", "那", "这些", "那些", "这个", "那个",
    "这种", "那种", "该", "上述", "上面", "底下",
    "刚才", "之前", "上文", "前面", "此后", "接下来",
    "其", "其中", "此事", "下面", "上一题",
)
# 口语/省略词：含任一即是"日常口语化提问"（无历史也可触发——正式化不依赖上下文）。
# 口语直接进向量检索与书面文本文档写作风格差距大，正式化后命中率更高。
# 刻意不收录过宽泛的单字（吗/呢/呀/啦/先/哦——疑问句几乎必带，会造成
# 每轮都触发改写，成本无谓放大），只收"明显口语"的组合词。
_COLLOQUIAL_INDICATORS = (
    "帮我", "请帮我", "查一下", "查查", "看看", "看下", "瞅瞅", "瞧瞧",
    "知不知道", "晓不晓得", "有啥", "啥", "咋", "怎么搞", "咋整",
    "嘛", "呗", "帮帮忙", "麻烦", "请教", "问一下", "想问", "我想知道",
    "你知不知道", "能告诉", "告诉我", "给我讲讲",
    "讲一下", "说一下", "给说说", "讲讲", "搞不懂", "弄明白", "说清楚",
    "之类的", "什么的", "有没有", "是不是", "对不对", "可不可以",
    "能不能", "行不行", "不知道", "想问下", "好想", "快点",
)
# 改写指令：指代消解 + 省略补全 + 口语→正式 三合一。
# 约束"不得引入原文与历史中不存在的实体或数字"防幻觉改写（改写写歪比不改写更伤检索）。
_SYSTEM_PROMPT = (
    "你是知识库检索查询改写助手。把用户当前问题改写为一条独立、自包含、"
    "无歧义的正式检索查询：\n"
    "1. 消除\"它/这/那/上述/刚才\"等指代，补全省略的实体与上下文；\n"
    "2. 把口语化表述改写为正式、规范、书面化的语句（去除\"帮我/查查/一下/"
    "呀/啦/呢\"等口头语，语句完整、明确）；\n"
    "3. 不得引入原文与历史中不存在的实体或数字，只做指代消解与正式化；\n"
    "4. 若无对话历史且问题已是正式独立表述，原样输出。\n"
    "只输出改写后的查询文本本身，不要任何解释、引号或多余文字；若当前"
    "问题本身已独立完整、书面正式，原样输出。"
)
_USER_TEMPLATE = (
    "【对话历史（旧→新）】\n{history}\n\n"
    "【当前问题】\n{query}\n\n"
    "【改写后检索查询】"
)
# 历史为空时占位（避免 prompt 出现空段误导）
_NO_HISTORY_PLACEHOLDER = "（无对话历史）"
# 输出长度/调用参数控制
_MAX_TOKENS = 256
_TIMEOUT = 8.0
_REWRITE_LEN_LIMIT = 300
# 模型可能带的前缀（输出非纯查询时剥掉）
_PREFIX_RE = re.compile(
    r"^(?:改写(?:为|成)?[:：]?|检索(?:查询)?[:：]?|查询[:：]?|"
    r"独立查询[:：]?|已改写(?:为)?[:：]?)\s*")


def _needs_rewrite(query: str, has_history: bool) -> bool:
    """启发式：命中即值得改写

    - 含口语词 → 触发（正式化不依赖历史：口语直接进向量检索与书面文本文档
      风格差距大，正式化后命中率更高）
    - 含指代词且**有历史** → 触发（无历史时指代无从消解，改写=白费一次
      调用，退回不触发）
    - 两者都无（独立书面问题）→ 改写 = 原样输出，纯费调用，不触发
    """
    if any(w in query for w in _COLLOQUIAL_INDICATORS):
        return True
    return has_history and any(w in query for w in _REFERENCE_INDICATORS)


def _format_history(history: List[Dict[str, str]], max_pairs: int = 6) -> str:
    """历史（ChatMessage dict 列表）→ 人肉可读的"用户/助手"交替文本

    max_pairs：最多携带最近 N 对问答（历史过长时截断，避免改写投入大量
    token——改写只需最近几轮上下文；调用方已按 history_rounds 切片，
    这里再限 6 对双保险）。空历史 → 占位文本（口语正式化不需要历史）。
    """
    pairs = []
    for item in history[-max_pairs * 2:]:
        role = item.get("role", "")
        content = (item.get("content") or "").strip()
        if not content:
            continue
        label = "用户" if role == "user" else "助手"
        pairs.append(f"{label}：{content}")
    return "\n".join(pairs) if pairs else _NO_HISTORY_PLACEHOLDER


def _normalize(content: str) -> Optional[str]:
    """LLM 输出 → 干净查询：取首个非空行 / 剥前缀与引号 / 长度截断"""
    if not content:
        return None
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        line = _PREFIX_RE.sub("", line)
        # 剥离模型可能包上的引号（"…"、'…'、" " 全角引号）
        line = (line.strip('\'"“‘”’‘’「」『』')
                .replace("”", "").replace("“", "").strip())
        if not line:
            continue
        return line[:_REWRITE_LEN_LIMIT]
    return None


# 独立 LLM 客户端（委托统一工厂；保留模块级函数名供测试 monkeypatch）
def _get_client(llm_cfg: Optional[dict] = None):
    return get_llm_client(llm_cfg)


async def rewrite_query(query: str,
                        history: List[Dict[str, str]]) -> Optional[str]:
    """改写查询：返回正式独立检索查询；不满足触发条件/LLM 失败/空 → None

    触发条件（见 _needs_rewrite）：口语词必触发（不需要历史）；指代词需
    有历史（无历史消不掉，白费调用直接 None）。返回 None 时调用方
    （chat_service）回退原问题。
    """
    q = (query or "").strip()
    if not q or not _needs_rewrite(q, bool(history)):
        return None
    llm_cfg = llm_to_dict(get_active_config().llm)
    cfg_obj = LLMConfig.from_dict(llm_cfg)
    if not (cfg_obj.base_url and cfg_obj.model):
        logger.warning("LLM 未配置（base_url/model 为空），跳过查询改写")
        return None
    history_text = _format_history(history)
    try:
        client = _get_client(llm_cfg)
        # 思考关闭策略：改写是延迟敏感的短任务（与查询实体抽取同策略）
        strategy = get_thinking_strategy(llm_cfg, "disabled")
        payload = {
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",
                 "content": _USER_TEMPLATE.format(
                     history=history_text, query=q)},
            ],
        }
        strategy.apply(payload)
        resp = await llm_completion(
            client, model=cfg_obj.model, messages=payload["messages"],
            max_tokens=_MAX_TOKENS, temperature=0.1,
            extra_body=payload.get("extra_body"), timeout=_TIMEOUT,
        )
        try:
            content = (resp.choices[0].message.content or "").strip()
        except Exception:
            content = ""
        return _normalize(content)
    except LLMTimeoutError:
        logger.warning("查询改写超时（>%.0fs），使用原问题检索", _TIMEOUT)
        return None
    except LLMRequestError as e:
        # 可预期失败（网络/限流/HTTP 错误）：warning，原问题降级
        logger.warning("查询改写失败，使用原问题检索: %s", str(e)[:150])
        return None
    except Exception as e:
        # 兜底（未知异常）：同样原问题降级，信息保留
        logger.warning("查询改写失败，使用原问题检索: %s", str(e)[:150])
        return None
