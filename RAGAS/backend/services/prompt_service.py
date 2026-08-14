"""提示词管理服务 — 管理 RAGAS 指标的提示词、翻译、编辑、语言切换"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from backend.config import settings
from backend.models.eval_models import EvalMetric
from backend.evaluation.metrics import METRIC_MAP

logger = logging.getLogger(__name__)


class PromptService:

    def __init__(self):
        self._prompts_dir = settings.BASE_DIR / "prompts"
        self._prompts_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._prompts_dir / "prompts_db.json"
        self._config = self._load_or_init()

    # ── 持久化 ────────────────────────────────────────────────

    def _load_or_init(self) -> dict:
        """加载 prompts_db.json，首次运行时从 RAGAS 指标初始化英文原文"""
        if self._db_path.exists():
            try:
                cfg = json.loads(self._db_path.read_text())
            except Exception:
                cfg = {"active_language": "zh", "metrics": {}}
        else:
            cfg = {"active_language": "zh", "metrics": {}}

        changed = False
        for metric_enum, ragas_metric in METRIC_MAP.items():
            key = metric_enum.value
            if not hasattr(ragas_metric, 'get_prompts'):
                continue  # 该指标不支持提示词管理（如 answer_similarity）
            prompts_dict = ragas_metric.get_prompts()

            if key not in cfg["metrics"]:
                prompts_cfg = {}
                for name, p_obj in prompts_dict.items():
                    prompts_cfg[name] = {
                        "en": p_obj.instruction,
                        "zh": "",
                        "edited": False,
                    }
                cfg["metrics"][key] = {"prompts": prompts_cfg}
                changed = True

            # 从磁盘缓存更新中文翻译
            self._sync_zh_from_cache(cfg, key, metric_enum)

        if changed:
            self._save_config(cfg)

        return cfg

    def _sync_zh_from_cache(self, cfg: dict, key: str, metric_enum: EvalMetric):
        """从已经缓存的 JSON 文件同步中文翻译到 config"""
        ragas_metric = METRIC_MAP[metric_enum]
        if not hasattr(ragas_metric, 'load_prompts'):
            return
        cache_dir = self._prompts_dir / key
        try:
            zh_prompts = ragas_metric.load_prompts(path=str(cache_dir), language="chinese")
            for name, zh_p in zh_prompts.items():
                if name in cfg["metrics"][key]["prompts"]:
                    cfg["metrics"][key]["prompts"][name]["zh"] = zh_p.instruction
        except Exception:
            pass  # 没有缓存

    def _clear_cache_files(self, cache_dir: Path):
        """清理缓存目录中的旧提示词文件（save_prompts 不允许覆盖已有文件）"""
        for f in cache_dir.glob("*_chinese.json"):
            f.unlink()

    def _save_config(self, cfg: Optional[dict] = None):
        if cfg is None:
            cfg = self._config
        self._db_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2)
        )

    # ── 公共方法 ──────────────────────────────────────────────

    def list_metrics(self) -> list[dict]:
        """返回所有指标的提示词概览"""
        from backend.models.eval_models import METRIC_INFO

        result = []
        for metric_enum in EvalMetric:
            key = metric_enum.value
            info = METRIC_INFO.get(metric_enum, {})
            prompts_cfg = self._config.get("metrics", {}).get(key, {}).get("prompts", {})
            has_zh = any(p["zh"] for p in prompts_cfg.values())
            result.append({
                "metric": key,
                "name": info.get("name", key),
                "desc": info.get("desc", ""),
                "prompt_count": len(prompts_cfg),
                "has_chinese": has_zh,
            })
        return result

    def get_metric(self, metric_key: str) -> Optional[dict]:
        """返回单个指标的完整提示词（中英文）"""
        try:
            metric_enum = EvalMetric(metric_key)
        except ValueError:
            return None

        ragas_metric = METRIC_MAP.get(metric_enum)
        if ragas_metric and not hasattr(ragas_metric, 'get_prompts'):
            return None  # 该指标不支持提示词管理

        from backend.models.eval_models import METRIC_INFO
        info = METRIC_INFO.get(metric_enum, {})
        prompts_cfg = self._config.get("metrics", {}).get(metric_key, {}).get("prompts", {})
        prompts_list = [
            {
                "name": name,
                "en": p["en"],
                "zh": p["zh"],
                "edited": p.get("edited", False),
            }
            for name, p in prompts_cfg.items()
        ]

        return {
            "metric": metric_key,
            "name": info.get("name", metric_key),
            "desc": info.get("desc", ""),
            "prompts": prompts_list,
            "active_language": self.get_active_language(),
        }

    # ── 翻译 ──────────────────────────────────────────────────

    async def translate_metric(self, metric_key: str) -> dict:
        """AI 翻译指标的所有提示词为中文"""
        import asyncio
        from ragas.llms import llm_factory
        from backend.evaluation.llm_config import create_qwen_client

        metric_enum = EvalMetric(metric_key)
        ragas_metric = METRIC_MAP[metric_enum]
        if not hasattr(ragas_metric, 'adapt_prompts'):
            raise ValueError(f"该指标不支持提示词翻译: {metric_key}")
        cache_dir = self._prompts_dir / metric_key
        cache_dir.mkdir(parents=True, exist_ok=True)

        client = create_qwen_client()
        llm = llm_factory(
            model=settings.LLM_MODEL,
            client=client,
            temperature=0.0,
            max_tokens=4096,
            timeout=120,
        )

        adapted = await ragas_metric.adapt_prompts(language="chinese", llm=llm, adapt_instruction=True)
        ragas_metric.set_prompts(**adapted)
        self._clear_cache_files(cache_dir)
        ragas_metric.save_prompts(path=str(cache_dir))

        # 同步到 config
        for name, p_obj in adapted.items():
            if metric_key in self._config["metrics"] and name in self._config["metrics"][metric_key]["prompts"]:
                self._config["metrics"][metric_key]["prompts"][name]["zh"] = p_obj.instruction

        self._save_config()
        return self.get_metric(metric_key)

    # ── 编辑 ──────────────────────────────────────────────────

    def update_metric(self, metric_key: str, prompts_data: list[dict]) -> dict:
        """保存用户手动编辑的中文提示词"""
        metric_enum = EvalMetric(metric_key)
        ragas_metric = METRIC_MAP[metric_enum]
        if not hasattr(ragas_metric, 'set_prompts'):
            raise ValueError(f"该指标不支持提示词编辑: {metric_key}")
        cache_dir = self._prompts_dir / metric_key
        cache_dir.mkdir(parents=True, exist_ok=True)

        # 加载已有中文缓存，如果没有则加载当前值
        try:
            zh_prompts = ragas_metric.load_prompts(path=str(cache_dir), language="chinese")
        except Exception:
            zh_prompts = dict(ragas_metric.get_prompts())

        # 更新指令文本
        for item in prompts_data:
            name = item["name"]
            zh_text = item.get("zh", "")
            if name in zh_prompts:
                zh_prompts[name].instruction = zh_text
                if metric_key in self._config["metrics"] and name in self._config["metrics"][metric_key]["prompts"]:
                    self._config["metrics"][metric_key]["prompts"][name]["zh"] = zh_text
                    self._config["metrics"][metric_key]["prompts"][name]["edited"] = True

        ragas_metric.set_prompts(**zh_prompts)
        self._clear_cache_files(cache_dir)
        ragas_metric.save_prompts(path=str(cache_dir))
        self._save_config()

        return self.get_metric(metric_key)

    # ── 语言切换 ──────────────────────────────────────────────

    def get_active_language(self) -> str:
        return self._config.get("active_language", "zh")

    def set_active_language(self, language: str):
        if language not in ("en", "zh"):
            raise ValueError(f"不支持的语言: {language}")
        self._config["active_language"] = language
        self._save_config()

    # ── 评估集成 ──────────────────────────────────────────────

    def apply_prompts_for_eval(self, metric_enums: list[EvalMetric]):
        """在评估前应用当前语言设置的提示词到指标单例"""
        lang = self.get_active_language()
        if lang == "en":
            return  # RAGAS 默认即为英文

        for metric_enum in metric_enums:
            if metric_enum not in METRIC_MAP:
                continue
            ragas_metric = METRIC_MAP[metric_enum]
            if not hasattr(ragas_metric, 'load_prompts'):
                continue
            cache_dir = self._prompts_dir / metric_enum.value
            try:
                cached = ragas_metric.load_prompts(path=str(cache_dir), language="chinese")
                ragas_metric.set_prompts(**cached)
                logger.info("已应用中文提示词: %s", metric_enum.value)
            except Exception as e:
                logger.warning("中文提示词不可用，保留英文: %s (%s)", metric_enum.value, e)


_service = PromptService()


def get_prompt_service() -> PromptService:
    return _service
