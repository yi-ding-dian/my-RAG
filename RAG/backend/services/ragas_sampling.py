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

import asyncio
import json
import logging
import threading
import uuid
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
    """读取**含该 kb** 的会话原始 dict（按 updated_at 倒序）；文件损坏静默跳过

    多库问答的会话存的是 `kb_ids` 数组、没有 `kb_id` 字段——只认 `kb_id` 会让这些
    会话整个采不到（表现就是"从聊天历史导入"里看不到多库聊天的记录）。
    这里与 list_sessions 取同一口径，两种形态都认。
    """
    sessions: List[Dict] = []
    if not CHAT_DIR.exists():
        return sessions
    for f in CHAT_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("RAGAS 采样读取会话失败: %s err=%s", f.name, e)
            continue
        if not isinstance(data, dict):
            continue
        s_kb_ids = (data.get("kb_ids")
                    or ([data["kb_id"]] if data.get("kb_id") else []))
        if kb_id in s_kb_ids:
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


async def fill_contexts(kb_ids: List[str], samples: List[Dict], top_k: int) -> None:
    """为每个样本填充 contexts（原地修改）：检索 top_k 片段，(parent_text or text)

    kb_ids 支持**多库**：多库问答的问题拿来评估时，检索也得走多库，否则评的不是
    用户实际用的那条链路。统一走 retrieve_multi（单库传单元素列表，行为与
    retrieve 一致）。

    无命中 → 空列表（保留样本，忠实度等指标仍可评估）；
    检索异常（如 embedding 服务不可用）→ 该样本 contexts 留空，不阻断整体。
    """
    svc = get_retrieval_service()
    for s in samples:
        try:
            sources = await svc.retrieve_multi(kb_ids, s["question"], top_k=top_k)
            contexts = [(src.parent_text or src.text).strip()
                        for src in sources if (src.parent_text or src.text).strip()]
        except Exception as e:
            logger.warning("RAGAS 样本检索失败（contexts 留空）: kb=%s err=%s",
                           kb_ids, e)
            contexts = []
        s["contexts"] = contexts


# ==================== 实时生成（自动评估） ====================
#
# 「题集参考答案」模式测的是检索；「实时生成」模式走**完整生产链路**（检索→图谱→
# 改写→组装 prompt→生成），answer 是模型真实输出，评出来的分数才等于用户体验。

# 生成并发数：每条样本是一次完整问答（检索 + LLM），串行跑几十条会久到 HTTP 超时；
# 数值保守，兼容本地 vLLM 的并发上限
GENERATE_CONCURRENCY = 3


