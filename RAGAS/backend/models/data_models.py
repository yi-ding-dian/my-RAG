"""数据模型定义"""
from __future__ import annotations
from typing import List, Optional, Any
from pydantic import BaseModel, Field


class EvalExample(BaseModel):
    """单条评估样本"""
    question: str = Field(..., description="用户问题")
    answer: str = Field(default="", description="LLM 生成的答案")
    contexts: List[str] = Field(default_factory=list, description="检索到的文档片段")
    ground_truth: Optional[str] = Field(default=None, description="参考答案")


class EvalDataset(BaseModel):
    """评估数据集"""
    id: str = Field("", description="数据集 ID")
    name: str = Field(..., description="数据集名称")
    description: str = Field("", description="描述")
    file_name: str = Field("", description="原始文件名")
    row_count: int = Field(0, description="样本数量")
    columns: List[str] = Field(default_factory=list, description="可用列名")
    samples: List[EvalExample] = Field(default_factory=list, description="样本数据")
    created_at: str = Field("", description="创建时间")


class DatasetListItem(BaseModel):
    """数据集列表项"""
    id: str
    name: str
    description: str
    file_name: str
    row_count: int
    columns: List[str]
    created_at: str


class CreateDatasetRequest(BaseModel):
    """创建数据集请求"""
    name: str = Field(..., description="数据集名称")
    description: str = Field("", description="描述")


class AddSampleRequest(BaseModel):
    """添加样本请求"""
    question: str = Field(..., description="问题")
    answer: str = Field("", description="答案")
    contexts: List[str] = Field(default_factory=list, description="上下文")
    ground_truth: Optional[str] = Field(None, description="参考答案")


class UpdateSampleRequest(BaseModel):
    """更新样本请求"""
    question: Optional[str] = None
    answer: Optional[str] = None
    contexts: Optional[List[str]] = None
    ground_truth: Optional[str] = None


class DatasetUploadResponse(BaseModel):
    """上传响应"""
    id: str
    name: str
    row_count: int
    columns: List[str]
    message: str


class DatasetPreview(BaseModel):
    """数据集预览"""
    id: str
    columns: List[str]
    rows: List[dict[str, Any]]
    total: int
