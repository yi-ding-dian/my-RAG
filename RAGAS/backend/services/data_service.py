"""数据集管理服务"""
from __future__ import annotations
import json
import csv
import uuid
import logging
import os
from datetime import datetime
from typing import List, Optional
from pathlib import Path

import pandas as pd
import numpy as np
import threading
from fastapi import UploadFile

from backend.config import settings
from backend.models.data_models import EvalExample, EvalDataset, DatasetListItem

logger = logging.getLogger(__name__)


class DataService:

    def __init__(self):
        self._lock = threading.Lock()
        self._datasets: dict[str, EvalDataset] = {}
        self._load_all()

    def _get_meta_path(self, dataset_id: str) -> Path:
        return settings.DATASETS_DIR / f"{dataset_id}.json"

    def _load_all(self):
        """从磁盘加载所有数据集元数据"""
        if not settings.DATASETS_DIR.exists():
            return
        for f in settings.DATASETS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                ds = EvalDataset(**data)
                self._datasets[ds.id] = ds
            except Exception as e:
                logger.warning("加载数据集 %s 失败: %s", f.name, e)

    def _save_meta(self, ds: EvalDataset):
        """保存数据集元数据"""
        self._get_meta_path(ds.id).write_text(
            json.dumps(ds.model_dump(mode="json"), ensure_ascii=False, indent=2)
        )

    async def upload(self, file: UploadFile, name: str = "", description: str = "") -> EvalDataset:
        """上传并解析数据集文件"""
        dataset_id = str(uuid.uuid4())[:8]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 保存原始文件
        ext = os.path.splitext(file.filename or "unknown")[1] or ".csv"
        save_path = settings.UPLOAD_DIR / f"{dataset_id}{ext}"
        max_bytes = 100 * 1024 * 1024
        content = b""
        while chunk := await file.read(8 * 1024 * 1024):
            content += chunk
            if len(content) > max_bytes:
                raise ValueError("文件大小超过限制（最大 100MB）")
        save_path.write_bytes(content)

        # 解析为 DataFrame
        if ext.lower() in (".csv", ".tsv"):
            sep = "\t" if ext.lower() == ".tsv" else ","
            df = pd.read_csv(save_path, sep=sep)
        elif ext.lower() in (".xlsx", ".xls"):
            df = pd.read_excel(save_path)
        elif ext.lower() == ".json":
            try:
                df = pd.read_json(save_path)
            except ValueError:
                # pandas 直接读取失败时，用 json.load 手动解析
                with open(save_path, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    df = pd.DataFrame(raw)
                elif isinstance(raw, dict):
                    # 支持嵌套格式: { "samples": [{...}, ...] } 或列式对象
                    if "samples" in raw and isinstance(raw["samples"], list):
                        df = pd.DataFrame(raw["samples"])
                    else:
                        df = pd.DataFrame(raw)
                else:
                    raise ValueError("JSON 格式不支持: 请使用对象数组 [{...}, ...] 或列式对象 {...}")
        else:
            raise ValueError(f"不支持的文件格式: {ext}")

        # 列名校验
        required = {"question"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"缺少必要列: {missing}，当前列: {list(df.columns)}")

        def _isna(v):
            """pd.isna 的安全版，避免 numpy 数组引发 The truth value 错误"""
            if isinstance(v, np.ndarray):
                return v.size == 0 or bool(v.size > 0 and pd.isna(v).all())
            if isinstance(v, (list, tuple)):
                return len(v) == 0
            try:
                return bool(pd.isna(v))
            except (ValueError, TypeError):
                return False

        # 转换为 EvalExample
        samples = []
        for _, row in df.iterrows():
            contexts = row.get("contexts", row.get("context", ""))
            if isinstance(contexts, np.ndarray):
                contexts = contexts.tolist()
            if isinstance(contexts, list):
                pass  # 已经是列表
            elif isinstance(contexts, str) and contexts:
                try:
                    contexts = json.loads(contexts) if contexts.startswith("[") else [contexts]
                except json.JSONDecodeError:
                    contexts = [contexts]
            elif _isna(contexts):
                contexts = []
            else:
                contexts = [str(contexts)]

            gt = row.get("ground_truth", row.get("ground_truths", None))
            if isinstance(gt, np.ndarray):
                gt = gt.tolist()
            if isinstance(gt, list):
                gt = gt[0] if len(gt) > 0 else None
            elif _isna(gt):
                gt = None

            samples.append(EvalExample(
                question=str(row.get("question", "")),
                answer=str(row.get("answer", row.get("response", ""))),
                contexts=list(contexts) if isinstance(contexts, list) else [str(contexts)],
                ground_truth=str(gt) if gt else None,
            ))

        ds = EvalDataset(
            id=dataset_id,
            name=name or (file.filename or "未命名"),
            description=description,
            file_name=file.filename or "",
            row_count=len(samples),
            columns=list(df.columns),
            samples=samples,
            created_at=now,
        )

        self._datasets[dataset_id] = ds
        self._save_meta(ds)
        logger.info("数据集已上传: %s (%d 条)", ds.name, ds.row_count)
        return ds

    def list(self) -> List[DatasetListItem]:
        """获取数据集列表"""
        with self._lock:
            datasets = list(self._datasets.values())
        return [
            DatasetListItem(
                id=ds.id,
                name=ds.name,
                description=ds.description,
                file_name=ds.file_name,
                row_count=ds.row_count,
                columns=ds.columns,
                created_at=ds.created_at,
            )
            for ds in sorted(datasets, key=lambda x: x.created_at, reverse=True)
        ]

    def get(self, dataset_id: str) -> Optional[EvalDataset]:
        with self._lock:
            return self._datasets.get(dataset_id)

    def get_samples(self, dataset_id: str, limit: int = 50, offset: int = 0) -> List[EvalExample]:
        ds = self.get(dataset_id)
        if not ds:
            return []
        return ds.samples[offset:offset + limit]

    def delete(self, dataset_id: str) -> bool:
        with self._lock:
            ds = self._datasets.pop(dataset_id, None)
        if not ds:
            return False
        meta_path = self._get_meta_path(dataset_id)
        if meta_path.exists():
            meta_path.unlink()
        return True

    def create(self, name: str, description: str = "") -> EvalDataset:
        """创建一个空数据集"""
        dataset_id = str(uuid.uuid4())[:8]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ds = EvalDataset(
            id=dataset_id,
            name=name,
            description=description,
            file_name=f"{name}.json",
            row_count=0,
            columns=["question", "answer", "contexts"],
            samples=[],
            created_at=now,
        )
        with self._lock:
            self._datasets[dataset_id] = ds
        self._save_meta(ds)
        return ds

    def add_sample(self, dataset_id: str, sample: EvalExample) -> EvalDataset:
        """添加一条样本"""
        with self._lock:
            ds = self._datasets.get(dataset_id)
            if not ds:
                raise ValueError(f"数据集不存在: {dataset_id}")
            ds.samples.append(sample)
            ds.row_count = len(ds.samples)
        self._save_meta(ds)
        return ds

    def update_sample(self, dataset_id: str, index: int, sample: EvalExample) -> EvalDataset:
        """更新指定索引的样本"""
        with self._lock:
            ds = self._datasets.get(dataset_id)
            if not ds:
                raise ValueError(f"数据集不存在: {dataset_id}")
            if index < 0 or index >= len(ds.samples):
                raise ValueError(f"索引超出范围: {index}, 共 {len(ds.samples)} 条")
            ds.samples[index] = sample
        self._save_meta(ds)
        return ds

    def delete_sample(self, dataset_id: str, index: int) -> EvalDataset:
        """删除指定索引的样本"""
        with self._lock:
            ds = self._datasets.get(dataset_id)
            if not ds:
                raise ValueError(f"数据集不存在: {dataset_id}")
            if index < 0 or index >= len(ds.samples):
                raise ValueError(f"索引超出范围: {index}, 共 {len(ds.samples)} 条")
            ds.samples.pop(index)
            ds.row_count = len(ds.samples)
        self._save_meta(ds)
        return ds


data_service = DataService()


def get_data_service() -> DataService:
    return data_service
