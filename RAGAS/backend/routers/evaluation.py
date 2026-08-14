"""评估任务 API"""
from __future__ import annotations
import logging
from fastapi import APIRouter, HTTPException
from typing import List

from backend.models.eval_models import (
    EvalConfig, EvalTask, EvalTaskListItem, EvalResults, EvalStatus,
)
from backend.services.eval_service import get_eval_service
from backend.evaluation.metrics import get_all_metrics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/evaluations", tags=["评估"])


@router.get("/metrics")
def list_metrics():
    """获取所有可用的评估指标"""
    return get_all_metrics()


@router.post("", response_model=EvalTask)
async def create_evaluation(config: EvalConfig):
    """创建评估任务"""
    try:
        task = await get_eval_service().create_task(config)
        return task
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("创建评估失败")
        raise HTTPException(status_code=500, detail="创建评估失败，请检查后端日志")


@router.get("", response_model=List[EvalTaskListItem])
def list_evaluations():
    """获取评估任务列表"""
    return get_eval_service().list_tasks()


@router.get("/{task_id}", response_model=EvalTask)
def get_evaluation(task_id: str):
    """获取评估任务状态"""
    task = get_eval_service().get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="评估任务不存在")
    return task


@router.get("/{task_id}/logs")
def get_evaluation_logs(task_id: str, since: int = 0):
    """获取评估任务的实时日志"""
    task = get_eval_service().get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="评估任务不存在")
    logs = get_eval_service().get_logs(task_id, since=since)
    return {"task_id": task_id, "logs": logs, "total": len(logs)}


@router.post("/{task_id}/cancel")
def cancel_evaluation(task_id: str):
    """取消正在运行的评估任务"""
    ok = get_eval_service().cancel_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不在运行状态或不存在")
    return {"message": "正在停止评估..."}


@router.get("/{task_id}/results", response_model=EvalResults)
def get_evaluation_results(task_id: str):
    """获取评估结果"""
    task = get_eval_service().get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="评估任务不存在")
    if task.status != EvalStatus.COMPLETED:
        raise HTTPException(status_code=400, detail=f"评估尚未完成，当前状态: {task.status.value}")

    results = get_eval_service().get_results(task_id)
    if not results:
        raise HTTPException(status_code=404, detail="结果未找到")
    return results


@router.delete("/{task_id}")
def delete_evaluation(task_id: str):
    """删除评估任务"""
    ok = get_eval_service().delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或正在运行中")
    return {"message": "已删除"}
