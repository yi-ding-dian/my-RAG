"""RAGAS 评估样本采样 + 本地任务元数据

样本来源：
- 手动测试集（stats 路由组装）：用户填写问题 + 正确答案（=ground_truth），
  不经过本模块采样，仅复用 fill_contexts 检索填上下文。
- 自动采样（本模块，从知识库真实使用记录中采样）：
  - logs（默认）：data/retrieval_logs 近 30 天该 kb 的真实检索问题。按 query
    去重保留最近一次，answer 留空（RAGAS 样本 answer 非必填，默认 ""），可
    评估忠实度/上下文类指标；局限：无标准答案，需 ground_truth 的指标不可用。
  - chat：data/chat 该 kb 会话中的 user 问题 + 对应 assistant 回答（最近会话
    优先，问题去重，答案截断 MAX_ANSWER_CHARS 防超长样本撑爆数据集）。
- contexts：对每个问题调 retrieval_service.retrieve(kb_id, question, top_k)，
  取 (parent_text or text)（父块优先，与 chat 知识构建一致）；无命中保留空
  contexts（不跳过样本——忠实度等指标仍可评估）；检索异常留空不阻断整体。

本地任务元数据 data/ragas_tasks.json（{tasks: [...]}）：
  记录知识库侧发起的评估任务（RAGAS 任务列表无 kb 归属信息），供任务列表
  接口合并展示 kb_name / 发起来源。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from backend.config import CHAT_DIR, DATA_DIR
from backend.services.retrieval_service import get_retrieval_service
from backend.services.retrieval_log import get_retrieval_log_service

logger = logging.getLogger(__name__)

# 采样窗口（与检索质量统计一致，近 30 天）
SAMPLE_WINDOW_DAYS = 30
# chat 样本答案截断长度（防超长回答撑爆 RAGAS 数据集/请求体）
MAX_ANSWER_CHARS = 4000

RAGAS_TASKS_FILE: Path = DATA_DIR / "ragas_tasks.json"


# ==================== 采样 ====================

def sample_from_logs(kb_id: str, limit: int) -> List[Dict]:
    """从检索日志采样真实问题（近 30 天，query 去重保最近，answer 留空）

    返回 [{question, answer: ""}, ...]（最多 limit 条，最近优先）；
    日志 query 落盘时截断 100 字（retrieval_log 约束）。
    """
    entries = get_retrieval_log_service().read(kb_id, window_days=SAMPLE_WINDOW_DAYS)
    seen: set = set()
    out: List[Dict] = []
    # 日志按时间正序 → 倒序遍历保证去重后保留"最近一次"出现
    for e in reversed(entries):
        q = (e.get("query") or "").strip()
        if not q or q in seen:
            continue
        seen.add(q)
        out.append({"question": q, "answer": ""})
        if len(out) >= limit:
            break
    return out


def _read_chat_sessions(kb_id: str) -> List[Dict]:
    """读取该 kb 的会话原始 dict（按 updated_at 倒序）；文件损坏静默跳过"""
    sessions: List[Dict] = []
    if not CHAT_DIR.exists():
        return sessions
    for f in CHAT_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("RAGAS 采样读取会话失败: %s err=%s", f.name, e)
            continue
        if isinstance(data, dict) and data.get("kb_id") == kb_id:
            sessions.append(data)
    return sorted(sessions, key=lambda s: s.get("updated_at", ""), reverse=True)


def sample_from_chat(kb_id: str, limit: int) -> List[Dict]:
    """从会话历史采样真实问答（该 kb 的 user 问题 + 对应 assistant 回答）

    返回 [{question, answer, ground_truth}, ...]（最多 limit 条，最近会话优先，
    问题去重）；遍历 messages 提取 user 后紧跟 assistant 的对（孤儿 user 消息
    无回答则跳过）；答案截断 MAX_ANSWER_CHARS。
    ground_truth 与 answer 相同（RAGAS 样本字段，context_precision 等指标
    需要 reference 列，RAGAS 数据模型从样本 ground_truth 映射）。
    """
    out: List[Dict] = []
    seen: set = set()
    for sess in _read_chat_sessions(kb_id):
        messages = sess.get("messages") or []
        for i, m in enumerate(messages):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            q = (m.get("content") or "").strip()
            if not q or q in seen:
                continue
            # 只取完整问答对：紧邻的 assistant 回答缺失/为空 → 跳过
            # （无回答的样本由 logs 来源覆盖，chat 来源的价值在有答案）
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            answer = ((nxt or {}).get("content") or "").strip() if (
                isinstance(nxt, dict) and nxt.get("role") == "assistant") else ""
            if not answer:
                continue
            seen.add(q)
            answer_trunc = answer[:MAX_ANSWER_CHARS]
            out.append({
                "question": q,
                "answer": answer_trunc,
                # 会话答案为天然参考答案：RAGAS context_precision 需要 reference
                # 列（数据模型字段 ground_truth），随样本上传
                "ground_truth": answer_trunc,
            })
            if len(out) >= limit:
                return out
    return out


async def fill_contexts(kb_id: str, samples: List[Dict], top_k: int) -> None:
    """为每个样本填充 contexts（原地修改）：检索 top_k 片段，(parent_text or text)

    无命中 → 空列表（保留样本，忠实度等指标仍可评估）；
    检索异常（如 embedding 服务不可用）→ 该样本 contexts 留空，不阻断整体。
    """
    svc = get_retrieval_service()
    for s in samples:
        try:
            sources = await svc.retrieve(kb_id, s["question"], top_k=top_k)
            contexts = [(src.parent_text or src.text).strip()
                        for src in sources if (src.parent_text or src.text).strip()]
        except Exception as e:
            logger.warning("RAGAS 样本检索失败（contexts 留空）: kb=%s err=%s",
                           kb_id, e)
            contexts = []
        s["contexts"] = contexts


# ==================== 本地任务元数据 ====================

def load_task_meta() -> List[Dict]:
    """读取本地发起的 RAGAS 任务元数据列表（文件缺失/损坏返回空列表）"""
    try:
        if RAGAS_TASKS_FILE.exists():
            data = json.loads(RAGAS_TASKS_FILE.read_text(encoding="utf-8"))
            tasks = data.get("tasks", []) if isinstance(data, dict) else []
            return [t for t in tasks if isinstance(t, dict)]
    except Exception as e:
        logger.warning("RAGAS 任务元数据读取失败: %s", e)
    return []


def append_task_meta(meta: dict) -> None:
    """追加一条任务元数据（失败仅记日志，不影响发起流程返回）"""
    try:
        tasks = load_task_meta()
        tasks.append(meta)
        RAGAS_TASKS_FILE.write_text(
            json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        logger.warning("RAGAS 任务元数据落盘失败: %s", e)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