def _parse_sse(chunk: str, state: Dict) -> None:
    """把一段 SSE 文本累积进 state（meta→sources / delta→answer / done→session_id）

    事件格式见 chat_service.sse_event：`event: xxx\ndata: {...}\n\n`。
    meta 的 data 兼容裸数组与 {"sources": [...]} 两种形态（与前端解析同口径）。
    """
    etype = str(state.get("_etype") or "")
    for line in chunk.split("\n"):
        if line.startswith("event:"):
            etype = line[6:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if etype == "meta":
                srcs = data if isinstance(data, list) else (data.get("sources") or [])
                if isinstance(srcs, list):
                    state["sources"] = srcs
            elif etype == "delta":
                text = data if isinstance(data, str) else (data or {}).get("text") or ""
                state["answer"] = str(state.get("answer") or "") + str(text)
            elif etype == "done" and isinstance(data, dict):
                if data.get("session_id"):
                    state["session_id"] = data["session_id"]
    state["_etype"] = etype


async def _generate_one(kb_ids: List[str], question: str, top_k: int,
                        user_id: Optional[str]) -> tuple:
    """跑一次真实问答链路，返回 (answer, contexts, session_id)

    用 chat_service.stream_chat 而不是只调检索：它才是生产链路，
    这样评出来的分数等于用户实际体验到的质量。kb_ids 支持多库（stream_chat 本身
    就收列表），多库问答的问题评估时不会退化成单库。

    contexts 取 **同一次** 流里的 meta 事件——与 answer 同源；分两次取会得到
    "回答引用的内容"和"评估用的上下文"对不上的样本。
    """
    # 延迟导入：避免模块级循环依赖（ragas_sampling ← chat_service 的方向）
    from backend.services.chat_service import get_chat_service

    state: Dict = {"answer": "", "sources": [], "session_id": None}
    async for chunk in get_chat_service().stream_chat(
            kb_ids, question, session_id=None, user_id=user_id, top_k=top_k):
        _parse_sse(chunk, state)

    contexts: List[str] = []
    for s in (state.get("sources") or []):
        if isinstance(s, dict):
            ctx = str(s.get("parent_text") or s.get("text") or "").strip()
            if ctx:
                contexts.append(ctx)
    return str(state.get("answer") or "").strip(), contexts, state.get("session_id")


async def generate_answers(kb_ids: List[str], samples: List[Dict], top_k: int,
                           user_id: Optional[str] = None) -> List[str]:
    """对每条样本走真实链路生成 answer + contexts（原地写入样本）

    kb_ids 支持多库（多库问答的问题评估时不会退化成单库）。

    返回**本次产生的会话 id**——实时生成会真实落盘会话文件，那是评估的副产物，
    不该留在用户的会话列表里，调用方负责清理（cleanup_sessions）。

    单条失败不中断整体：该条 answer/contexts 留空（RAGAS 按空值处理），
    只是这条样本的分数无参考意义。
    """
    sem = asyncio.Semaphore(GENERATE_CONCURRENCY)

    async def one(s: Dict) -> Optional[str]:
        async with sem:
            try:
                answer, contexts, sid = await _generate_one(
                    kb_ids, str(s.get("question") or ""), top_k, user_id)
                s["answer"] = answer
                s["contexts"] = contexts
                return sid
            except Exception as e:
                logger.warning("评估样本实时生成失败（该条留空）: q=%s err=%s",
                               str(s.get("question") or "")[:40], e)
                s["answer"] = ""
                s["contexts"] = []
                return None

    sids = await asyncio.gather(*(one(s) for s in samples))
    return [sid for sid in sids if sid]


def cleanup_sessions(session_ids: List[str]) -> int:
    """删除评估过程中生成的会话（同步文件操作，调用方自行决定是否放线程池）

    keep_msg_idxs 传空集 = 确认无反馈，走物理删除（这些会话刚建、必然没有反馈）。
    """
    if not session_ids:
        return 0
    from backend.services.chat_service import get_chat_service
    svc = get_chat_service()
    removed = 0
    for sid in session_ids:
        try:
            if svc.delete_session(sid, keep_msg_idxs=set()):
                removed += 1
        except OSError as e:
            logger.warning("清理评估会话 %s 失败: %s", sid, e)
    return removed


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


# 任务元数据读-改-写的互斥锁（与评估集分开：两个文件各自独立加锁）
_tasks_lock = threading.Lock()


def append_task_meta(meta: dict) -> None:
    """追加一条任务元数据（失败仅记日志，不影响发起流程返回）"""
    try:
        with _tasks_lock:
            tasks = load_task_meta()
            tasks.append(meta)
            RAGAS_TASKS_FILE.write_text(
                json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
                encoding="utf-8")
    except Exception as e:
        logger.warning("RAGAS 任务元数据落盘失败: %s", e)


def update_task_meta(task_id: str, patch: Dict) -> bool:
    """按 task_id 合并更新任务元数据字段（回写分数快照用）

    RAGAS 服务端只保留任务与报告，本地要展示"同一评估集的历史分数对比"就得把
    分数快照存下来——否则每次对比都要回源拉 N 个任务的报告。任务不存在或写入
    失败返回 False（调用方无需中断主流程）。
    """
    if not task_id:
        return False
    try:
        with _tasks_lock:
            tasks = load_task_meta()
            target = next((t for t in tasks if t.get("task_id") == task_id), None)
            if target is None:
                return False
            target.update(patch)
            RAGAS_TASKS_FILE.write_text(
                json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        return True
    except Exception as e:
        logger.warning("RAGAS 任务元数据回写失败: %s", e)
        return False


# ==================== 本地评估集（可复用、可对比） ====================
#
# 存 question + ground_truth，**刻意不存 contexts**：评估时 contexts 由
# fill_contexts 实时检索填充，这样检索链路（切块/混合检索/rerank/参数）的改动
# 才能反映到分数变化上——若把 contexts 一并冻结存下，改了检索分数也不会动，
# 评估就失去意义了。
#
# 与任务元数据里的 dataset_id 的区别：那是每次评估在 RAGAS 服务端新建的数据集 id，
# 一次一换；本地评估集是可反复复用的题集，靠任务里的 local_dataset_id 关联。

RAGAS_DATASETS_FILE: Path = DATA_DIR / "ragas_datasets.json"
# 评估集增删改的互斥锁：读-改-写整体加锁，避免并发请求互相覆盖（与 chat_service 同策略）
_datasets_lock = threading.Lock()


def load_datasets() -> List[Dict]:
    """读取本地评估集列表（文件缺失/损坏返回空列表）"""
    try:
        if RAGAS_DATASETS_FILE.exists():
            data = json.loads(RAGAS_DATASETS_FILE.read_text(encoding="utf-8"))
            items = data.get("datasets", []) if isinstance(data, dict) else []
            return [d for d in items if isinstance(d, dict)]
    except Exception as e:
        logger.warning("评估集读取失败: %s", e)
    return []


def get_dataset(dataset_id: str) -> Optional[Dict]:
    """按 id 取单个评估集（不存在返回 None）"""
    if not dataset_id:
        return None
    for d in load_datasets():
        if d.get("id") == dataset_id:
            return d
    return None


def _write_datasets(datasets: List[Dict]) -> None:
    RAGAS_DATASETS_FILE.write_text(
        json.dumps({"datasets": datasets}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _normalize_sample(s: Dict) -> Optional[Dict]:
    """把一条样本规整成 {question, ground_truth}（question 为空返回 None 丢弃）"""
    q = str(s.get("question") or "").strip()
    if not q:
        return None
    return {"question": q,
            "ground_truth": str(s.get("ground_truth") or "").strip()}


def create_dataset(name: str, kb_id: str, kb_name: str, samples: List[Dict],
                   source: str, user_id: str) -> Dict:
    """新建评估集（返回落盘后的完整记录）"""
    cleaned = [x for x in (_normalize_sample(s) for s in samples) if x]
    now = now_iso()
    ds = {
        "id": f"ds_{uuid.uuid4().hex[:12]}",
        "name": name,
        "kb_id": kb_id,
        "kb_name": kb_name,
        "samples": cleaned,
        "source": source,
        "user_id": user_id,
        "created_at": now,
        "updated_at": now,
    }
    with _datasets_lock:
        items = load_datasets()
        items.append(ds)
        _write_datasets(items)
    return ds


def delete_dataset(dataset_id: str) -> bool:
    """删除评估集（不存在返回 False）。已跑过的任务记录不受影响"""
    with _datasets_lock:
        items = load_datasets()
        left = [d for d in items if d.get("id") != dataset_id]
        if len(left) == len(items):
            return False
        _write_datasets(left)
    return True


def add_samples(dataset_id: str, new_samples: List[Dict]) -> Dict:
    """往评估集追加样本（按 question 去重）

    返回 {added, skipped, total}；评估集不存在抛 KeyError（路由层转 404）。
    """
    cleaned = [x for x in (_normalize_sample(s) for s in new_samples) if x]
    with _datasets_lock:
        items = load_datasets()
        target = next((d for d in items if d.get("id") == dataset_id), None)
        if target is None:
            raise KeyError(dataset_id)
        existing = target.setdefault("samples", [])
        seen = {str(s.get("question") or "").strip() for s in existing}
        added = 0
        for s in cleaned:
            if s["question"] in seen:
                continue
            existing.append(s)
            seen.add(s["question"])
            added += 1
        target["updated_at"] = now_iso()
        _write_datasets(items)
        total = len(existing)
    return {"added": added, "skipped": len(cleaned) - added, "total": total}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
