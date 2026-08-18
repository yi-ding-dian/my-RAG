"""全局配置管理

设计要点（阶段2 预留）：
- LLM / Embedding / MinerU / 检索 / 切块 / 会话 / MySQL / MinIO 配置集中在一个
  ServiceConfig pydantic 模型里，.env 提供出厂默认值；
- 阶段2 settings_service 实现"配置档案"后，只需替换 _active_config（或调用
  set_active_config），各 service 均通过 get_active_config() 运行时动态读取，
  无需改动任何调用方代码。

多租户+团队协作阶段新增（Agent 1）：
- MySQL（users/departments/kbs 三表）与 MinIO（对象存储）配置段，.env 默认 +
  配置档案可覆盖；MYSQL_URL 用于测试覆盖（如 sqlite+aiosqlite://，离线跑）；
- JWT_SECRET 仅 .env 注入（安全材料，不进配置档案 UI）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# 项目根目录（backend 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """.env 驱动的出厂默认配置（字段名大写平铺，便于 .env 书写）"""

    # 服务
    HOST: str = "0.0.0.0"
    PORT: int = 8091
    CORS_ORIGINS: str = "*"

    # 数据目录（默认在项目下 data/，Docker 中可用环境变量覆盖为挂载卷）
    DATA_DIR: Path = BASE_DIR / "data"

    # LLM（生产环境通过 .env / 配置档案注入真实地址与密钥）
    LLM_BASE_URL: str = "http://127.0.0.1:1234/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "qwen3.6-35b-a3b-apex-quality"
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_TOKENS: int = 4096
    LLM_TIMEOUT: float = 120.0

    # Embedding（生产环境通过 .env / 配置档案注入真实地址与密钥）
    EMBEDDING_BASE_URL: str = "http://127.0.0.1:8300/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = "bge-m3"
    EMBEDDING_BATCH_SIZE: int = 32
    EMBEDDING_MAX_CHARS: int = 8000
    EMBEDDING_TIMEOUT: float = 60.0

    # MinerU（独立部署，默认本机 8001，服务名 mineru-api）
    MINERU_API_URL: str = "http://127.0.0.1:8001"
    MINERU_TIMEOUT: float = 300.0

    # DeepDoc（ragflow-server 默认本机 9380，RAGFlow API；
    # DeepDoc 是 RAGFlow 内置进程内解析器，表格输出为可检索 HTML）
    DEEPDOC_BASE_URL: str = "http://127.0.0.1:9380"
    DEEPDOC_EMAIL: str = ""
    DEEPDOC_PASSWORD: str = ""
    DEEPDOC_TIMEOUT: float = 300.0
    # 临时数据集命名前缀（解析完成后自动清理，前缀便于排查残留）
    DEEPDOC_DATASET_PREFIX: str = "myrag-tmp-"

    # 检索
    RETRIEVAL_TOP_K: int = 5

    # 切块
    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 100

    # 会话
    CHAT_HISTORY_ROUNDS: int = 8

    # 入库并发数（后台解析任务并发上限的出厂默认，配置档案可覆盖）
    INGEST_CONCURRENCY: int = 3

    # ---- 数据库（MySQL，多租户 users/departments/kbs 三表）----
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 5455
    MYSQL_USER: str = "ragflow"
    MYSQL_PASSWORD: str = ""
    MYSQL_DATABASE: str = "my_rag"
    # URL 覆盖字段：正常为空走 host/port/user/password/database 组装；
    # 测试时注入 sqlite+aiosqlite://（内存库）离线跑
    MYSQL_URL: str = ""

    # ---- 对象存储（MinIO，原始文档 + 解析图片）----
    MINIO_ENDPOINT: str = "127.0.0.1:9000"
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "my-rag"
    MINIO_SECURE: bool = False
    MINIO_REGION: str = ""

    # 存储后端: minio / local（local 存本地 data/uploads，测试离线用）
    STORAGE_BACKEND: str = "minio"

    # 认证（JWT 签名密钥，必须通过 .env 注入强随机值 ≥16 字符，否则拒绝启动）
    JWT_SECRET: str = ""

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
    }


# ---------------- 路径常量（全部基于 DATA_DIR 绝对路径，Docker 化友好） ----------------

def _paths(data_dir: Path):
    return {
        "DATA_DIR": data_dir,
        "UPLOAD_DIR": data_dir / "uploads",
        "PARSED_DIR": data_dir / "parsed",
        "KBS_DIR": data_dir / "kbs",
        "DOCUMENTS_DIR": data_dir / "documents",
        "CHAT_DIR": data_dir / "chat",
        "USER_MEMORY_DIR": data_dir / "user_memory",
        "CHROMA_DIR": data_dir / "chroma",
        # 本地存储后端（STORAGE_BACKEND=local）对象存放目录，与 MinIO 桶 key 同构
        "STORAGE_DIR": data_dir / "storage",
    }


# ---------------- 服务配置（集中定义，阶段2 可被配置档案整体覆盖） ----------------

class LLMConfig(BaseModel):
    """LLM 对话模型配置"""
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int
    timeout: float

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "LLMConfig":
        """dict → LLMConfig 兼容转换（历史裸 dict 调用点的类型化入口）

        - dict 可能含 LLMConfig 没有的扩展字段（部门合并产物/模型列表条目）
          → 只取已知字段，忽略未知
        - base_url/api_key/model 缺省或 None → ""（客户端构造语义与裸 dict
          的 .get(key, "") 一致）；temperature/max_tokens/timeout 缺省或
          None/0 → 出厂默认（0.3/4096/60，与历史 `float(x or 60)` 兜底一致）
        - None/空 dict 输入 → 出厂默认（防御脏数据）
        """
        data = data or {}
        return cls(
            base_url=data.get("base_url") or "",
            api_key=data.get("api_key") or "",
            model=data.get("model") or "",
            temperature=float(data.get("temperature") or 0.3),
            max_tokens=int(data.get("max_tokens") or 4096),
            timeout=float(data.get("timeout") or 60.0),
        )


class EmbeddingConfig(BaseModel):
    """Embedding 模型配置"""
    base_url: str
    api_key: str
    model: str
    batch_size: int
    max_chars: int
    timeout: float


class MinerUConfig(BaseModel):
    """MinerU 解析服务配置"""
    api_url: str
    timeout: float


class DeepDocConfig(BaseModel):
    """DeepDoc 解析服务配置（RAGFlow API 默认本机 9380 ragflow-server）

    DeepDoc 是 ragflow-server 内置进程内解析器（ONNX OCR + 版面识别 +
    表格结构识别），通过 RAGFlow API 调用；核心价值：表格输出为
    HTML <table> 文本可检索（vs MinerU 表格为图片不可检索）。
    """
    base_url: str = "http://127.0.0.1:9380"
    email: str = ""
    password: str = ""
    timeout: float = 300.0
    # 临时数据集命名前缀（解析完成后 finally 自动清理）
    dataset_prefix: str = "myrag-tmp-"


class RerankConfig(BaseModel):
    """Rerank 重排序配置（OpenAI 兼容 /rerank 协议：POST {base_url}/rerank）

    - enabled 为 False 或 base_url/model 任一为空时跳过重排（严格降级，不报错）
    - top_n: 参与重排的候选条数（大于等于最终 top_k 才有意义）
    """
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    top_n: int = 10


class RetrievalConfig(BaseModel):
    """检索参数"""
    top_k: int
    # 相似度阈值：低于该分数的检索结果过滤（0=不过滤，默认）
    similarity_threshold: float = 0.0
    # 混合检索开关：BM25 关键词 + 向量 RRF 融合（默认开启；关闭=纯向量原逻辑）
    enable_hybrid: bool = True
    # Rerank 重排序（默认关闭，企业用户可开启）
    rerank: RerankConfig = Field(default_factory=RerankConfig)


class ChunkingConfig(BaseModel):
    """切块参数"""
    chunk_size: int
    chunk_overlap: int


class ChatConfig(BaseModel):
    """会话参数（生成参数 None=用 LLM 配置默认值）"""
    history_rounds: int
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    # 多轮对话开关：False 时不带历史（只发 system + 当前问题）
    enable_multi_turn: bool = True
    # 自定义系统提示词：空串 = 使用内置默认模板（chat_service._SYSTEM_PROMPT_TEMPLATE）
    system_prompt: str = ""
    # 知识图谱增强：查询时 LLM 抽实体 → 图谱匹配 → 1-hop 邻接扩展，
    # 图谱上下文作为"知识图谱"来源引用注入回答（默认开；无图谱自动跳过零成本）
    kg_enhance: bool = True
    # 思考模式（聊天问答 LLM 调用）：disabled=关闭思考（默认，更快更省 token）
    # | enabled_low/enabled_high/enabled_max=开启思考并指定强度。注入方式按
    # 服务商区分（见 thinking_strategy）：在线 API（api.deepseek.com 等）经
    # extra_body 控制；本地 Qwen 思考模型 disabled 时注入空 <think> prefill
    # 跳过思考（LM Studio 忽略 extra_body）
    thinking_mode: str = "disabled"


class ContextualRetrievalConfig(BaseModel):
    """上下文检索增强配置（入库切块后处理，运行时动态读取）

    - max_full_doc_chars：完整文档视角阈值（字符，默认 20000）。解析文本
      <= 阈值时，摘要生成把完整文档作为上下文（全局视角，替代文档名+前
      1500 字符截断）；超过阈值 → 提示效果不佳，任务失败建议换用其他切块
      方式或关闭增强（见 contextual_retriever.DocTooLongError）
    """
    max_full_doc_chars: int = 20000


class IngestionConfig(BaseModel):
    """入库并发配置（后台解析任务并发上限，超管在系统配置页可调，即时生效）

    - concurrency：同时解析入库的文档数上限（默认 3，范围 1~10）。
      超出上限的任务在信号量队列等待，避免批量解析打爆 MinerU/embedding；
      运行时由 ingestion_service 每次 acquire 前实时读取，改动即生效
      （信号量按配置值惰性重建，见 _get_ingest_semaphore）
    """
    concurrency: int = 3


class MySQLConfig(BaseModel):
    """MySQL 连接配置（url 非空时优先使用，测试覆盖 sqlite 用）"""
    host: str = "127.0.0.1"
    port: int = 5455
    user: str = "ragflow"
    password: str = ""
    database: str = "my_rag"
    url: str = ""


class MinIOConfig(BaseModel):
    """MinIO 连接配置"""
    endpoint: str = "127.0.0.1:9000"
    access_key: str = ""
    secret_key: str = ""
    bucket: str = "my-rag"
    secure: bool = False
    region: str = ""


class ServiceConfig(BaseModel):
    """全部服务配置集合（阶段2 配置档案的完整形态）"""
    llm: LLMConfig
    embedding: EmbeddingConfig
    mineru: MinerUConfig
    deepdoc: DeepDocConfig = Field(default_factory=DeepDocConfig)
    retrieval: RetrievalConfig
    chunking: ChunkingConfig
    chat: ChatConfig
    contextual_retrieval: ContextualRetrievalConfig = Field(
        default_factory=ContextualRetrievalConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    mysql: MySQLConfig
    minio: MinIOConfig


settings = Settings()

# 数据目录自动创建（含 uploads/parsed/kbs/documents/chat/chroma）
_path_map = _paths(settings.DATA_DIR)
for _dir in _path_map.values():
    _dir.mkdir(parents=True, exist_ok=True)

DATA_DIR = _path_map["DATA_DIR"]
UPLOAD_DIR = _path_map["UPLOAD_DIR"]
PARSED_DIR = _path_map["PARSED_DIR"]
KBS_DIR = _path_map["KBS_DIR"]
DOCUMENTS_DIR = _path_map["DOCUMENTS_DIR"]
CHAT_DIR = _path_map["CHAT_DIR"]
USER_MEMORY_DIR = _path_map["USER_MEMORY_DIR"]
CHROMA_DIR = _path_map["CHROMA_DIR"]
STORAGE_DIR = _path_map["STORAGE_DIR"]


def build_default_config() -> ServiceConfig:
    """从 .env 出厂配置构造 ServiceConfig（阶段2 初始化配置档案时复用此函数）"""
    return ServiceConfig(
        llm=LLMConfig(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL,
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
            timeout=settings.LLM_TIMEOUT,
        ),
        embedding=EmbeddingConfig(
            base_url=settings.EMBEDDING_BASE_URL,
            api_key=settings.EMBEDDING_API_KEY,
            model=settings.EMBEDDING_MODEL,
            batch_size=settings.EMBEDDING_BATCH_SIZE,
            max_chars=settings.EMBEDDING_MAX_CHARS,
            timeout=settings.EMBEDDING_TIMEOUT,
        ),
        mineru=MinerUConfig(
            api_url=settings.MINERU_API_URL,
            timeout=settings.MINERU_TIMEOUT,
        ),
        deepdoc=DeepDocConfig(
            base_url=settings.DEEPDOC_BASE_URL,
            email=settings.DEEPDOC_EMAIL,
            password=settings.DEEPDOC_PASSWORD,
            timeout=settings.DEEPDOC_TIMEOUT,
            dataset_prefix=settings.DEEPDOC_DATASET_PREFIX,
        ),
        retrieval=RetrievalConfig(top_k=settings.RETRIEVAL_TOP_K),
        chunking=ChunkingConfig(
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
        ),
        chat=ChatConfig(history_rounds=settings.CHAT_HISTORY_ROUNDS),
        # 入库并发数：.env INGEST_CONCURRENCY 出厂默认（配置档案可覆盖）
        ingestion=IngestionConfig(
            concurrency=max(1, int(settings.INGEST_CONCURRENCY))),
        mysql=MySQLConfig(
            host=settings.MYSQL_HOST,
            port=settings.MYSQL_PORT,
            user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD,
            database=settings.MYSQL_DATABASE,
            url=settings.MYSQL_URL,
        ),
        minio=MinIOConfig(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            bucket=settings.MINIO_BUCKET,
            secure=settings.MINIO_SECURE,
            region=settings.MINIO_REGION,
        ),
    )


# 活跃配置（阶段1：默认值来自 .env；阶段2：settings_service 启动时替换为档案）
_active_config: ServiceConfig = build_default_config()


def get_active_config() -> ServiceConfig:
    """各 service 运行时动态读取活跃配置（不缓存启动值）"""
    return _active_config


def set_active_config(config: ServiceConfig):
    """阶段2 settings_service 覆盖活跃配置（本阶段仅默认实现）"""
    global _active_config
    _active_config = config
