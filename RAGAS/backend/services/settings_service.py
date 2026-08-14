"""运行时配置管理 — 多配置档案 + 连接测试"""
from __future__ import annotations
import json
import uuid
import logging
from pathlib import Path
from typing import Optional
from openai import OpenAI
from elasticsearch7 import Elasticsearch

from backend.config import settings

logger = logging.getLogger(__name__)

PROFILES_FILE = settings.BASE_DIR / "profiles.json"
PROFILE_FIELDS = [
    "llm_base_url", "llm_api_key", "llm_model",
    "llm_temperature", "llm_max_tokens",
    "embedding_base_url", "embedding_api_key", "embedding_model",
    "es_host", "es_port", "es_user", "es_password",
]


class SettingsService:

    def __init__(self):
        self._profiles: dict = {}
        self._load()

    # ---- 持久化 ----
    def _load(self):
        # 容错: profiles.json 可能不存在（首次运行）或是目录
        # （Docker bind mount 源缺失时 Docker 会创建目录），此时跳过读取不崩溃
        if PROFILES_FILE.is_file():
            try:
                data = json.loads(PROFILES_FILE.read_text())
                self._profiles = data.get("profiles", {})
                active_id = data.get("active_id")
                if active_id and active_id in self._profiles:
                    self._apply_profile(active_id)
                logger.info("已加载 %d 个配置档案", len(self._profiles))
            except Exception as e:
                logger.warning("加载配置档案失败: %s", e)
        elif PROFILES_FILE.is_dir():
            logger.warning("profiles.json 路径是目录（可能被 Docker bind mount 误创建），跳过加载")
        # 如果没有档案，从 .env 导入一个默认档案
        if not self._profiles:
            self._migrate_from_config()

    def _migrate_from_config(self):
        """从 .env 导入默认配置"""
        from backend.config import settings as s
        default = {
            "llm_base_url": s.LLM_BASE_URL,
            "llm_api_key": s.LLM_API_KEY,
            "llm_model": s.LLM_MODEL,
            "llm_temperature": s.LLM_TEMPERATURE,
            "llm_max_tokens": s.LLM_MAX_TOKENS,
            "embedding_base_url": s.EMBEDDING_BASE_URL,
            "embedding_api_key": s.EMBEDDING_API_KEY,
            "embedding_model": s.EMBEDDING_MODEL,
            "es_host": s.ES_HOST,
            "es_port": s.ES_PORT,
            "es_user": s.ES_USER,
            "es_password": s.ES_PASSWORD,
        }

        pid = str(uuid.uuid4())[:8]
        self._profiles[pid] = {
            "id": pid,
            "name": "默认配置",
            **default,
        }
        self._save()
        self.activate(pid)

    def _save(self):
        # 容错: 路径是目录（Docker bind mount 误建）或不可写时记日志不崩溃
        if PROFILES_FILE.is_dir():
            logger.warning("profiles.json 路径是目录，跳过保存（配置档案本次不持久化）")
            return
        active_id = None
        for pid, p in self._profiles.items():
            if p.get("active"):
                active_id = pid
                break
        try:
            PROFILES_FILE.write_text(
                json.dumps({"profiles": self._profiles, "active_id": active_id},
                           ensure_ascii=False, indent=2)
            )
        except Exception as e:
            logger.error("保存配置档案失败: %s", e)

    # ---- 应用配置到全局 ----
    def _apply_profile(self, profile_id: str):
        """将档案中的配置应用到全局 settings 对象。
        BaseSettings 继承自 BaseModel，支持运行时属性赋值。
        """
        p = self._profiles.get(profile_id)
        if not p:
            return
        for key in PROFILE_FIELDS:
            if key in p and p[key] is not None and p[key] != "":
                if hasattr(settings, key.upper()):
                    setattr(settings, key.upper(), p[key])
        self._reset_retrieval_client()

    def _reset_retrieval_client(self):
        try:
            from backend.services.retrieval_service import get_retrieval_service
            svc = get_retrieval_service()
            svc.close()
        except Exception:
            pass

    # ---- 档案 CRUD ----
    def list_profiles(self) -> list:
        return [
            {k: v for k, v in p.items() if k != "active"}
            for p in self._profiles.values()
        ]

    def get_profile(self, profile_id: str) -> Optional[dict]:
        p = self._profiles.get(profile_id)
        if p:
            return {k: v for k, v in p.items() if k != "active"}
        return None

    def get_active(self) -> Optional[dict]:
        for p in self._profiles.values():
            if p.get("active"):
                return {k: v for k, v in p.items() if k != "active"}
        return None

    def create_profile(self, name: str, data: dict) -> dict:
        pid = str(uuid.uuid4())[:8]
        profile = {"id": pid, "name": name}
        for k in PROFILE_FIELDS:
            if k in data and data[k] is not None and data[k] != "":
                profile[k] = data[k]
            else:
                profile[k] = self._default_for(k)
        profile["active"] = False
        self._profiles[pid] = profile
        self._save()
        return {k: v for k, v in profile.items() if k != "active"}

    def update_profile(self, profile_id: str, data: dict) -> Optional[dict]:
        p = self._profiles.get(profile_id)
        if not p:
            return None
        for k in PROFILE_FIELDS:
            if k in data and data[k] is not None and data[k] != "":
                p[k] = data[k]
        if "name" in data and data["name"]:
            p["name"] = data["name"]
        self._save()
        if p.get("active"):
            self._apply_profile(profile_id)
        return {k: v for k, v in p.items() if k != "active"}

    def delete_profile(self, profile_id: str) -> bool:
        p = self._profiles.pop(profile_id, None)
        if not p:
            return False
        if p.get("active") and self._profiles:
            first_id = next(iter(self._profiles))
            self.activate(first_id)
        elif not self._profiles:
            self._migrate_from_config()
        else:
            self._save()
        return True

    def activate(self, profile_id: str) -> Optional[dict]:
        p = self._profiles.get(profile_id)
        if not p:
            return None
        for pid in self._profiles:
            self._profiles[pid]["active"] = (pid == profile_id)
        self._apply_profile(profile_id)
        self._save()
        return {k: v for k, v in p.items() if k != "active"}

    def _default_for(self, key: str):
        defaults = {
            "llm_base_url": settings.LLM_BASE_URL,
            "llm_api_key": settings.LLM_API_KEY,
            "llm_model": settings.LLM_MODEL,
            "llm_temperature": settings.LLM_TEMPERATURE,
            "llm_max_tokens": settings.LLM_MAX_TOKENS,
            "embedding_base_url": settings.EMBEDDING_BASE_URL,
            "embedding_api_key": settings.EMBEDDING_API_KEY,
            "embedding_model": settings.EMBEDDING_MODEL,
            "es_host": settings.ES_HOST,
            "es_port": settings.ES_PORT,
            "es_user": settings.ES_USER,
            "es_password": settings.ES_PASSWORD,
        }
        return defaults.get(key, "")

    # ---- 连接测试 ----
    def test_llm(self, base_url: str, api_key: str, model: str) -> str:
        try:
            client = OpenAI(api_key=api_key, base_url=base_url)
            client.models.retrieve(model=model)
            return "连接成功"
        except Exception:
            try:
                client = OpenAI(api_key=api_key, base_url=base_url)
                models = client.models.list()
                ids = [m.id for m in models.data]
                if any(model in m for m in ids):
                    return f"连接成功（模型列表中匹配: {ids[:5]}）"
                return f"连接成功，但未找到模型 '{model}'，可用: {ids[:5]}"
            except Exception as e2:
                raise Exception(f"连接失败: {e2}")

    def test_embedding(self, base_url: str, api_key: str, model: str) -> str:
        try:
            client = OpenAI(api_key=api_key, base_url=base_url)
            resp = client.embeddings.create(model=model, input="test")
            dim = len(resp.data[0].embedding)
            return f"连接成功，向量维度: {dim}"
        except Exception as e:
            raise Exception(f"连接失败: {e}")

    def test_es(self, host: str, port: int, user: str, password: str) -> str:
        try:
            client = Elasticsearch(
                [{"host": host, "port": port}],
                http_auth=(user, password), timeout=10,
            )
            if not client.ping():
                raise Exception("ping 失败")
            info = client.info()
            version = info.get("version", {}).get("number", "unknown")
            indices = list(client.indices.get(index="*", allow_no_indices=True).keys())[:10]
            idx_info = f"，索引: {indices}" if indices else ""
            return f"连接成功，ES 版本: {version}{idx_info}"
        except Exception as e:
            raise Exception(f"连接失败: {e}")


_settings_service = SettingsService()


def get_settings_service() -> SettingsService:
    return _settings_service
