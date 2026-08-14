"""外部查询配置服务（知识库对外开放查询）

超管将选定的知识库（一个或多个）暴露为带 token 的外部查询链接，外部人员
无需系统账号、仅凭链接即可查询。本服务负责：

- 配置持久化：data/ext_queries.json（列表，创建/编辑/重置/停用/删除）
- 查询审计日志：data/ext_query_logs.jsonl（{ts, config_id, query(截100),
  hit_count}，逐条追加——外部使用无账号可溯，超管审计用）
- 限流：每 config 每分钟最多 RATE_LIMIT_PER_MIN 次（内存滑动窗口，
  超限由路由层返回 429；进程重启后计数清零，可接受）
- 多轮上下文：内存缓存 (config_id, session_id) 的最近对话（外部会话
  不落盘——无账号归属；重启即清空，可接受）

安全说明（管理端 token 返回策略）：管理 API 列表/详情返回完整 token——
本系统为内网部署，链接含访问凭证（token 即密钥），前端展示"复制链接"
方便分发；token 泄露风险等价于内网泄露，超管可随时重置/停用。
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from backend.config import DATA_DIR

logger = logging.getLogger(__name__)

EXT_QUERIES_FILE = DATA_DIR / "ext_queries.json"
EXT_QUERY_LOG_FILE = DATA_DIR / "ext_query_logs.jsonl"

# 限流：每 config 每分钟最大外部查询次数（滑动窗口）
RATE_LIMIT_PER_MIN = 20

# 多轮上下文内存缓存上限（会话条数，超限清最旧）
MAX_CONTEXT_SESSIONS = 100

# 日志 query 截断长度
LOG_QUERY_MAX_LEN = 100

# 名称长度限制
NAME_MAX_LEN = 50

# kb_ids 数量限制（1~10）
KB_IDS_MIN = 1
KB_IDS_MAX = 10

# config 字段白名单（复用聊天配置字段语义；None = 跟随全局活跃配置）：
# {字段名: (类型, 最小值, 最大值)}；system_prompt 为 str 不限范围
CONFIG_FIELDS: Dict[str, Tuple[str, float | None, float | None]] = {
    "system_prompt": ("str", None, None),
    "temperature": ("float", 0.0, 2.0),
    "top_p": ("float", 0.0, 1.0),
    "max_tokens": ("int", 1, 16384),
    "top_k": ("int", 1, 20),
    "similarity_threshold": ("float", 0.0, 1.0),
    "enable_multi_turn": ("bool", None, None),
    "history_rounds": ("int", 1, 20),
}

# 字段中文名（校验错误信息用）
_FIELD_LABELS = {
    "system_prompt": "系统提示词",
    "temperature": "温度",
    "top_p": "Top P",
    "max_tokens": "最大输出 Token",
    "top_k": "检索条数",
    "similarity_threshold": "相似度阈值",
    "enable_multi_turn": "多轮对话",
    "history_rounds": "历史轮数",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iso_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def coerce_config(raw: Optional[dict]) -> dict:
    """清洗/校验外部查询 config（字段白名单 + 类型 + 范围）

    - 未知字段丢弃（不报错，前端只提交已知字段）；None/缺失 = 跟随全局
    - 范围越界或类型非法 → ValueError（路由层转 400 中文提示）
    - 返回清洗后的完整 config dict（含全部白名单字段，缺省为 None/默认值）
    """
    src = raw or {}
    if not isinstance(src, dict):
        raise ValueError("查询配置格式非法")
    out: dict = {}
    for field, (ctype, lo, hi) in CONFIG_FIELDS.items():
        v = src.get(field)
        if v is None:
            # 缺省默认值：system_prompt=""（空=内置默认模板），
            # enable_multi_turn=True（与聊天配置语义一致），其余 None=跟随全局
            if field == "system_prompt":
                out[field] = ""
            elif field == "enable_multi_turn":
                out[field] = True
            else:
                out[field] = None
            continue
        label = _FIELD_LABELS[field]
        try:
            if ctype == "str":
                out[field] = str(v).strip()[:2000]
            elif ctype == "bool":
                if isinstance(v, str):
                    v = v.strip().lower() in ("1", "true", "yes", "on")
                else:
                    v = bool(v)
                out[field] = v
            elif ctype == "int":
                iv = int(v)
                if lo is not None and hi is not None and not lo <= iv <= hi:
                    raise ValueError
                out[field] = iv
            else:  # float
                fv = float(v)
                if lo is not None and hi is not None and not lo <= fv <= hi:
                    raise ValueError
                out[field] = fv
        except (TypeError, ValueError):
            if lo is not None and hi is not None:
                raise ValueError(f"{label} 需为 {lo}~{hi} 之间的数值") from None
            raise ValueError(f"{label} 格式非法") from None
    return out


class ExtQueryService:
    """外部查询配置 CRUD + 审计日志 + 限流 + 多轮上下文"""

    def __init__(self):
        self._lock = threading.Lock()
        self._queries: List[dict] = []
        self._contexts: Dict[Tuple[str, str], List[dict]] = {}
        # 限流滑动窗口：config_id -> 时间戳队列
        self._rate_hits: Dict[str, Deque[float]] = {}
        self._load()

    # ================= 持久化 =================

    def _load(self):
        try:
            if EXT_QUERIES_FILE.exists():
                data = json.loads(EXT_QUERIES_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._queries = [self._normalize(q) for q in data
                                     if isinstance(q, dict)]
        except Exception as e:
            logger.warning("加载外部查询配置失败: %s", e)
            self._queries = []

    @staticmethod
    def _normalize(q: dict) -> dict:
        """旧数据缺字段补齐（防御性；config 字段缺失走 coerce_config 补默认）"""
        out = dict(q)
        out.setdefault("id", uuid.uuid4().hex[:12])
        out.setdefault("name", "未命名外部查询")
        out.setdefault("kb_ids", [])
        out["config"] = coerce_config(q.get("config"))
        out.setdefault("token", secrets.token_urlsafe(32))
        out.setdefault("enabled", True)
        out.setdefault("created_by", "")
        out.setdefault("created_at", "")
        out.setdefault("updated_at", "")
        return out

    def _save(self):
        EXT_QUERIES_FILE.write_text(
            json.dumps(self._queries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ================= CRUD =================

    def list(self) -> List[dict]:
        with self._lock:
            return [dict(q) for q in self._queries]

    def get(self, config_id: str) -> Optional[dict]:
        with self._lock:
            q = self._find(config_id)
            return dict(q) if q else None

    def _find(self, config_id: str) -> Optional[dict]:
        return next((q for q in self._queries if q["id"] == config_id), None)

    def create(self, name: str, kb_ids: List[str],
               config: dict, user_id: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("名称不能为空")
        if len(name) > NAME_MAX_LEN:
            raise ValueError(f"名称长度需为 1~{NAME_MAX_LEN} 字")
        kb_ids = self._validate_kb_ids(kb_ids)
        now = _now()
        item = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "kb_ids": kb_ids,
            "config": coerce_config(config),
            "token": secrets.token_urlsafe(32),
            "enabled": True,
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._queries.append(item)
            self._save()
        logger.info("创建外部查询: %s (%s) kb_ids=%s",
                    name, item["id"], len(kb_ids))
        return dict(item)

    @staticmethod
    def _validate_kb_ids(kb_ids: Optional[List[str]]) -> List[str]:
        """kb_ids 校验：1~10 个、去重保序（存在性由路由层按超管视角校验）"""
        if not kb_ids:
            raise ValueError(f"请选择暴露的知识库（1~{KB_IDS_MAX} 个）")
        if not isinstance(kb_ids, list):
            raise ValueError("知识库列表格式非法")
        cleaned = list(dict.fromkeys(str(k) for k in kb_ids))
        if not KB_IDS_MIN <= len(cleaned) <= KB_IDS_MAX:
            raise ValueError(f"知识库数量需为 {KB_IDS_MIN}~{KB_IDS_MAX} 个")
        return cleaned

    def update(self, config_id: str, name: Optional[str] = None,
               kb_ids: Optional[List[str]] = None,
               config: Optional[dict] = None) -> Optional[dict]:
        with self._lock:
            q = self._find(config_id)
            if not q:
                return None
            if name is not None:
                name = name.strip()
                if not name:
                    raise ValueError("名称不能为空")
                if len(name) > NAME_MAX_LEN:
                    raise ValueError(f"名称长度需为 1~{NAME_MAX_LEN} 字")
                q["name"] = name
            if kb_ids is not None:
                q["kb_ids"] = self._validate_kb_ids(kb_ids)
            if config is not None:
                q["config"] = coerce_config(config)
            q["updated_at"] = _now()
            self._save()
            return dict(q)

    def reset_token(self, config_id: str) -> Optional[str]:
        """重置访问 token（旧链接立即失效），返回新 token"""
        with self._lock:
            q = self._find(config_id)
            if not q:
                return None
            q["token"] = secrets.token_urlsafe(32)
            q["updated_at"] = _now()
            self._save()
            return q["token"]

    def toggle(self, config_id: str) -> Optional[dict]:
        """启用/停用切换（停用后外部请求一律 401）"""
        with self._lock:
            q = self._find(config_id)
            if not q:
                return None
            q["enabled"] = not q["enabled"]
            q["updated_at"] = _now()
            self._save()
            return dict(q)

    def delete(self, config_id: str) -> bool:
        with self._lock:
            before = len(self._queries)
            self._queries = [q for q in self._queries if q["id"] != config_id]
            removed = len(self._queries) < before
            if removed:
                self._save()
        return removed

    # ================= 外部查询审计日志 =================

    def log_query(self, config_id: str, query: str, hit_count: int) -> None:
        """外部查询记录：逐行追加 jsonl（仅 query 摘要，不记回答内容）"""
        try:
            with self._lock:
                with open(EXT_QUERY_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "ts": _iso_ts(),
                        "config_id": config_id,
                        "query": (query or "")[:LOG_QUERY_MAX_LEN],
                        "hit_count": int(hit_count),
                    }, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("外部查询日志落盘失败: %s", e)

    # ================= 限流（内存滑动窗口） =================

    def check_rate_limit(self, config_id: str,
                         per_min: Optional[int] = None) -> bool:
        """每 config 每分钟最多 per_min 次（默认取模块常量，测试可 monkeypatch）；
        窗口内计数满返回 False（路由层 429）。命中计数与判定同一次完成
        （被限流的请求也计入，防窗口边界刷量）。
        """
        if per_min is None:
            per_min = RATE_LIMIT_PER_MIN
        now = time.monotonic()
        with self._lock:
            dq = self._rate_hits.setdefault(config_id, deque())
            while dq and now - dq[0] >= 60.0:
                dq.popleft()
            if len(dq) >= per_min:
                return False
            dq.append(now)
        return True

    # ================= 多轮上下文（内存缓存，不落盘） =================

    def get_context(self, config_id: str, session_id: str,
                    rounds: int) -> List[dict]:
        """取最近 N 轮历史（每轮 user+assistant 两条；rounds<=0 返回空）"""
        if not session_id or rounds <= 0:
            return []
        with self._lock:
            msgs = self._contexts.get((config_id, session_id), [])
            return [dict(m) for m in msgs[-(rounds * 2):]]

    def append_context(self, config_id: str, session_id: str,
                       user_msg: str, assistant_text: str) -> None:
        if not session_id:
            return
        with self._lock:
            msgs = self._contexts.setdefault((config_id, session_id), [])
            msgs.append({"role": "user", "content": (user_msg or "")[:2000]})
            msgs.append({"role": "assistant",
                         "content": (assistant_text or "")[:8000]})
            # 容量控制：超限清最旧会话（dict 保持插入序，取前几个淘汰）
            if len(self._contexts) > MAX_CONTEXT_SESSIONS:
                for k in list(self._contexts)[:len(self._contexts)
                                              - MAX_CONTEXT_SESSIONS]:
                    self._contexts.pop(k, None)


_ext_query_service: Optional[ExtQueryService] = None


def get_ext_query_service() -> ExtQueryService:
    global _ext_query_service
    if _ext_query_service is None:
        _ext_query_service = ExtQueryService()
    return _ext_query_service
