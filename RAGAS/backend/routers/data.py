"""数据集管理 API"""
from __future__ import annotations
import logging
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import List

from backend.models.data_models import (
    DatasetListItem, DatasetUploadResponse, DatasetPreview,
    CreateDatasetRequest, AddSampleRequest, UpdateSampleRequest, EvalExample,
)
from backend.services.data_service import get_data_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/datasets", tags=["数据集"])


@router.post("/upload", response_model=DatasetUploadResponse)
async def upload_dataset(
    file: UploadFile = File(...),
    name: str = Form(""),
    description: str = Form(""),
):
    """上传数据集文件（CSV / JSON / Excel）"""
    try:
        ds = await get_data_service().upload(file, name=name, description=description)
        return DatasetUploadResponse(
            id=ds.id,
            name=ds.name,
            row_count=ds.row_count,
            columns=ds.columns,
            message=f"上传成功，共 {ds.row_count} 条记录",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("上传失败")
        raise HTTPException(status_code=500, detail="上传失败，请检查文件格式或联系管理员")


@router.get("", response_model=List[DatasetListItem])
def list_datasets():
    """获取数据集列表"""
    return get_data_service().list()


@router.get("/{dataset_id}", response_model=DatasetPreview)
def get_dataset(dataset_id: str, limit: int = 50, offset: int = 0):
    """获取数据集详情及样本预览"""
    ds = get_data_service().get(dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="数据集不存在")
    samples = get_data_service().get_samples(dataset_id, limit=limit, offset=offset)
    return DatasetPreview(
        id=ds.id,
        columns=ds.columns,
        rows=[s.model_dump(mode="json") for s in samples],
        total=ds.row_count,
    )


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: str):
    """删除数据集"""
    ok = get_data_service().delete(dataset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return {"message": "已删除"}


@router.post("/create", response_model=DatasetUploadResponse)
def create_dataset(body: CreateDatasetRequest):
    """创建空数据集"""
    try:
        ds = get_data_service().create(name=body.name, description=body.description)
        return DatasetUploadResponse(
            id=ds.id,
            name=ds.name,
            row_count=0,
            columns=ds.columns,
            message=f"数据集 '{ds.name}' 创建成功",
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{dataset_id}/samples")
def add_sample(dataset_id: str, body: AddSampleRequest):
    """添加一条样本"""
    try:
        sample = EvalExample(
            question=body.question,
            answer=body.answer,
            contexts=body.contexts,
            ground_truth=body.ground_truth,
        )
        ds = get_data_service().add_sample(dataset_id, sample)
        return {
            "index": len(ds.samples) - 1,
            "row_count": ds.row_count,
            "message": f"已添加第 {len(ds.samples)} 条样本",
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{dataset_id}/samples/{idx}")
def update_sample(dataset_id: str, idx: int, body: UpdateSampleRequest):
    """更新指定索引的样本"""
    try:
        svc = get_data_service()
        ds = svc.get(dataset_id)
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")
        old = ds.samples[idx]
        sample = EvalExample(
            question=body.question if body.question is not None else old.question,
            answer=body.answer if body.answer is not None else old.answer,
            contexts=body.contexts if body.contexts is not None else old.contexts,
            ground_truth=body.ground_truth if body.ground_truth is not None else old.ground_truth,
        )
        svc.update_sample(dataset_id, idx, sample)
        return {"message": f"第 {idx + 1} 条样本已更新", "index": idx}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{dataset_id}/samples/{idx}")
def delete_sample(dataset_id: str, idx: int):
    """删除指定索引的样本"""
    try:
        svc = get_data_service()
        svc.delete_sample(dataset_id, idx)
        return {"message": f"第 {idx + 1} 条样本已删除"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
