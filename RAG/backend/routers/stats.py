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
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_active_config
from backend.db import get_db
from backend.deps import get_current_user, kb_or_404
from backend.models.user_models import UserORM, UserPublic
from backend.services import audit_service, ragas_sampling
from backend.services.chat_service import get_chat_service
from backend.services.document_service import get_document_service
from backend.services.kb_service import get_kb_service
from backend.services.probes import probe_embedding, probe_llm
from backend.services.ragas_client import RagasApiError, get_ragas_client
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
MAX_TOP_K = 20
# 发起来源（数据集描述 / 本地元数据 source 用）
SOURCE_LABELS = {
    "logs": "检索日志",
    "chat": "会话问答",
    "manual": "手动填写",
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
    kb_id: str = Field(..., description="知识库 ID")
    metrics: Optional[List[str]] = Field(None, description="评估指标（默认取需要 "
                                         "ground_truth 的 3 个）")
    sample_count: int = Field(20, description="自动采样样本数（1~100；samples 模式忽略）")
    sample_source: str = Field("logs", description='样本来源："logs"=检索日志真实问题'
                                '（无答案）；"chat"=会话问答（问题+答案）；'
                                '仅自动采样/preview 模式使用，samples 模式忽略')
    top_k: int = Field(3, description="检索上下文 top_k（1~20）")
    samples: Optional[List[RagasSampleInput]] = Field(
        None, description="手动测试集（1~100 条）；传了 samples 时优先于自动采样")
    preview: bool = Field(False, description="预览模式：仅采样返回样本列表，不发起评估")


@router.get("/ragas")
async def ragas_status(user: UserPublic = Depends(get_current_user)):
    """探测 RAGAS 8090：3s 超时，失败返回 {available:false}（自身统计不受影响）

    可用时合并本地发起任务元数据（kb_name/发起来源/样本数），供前端展示归属。
    """
    result = await get_ragas_client().probe()
    if result.get("available"):
        meta_by_task = {t["task_id"]: t for t in ragas_sampling.load_task_meta()}
        for t in result.get("tasks", []):
            meta = meta_by_task.get(t.get("id"))
            if meta:
                t["kb_name"] = meta.get("kb_name")
                t["source"] = meta.get("source")
                t["sample_count"] = meta.get("sample_count")
                # 发起人 user_id（前端取消按钮按当前用户比对显隐；旧任务无此字段）
                if meta.get("user_id"):
                    t["user_id"] = meta["user_id"]
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

    kb = await kb_or_404(db, body.kb_id, user)

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

    # 样本来源 1：手动测试集（samples 优先，忽略 sample_count/sample_source 采样）
    if body.samples is not None:
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

    # 知识库检索填 contexts（无命中保留空列表，检索异常不阻断）
    await ragas_sampling.fill_contexts(kb.id, samples, body.top_k)

    # 上传数据集 + 创建评估任务（LLM 用知识库活跃配置覆盖 RAGAS judge）
    name = f"{kb.name}-RAGAS评估-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    llm = get_active_config().llm
    # RAGAS 侧对 DeepSeek 推理模型已关闭思考（extra_body thinking.disabled），
    # max_tokens 全部用于评分输出；此处再保底放大到 4096，防御性兜底（不截断）。
    eval_max_tokens = max(llm.max_tokens, 4096)
    try:
        dataset_id = await get_ragas_client().upload_dataset(
            samples, name, description=f"知识库发起（{SOURCE_LABELS[source]}来源，"
                                       f"{len(samples)} 条样本，top_k={body.top_k}）")
        task = await get_ragas_client().create_evaluation(
            dataset_id, metrics,
            llm_cfg={
                "base_url": llm.base_url,
                "api_key": llm.api_key,
                "model": llm.model,
                "temperature": llm.temperature,
                "max_tokens": eval_max_tokens,
            },
            name=name, top_k=body.top_k)
        task_id = task.get("id") if isinstance(task, dict) else None
        if not task_id:
            raise RagasApiError("RAGAS 创建评估任务失败：响应缺少任务 ID")
    except RagasApiError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # 本地元数据落盘（任务列表关联 kb_name/发起来源用；user_id 供取消任务
    # 权限校验：发起人本人/super_admin/dept_admin 本部门可取消，旧任务无此字段）
    ragas_sampling.append_task_meta({
        "task_id": task_id,
        "kb_id": kb.id,
        "kb_name": kb.name,
        "dataset_id": dataset_id,
        "name": name,
        "source": source,
        "sample_count": len(samples),
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
        "kb_name": kb.name,
        "sample_count": len(samples),
        "dataset_id": dataset_id,
        "name": name,
    }


@router.get("/ragas/tasks/{task_id}")
async def ragas_report(task_id: str, user: UserPublic = Depends(get_current_user)):
    """RAGAS 任务报告（aggregate.scores + 逐样本 results）"""
    result = await get_ragas_client().get_report(task_id)
    if not result["available"] or not result["report"]:
        raise HTTPException(status_code=502, detail=result["message"])
    return result["report"]


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


# ---- 发起前可用性探测（LLM / Embedding；探测逻辑统一在 services/probes.py） ----

# 探测超时（与设置页连接测试一致；发起时 llm/embedding 并行探测，总耗时 ≤ 5s）
PRECHECK_TIMEOUT = 5.0


def _probe_to_available(r: dict) -> dict:
    """probes 结果 {ok, latency_ms, reason} → 对外 {available, reason}"""
    return {"available": r["ok"], "reason": "" if r["ok"] else r["reason"]}


async def _probe_llm(cfg) -> dict:
    """LLM 轻量探测（薄包装：probes.probe_llm，GET {base_url}/models 5s 超时）"""
    return _probe_to_available(await probe_llm(cfg, timeout=PRECHECK_TIMEOUT))


async def _probe_embedding(cfg) -> dict:
    """Embedding 轻量探测（薄包装：probes.probe_embedding，
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
