"""系统统计 API：/api/stats

- GET /api/stats                统计（按角色过滤：super_admin 全量 /
                                dept_admin 本部门 kb/doc/chunk + 本部门用户会话 /
                                user 本部门 kb/doc/chunk + 自己会话；
                                chunk 数取 document_service 统计，排除软删，
                                与知识库列表/详情一致）
- GET /api/stats/quality        检索质量统计（近 30 天检索日志汇总：
                                总次数/平均命中/文档命中排行/零命中文档/日粒度）
- GET /api/stats/ragas          RAGAS 可用性探测 + 任务列表（3s 超时，
                                合并本地发起的任务元数据 kb_name/发起来源）
- POST /api/stats/ragas/evaluations  从知识库发起 RAGAS 评估（默认真实问题采样
                                → 知识库检索填 contexts → 上传数据集 →
                                创建评估任务；支持 samples 手动测试集优先与
                                preview 预览模式；super_admin/dept_admin 本部门库）
- GET /api/stats/ragas/tasks/{task_id}  RAGAS 任务报告
- POST /api/stats/ragas/evaluations/{task_id}/cancel  取消 RAGAS 评估任务
  （发起人本人/super_admin/dept_admin 本部门可取消，无权 404 伪装）
- GET /api/stats/chat-feedback/logs  聊天反馈分页查询（仅超管，依赖注入点已预留
                                      部门管理员接入；rating/用户名/部门/关键词/
                                      时间范围过滤 + 倒序分页；用户/部门/知识库名
                                      回填，缺失回退）
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_active_config
from backend.db import get_db
from backend.deps import get_current_user, kb_or_404
from backend.models.user_models import (DepartmentORM, FeedbackORM, KBORM,
                                        UserORM, UserPublic)
from backend.services import audit_service, ragas_sampling
from backend.services.chat_service import get_chat_service
from backend.services.document_service import get_document_service
from backend.services.kb_service import get_kb_service
from backend.services.parsers.probes import probe_embedding, probe_llm
from backend.services.ragas_client import RagasApiError, get_ragas_client
from backend.services.settings.service import find_llm_item
from backend.services.retrieval_log import get_retrieval_log_service
from backend.services.user_service import list_users

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stats", tags=["统计"])


@router.get("")
async def get_stats(db: AsyncSession = Depends(get_db),
                    user: UserPublic = Depends(get_current_user)):
    """自身统计：知识库/文档/切块/会话/消息（按角色过滤）"""
    kb_svc = get_kb_service()
    doc_svc = get_document_service()
    chat_svc = get_chat_service()

    # 1) 知识库范围（super_admin 全量；dept_admin/user 本部门；无部门 → 空）
    if user.role == "super_admin":
        kbs = await kb_svc.list(db)
    elif user.department_id:
        kbs = await kb_svc.list(db, department_id=user.department_id)
    else:
        kbs = []

    # 2) 会话范围（super_admin 全部；dept_admin 本部门用户；user 自己）
    if user.role == "super_admin":
        sessions = chat_svc.list_sessions(None)
    elif user.role == "dept_admin":
        if user.department_id:
            users = await list_users(db, user.department_id)
            sessions = chat_svc.list_sessions({u.id for u in users})
        else:
            sessions = []
    else:
        sessions = chat_svc.list_sessions(user.id)

    doc_count = sum(doc_svc.count_by_kb(kb.id) for kb in kbs)
    # P1-1: 切块数改用 document_service 统计（排除软删文档，与知识库列表/详情
    # 的 chunk_count 语义一致；Chroma count 含软删向量会导致两处数字不一致）
    chunk_count = sum(doc_svc.chunk_count_by_kb(kb.id) for kb in kbs)
    message_count = sum(s.message_count for s in sessions)

    return {
        "kb_count": len(kbs),
        "doc_count": doc_count,
        "chunk_count": chunk_count,
        "session_count": len(sessions),
        "message_count": message_count,
    }


# 检索质量统计窗口（天）
QUALITY_WINDOW_DAYS = 30


def _build_daily(entries) -> list:
    """近 30 天日粒度汇总（含今天，按日期正序；无检索的天 hit_rate=0）"""
    today = datetime.now().date()
    days = [today - timedelta(days=i) for i in range(QUALITY_WINDOW_DAYS - 1, -1, -1)]
    by_date: dict = defaultdict(list)
    for e in entries:
        try:
            d = datetime.fromisoformat(e.get("ts", "")).date()
        except (ValueError, TypeError):
            continue
        by_date[d].append(e)
    out = []
    for d in days:
        day_entries = by_date.get(d, [])
        total = len(day_entries)
        hit = sum(1 for e in day_entries if e.get("hit_doc_ids"))
        out.append({
            "date": d.isoformat(),
            "retrievals": total,
            # 命中率 = 当日有命中的检索数 / 当日检索总数
            "hit_rate": round(hit / total, 4) if total else 0,
        })
    return out


@router.get("/quality")
async def get_retrieval_quality(kb_id: str,
                                db: AsyncSession = Depends(get_db),
                                user: UserPublic = Depends(get_current_user)):
    """检索质量统计（近 30 天，登录即可；kb 不可访问 404 伪装）

    返回 {kb_id, window_days, total_retrievals, avg_hits_per_retrieval,
    hit_docs(文档命中排行 top10 降序), zero_hit_docs(窗口期从未命中的
    ingested 文档), daily(日粒度检索数/命中率)}；无检索数据时返回空数组不报错。
    """
    await kb_or_404(db, kb_id, user)

    entries = get_retrieval_log_service().read(kb_id, window_days=QUALITY_WINDOW_DAYS)

    # 文档名/切块数映射（零命中文档判断用）
    docs = get_document_service().list_by_kb(kb_id)
    name_map = {d.id: d.original_name for d in docs}

    # 文档命中排行（按 hit_doc_ids 内出现的次数，top10 降序）
    counter: Counter = Counter()
    for e in entries:
        for doc_id in e.get("hit_doc_ids", []):
            counter[doc_id] += 1
    hit_docs = [{"doc_id": did, "doc_name": name_map.get(did, did), "hits": n}
                for did, n in counter.most_common(10)]

    # 零命中文档：窗口期内从未出现在任何检索命中的 ingested 文档
    hit_ids = set(counter)
    zero_hit_docs = [
        {"doc_id": d.id, "doc_name": d.original_name, "chunks": d.chunk_count}
        for d in docs if d.status == "ingested" and d.id not in hit_ids
    ]

    total = len(entries)
    avg_hits = (round(sum(len(e.get("hit_doc_ids", [])) for e in entries) / total, 2)
                if total else 0)
    return {
        "kb_id": kb_id,
        "window_days": QUALITY_WINDOW_DAYS,
        "total_retrievals": total,
        "avg_hits_per_retrieval": avg_hits,
        "hit_docs": hit_docs,
        "zero_hit_docs": zero_hit_docs,
        "daily": _build_daily(entries),
    }


# ---- RAGAS 评估发起 ----

# RAGAS 6 个指标白名单（英文名 → 中文名；其中 context_recall/answer_correctness/
# answer_similarity 需要 ground_truth，answer_similarity 还需 embedding）
RAGAS_METRICS = {
    "faithfulness": "忠实度",
    "answer_relevancy": "答案相关性",
    "context_precision": "上下文精确率",
    "context_recall": "上下文召回率",
    "answer_correctness": "答案正确性",
    "answer_similarity": "答案相似度",
}
# 默认指标（手动测试集场景：用户填写了正确答案，默认启用需 ground_truth 的 3 个）
RAGAS_DEFAULT_METRICS = ["context_recall", "answer_correctness", "answer_similarity"]
MAX_SAMPLE_COUNT = 100
# 单次评估可选的知识库上限（与聊天页多选、/chat/stream 的校验口径一致）
MAX_EVAL_KBS = 5
# 每轮 /ragas 轮询最多补写几个任务的分数快照（避免一次回源拉太多报告拖慢接口）
MAX_SCORE_BACKFILL = 3
MAX_TOP_K = 20
# 发起来源（数据集描述 / 本地元数据 source 用）
SOURCE_LABELS = {
    "logs": "检索日志",
    "chat": "会话问答",
    "manual": "手动填写",
    "dataset": "评估集重跑",
}


class RagasSampleInput(BaseModel):
    """手动填写的一条测试集样本

    question 必填（测试问题）、ground_truth 必填（用户填的正确答案即参考答案）；
    answer 可选——用户只填一次答案，缺省时后端把 ground_truth 同时写入 answer
    （用户填的正确答案既是 answer 也是 ground_truth，answer_relevancy/context
    _precision 等需 answer 字段的指标才能评分）。
    """
    question: str = Field("", description="测试问题（必填非空）")
    answer: Optional[str] = Field(None, description="答案（可选，缺省用 ground_truth）")
    ground_truth: Optional[str] = Field(None, description="正确答案/参考答案（必填非空）")


class RagasEvaluationRequest(BaseModel):
    """发起评估请求体（samples 与 preview 均为可选扩展，旧调用不受影响）"""
    kb_id: str = Field("", description="知识库 ID（单库；与 kb_ids 二选一，"
                                      "传 dataset_id 时忽略、以评估集绑定的为准）")
    kb_ids: Optional[List[str]] = Field(
        None, description="知识库 ID 数组（1~5 个，多库评估；与 kb_id 二选一，"
                          "都传时 kb_ids 优先）——多库问答的问题拿去评估时，"
                          "检索/生成都按这组库走，才和用户实际用的是同一条链路")
    metrics: Optional[List[str]] = Field(None, description="评估指标（默认取需要 "
                                         "ground_truth 的 3 个）")
    sample_count: int = Field(20, description="自动采样样本数（1~100；samples 模式忽略）")
    sample_source: str = Field("logs", description='样本来源："logs"=检索日志真实问题'
                                '（无答案）；"chat"=会话问答（问题+答案）；'
                                '仅自动采样/preview 模式使用，samples 模式忽略')
    top_k: int = Field(3, description="检索上下文 top_k（1~20）")
    samples: Optional[List[RagasSampleInput]] = Field(
        None, description="手动测试集（1~100 条）；传了 samples 时优先于自动采样")
    dataset_id: Optional[str] = Field(
        None, description="本地评估集 ID（重跑同一份题集用）：传了则取其样本，"
                          "优先于 samples 与自动采样；知识库以评估集绑定的为准")
    llm_model: Optional[str] = Field(
        None, description="评估用的评委模型（取当前档案模型列表里的 name；"
                          "留空=用当前激活模型）。**换评委分数不可比**，"
                          "任务会记下实际用的模型供对比时区分")
    answer_source: str = Field(
        "dataset", description='回答（answer）来源："dataset"=题集/样本里的参考答案'
                               '（默认，快，实际测的是"检索够不够"）；'
                               '"generate"=系统实时生成（走完整问答链路，'
                               'answer 是模型真实输出、contexts 与它同源——'
                               '慢但测的是端到端质量）')
    preview: bool = Field(False, description="预览模式：仅采样返回样本列表，不发起评估")


class RagasDatasetCreateRequest(BaseModel):
    """新建本地评估集（把一份题集沉淀下来，供反复重跑对比）"""
    kb_id: str = Field(..., description="知识库 ID")
    name: str = Field(..., description="评估集名称（1~50 字）")
    samples: List[RagasSampleInput] = Field(..., description="样本（1~100 条）")
    source: str = Field("manual", description="来源标记：manual=手动填写/导入，"
                                              "chat=从聊天历史导入，feedback=点踩沉淀")


class RagasDatasetAddSamplesRequest(BaseModel):
    """往已有评估集追加样本（反馈页「加入评估集」用）"""
    samples: List[RagasSampleInput] = Field(..., description="要追加的样本（1~100 条）")


@router.get("/ragas")
async def ragas_status(user: UserPublic = Depends(get_current_user)):
    """探测 RAGAS 8090：3s 超时，失败返回 {available:false}（自身统计不受影响）

    可用时合并本地发起任务元数据（kb_name/来源/样本数/本地评估集），并把已完成
    任务的**分数快照**补写到本地（每轮最多 MAX_SCORE_BACKFILL 个）。

    为什么要在本地存分数：RAGAS 服务端只保留任务与报告，而"同一评估集的分数变化"
    是反复重跑的核心价值——不在本地留快照，每次展示对比都得回源拉全部报告。
    """
    result = await get_ragas_client().probe()
    if not result.get("available"):
        return result
    meta_by_task = {t["task_id"]: t for t in ragas_sampling.load_task_meta()}
    pending: List[dict] = []  # 已完成、但本地还没记分数的任务
    for t in result.get("tasks", []):
        meta = meta_by_task.get(t.get("id"))
        if not meta:
            continue
        t["kb_name"] = meta.get("kb_name")
        t["source"] = meta.get("source")
        t["sample_count"] = meta.get("sample_count")
        # 发起人 user_id（前端取消按钮按当前用户比对显隐；旧任务无此字段）
        if meta.get("user_id"):
            t["user_id"] = meta["user_id"]
        # 本地评估集：前端据此把同一题集的历次任务排在一起做分数对比
        if meta.get("local_dataset_id"):
            t["local_dataset_id"] = meta["local_dataset_id"]
        # 本次的评委模型：换评委分数不可比，前端只在同评委间显示 ↑↓ 对比
        if meta.get("eval_model"):
            t["eval_model"] = meta["eval_model"]
        # answer 来源：两种模式测的东西不同（检索 vs 端到端），同样不能混着比
        if meta.get("answer_source"):
            t["answer_source"] = meta["answer_source"]
        if meta.get("scores"):
            t["scores"] = meta["scores"]
        elif t.get("status") == "completed":
            pending.append(t)

    for t in pending[:MAX_SCORE_BACKFILL]:
        try:
            r = await get_ragas_client().get_report(t["id"])
            report = r.get("report") if r.get("available") else None
            scores = ((report or {}).get("aggregate") or {}).get("scores") or {}
            if scores:
                t["scores"] = scores
                ragas_sampling.update_task_meta(t["id"], {
                    "scores": scores,
                    "completed_at": (report or {}).get("completed_at")
                                    or t.get("completed_at"),
                })
        except Exception as e:
            # 补分失败不影响列表返回（下次轮询再补）
            logger.warning("补写 RAGAS 分数快照失败 task=%s: %s", t.get("id"), e)
    return result


def _build_manual_samples(raw: List[RagasSampleInput]) -> List[Dict]:
    """校验并组装手动测试集样本（question + ground_truth 必填非空）

    answer 缺省时写入 ground_truth——用户只填一次答案，它既是 answer 也是
    ground_truth（answer_relevancy 等需 answer 字段的指标才能评分）。
    校验失败抛 400 并指明具体第几条。
    """
    if not 1 <= len(raw) <= MAX_SAMPLE_COUNT:
        raise HTTPException(status_code=400,
                            detail=f"样本数量需在 1~{MAX_SAMPLE_COUNT} 之间")
    out: List[Dict] = []
    for i, s in enumerate(raw, start=1):
        question = (s.question or "").strip()
        ground_truth = (s.ground_truth or "").strip()
        if not question:
            raise HTTPException(status_code=400,
                                detail=f"第 {i} 条样本缺少测试问题（question）")
        if not ground_truth:
            raise HTTPException(status_code=400,
                                detail=f"第 {i} 条样本缺少正确答案（ground_truth）")
        answer = (s.answer or "").strip() or ground_truth
        out.append({
            "question": question,
            "answer": answer,
            "ground_truth": ground_truth,
        })
    return out


async def _sample_questions(kb_id: str, sample_source: str,
                            sample_count: int) -> List[Dict]:
    """按来源采样真实问题（logs 检索日志 / chat 会话问答），空则 400"""
    if sample_source == "chat":
        questions = ragas_sampling.sample_from_chat(kb_id, sample_count)
    else:
        questions = ragas_sampling.sample_from_logs(kb_id, sample_count)
    if not questions:
        raise HTTPException(
            status_code=400,
            detail="该知识库暂无可用样本（近 30 天无检索日志或问答记录），"
                   "请先通过聊天或检索测试页积累真实问题，或手动填写测试集")
    return questions


def _build_eval_llm_cfg(model_name: Optional[str]) -> tuple[dict, str]:
    """组装 RAGAS **评委**模型的 llm_cfg，返回 (llm_cfg, 实际使用的模型名)

    语义提醒：RAGAS 里这个 LLM 是给回答打分的**评委**，不是被评估的对象。
    **换评委 = 分数不可比**——所以调用方必须把实际用的模型名记进任务元数据，
    分数对比也只在同一个评委之间做，否则会把"换评委的口味差异"误读成"改坏了"。

    未指定 model_name / 找不到该模型 → 用当前激活模型（现状行为）。
    模型条目从当前激活档案的模型列表里按 name 取（复用 find_llm_item，
    与「解析配置」指定模型同一套机制）。
    """
    llm = get_active_config().llm
    item = find_llm_item(model_name) if model_name else None
    if not item:
        return {
            "base_url": llm.base_url,
            "api_key": llm.api_key,
            "model": llm.model,
            "temperature": llm.temperature,
            # 保底放大到 4096：RAGAS 的评分输出不能被截断
            "max_tokens": max(llm.max_tokens, 4096),
        }, llm.model
    model = item.get("model") or item.get("name") or llm.model
    return {
        "base_url": item.get("base_url") or llm.base_url,
        "api_key": item.get("api_key") or llm.api_key,
        "model": model,
        "temperature": item.get("temperature", llm.temperature),
        "max_tokens": max(int(item.get("max_tokens") or llm.max_tokens), 4096),
    }, model


@router.post("/ragas/evaluations")
async def start_ragas_evaluation(body: RagasEvaluationRequest,
                                 request: Request,
                                 db: AsyncSession = Depends(get_db),
                                 user: UserPublic = Depends(get_current_user)):
    """从知识库发起 RAGAS 评估（super_admin 全量 / dept_admin 本部门库）

    两种样本来源：
    - samples（手动测试集）：用户填写问题 + 正确答案（=ground_truth，answer
      自动同写），传了 samples 时优先于自动采样；
    - 自动采样（兼容旧调用）：logs 检索日志 / chat 会话问答。
    统一流程：样本 → 知识库检索填 contexts → 上传 RAGAS 数据集 → 创建评估任务
    （use_retrieval=false，LLM 用知识库活跃配置覆盖）→ 本地元数据落盘。
    preview=true 时仅采样返回 {samples}，不发起评估（"从聊天历史导入"用）。
    返回 {task_id, kb_id, kb_name, sample_count}。
    """
    if user.role not in ("super_admin", "dept_admin"):
        raise HTTPException(status_code=403, detail="仅管理员可发起 RAGAS 评估")

    # 评估集模式：知识库以**评估集绑定的**为准——题集是为某个库写的，
    # 拿 A 库的题集去跑 B 库，得到的是没有意义的分数
    dataset = None
    if body.dataset_id:
        dataset = ragas_sampling.get_dataset(body.dataset_id)
        if not dataset:
            raise HTTPException(status_code=404, detail="评估集不存在")
    if dataset is not None:
        kb_ids = [dataset.get("kb_id") or body.kb_id]
    else:
        kb_ids = list(body.kb_ids or ([body.kb_id] if body.kb_id else []))
    if not kb_ids:
        raise HTTPException(status_code=422, detail="kb_id 与 kb_ids 至少传一个")
    if len(kb_ids) > MAX_EVAL_KBS:
        raise HTTPException(status_code=400,
                            detail=f"知识库数量需为 1~{MAX_EVAL_KBS} 个")
    kbs = [await kb_or_404(db, kid, user) for kid in kb_ids]
    kb = kbs[0]  # 主库：任务元数据的 kb_id/kb_name 取它（多库时 kb_name 拼接全名）

    if not 1 <= body.top_k <= MAX_TOP_K:
        raise HTTPException(status_code=400,
                            detail=f"检索 top_k 需在 1~{MAX_TOP_K} 之间")

    # preview 模式：仅采样返回样本列表（不校验 metrics，不发起评估）
    if body.preview:
        if not 1 <= body.sample_count <= MAX_SAMPLE_COUNT:
            raise HTTPException(status_code=400,
                                detail=f"样本数量需在 1~{MAX_SAMPLE_COUNT} 之间")
        if body.sample_source not in ("logs", "chat"):
            raise HTTPException(status_code=400, detail="样本来源仅支持 logs 或 chat")
        questions = await _sample_questions(kb.id, body.sample_source,
                                            body.sample_count)
        return {"samples": questions}

    # 指标校验（发起才需要；preview 分支已提前返回）
    metrics = list(dict.fromkeys(body.metrics or RAGAS_DEFAULT_METRICS))
    if not metrics:
        raise HTTPException(status_code=400, detail="至少选择一个评估指标")
    bad = [m for m in metrics if m not in RAGAS_METRICS]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"不支持的评估指标: {', '.join(bad)}")

    # 样本来源 0：本地评估集重跑（同一份题集反复跑，分数才可比；优先级最高）
    if dataset is not None:
        samples = [{"question": s.get("question", ""),
                    # 与其他来源口径一致：用户填的正确答案既是 answer 也是 ground_truth
                    "answer": s.get("ground_truth") or "",
                    "ground_truth": s.get("ground_truth") or ""}
                   for s in dataset.get("samples", [])]
        if not samples:
            raise HTTPException(status_code=400, detail="该评估集没有样本，无法重跑")
        source = "dataset"
    # 样本来源 1：手动测试集（samples 优先，忽略 sample_count/sample_source 采样）
    elif body.samples is not None:
        samples = _build_manual_samples(body.samples)
        source = "manual"
    # 样本来源 2：自动采样（兼容旧调用，不传 samples 时走原逻辑）
    else:
        if not 1 <= body.sample_count <= MAX_SAMPLE_COUNT:
            raise HTTPException(status_code=400,
                                detail=f"样本数量需在 1~{MAX_SAMPLE_COUNT} 之间")
        if body.sample_source not in ("logs", "chat"):
            raise HTTPException(status_code=400, detail="样本来源仅支持 logs 或 chat")
        samples = await _sample_questions(kb.id, body.sample_source,
                                          body.sample_count)
        source = body.sample_source

    # 回答来源校验（默认 dataset=用题集里的参考答案）
    if body.answer_source not in ("dataset", "generate"):
        raise HTTPException(status_code=400,
                            detail='回答来源仅支持 dataset 或 generate')

    # 样本准备：填 contexts；generate 模式还会额外用真实链路生成 answer
    gen_session_ids: List[str] = []
    if body.answer_source == "generate":
        # 实时生成：走生产链路（检索→图谱→改写→组装 prompt→生成），
        # answer 是模型真实输出、contexts 与它同一次检索——评的是端到端质量。
        # 代价：每条样本一次完整问答，比只检索慢得多
        gen_session_ids = await ragas_sampling.generate_answers(
            kb_ids, samples, body.top_k, user.id)
    else:
        # 知识库检索填 contexts（无命中保留空列表，检索异常不阻断）
        await ragas_sampling.fill_contexts(kb_ids, samples, body.top_k)

    # 上传数据集 + 创建评估任务（评委 LLM 用本系统配置覆盖 RAGAS 默认 judge）
    name = f"{kb.name}-RAGAS评估-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    # 评委模型：请求指定了就用它，否则用当前激活模型（现状行为）
    eval_llm, eval_model = _build_eval_llm_cfg(body.llm_model)
    try:
        dataset_id = await get_ragas_client().upload_dataset(
            samples, name, description=f"知识库发起（{SOURCE_LABELS[source]}来源，"
                                       f"{len(samples)} 条样本，top_k={body.top_k}）")
        task = await get_ragas_client().create_evaluation(
            dataset_id, metrics,
            llm_cfg=eval_llm,
            name=name, top_k=body.top_k)
        task_id = task.get("id") if isinstance(task, dict) else None
        if not task_id:
            raise RagasApiError("RAGAS 创建评估任务失败：响应缺少任务 ID")
    except RagasApiError as e:
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        # 实时生成的副产物：那些会话文件不该留在用户的会话列表里（跑一批样本就是
        # 几十个会话）。放线程池——删除是同步文件读写，条数多时会堵事件循环。
        # 放在 finally：上传/创建任务失败也要清，否则会漏在用户列表里
        if gen_session_ids:
            removed = await asyncio.to_thread(
                ragas_sampling.cleanup_sessions, gen_session_ids)
            logger.info("评估实时生成产生的会话已清理: %d/%d",
                        removed, len(gen_session_ids))

    # 本地元数据落盘（任务列表关联 kb_name/发起来源用；user_id 供取消任务
    # 权限校验：发起人本人/super_admin/dept_admin 本部门可取消，旧任务无此字段）
    ragas_sampling.append_task_meta({
        "task_id": task_id,
        "kb_id": kb.id,
        "kb_ids": kb_ids,
        # 多库评估时把库名拼起来展示（单库就是它自己）
        "kb_name": "、".join(k.name for k in kbs),
        # dataset_id：本次在 RAGAS 服务端新建的数据集（一次一换，仅用于溯源）
        # local_dataset_id：本地评估集（可反复重跑，分数对比按它分组）
        "dataset_id": dataset_id,
        "local_dataset_id": dataset.get("id") if dataset else None,
        "name": name,
        "source": source,
        "sample_count": len(samples),
        # 本次用的**评委**模型：换评委分数不可比，前端对比时按它分组
        "eval_model": eval_model,
        # answer 来源：dataset=题集参考答案（测检索）/ generate=实时生成（测端到端）
        "answer_source": body.answer_source,
        "user_id": user.id,
        "created_at": ragas_sampling.now_iso(),
    })
    logger.info("RAGAS 评估发起: task=%s kb=%s source=%s samples=%d metrics=%s",
                task_id, kb.name, source, len(samples), metrics)
    # 审计埋点（preview 模式已提前返回，不落评估记录）
    await audit_service.record_action(
        user, action="ragas.evaluate", target_type="kb",
        target_id=kb.id, target_name=kb.name,
        detail={"task_id": task_id, "kb_name": kb.name,
                "sample_count": len(samples), "source": source,
                "metrics": metrics, "top_k": body.top_k},
        request=request)
    return {
        "task_id": task_id,
        "kb_id": kb.id,
        "kb_ids": kb_ids,
        "kb_name": "、".join(k.name for k in kbs),
        "sample_count": len(samples),
        "dataset_id": dataset_id,
        "name": name,
    }


@router.get("/ragas/tasks/{task_id}")
async def ragas_report(task_id: str, user: UserPublic = Depends(get_current_user)):
    """RAGAS 任务报告（aggregate.scores + 逐样本 results）

    顺带把分数回写本地任务元数据：轮询里的自动补写每轮限量，这里"用户点开就落一份"，
    两条路径互补，保证看过的任务一定有本地快照可供后续对比。
    """
    result = await get_ragas_client().get_report(task_id)
    if not result["available"] or not result["report"]:
        raise HTTPException(status_code=502, detail=result["message"])
    report = result["report"]
    scores = (report.get("aggregate") or {}).get("scores") or {}
    if scores:
        ragas_sampling.update_task_meta(task_id, {
            "scores": scores,
            "completed_at": report.get("completed_at"),
        })
    return report


@router.post("/ragas/evaluations/{task_id}/cancel")
async def cancel_ragas_evaluation(task_id: str,
                                  db: AsyncSession = Depends(get_db),
                                  user: UserPublic = Depends(get_current_user)):
    """取消 RAGAS 评估任务（仅 running/queued 状态可取消，RAGAS 侧兜底）

    权限（无权一律 404 伪装，防任务存在性探测；user 角色整体 403，
    与发起评估权限口径一致）：
    - 元数据含 user_id：发起人本人 / super_admin / dept_admin（发起人同部门）
      可取消；
    - 旧任务（无 user_id）：仅 super_admin 可取消；
    - 任务不在本地元数据（非本系统发起）→ 404。
    RAGAS 错误透传中文提示：任务不存在 → 404；已完成取消被拒/服务不可达 → 400。
    返回 {"message": "评估任务已取消"}。
    """
    if user.role == "user":
        raise HTTPException(status_code=403,
                            detail="仅管理员或任务发起人可取消 RAGAS 评估任务")

    meta = next((t for t in ragas_sampling.load_task_meta()
                 if t.get("task_id") == task_id), None)
    if not meta:
        raise HTTPException(status_code=404, detail="评估任务不存在")

    allowed = user.role == "super_admin"
    if meta.get("user_id"):
        allowed |= meta["user_id"] == user.id
        if not allowed and user.role == "dept_admin":
            # dept_admin 可取消本部门用户发起的任务（查发起人部门）
            owner = await db.get(UserORM, meta["user_id"])
            allowed = bool(owner and owner.department_id
                           and owner.department_id == user.department_id)
    # 无 user_id 的旧任务：allowed 维持仅 super_admin
    if not allowed:
        raise HTTPException(status_code=404, detail="评估任务不存在")

    try:
        await get_ragas_client().cancel_task(task_id)
    except RagasApiError as e:
        logger.info("RAGAS 取消任务 %s 失败: %s", task_id, e)
        if e.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="评估任务不存在（RAGAS 侧已无此任务，可能已被清理）")
        # 服务不可达 / RAGAS 业务拒绝（如任务已完成无法取消）→ 透传中文提示
        raise HTTPException(status_code=400, detail=f"取消评估任务失败：{e}")
    logger.info("RAGAS 评估任务已取消: task=%s user=%s", task_id, user.id)
    return {"message": "评估任务已取消"}


# ---- 发起前可用性探测（LLM / Embedding；探测逻辑统一在 services/parsers/probes.py） ----

# 探测超时（与设置页连接测试一致；发起时 llm/embedding 并行探测，总耗时 ≤ 5s）
PRECHECK_TIMEOUT = 5.0


# ==================== 本地评估集（可复用题集） ====================
#
# 评估集 = 一份固定的 question + ground_truth 题集，反复重跑同一份题集，分数才可比。
# 不存 contexts：评估时实时检索填充，检索链路的改动才能反映到分数上。

def _can_manage_ragas(user: UserPublic) -> bool:
    """评估集管理权限：与发起评估同口径（仅管理员）"""
    return user.role in ("super_admin", "dept_admin")


@router.get("/ragas/datasets")
async def list_ragas_datasets(kb_id: Optional[str] = None,
                              user: UserPublic = Depends(get_current_user)):
    """本地评估集列表（kb_id 可选过滤），按更新时间倒序

    返回每条含全量 samples（评估集规模有 100 条上限，前端要展示样本明细）。
    """
    items = ragas_sampling.load_datasets()
    if kb_id:
        items = [d for d in items if d.get("kb_id") == kb_id]
    return {"datasets": sorted(items, key=lambda d: d.get("updated_at", ""),
                               reverse=True)}


@router.post("/ragas/datasets")
async def create_ragas_dataset(body: RagasDatasetCreateRequest,
                               request: Request,
                               db: AsyncSession = Depends(get_db),
                               user: UserPublic = Depends(get_current_user)):
    """新建本地评估集（把一份题集沉淀下来，之后可反复重跑对比分数）"""
    if not _can_manage_ragas(user):
        raise HTTPException(status_code=403, detail="仅管理员可管理评估集")
    name = (body.name or "").strip()
    if not name or len(name) > 50:
        raise HTTPException(status_code=400, detail="评估集名称需为 1~50 字")
    if not 1 <= len(body.samples) <= MAX_SAMPLE_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"样本数量需在 1~{MAX_SAMPLE_COUNT} 条之间")
    kb = await kb_or_404(db, body.kb_id, user)
    ds = ragas_sampling.create_dataset(
        name=name, kb_id=kb.id, kb_name=kb.name,
        samples=[{"question": s.question, "ground_truth": s.ground_truth or ""}
                 for s in body.samples],
        source=(body.source or "manual").strip(), user_id=user.id)
    await audit_service.record_action(
        user, action="ragas.dataset_create", target_type="kb",
        target_id=kb.id, target_name=kb.name,
        detail={"dataset_id": ds["id"], "name": name,
                "sample_count": len(ds["samples"])},
        request=request)
    return ds


@router.delete("/ragas/datasets/{dataset_id}")
async def delete_ragas_dataset(dataset_id: str, request: Request,
                               user: UserPublic = Depends(get_current_user)):
    """删除本地评估集（已跑过的任务记录不受影响，仍可在列表里回看）"""
    if not _can_manage_ragas(user):
        raise HTTPException(status_code=403, detail="仅管理员可管理评估集")
    ds = ragas_sampling.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="评估集不存在")
    ragas_sampling.delete_dataset(dataset_id)
    await audit_service.record_action(
        user, action="ragas.dataset_delete", target_type="kb",
        target_id=ds.get("kb_id", ""), target_name=ds.get("kb_name", ""),
        detail={"dataset_id": dataset_id, "name": ds.get("name")},
        request=request)
    return {"message": "评估集已删除", "dataset_id": dataset_id}


@router.post("/ragas/datasets/{dataset_id}/samples")
async def add_ragas_dataset_samples(dataset_id: str,
                                    body: RagasDatasetAddSamplesRequest,
                                    user: UserPublic = Depends(get_current_user)):
    """往评估集追加样本（按 question 去重）

    反馈页「加入评估集」用：把点踩的问答沉淀成回归用例，让踩过的坑不再复发。
    返回 {added, skipped, total}——question 重复的计入 skipped。
    """
    if not _can_manage_ragas(user):
        raise HTTPException(status_code=403, detail="仅管理员可管理评估集")
    if not 1 <= len(body.samples) <= MAX_SAMPLE_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"样本数量需在 1~{MAX_SAMPLE_COUNT} 条之间")
    try:
        return ragas_sampling.add_samples(
            dataset_id,
            [{"question": s.question, "ground_truth": s.ground_truth or ""}
             for s in body.samples])
    except KeyError:
        raise HTTPException(status_code=404, detail="评估集不存在")


def _probe_to_available(r: dict) -> dict:
    """parsers.probes 结果 {ok, latency_ms, reason} → 对外 {available, reason}"""
    return {"available": r["ok"], "reason": "" if r["ok"] else r["reason"]}


async def _probe_llm(cfg) -> dict:
    """LLM 轻量探测（薄包装：parsers.probes.probe_llm，GET {base_url}/models 5s 超时）"""
    return _probe_to_available(await probe_llm(cfg, timeout=PRECHECK_TIMEOUT))


async def _probe_embedding(cfg) -> dict:
    """Embedding 轻量探测（薄包装：parsers.probes.probe_embedding，
    POST {base_url}/embeddings 一条测试文本 5s 超时）"""
    return _probe_to_available(await probe_embedding(cfg, timeout=PRECHECK_TIMEOUT))


@router.get("/ragas/precheck")
async def ragas_precheck(user: UserPublic = Depends(get_current_user)):
    """发起评估前探测 LLM / Embedding 可用性（登录即可，5s 超时并行）

    检测发起评估实际使用的配置：全局活跃配置的 LLM（RAGAS judge 评分模型，
    发起时 stats.py 用 get_active_config().llm 覆盖 RAGAS 侧 judge）与
    Embedding（检索填 contexts 用）；任一端不可用 → {available: false,
    reason: 中文原因}，前端阻止发起。探测失败不抛异常。
    返回 {llm: {available, reason}, embedding: {available, reason}}。
    """
    cfg = get_active_config()
    llm, embedding = await asyncio.gather(
        _probe_llm(cfg.llm),
        _probe_embedding(cfg.embedding),
    )
    if not llm["available"]:
        logger.info("RAGAS precheck LLM 不可用: %s", llm["reason"])
    if not embedding["available"]:
        logger.info("RAGAS precheck Embedding 不可用: %s", embedding["reason"])
    return {"llm": llm, "embedding": embedding}


class FeedbackScope:
    """反馈数据可见范围（当前恒为全量；预留给部门管理员接入）"""

    def __init__(self, all_departments: bool = True,
                 department_id: Optional[str] = None):
        self.all_departments = all_departments
        self.department_id = department_id


async def feedback_viewer_scope(
        user: UserPublic = Depends(get_current_user)) -> FeedbackScope:
    """反馈数据查看权限（依赖注入点，两个反馈接口共用）

    当前仅 super_admin 可看全量；其余角色 404 伪装（与 require_super_admin
    口径一致：不暴露接口存在性与权限边界）。
    ---- 后续接入部门管理员只改本函数 ----
    增加分支 user.role == "dept_admin" → 返回 FeedbackScope(
        all_departments=False, department_id=user.department_id)：路由内已按
    scope 组织过滤条件（all_departments 为 False 时自动收窄到该部门），
    路由签名与前端均无需改动。
    """
    if user.role != "super_admin":
        raise HTTPException(status_code=404, detail="资源不存在")
    return FeedbackScope()


def _day_bound(value: Optional[str], end: bool) -> Optional[str]:
    """日期参数归一：纯日期补足时分秒，带时间原样返回；空串 → None

    created_at 是 "%Y-%m-%d %H:%M:%S" 字符串，按字符串比较时
    "2026-09-11 08:00:00" <= "2026-09-11" 为 False（同前缀下更长的更大），
    date_to 不补 23:59:59 会把当天的反馈整体漏掉。
    """
    v = (value or "").strip()
    if len(v) == 10 and v.count("-") == 2:
        return v + (" 23:59:59" if end else " 00:00:00")
    return v or None


@router.get("/chat-feedback")
async def chat_feedback_stats(
        scope: FeedbackScope = Depends(feedback_viewer_scope)):
    """用户回答反馈汇总（仅超管）：总数/好评/差评 + 最近反馈"""
    from backend.services.feedback_service import feedback_stats
    return await feedback_stats()


@router.get("/chat-feedback/logs")
async def chat_feedback_logs(
        page: int = Query(1, ge=1, description="页码（从 1 开始）"),
        page_size: int = Query(20, ge=1, le=200, description="每页条数（1~200）"),
        rating: Optional[str] = Query(None, description="评价过滤（up/down），缺省全部"),
        username: Optional[str] = Query(None, description="用户名模糊搜索"),
        department_id: Optional[str] = Query(None, description="部门 ID 过滤"),
        keyword: Optional[str] = Query(None, description="关键词（模糊匹配点踩原因）"),
        date_from: Optional[str] = Query(None, description="起始日期（YYYY-MM-DD）"),
        date_to: Optional[str] = Query(None, description="结束日期（YYYY-MM-DD，含当天）"),
        db: AsyncSession = Depends(get_db),
        scope: FeedbackScope = Depends(feedback_viewer_scope)):
    """聊天反馈分页查询（仅超管；鉴权口径与上方 chat-feedback 汇总接口一致）

    - rating/username/department_id/keyword/date_from/date_to 全可选，多条件
      AND；rating 非法值 400 显式提示（便于前端拼写/大小写错误暴露），其余
      条件为空白串时按未传处理；page>=1、page_size 1~200（默认 20）；按
      created_at 倒序（同秒时按 id 倒序稳定排序，避免同秒提交顺序抖动）
    - 用户名/部门条件走 **outer join**：inner join 会让"用户已注销但反馈仍在"
      的记录在按用户名/部门筛选时凭空消失（展示侧本就对缺失回填空字符串）
    - username/display_name/department_name 查用户与部门表回填（用户已删除/
      无部门 → 空字符串）；kb_name 查知识库表（kb 已删除/从未入库 → 回退 kb_id）
    响应契约: {total, page, page_size, items: [...]}。
    """
    # rating 过滤条件（与反馈提交口径一致仅 up/down；非法值 400 而非静默
    # 空页，便于前端拼写/大小写错误显性暴露）
    conditions = []
    if rating is not None:
        if rating not in ("up", "down"):
            raise HTTPException(status_code=400,
                                detail="rating 仅支持 up（好评）/down（差评）")
        conditions.append(FeedbackORM.rating == rating)
    if username and username.strip():
        conditions.append(UserORM.username.like(f"%{username.strip()}%"))
    if department_id:
        conditions.append(UserORM.department_id == department_id)
    if keyword and keyword.strip():
        conditions.append(FeedbackORM.reason.like(f"%{keyword.strip()}%"))
    d_from = _day_bound(date_from, end=False)
    d_to = _day_bound(date_to, end=True)
    if d_from:
        conditions.append(FeedbackORM.created_at >= d_from)
    if d_to:
        conditions.append(FeedbackORM.created_at <= d_to)
    # 预留：部门管理员只看本部门（当前 scope 恒为全量，不会进该分支；接入
    # dept_admin 时由 feedback_viewer_scope 返回收窄的 scope 即自动生效）
    if not scope.all_departments:
        conditions.append(UserORM.department_id == scope.department_id)

    # outer join：用户注销后反馈记录仍要能展示与筛选（见 docstring）
    on_clause = FeedbackORM.user_id == UserORM.id
    total = (await db.execute(
        select(func.count()).select_from(FeedbackORM)
        .outerjoin(UserORM, on_clause).where(*conditions)
    )).scalar() or 0

    rows = (await db.execute(
        select(FeedbackORM).outerjoin(UserORM, on_clause).where(*conditions)
        .order_by(FeedbackORM.created_at.desc(), FeedbackORM.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    # 用户名映射（users 表；用户已删除 → 空字符串回退）
    user_ids = {r.user_id for r in rows}
    users = {}
    if user_ids:
        users = {u.id: u for u in (
            await db.execute(select(UserORM).where(UserORM.id.in_(user_ids)))
        ).scalars()}
    # 部门名映射（departments 表；用户无部门/部门已删除 → 空字符串）
    dept_ids = {u.department_id for u in users.values() if u.department_id}
    dept_names: Dict[str, str] = {}
    if dept_ids:
        dept_names = {d.id: d.name for d in (
            await db.execute(
                select(DepartmentORM).where(DepartmentORM.id.in_(dept_ids)))
        ).scalars()}
    # 知识库名映射（kbs 表；批量一次查询防 N+1，缺失回退 kb_id）
    kb_ids = {r.kb_id for r in rows if r.kb_id}
    kb_names: Dict[str, str] = {}
    if kb_ids:
        kb_names = {kb.id: kb.name for kb in (
            await db.execute(select(KBORM).where(KBORM.id.in_(kb_ids)))
        ).scalars()}

    items = []
    for r in rows:
        u = users.get(r.user_id)
        dept_id = u.department_id if u else None
        items.append({
            "id": r.id,
            "user_id": r.user_id,
            "username": u.username if u else "",
            "display_name": u.display_name if u else "",
            "department_id": dept_id,
            "department_name": dept_names.get(dept_id, "") if dept_id else "",
            "kb_id": r.kb_id,
            # kb 不存在回退 kb_id（kb_id 为 None 时同样回退 None，原样返回）
            "kb_name": kb_names.get(r.kb_id, r.kb_id),
            "session_id": r.session_id,
            "msg_idx": r.msg_idx,
            "rating": r.rating,
            "reason": r.reason or "",
            "created_at": r.created_at,
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items}
