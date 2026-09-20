"""外部查询配置服务（知识库对外开放查询）

超管将选定的知识库（一个或多个）暴露为带 token 的外部查询链接，外部人员
无需系统账号、仅凭链接即可查询。本服务负责：

- 配置持久化：data/ext_queries.json（列表，创建/编辑/重置/停用/续期/删除）
- 有效期：配置 expires_at（空 = 永久有效；到期后外部一律 401，超管可续期）
- 查询记录：落库到 ext_query_logs 表（见 models/ext_query_models.py），
  含 query 摘要 + 命中数 + **来源 IP / UA** + 接入方式——外部使用无账号
  可溯，超管审计用；超期（LOG_RETAIN_DAYS）自动清理
- 限流：每 config 每分钟最多 RATE_LIMIT_PER_MIN 次（内存滑动窗口，
  超限由路由层返回 429；进程重启后计数清零，可接受）
- 多轮上下文：内存缓存 (config_id, session_id) 的最近对话（外部会话
  不落盘——无账号归属；重启即清空，可接受）

安全说明（管理端 token 返回策略）：列表/编辑接口**只回传打码 token**，
完整凭证需经 GET /api/ext-queries/{id}/token 单独取回（带审计），避免列表
被批量拖走即等于全量凭证泄露；创建/重置时在本次响应返回明文用于分发。
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from backend.config import DATA_DIR, get_active_config

logger = logging.getLogger(__name__)

EXT_QUERIES_FILE = DATA_DIR / "ext_queries.json"
# 说明：查询记录已改存数据库表 ext_query_logs（见 models/ext_query_models.py，
# 便于按时间/链接/IP 筛选与分页），旧的 data/ext_query_logs.jsonl 不再写入，
# 历史文件保留不删（如需回溯旧数据可手工查阅）。

# 限流：每 config 每分钟最大外部查询次数（滑动窗口）
RATE_LIMIT_PER_MIN = 20

# 多轮上下文内存缓存上限（会话条数，超限清最旧）
MAX_CONTEXT_SESSIONS = 100

# 日志 query 截断长度
LOG_QUERY_MAX_LEN = 100

# 记录保留天数（超期自动清理；外部查询无账号，记录是唯一的追溯依据）
LOG_RETAIN_DAYS = 90

# "即将到期"阈值（总览统计与列表标记用）
EXPIRING_SOON_DAYS = 7

# 记录列表分页上限（首页/单页最多取多少条）
LOG_PAGE_MAX = 200

# 名称长度限制
NAME_MAX_LEN = 50

# 到期时间格式（与库内其余时间字段一致：字符串字典序即时间序，可直接比较）
EXPIRES_FMT = "%Y-%m-%d %H:%M:%S"

# 续期允许的天数范围（1~3650）
RENEW_DAYS_MIN = 1
RENEW_DAYS_MAX = 3650

# kb_ids 数量限制（1~10）
KB_IDS_MIN = 1
KB_IDS_MAX = 10

# config 字段白名单（复用聊天配置字段语义；None = 跟随全局活跃配置）：
# {字段名: (类型, 最小值, 最大值)}；system_prompt 为 str 不限范围
CONFIG_FIELDS: Dict[str, Tuple[str, float | None, float | None]] = {
    "system_prompt": ("str", None, None),
    # 引用的提示词库条目名（配置档案 prompts 段）：与 system_prompt 二选一，
    # 自定义优先；存名字而非内容 → 改库正文，所有引用它的链接立刻生效
    "system_prompt_ref": ("str", None, None),
    "temperature": ("float", 0.0, 2.0),
    "top_p": ("float", 0.0, 1.0),
    "max_tokens": ("int", 1, 16384),
    "top_k": ("int", 1, 20),
    "similarity_threshold": ("float", 0.0, 1.0),
    # 多轮对话：外部场景以一次性问答为主，且 Agent 接入（/query）本就无会话概念，
    # 故默认关闭（需要网页内连续追问时再由超管打开）
    "enable_multi_turn": ("bool", None, None),
    # 是否允许外部页展示知识库图片（文档截图可能含敏感信息，超管可按配置关闭）
    "enable_images": ("bool", None, None),
    # 指定 LLM 模型（全局活跃档案模型列表里的 name）；空 = 跟随全局激活模型
    "llm_model": ("str", None, None),
}

# 字段中文名（校验错误信息用）
_FIELD_LABELS = {
    "system_prompt": "系统提示词",
    "system_prompt_ref": "引用的系统提示词",
    "temperature": "温度",
    "top_p": "Top P",
    "max_tokens": "最大输出 Token",
    "top_k": "检索条数",
    "similarity_threshold": "相似度阈值",
    "enable_multi_turn": "多轮对话",
    "enable_images": "显示图片",
    "llm_model": "LLM 模型",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_expires(value: Optional[str]) -> Optional[str]:
    """清洗到期时间：空/缺省 → None（永久有效）；格式非法 → ValueError

    格式做严格校验（而非只看长度）——过期判定用的是字符串字典序比较，
    要求两侧格式完全一致，混入 "2026/12/31" 之类会得到错误的比较结果。
    """
    v = (value or "").strip()
    if not v:
        return None
    try:
        datetime.strptime(v, EXPIRES_FMT)
    except ValueError:
        raise ValueError("到期时间格式非法") from None
    return v


def is_expired(ext: Optional[dict]) -> bool:
    """链接是否已到期（空 / 缺省 / 格式异常一律视为永久有效）

    格式异常的脏数据按"永久"而非"已过期"处理：宁可不失效，也不要让存量
    配置因为一个格式问题突然全部打不开。
    """
    exp = (ext or {}).get("expires_at") or ""
    if not exp:
        return False
    try:
        datetime.strptime(exp, EXPIRES_FMT)
    except ValueError:
        return False
    # 同格式下字符串字典序即时间序，直接比较
    return datetime.now().strftime(EXPIRES_FMT) > exp


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
            # 缺省默认值：system_prompt / llm_model=""（空=内置默认模板 / 跟随全局），
            # enable_images=True、enable_multi_turn=False（外部以一次性问答为主），
            # 其余 None=跟随全局
            if field in ("system_prompt", "system_prompt_ref", "llm_model"):
                out[field] = ""
            elif field == "enable_images":
                out[field] = True
            elif field == "enable_multi_turn":
                out[field] = False
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


# 外部查询 Top P 兜底值（全局聊天设置也未配置时使用）
DEFAULT_TOP_P = 0.9


def resolve_top_p(conf: Optional[dict],
                  llm_cfg: Optional[dict]) -> float:
    """外部查询实际生效的 Top P：本配置 → 模型配置 → 默认 0.9

    Top P 是**模型级**参数（`LLMConfig.top_p`，默认 0.9）：指定了模型就取该模型
    的值，未指定则取全局激活模型的——与 temperature/max_tokens 同为"跟随模型"。
    模型上再没有（None）时落到 DEFAULT_TOP_P，保证外部查询**始终下发**该参数，
    不会因不传而让采样范围随各家服务端默认漂移。
    """
    v = (conf or {}).get("top_p")
    if v is not None:
        return float(v)
    v = (llm_cfg or {}).get("top_p")
    if v is not None:
        return float(v)
    return DEFAULT_TOP_P


def resolve_system_prompt(conf: Optional[dict],
                          chat_prompt: Optional[str] = None) -> str:
    """外部查询实际使用的系统提示词

    回退链：**外部链接自定义 → 引用的提示词库条目 → 全局聊天设置 → 内置模板**

    最后一档返回空串——`ChatService._build_system_content` 收到空串即用内置
    模板（其既有语义），所以这里不重复判断。引用的条目若已被删除或改名，
    同样回退全局默认，只记 warning：不能让一条链接因为库里改了名字就答不出话。
    """
    custom = str((conf or {}).get("system_prompt") or "").strip()
    if custom:
        return custom
    ref = str((conf or {}).get("system_prompt_ref") or "").strip()
    if ref:
        from backend.services.settings.service import resolve_prompt_ref
        content = resolve_prompt_ref(ref)
        if content:
            return content
        logger.warning("外部查询引用的提示词不存在，回退全局默认: %s", ref)
    return str(chat_prompt or "").strip()


def resolve_llm_config(conf: Optional[dict]) -> dict:
    """取该外部查询实际生效的 LLM 配置（model / base_url / api_key / 上限等）

    - `config.llm_model` 指定了模型 → 从**全局活跃档案的模型列表**按标识取该条
      （复用 find_llm_item：name 优先、model 次之）
    - 未指定 / 指定但已不存在（该模型在全局配置里被改名或删除）→ 回退全局激活模型

    调用方拿到完整 LLM 配置后，再叠加外部查询 config 里的生成参数
    （temperature / top_p / max_tokens）——与"可指定模型"引入前的口径一致，
    即生成参数始终以外部查询的配置为准，模型只决定用哪一套连接与默认值。
    """
    from backend.services.llm_client import llm_to_dict

    ident = ((conf or {}).get("llm_model") or "").strip()
    if ident:
        try:
            from backend.services.settings.service import find_llm_item
            item = find_llm_item(ident)
            if item:
                return llm_to_dict(item)
            logger.warning("外部查询指定的模型不存在，回退全局激活模型: %s", ident)
        except Exception as e:
            logger.warning("解析外部查询指定模型失败(%s)，回退全局: %s", ident, e)
    return llm_to_dict(get_active_config().llm)


class ExtQueryService:
    """外部查询配置 CRUD + 审计日志 + 限流 + 多轮上下文"""

    def __init__(self):
        self._lock = threading.Lock()
        self._queries: List[dict] = []
        self._contexts: Dict[Tuple[str, str], List[dict]] = {}
        # 限流滑动窗口：config_id -> 时间戳队列
        self._rate_hits: Dict[str, Deque[float]] = {}
        # 记录清理节流（当天已清过就不重复清，见 maybe_cleanup_logs）
        self._last_cleanup = ""
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
        out.setdefault("expires_at", None)  # 存量配置无此字段 = 永久有效
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
               config: dict, user_id: str = "",
               expires_at: Optional[str] = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("名称不能为空")
        if len(name) > NAME_MAX_LEN:
            raise ValueError(f"名称长度需为 1~{NAME_MAX_LEN} 字")
        kb_ids = self._validate_kb_ids(kb_ids)
        expires = _clean_expires(expires_at)
        now = _now()
        item = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "kb_ids": kb_ids,
            "config": coerce_config(config),
            "token": secrets.token_urlsafe(32),
            "enabled": True,
            "expires_at": expires,
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
               config: Optional[dict] = None,
               expires_at: Optional[str] = None) -> Optional[dict]:
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
            # 空串 = 清除有效期（改为永久）；不传（None）= 保持不变
            if expires_at is not None:
                q["expires_at"] = _clean_expires(expires_at)
            q["updated_at"] = _now()
            self._save()
            return dict(q)

    def renew(self, config_id: str, days: int) -> Optional[dict]:
        """续期：从 max(现在, 原到期时间) 起算 + days 天

        未过期时从原到期时间顺延——否则每次续期都会白白吃掉剩余天数；
        已过期则从当前时间重新起算。
        """
        days = int(days)
        if not RENEW_DAYS_MIN <= days <= RENEW_DAYS_MAX:
            raise ValueError(
                f"续期天数需为 {RENEW_DAYS_MIN}~{RENEW_DAYS_MAX}")
        with self._lock:
            q = self._find(config_id)
            if not q:
                return None
            base = datetime.now()
            cur = q.get("expires_at") or ""
            if cur:
                try:
                    parsed = datetime.strptime(cur, EXPIRES_FMT)
                    if parsed > base:
                        base = parsed
                except ValueError:
                    pass  # 脏数据：按已过期处理，从当前时间起算
            q["expires_at"] = (base + timedelta(days=days)).strftime(EXPIRES_FMT)
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

    # ================= 外部查询记录（落库，含来源 IP） =================

    async def log_query(self, config_id: str, config_name: str, query: str,
                        hit_count: int, request=None,
                        source: str = "chat") -> None:
        """外部查询记录落库：query 摘要 + 命中数 + 来源 IP/UA + 接入方式

        - 失败只 warning 不抛：记录是审计辅助，不该让一次查询因为写日志失败
          而失败（与旧 jsonl 实现的容错口径一致）
        - 顺带按天触发一次超期清理（记录只增不减，不清理会无限膨胀）
        """
        try:
            from backend.db import get_session
            from backend.models.ext_query_models import ExtQueryLogORM
            from backend.services.audit_service import get_client_ip

            user_agent = ""
            if request is not None:
                user_agent = (request.headers.get("user-agent") or "")[:256]
            async with get_session() as session:
                session.add(ExtQueryLogORM(
                    config_id=config_id,
                    config_name=(config_name or "")[:64],
                    query=(query or "")[:LOG_QUERY_MAX_LEN],
                    hit_count=int(hit_count),
                    source=source,
                    client_ip=get_client_ip(request),
                    user_agent=user_agent or None,
                    created_at=_now(),
                ))
                await session.commit()
        except Exception as e:
            logger.warning("外部查询记录落库失败: %s", e)
        await self.maybe_cleanup_logs()

    async def maybe_cleanup_logs(self) -> None:
        """按天节流清理超期记录（每次查询顺带调用，当天只真正清一次）

        外部查询无账号，记录是唯一的追溯依据，但也不能无限增长；保留期见
        LOG_RETAIN_DAYS。清理失败只 warning，不影响查询本身。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        # 检查-设置放在锁内：并发请求同时打进来时只让一个真正执行清理
        # （清理本身幂等，重复执行只是浪费一次查询，但没必要）
        with self._lock:
            if self._last_cleanup == today:
                return
            self._last_cleanup = today
        cutoff = (datetime.now() - timedelta(days=LOG_RETAIN_DAYS)).strftime(
            EXPIRES_FMT)
        try:
            from sqlalchemy import delete
            from backend.db import get_session
            from backend.models.ext_query_models import ExtQueryLogORM
            async with get_session() as session:
                await session.execute(delete(ExtQueryLogORM).where(
                    ExtQueryLogORM.created_at < cutoff))
                await session.commit()
            logger.info("外部查询记录：已清理 %s 之前的记录", cutoff)
        except Exception as e:
            logger.warning("外部查询记录清理失败: %s", e)

    # ================= 记录查询与总览统计 =================

    @staticmethod
    def _log_to_dict(row) -> dict:
        """记录行 → 对外契约 dict"""
        return {
            "id": row.id,
            "config_id": row.config_id,
            "config_name": row.config_name,
            "query": row.query,
            "hit_count": row.hit_count,
            "source": row.source,
            "client_ip": row.client_ip or "",
            "user_agent": row.user_agent or "",
            "created_at": row.created_at,
        }

    async def list_logs(self, config_id: Optional[str] = None,
                        ip: Optional[str] = None,
                        start: Optional[str] = None,
                        end: Optional[str] = None,
                        page: int = 1, page_size: int = 20) -> dict:
        """查询记录列表（按链接 / IP / 时间段筛选，按时间倒序分页）

        筛选值均可选；ip 为模糊匹配（IPv4 片段与 IPv6 都能过滤）。
        返回 {items, total, page, page_size}。
        """
        from sqlalchemy import func, select
        from backend.db import get_session
        from backend.models.ext_query_models import ExtQueryLogORM

        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 20), LOG_PAGE_MAX))
        conds = []
        if config_id:
            conds.append(ExtQueryLogORM.config_id == config_id)
        if ip and ip.strip():
            conds.append(ExtQueryLogORM.client_ip.like(f"%{ip.strip()}%"))
        if start:
            conds.append(ExtQueryLogORM.created_at >= start)
        if end:
            conds.append(ExtQueryLogORM.created_at <= end)

        async with get_session() as session:
            count_stmt = select(func.count()).select_from(ExtQueryLogORM)
            data_stmt = select(ExtQueryLogORM)
            for c in conds:
                count_stmt = count_stmt.where(c)
                data_stmt = data_stmt.where(c)
            total = int((await session.execute(count_stmt)).scalar() or 0)
            rows = (await session.execute(
                data_stmt.order_by(ExtQueryLogORM.id.desc())
                .offset((page - 1) * page_size).limit(page_size)
            )).scalars().all()
        return {"items": [self._log_to_dict(r) for r in rows],
                "total": total, "page": page, "page_size": page_size}

    async def overview(self) -> dict:
        """总览统计（总览页卡片用：链接维度来自内存配置，记录维度来自库聚合）"""
        from sqlalchemy import func, select
        from backend.db import get_session
        from backend.models.ext_query_models import ExtQueryLogORM

        items = self.list()
        now = datetime.now()
        soon_limit = (now + timedelta(days=EXPIRING_SOON_DAYS)).strftime(
            EXPIRES_FMT)
        today_start = now.strftime("%Y-%m-%d") + " 00:00:00"
        # 近 7 天（含今天）的日期骨架：无记录的日期也要出现在趋势图里
        days = [(now - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(6, -1, -1)]
        week_start = days[0] + " 00:00:00"
        expired = sum(1 for q in items if is_expired(q))
        expiring = sum(
            1 for q in items
            if q.get("expires_at") and not is_expired(q)
            and q["expires_at"] <= soon_limit)

        async with get_session() as session:
            total_logs = int((await session.execute(
                select(func.count()).select_from(ExtQueryLogORM)
            )).scalar() or 0)
            today_logs = int((await session.execute(
                select(func.count()).select_from(ExtQueryLogORM)
                .where(ExtQueryLogORM.created_at >= today_start)
            )).scalar() or 0)
            last_at = (await session.execute(
                select(func.max(ExtQueryLogORM.created_at)))).scalar() or ""
            # 每条链接的查询次数与最近访问（总览页"链接速览"用）：
            # 按 config_id 分组一次查出，避免前端逐条链接再请求
            stat_rows = (await session.execute(
                select(ExtQueryLogORM.config_id,
                       func.count().label("cnt"),
                       func.max(ExtQueryLogORM.created_at).label("last_at"))
                .group_by(ExtQueryLogORM.config_id)
            )).all()
            # 近 7 天按天聚合（created_at 是 "YYYY-MM-DD HH:MM:SS"，
            # 取前 10 位即日期；substr 两后端通用）
            day_rows = (await session.execute(
                select(func.substr(ExtQueryLogORM.created_at, 1, 10).label("d"),
                       func.count().label("cnt"))
                .where(ExtQueryLogORM.created_at >= week_start)
                .group_by("d")
            )).all()
        link_stats = {
            r.config_id: {"count": int(r.cnt), "last_at": r.last_at or ""}
            for r in stat_rows
        }
        day_map = {r.d: int(r.cnt) for r in day_rows}

        return {
            "links": {
                "total": len(items),
                "enabled": sum(1 for q in items if q.get("enabled", True)),
                "expiring_soon": expiring,
                "expired": expired,
            },
            "logs": {
                "today": today_logs,
                "total": total_logs,
                "last_at": last_at,
                "retain_days": LOG_RETAIN_DAYS,
            },
            # config_id → {count, last_at}；无记录的链接不出现（前端按 0 处理）
            "link_stats": link_stats,
            # 近 7 天每日查询数（升序，含今天；无记录的日期补 0）
            "daily": [{"date": d, "count": day_map.get(d, 0)} for d in days],
            "expiring_soon_days": EXPIRING_SOON_DAYS,
        }

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
