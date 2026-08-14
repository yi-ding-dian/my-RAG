"""全局配置管理"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 项目路径
    BASE_DIR: Path = Path(__file__).parent.parent
    DATA_DIR: Path = BASE_DIR / "data"
    DATASETS_DIR: Path = DATA_DIR / "datasets"
    UPLOAD_DIR: Path = DATA_DIR / "uploads"
    REPORTS_DIR: Path = BASE_DIR / "reports"

    # FastAPI
    HOST: str = "0.0.0.0"
    PORT: int = 8090
    CORS_ORIGINS: str = "*"

    # Qwen LLM (OpenAI 兼容)
    LLM_BASE_URL: str = "http://127.0.0.1:8000/v1"
    LLM_API_KEY: str = "not-needed"
    LLM_MODEL: str = "Qwen3.5-9B-GPTQ-4bit"
    LLM_TEMPERATURE: float = 0.0
    LLM_MAX_TOKENS: int = 8192

    # Embedding 模型（独立 bge-m3 服务）
    EMBEDDING_BASE_URL: str = "http://127.0.0.1:8300/v1"
    EMBEDDING_API_KEY: str = "not-needed"
    EMBEDDING_MODEL: str = "bge-m3"

    # Elasticsearch（知识库检索链路）
    ES_HOST: str = "localhost"
    ES_PORT: int = 1200
    ES_USER: str = "elastic"
    ES_PASSWORD: str = "<change-me>"
    ES_USE_SSL: bool = False
    ES_INDEX_PATTERN: str = "kb_*"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
os.makedirs(settings.DATASETS_DIR, exist_ok=True)
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.REPORTS_DIR, exist_ok=True)
