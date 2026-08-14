"""服务配置档案管理（多 profile 持久化 + 活跃切换 + 连接测试）

- 持久化到 data/settings.json，多档案（每档案含 LLM/Embedding/MinerU/DeepDoc/
  检索/切块/MySQL/MinIO 全量配置）
- 活跃档案切换时调用 config.set_active_config()，各 service 运行时动态读取 → 即时生效
  （MySQL 配置变更 → db.get_engine() key 比对自动重建，无需重启）
- 服务启动（模块导入）时自动加载活跃档案
- 连接测试：LLM 发最小 chat 请求、Embedding 发 1 条 embed、MinerU 健康探测、
  DeepDoc 登录探测（RSA 加密密码 POST /v1/user/login）、MySQL aiomysql ping、
  MinIO bucket 探测 → 返回 {ok, latency_ms, message} 结构
- 密钥字段脱敏泛化（is_secret_field: endswith api_key/password/secret_key）：
  GET 返回脱敏；保存时传回脱敏值不覆盖原值

======== 维护者指南：如何新增一个配置段（总共 3 处，其中 2 处是 schema 一行） ========
1. config.py：定义 dataclass（含默认值）并挂到 ServiceConfig；build_default_config()
   无需改动（已有段显式构造，新段由 pydantic 默认值兜底）。
2. 本文件 SECTION_SCHEMA：加一行 `_reflect_section("段名", 新Dataclass, ...)`——
   字段列表与字段类型（cast）自动从 dataclass 反射生成，默认值自动取自
   build_default_config() 对应段。仅当存在以下差异时需要在 overrides 里标注：
   - 档案字段名 ≠ dataclass 字段名（如 mineru.url → source="api_url"）
   - 覆盖条件特殊（condition: "not_none" 允许 0 覆盖 / "secret_truthy" 密钥防脱敏回传）
   - str 字段需要 strip（绝大多数 str 字段都标 strip=True）
   - 常量默认值（const=1024，如 embedding.dimension）
   - 只入白名单不存档案（in_profile=False + whitelist=True，如 llm.timeout）
   - 段内特殊空值语义（on_null: "clear" 清空置 None / "restore" 空串恢复默认）
   - 整数范围校验（range=(1, 20)，如 chat.history_rounds）
   - 旧档案缺段自动补默认（fill_section=True，如 mysql/minio/deepdoc）
   - 旧档案缺字段自动补默认（fill_missing=True，如 chat.system_prompt）
3. 前端 Settings.tsx 表单。routers/settings.py 白名单、_public 脱敏、_coerce 类型
   规范化、update_profile 语义、_merge_defaults、to_service_config 全部自动支持，
   **无需任何改动**（它们全部由 SECTION_SCHEMA 驱动）。
   /api/settings/chat 的读写白名单 = 各段 whitelist=True 的字段。
===============================================================================
"""
from __future__ import annotations

import dataclasses
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from backend.config import (ChatConfig, ChunkingConfig, DATA_DIR, DeepDocConfig,
                            EmbeddingConfig, LLMConfig, MinerUConfig,
                            MinIOConfig, MySQLConfig, RerankConfig,
                            RetrievalConfig, ServiceConfig,
                            build_default_config, set_active_config)
from backend.services.probes import (probe_deepdoc_sync, probe_embedding_sdk,
                                     probe_llm_sdk, probe_mineru_sync,
                                     probe_minio, probe_mysql)

logger = logging.getLogger(__name__)

SETTINGS_FILE = DATA_DIR / "settings.json"

# 连接测试超时（秒）
LLM_TEST_TIMEOUT = 5.0
EMBEDDING_TEST_TIMEOUT = 5.0
MINERU_TEST_TIMEOUT = 3.0
DEEPDOC_TEST_TIMEOUT = 8.0
MYSQL_TEST_TIMEOUT = 5.0
MINIO_TEST_TIMEOUT = 5.0

# ==================== 密钥与类型辅助（schema 反射依赖，定义于 schema 之前） ====================

# 密钥字段后缀（脱敏判定）
_SECRET_SUFFIXES = ("api_key", "password", "secret_key")


def is_secret_field(key: str) -> bool:
    """是否密钥字段（endswith api_key/password/secret_key）"""
    return isinstance(key, str) and any(key.endswith(s) for s in _SECRET_SUFFIXES)


def mask_api_key(key: str) -> str:
    """密钥脱敏：长值显示 前4****后4，短值直接 ****"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) > 8:
        return f"{key[:4]}****{key[-4:]}"
    return "****"


def is_masked(key: str) -> bool:
    """判断前端回传的密钥值是否为脱敏值（是则保留原值不覆盖）"""
    return isinstance(key, str) and "****" in key


def _coerce_bool(value) -> bool:
    """布尔宽松转换（"false"/"0"/False → False，其余真值 → True）"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ==================== 声明式 SECTION_SCHEMA（新配置段唯一需要定义的地方） ====================
# 段/字段信息集中在此一处：默认值来源（config.py dataclass 反射 + build_default_config
# 运行时值）、类型转换、覆盖条件、密钥标记、白名单、空值语义、范围校验。
# 各处理函数（_default_profile_data / _coerce / _merge_defaults / to_service_config /
# update_profile / _public）与 routers/settings.py 的白名单全部由此驱动。
# 加新段只需：config.py 加 dataclass + 此处一行 _reflect_section + 前端表单。
_MISSING = object()


@dataclass(frozen=True)
class FieldSpec:
    """单个档案字段的完整声明

    - name:       档案字段名（= dataclass 字段名，除非 source 指定）
    - source:     默认值来源的 dataclass 字段名（默认=name）
    - const:      常量默认值（优先于 source；如 embedding.dimension=1024）
    - cast:       值类型转换（raw 原样 / str / int / float / bool=_coerce_bool）
    - condition:  覆盖条件（truthy=真值才覆盖；not_none=null 才不覆盖；
                  secret_truthy=真值且非脱敏值才覆盖）
    - strip:      str cast 时 strip
    - target:     to_service_config 写入目标属性（默认=source 或 name）
    - apply_to_config: 是否写入全局配置（False=档案专用字段不生效，
                  如 embedding.dimension / 仅白名单字段 llm.timeout）
    - secret:     密钥标记（反射时自动按 is_secret_field 生成，可显式叠加）
    - on_null:    update_profile 空值（None/空串）语义：
                  ignore=不修改 / clear=置 None 清空（chat 生成参数）/ restore=置空串恢复默认
    - whitelist:  可经 /api/settings/chat 读写（routers/settings.py 白名单由此派生）
    - in_profile: 是否参与档案默认值/coerce/merge（False=仅白名单不落档案）
    - fill_missing: _coerce 时字段缺失（None）自动补默认值（旧档案兼容）
    - range:      update_profile 整数范围校验（越界 → 400；如 chat.history_rounds）
    """
    name: str
    source: Optional[str] = None
    const: Any = _MISSING
    cast: str = "raw"
    condition: str = "truthy"
    strip: bool = False
    target: Optional[str] = None
    apply_to_config: bool = True
    secret: bool = False
    on_null: str = "ignore"
    whitelist: bool = False
    in_profile: bool = True
    fill_missing: bool = False
    range: Optional[Tuple[int, int]] = None


@dataclass(frozen=True)
class SectionSpec:
    """一个配置段的完整声明

    - name:            档案段名
    - dataclass_name:  ServiceConfig 上的属性名（默认=name，用于取默认值与写入目标）
    - fields:          段内字段声明（反射生成 + overrides 覆盖）
    - subsections:     嵌套子段（如 retrieval.rerank）
    - fill_section:    _coerce 时缺段/空段自动补整段默认值（旧档案兼容）
    - pass_null:       _validate_chat_payload 时段内 null 原样传给保存层
                      （False=直接丢弃，如 retrieval）
    - list_structure:  模型列表段（llm）：段 = {models: [条目], active: int}，
                      fields 声明**条目**字段（dataclass 反射）；处理函数按
                      "模型列表"语义专门分支（迁移/条目类型转换/激活条目
                      写入全局配置/条目脱敏）——见 llm 段
    """
    name: str
    fields: Dict[str, FieldSpec]
    dataclass_name: Optional[str] = None
    subsections: Dict[str, SectionSpec] = field(default_factory=dict)
    fill_section: bool = False
    pass_null: bool = False
    list_structure: bool = False


def _infer_cast(annotation) -> str:
    """从 pydantic 字段 annotation 推断 cast（解包 Optional）"""
    args = getattr(annotation, "__args__", None)
    if args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _infer_cast(non_none[0])
        return "raw"
    return {"str": "str", "int": "int", "float": "float", "bool": "bool"}.get(
        getattr(annotation, "__name__", ""), "raw")


def _reflect_section(name: str, model_cls, *, overrides=None, drops=(),
                     subsections=None, dataclass_name=None,
                     fill_section=False, pass_null=False) -> SectionSpec:
    """从 config.py 的 dataclass 反射生成 SectionSpec（字段列表 + cast + secret 自动）

    - overrides: {字段名: FieldSpec 关键字}——调整行为；字段不在 dataclass 时视为新增
    - drops:     从 dataclass 中排除的字段（如 llm.timeout 不落档案）
    - 默认值不在此处硬编码，运行时由 build_default_config() 对应段提供
    """
    fields: Dict[str, FieldSpec] = {}
    model_fields = getattr(model_cls, "model_fields", None) or {}
    for fname, finfo in model_fields.items():
        if fname in drops:
            continue
        if subsections and fname in subsections:
            continue  # 子段不走标量反射
        fields[fname] = FieldSpec(
            name=fname, cast=_infer_cast(finfo.annotation),
            secret=is_secret_field(fname))
    for fname, patch in (overrides or {}).items():
        base = fields.get(fname)
        fields[fname] = (dataclasses.replace(base, **patch) if base is not None
                         else FieldSpec(name=fname, **patch))
    return SectionSpec(name=name, dataclass_name=dataclass_name or name,
                       fields=fields, subsections=subsections or {},
                       fill_section=fill_section, pass_null=pass_null)


def _reflect_model_list_section(name: str, model_cls, *,
                                item_overrides=None,
                                pass_null: bool = False) -> SectionSpec:
    """模型列表段反射：段 = {models: [条目], active: int}（list_structure）

    - 条目字段 = dataclass 字段反射（cast/secret 自动），默认全部
      fill_missing=True：旧数据/部分提交时条目缺失字段自动补默认，
      保证条目始终完整（to_service_config 取激活条目不悬空）
    - whitelist 标记作用于条目字段：/api/settings/chat 的 llm 段白名单
      = 部门可覆盖的**激活模型**字段（与旧 6 字段语义一致）；
      name 为显示名（const="默认"，apply_to_config=False 不写全局配置）
    - pass_null：/api/settings/chat 校验时段内 null 原样传给保存层
      （llm=True，与旧段语义一致：null=不覆盖全局）
    """
    fields: Dict[str, FieldSpec] = {}
    model_fields = getattr(model_cls, "model_fields", None) or {}
    for fname, finfo in model_fields.items():
        fields[fname] = FieldSpec(
            name=fname, cast=_infer_cast(finfo.annotation),
            secret=is_secret_field(fname), fill_missing=True)
    for fname, patch in (item_overrides or {}).items():
        base = fields.get(fname)
        fields[fname] = (dataclasses.replace(base, **patch) if base is not None
                         else FieldSpec(name=fname, **patch))
    return SectionSpec(name=name, dataclass_name=name, fields=fields,
                       list_structure=True, pass_null=pass_null)


# ---- 各段声明（行为与重构前逐字段核对一致；字段默认值运行时取自 .env 出厂配置） ----
SECTION_SCHEMA: Dict[str, SectionSpec] = {
    # LLM：多模型列表（list_structure）= {models: [{name, base_url, api_key,
    # model, temperature, max_tokens, timeout}], active: 索引}。
    # - 激活模型 = models[active]，to_service_config 时构造 LLMConfig 写入
    #   全局配置 → get_active_config().llm 对外语义不变（=激活模型），
    #   所有 LLM 使用方（问答/摘要/评估 Judge 等）零改动；
    # - 旧单对象档案在 _coerce 自动迁移为 models[0]+active=0；
    # - 条目字段 whitelist = 部门可覆盖的激活模型字段（旧 6 字段语义），
    #   name 仅显示名不写全局配置；条目 timeout 生效（多模型是完整配置）
    "llm": _reflect_model_list_section(
        "llm", LLMConfig,
        item_overrides={
            "name": {"const": "默认", "cast": "str", "strip": True,
                     "apply_to_config": False},
            "base_url": {"strip": True, "whitelist": True},
            "api_key": {"strip": True, "condition": "secret_truthy",
                        "whitelist": True},
            "model": {"strip": True, "whitelist": True},
            "temperature": {"condition": "not_none", "whitelist": True},
            "max_tokens": {"condition": "truthy", "whitelist": True},
            "timeout": {"condition": "not_none", "whitelist": True},
        },
        pass_null=True),
    # Embedding：档案含 dimension（固定 1024，非 dataclass 字段），
    # batch_size/max_chars/timeout 不进档案（行为与重构前一致）
    "embedding": _reflect_section("embedding", EmbeddingConfig,
        drops=("batch_size", "max_chars", "timeout"),
        overrides={
            "base_url": {"strip": True},
            "api_key": {"strip": True, "condition": "secret_truthy"},
            "model": {"strip": True},
            "dimension": {"const": 1024, "cast": "int", "condition": "truthy",
                          "apply_to_config": False},
        }),
    # MinerU：档案字段名 url ↔ dataclass api_url（改名：drop 原字段 + override 新字段）
    "mineru": _reflect_section("mineru", MinerUConfig,
        drops=("api_url",),
        overrides={
            "url": {"source": "api_url", "target": "api_url",
                    "cast": "str", "strip": True},
            "timeout": {"condition": "not_none"},
        }),
    "deepdoc": _reflect_section("deepdoc", DeepDocConfig,
        overrides={
            "base_url": {"strip": True},
            "email": {"strip": True},
            "password": {"strip": True, "condition": "secret_truthy"},
            "timeout": {"condition": "not_none"},
            "dataset_prefix": {"strip": True},
        },
        fill_section=True),
    "retrieval": _reflect_section("retrieval", RetrievalConfig,
        overrides={
            "top_k": {"condition": "truthy", "whitelist": True},
            "similarity_threshold": {"condition": "not_none", "whitelist": True},
            "enable_hybrid": {"condition": "not_none", "fill_missing": True},
        },
        subsections={
            "rerank": _reflect_section("rerank", RerankConfig,
                overrides={
                    "enabled": {"condition": "not_none", "fill_missing": True},
                    "base_url": {"strip": True, "fill_missing": True},
                    "model": {"strip": True, "fill_missing": True},
                    "top_n": {"condition": "not_none", "fill_missing": True},
                },
                fill_section=True),
        }),
    # 切块：档案字段名 overlap ↔ dataclass chunk_overlap（chunk_overlap 旧数据兼容
    # 逻辑保留在 _coerce，schema 只管新字段名）
    "chunking": _reflect_section("chunking", ChunkingConfig,
        drops=("chunk_overlap",),
        overrides={
            "chunk_size": {"condition": "truthy"},
            "overlap": {"source": "chunk_overlap", "target": "chunk_overlap",
                        "cast": "int", "condition": "not_none"},
        }),
    "chat": _reflect_section("chat", ChatConfig,
        overrides={
            "history_rounds": {"condition": "truthy", "range": (1, 20),
                               "whitelist": True},
            "temperature": {"condition": "not_none", "on_null": "clear",
                            "whitelist": True},
            "top_p": {"condition": "not_none", "on_null": "clear",
                      "whitelist": True},
            "max_tokens": {"condition": "not_none", "on_null": "clear",
                           "whitelist": True},
            "enable_multi_turn": {"condition": "not_none", "whitelist": True},
            "system_prompt": {"condition": "not_none", "on_null": "restore",
                              "whitelist": True, "fill_missing": True},
        },
        pass_null=True),
    "mysql": _reflect_section("mysql", MySQLConfig,
        overrides={
            "host": {"strip": True},
            "port": {"condition": "not_none"},
            "user": {"strip": True},
            "password": {"strip": True, "condition": "secret_truthy"},
            "database": {"strip": True},
            "url": {"strip": True, "condition": "not_none"},
        },
        fill_section=True),
    "minio": _reflect_section("minio", MinIOConfig,
        overrides={
            "endpoint": {"strip": True},
            "access_key": {"strip": True, "condition": "secret_truthy"},
            "secret_key": {"strip": True, "condition": "secret_truthy"},
            "bucket": {"strip": True},
            "secure": {"condition": "not_none"},
            "region": {"strip": True, "condition": "not_none"},
        },
        fill_section=True),
}


def _whitelist_fields(section: str) -> Tuple[str, ...]:
    """该段可经 /api/settings/chat 读写的字段（schema.whitelist 驱动）"""
    spec = SECTION_SCHEMA.get(section)
    if spec is None:
        return ()
    return tuple(f.name for f in spec.fields.values() if f.whitelist)


# 部门级配置与 /api/settings/chat 共用同一字段集合（routers/settings.py 的白名单
# 也从此处派生——schema 是唯一来源，改一处全联动）
CHAT_FIELD_NAMES = _whitelist_fields("chat")
CHAT_RETRIEVAL_FIELD_NAMES = _whitelist_fields("retrieval")
LLM_FIELD_NAMES = _whitelist_fields("llm")

# 可经 /api/settings/chat 读写的段（含至少一个 whitelist 字段的段）
CHAT_SECTIONS = frozenset(
    s for s, spec in SECTION_SCHEMA.items()
    if any(f.whitelist for f in spec.fields.values()))


def _passes_condition(fspec: FieldSpec, v) -> bool:
    """字段覆盖条件（truthy / not_none / secret_truthy）"""
    if fspec.condition == "not_none":
        return v is not None
    if fspec.condition == "secret_truthy":
        return bool(v) and not is_masked(v)
    return bool(v)


def _cast_value(fspec: FieldSpec, v):
    """按 schema cast 转换值（bool 用宽松转换 _coerce_bool）"""
    if fspec.cast == "int":
        return int(v)
    if fspec.cast == "float":
        return float(v)
    if fspec.cast == "bool":
        return _coerce_bool(v)
    if fspec.cast == "str":
        s = str(v)
        return s.strip() if fspec.strip else s
    return v


def chat_payload(profile: dict) -> dict:
    """提取活跃档案的聊天设置字段（GET/POST /api/settings/chat 共用响应结构）"""
    chat = profile.get("chat") or {}
    retrieval = profile.get("retrieval") or {}
    return {
        "retrieval": {
            "top_k": retrieval.get("top_k"),
            "similarity_threshold": retrieval.get("similarity_threshold"),
        },
        "chat": {
            "enable_multi_turn": chat.get("enable_multi_turn"),
            "history_rounds": chat.get("history_rounds"),
            "temperature": chat.get("temperature"),
            "top_p": chat.get("top_p"),
            "max_tokens": chat.get("max_tokens"),
            "system_prompt": chat.get("system_prompt", ""),
        },
    }


def merge_chat_config(global_profile: dict, dept_config: dict) -> dict:
    """聊天配置字段级合并（部门只覆盖它设置的字段，其余用全局）

    - global_profile：活跃配置档案（含 chat/retrieval 段）
    - dept_config：部门级配置（{"chat": {...}, "retrieval": {...}}，
      与 chat_payload 结构同构；空 dict = 未设置，返回纯全局）
    - 合并规则：部门字段值非 None 且非空串（system_prompt 空串视为
      未设置=跟随全局）→ 覆盖全局；其余保持全局值
    - 返回 chat_payload 同构结果（仅白名单字段）
    """
    base = chat_payload(global_profile)
    dept = dept_config or {}
    # 段内仅接受 dict（脏数据容错：非 dict 视为未设置）
    dchat = dept.get("chat") if isinstance(dept.get("chat"), dict) else {}
    dretr = dept.get("retrieval") if isinstance(dept.get("retrieval"), dict) else {}
    for k in CHAT_FIELD_NAMES:
        v = dchat.get(k)
        if v is None or v == "":
            continue  # 部门未设置该字段 → 用全局
        base["chat"][k] = v
    for k in CHAT_RETRIEVAL_FIELD_NAMES:
        v = dretr.get(k)
        if v is None:
            continue
        base["retrieval"][k] = v
    return base


def active_llm_item(llm: dict) -> dict:
    """从 llm 段（{models, active}）取激活模型条目；异常数据/无条目 → {}"""
    if not isinstance(llm, dict):
        return {}
    models = llm.get("models")
    if not isinstance(models, list) or not models:
        return {}
    try:
        idx = int(llm.get("active") or 0)
    except (TypeError, ValueError):
        idx = 0
    if not 0 <= idx < len(models):
        return {}
    item = models[idx]
    return item if isinstance(item, dict) else {}


def merge_department_llm(global_llm: dict, dept_llm: dict) -> dict:
    """LLM 配置字段级合并（部门只覆盖它显式设置的字段，其余用全局）

    - global_llm：全局 LLM 配置（dict 或 model_dump 结果，含
      base_url/api_key/model/temperature/max_tokens/timeout）
    - dept_llm：部门级 LLM 配置（空 dict/None = 未设置，返回纯全局）
    - 合并规则：部门字段非 None 且非空串 → 覆盖全局；None/空串 =
      跟随全局（api_key 空串即"故意清空用全局"，与白名单清除语义一致）
    - 返回完整 6 字段 dict（响应结构与 /api/settings/chat 的 llm 段同构）
    """
    base = {k: (global_llm or {}).get(k) for k in LLM_FIELD_NAMES}
    dept = dept_llm or {}
    if not isinstance(dept, dict):
        return base  # 脏数据容错：非 dict 视为未设置
    for k in LLM_FIELD_NAMES:
        v = dept.get(k)
        if v is None or v == "":
            continue  # 未设置/空串 → 用全局
        base[k] = v
    return base


def _build_default_section(cfg_sec, spec: SectionSpec) -> dict:
    """按 schema 从出厂配置构造一个段（子段）的默认值 dict"""
    if spec.list_structure:
        # 模型列表段：默认 = 单条目（.env 出厂值）+ active=0
        item = {}
        for fname, fspec in spec.fields.items():
            if not fspec.in_profile:
                continue
            item[fname] = (fspec.const if fspec.const is not _MISSING
                           else getattr(cfg_sec, fspec.source or fspec.name))
        return {"models": [item], "active": 0}
    sec = {}
    for fname, fspec in spec.fields.items():
        if not fspec.in_profile:
            continue  # 仅白名单字段不落档案
        if fspec.const is not _MISSING:
            sec[fname] = fspec.const
        else:
            sec[fname] = getattr(cfg_sec, fspec.source or fspec.name)
    for sub_name, sub_spec in spec.subsections.items():
        sec[sub_name] = _build_default_section(
            getattr(cfg_sec, sub_spec.dataclass_name), sub_spec)
    return sec


def _build_default_profile(cfg: ServiceConfig) -> dict:
    """按 schema 从 .env 出厂配置构造全量默认档案（_default_profile_data 数据源）"""
    out: Dict[str, dict] = {}
    for sec_name, spec in SECTION_SCHEMA.items():
        out[sec_name] = _build_default_section(
            getattr(cfg, spec.dataclass_name), spec)
    return out


class SettingsService:

    def __init__(self):
        self._lock = threading.Lock()
        self._profiles: Dict[str, dict] = {}   # id -> profile dict（含 active 布尔）
        self._active_id: Optional[str] = None
        self._load()

    # ================= 持久化 =================

    def _load(self):
        """启动加载：有活跃档案则应用到全局配置（set_active_config）"""
        try:
            if SETTINGS_FILE.exists():
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                profiles = data.get("profiles") or []
                if isinstance(profiles, dict):  # 兼容旧结构 {id: profile}
                    profiles = list(profiles.values())
                for p in profiles:
                    self._profiles[p["id"]] = self._coerce(p)  # 归一化（旧 chunk_overlap → overlap / 补 mysql/minio 段）
                self._active_id = data.get("active_id") or ""
                if self._active_id in self._profiles:
                    self._apply_active()
                    logger.info("已加载 %d 个配置档案，活跃: %s",
                                len(self._profiles), self._profiles[self._active_id].get("name"))
                else:
                    self._active_id = None
                if self._profiles:
                    self._save()  # 归一化结果写回磁盘
                    return
            logger.info("无配置档案，从 .env 初始化默认档案")
        except Exception as e:
            logger.warning("加载配置档案失败: %s", e)
            self._profiles = {}
            self._active_id = None
        # 无任何档案：从 .env 出厂配置创建"默认配置"并激活
        if not self._profiles:
            pid = uuid.uuid4().hex[:8]
            self._profiles[pid] = {
                "id": pid,
                "name": "默认配置",
                "active": True,
                **self._default_profile_data(),
            }
            self._active_id = pid
            self._save()
            self._apply_active()
            logger.info("已创建默认配置档案: %s", pid)

    def _save(self):
        data = {"profiles": list(self._profiles.values()),
                "active_id": self._active_id}
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ================= 默认值 =================

    def _default_profile_data(self) -> dict:
        """以 .env 出厂配置为初始值（schema 驱动：字段/默认值取自 dataclass 反射）"""
        return _build_default_profile(build_default_config())

    # ================= 档案 -> 全局配置 =================

    def to_service_config(self, profile: dict) -> ServiceConfig:
        """档案转 ServiceConfig：以 .env 出厂值为基底，档案有值则覆盖（schema 驱动）"""
        base = build_default_config()
        for sec_name, spec in SECTION_SCHEMA.items():
            sec = profile.get(sec_name)
            if not isinstance(sec, dict):
                continue
            self._apply_section_to_config(
                getattr(base, spec.dataclass_name), spec, sec)
        return base

    @staticmethod
    def _apply_section_to_config(cfg_sec, spec: SectionSpec, sec: dict) -> None:
        """按 schema 将档案段字段写入全局配置段（condition/cast/strip/target 驱动）"""
        if spec.list_structure:
            # 模型列表段：取激活条目（models[active]）逐字段写入全局 LLM 配置
            # → get_active_config().llm 即激活模型，所有 LLM 使用方零改动
            models = sec.get("models")
            if not isinstance(models, list) or not models:
                return  # 无模型 → 保持 .env 默认
            try:
                idx = int(sec.get("active") or 0)
            except (TypeError, ValueError):
                idx = 0
            if not 0 <= idx < len(models):
                return  # 索引非法（防御）→ 保持默认
            item = models[idx]
            if not isinstance(item, dict):
                return
            for fname, fspec in spec.fields.items():
                if not fspec.in_profile or not fspec.apply_to_config:
                    continue  # 显示名（name）不写全局配置
                if fname not in item or not _passes_condition(fspec, item[fname]):
                    continue
                setattr(cfg_sec, fspec.target or fspec.source or fspec.name,
                        _cast_value(fspec, item[fname]))
            return
        for fname, fspec in spec.fields.items():
            if not fspec.in_profile or not fspec.apply_to_config:
                continue  # 档案专用字段（dimension）/ 仅白名单字段（llm.timeout）不写配置
            if fname not in sec or not _passes_condition(fspec, sec[fname]):
                continue
            setattr(cfg_sec, fspec.target or fspec.source or fspec.name,
                    _cast_value(fspec, sec[fname]))
        for sub_name, sub_spec in spec.subsections.items():
            sub = sec.get(sub_name)
            if not isinstance(sub, dict):
                continue
            SettingsService._apply_section_to_config(
                getattr(cfg_sec, sub_spec.dataclass_name), sub_spec, sub)

    def _apply_active(self):
        """应用当前活跃档案到全局活跃配置（服务即时生效）"""
        if self._active_id and self._active_id in self._profiles:
            set_active_config(self.to_service_config(self._profiles[self._active_id]))

    # ================= 档案 CRUD =================

    def _public(self, profile: dict) -> dict:
        """对外输出：去掉 active 标志、密钥字段脱敏（泛化 is_secret_field，嵌套递归
        覆盖模型列表段的 models[].api_key）"""
        out = json.loads(json.dumps(profile, ensure_ascii=False))
        out.pop("active", None)
        for section in SECTION_SCHEMA:
            SettingsService._mask_secrets(out.get(section))
        return out

    @staticmethod
    def _mask_secrets(value) -> None:
        """递归脱敏 dict/list 内的密钥字段（endswith api_key/password/secret_key）"""
        if isinstance(value, dict):
            for k, v in value.items():
                if is_secret_field(k):
                    value[k] = mask_api_key(v if isinstance(v, str) else "")
                else:
                    SettingsService._mask_secrets(v)
        elif isinstance(value, list):
            for v in value:
                SettingsService._mask_secrets(v)

    def public_profile(self, profile_id: str) -> Optional[dict]:
        """对外形态（密钥脱敏），供路由层使用"""
        with self._lock:
            p = self._profiles.get(profile_id)
            return self._public(p) if p else None

    def list_profiles(self) -> List[dict]:
        with self._lock:
            items = [self._public(p) for p in self._profiles.values()]
        # 活跃档案标记 active=true（前端用于高亮）
        for item in items:
            item["active"] = (item["id"] == self._active_id)
        return items

    def get_profile(self, profile_id: str) -> Optional[dict]:
        with self._lock:
            p = self._profiles.get(profile_id)
            return dict(p) if p else None

    def get_active(self) -> Optional[dict]:
        with self._lock:
            p = self._profiles.get(self._active_id or "")
            return dict(p) if p else None

    @staticmethod
    def _coerce(data: dict) -> dict:
        """类型规范化 + 旧档案兼容（schema 驱动：缺字段/缺段按 fill_missing/fill_section 补默认）"""
        out = json.loads(json.dumps(data, ensure_ascii=False))
        cfg = build_default_config()
        # 0) llm 旧结构迁移（存量单对象 → 模型列表 models[0]+active=0；
        #    缺失字段用 .env 出厂默认补齐——data/settings.json 存量自动升级）
        llm = out.get("llm")
        if isinstance(llm, dict) and "models" not in llm:
            base_llm = build_default_config().llm
            item = {"name": "默认"}
            for _f in ("base_url", "api_key", "model", "temperature",
                       "max_tokens", "timeout"):
                _v = llm.get(_f)
                item[_f] = (_v if _v is not None else getattr(base_llm, _f))
            out["llm"] = {"models": [item], "active": 0}
        # 1) 逐段类型规范化（仅 int/float/bool；str 字段保持原值）+ 字段级默认补齐
        for sec_name, spec in SECTION_SCHEMA.items():
            sec = out.get(sec_name) or {}
            cfg_sec = getattr(cfg, spec.dataclass_name)
            SettingsService._coerce_section_types(sec, spec, cfg_sec)
            SettingsService._fill_missing_section(sec, spec, cfg_sec)
            out[sec_name] = sec
        # 2) 旧档案兼容：chunk_overlap → 统一为 overlap（历史数据迁移，非 schema 范畴）
        chunking = out.get("chunking") or {}
        if chunking.get("chunk_overlap") is not None:
            if "overlap" not in chunking:
                chunking["overlap"] = chunking.pop("chunk_overlap")
            else:
                chunking.pop("chunk_overlap", None)
        if chunking.get("overlap") is not None:
            chunking["overlap"] = int(chunking["overlap"])
        out["chunking"] = chunking
        # 3) 缺段自动补整段默认值（fill_section：mysql/minio/deepdoc）
        defaults = _build_default_profile(cfg)
        for sec_name, spec in SECTION_SCHEMA.items():
            if spec.fill_section and not out.get(sec_name):
                out[sec_name] = defaults[sec_name]
        return out

    @staticmethod
    def _coerce_section_types(sec: dict, spec: SectionSpec, cfg_sec) -> None:
        """按 schema 对段内数值/布尔字段做类型转换（str 字段不转换，与历史一致）"""
        if spec.list_structure:
            # 模型列表段：对 models 数组内每个条目做类型转换；active 转 int
            models = sec.get("models")
            if isinstance(models, list):
                for item in models:
                    if not isinstance(item, dict):
                        continue
                    for fname, fspec in spec.fields.items():
                        if fspec.cast not in ("int", "float", "bool"):
                            continue
                        if fname not in item or not _passes_condition(
                                fspec, item[fname]):
                            continue
                        item[fname] = _cast_value(fspec, item[fname])
            if sec.get("active") is not None:
                try:
                    sec["active"] = int(sec["active"])
                except (TypeError, ValueError):
                    pass
            return
        for fname, fspec in spec.fields.items():
            if fspec.cast not in ("int", "float", "bool"):
                continue
            if fname not in sec or not _passes_condition(fspec, sec[fname]):
                continue
            sec[fname] = _cast_value(fspec, sec[fname])
        for sub_name, sub_spec in spec.subsections.items():
            sub = sec.get(sub_name)
            if not isinstance(sub, dict):
                continue
            SettingsService._coerce_section_types(
                sub, sub_spec, getattr(cfg_sec, sub_spec.dataclass_name))

    @staticmethod
    def _fill_missing_section(sec: dict, spec: SectionSpec, cfg_sec) -> None:
        """旧档案缺字段补默认值（fill_missing：enable_hybrid/rerank 字段/system_prompt）；
        子段整体缺失且 fill_section → 补整段默认（rerank）"""
        if spec.list_structure:
            # 模型列表段：条目缺字段补默认（保证条目始终完整）
            models = sec.get("models")
            if isinstance(models, list):
                for item in models:
                    if not isinstance(item, dict):
                        continue
                    for fname, fspec in spec.fields.items():
                        if not fspec.fill_missing or item.get(fname) is not None:
                            continue
                        item[fname] = (fspec.const if fspec.const is not _MISSING
                                       else getattr(cfg_sec, fspec.source
                                                    or fspec.name))
            return
        for fname, fspec in spec.fields.items():
            if not fspec.fill_missing or sec.get(fname) is not None:
                continue
            sec[fname] = (fspec.const if fspec.const is not _MISSING
                          else getattr(cfg_sec, fspec.source or fspec.name))
        for sub_name, sub_spec in spec.subsections.items():
            cfg_sub = getattr(cfg_sec, sub_spec.dataclass_name)
            sub = sec.get(sub_name)
            if isinstance(sub, dict):
                SettingsService._fill_missing_section(sub, sub_spec, cfg_sub)
            elif sub_spec.fill_section:
                sec[sub_name] = _build_default_section(cfg_sub, sub_spec)

    def _merge_defaults(self, data: dict) -> dict:
        """缺省字段用默认档案补齐（schema 驱动；子段如 rerank 按字段合并，不丢默认值）"""
        merged = self._default_profile_data()
        for sec_name, spec in SECTION_SCHEMA.items():
            sec = data.get(sec_name)
            if not isinstance(sec, dict):
                continue
            target = merged[sec_name]
            if spec.list_structure:
                # 模型列表段：
                # - 新结构（models/active 键）：models 整体替换（条目缺字段
                #   用默认条目补齐）+ active 覆盖；
                # - 旧标量提交（无 models/active，如旧前端/测试）：字段级
                #   合并进激活条目（即"修改激活模型的字段"）
                if "models" in sec or "active" in sec:
                    if isinstance(sec.get("models"), list):
                        base_item = (target["models"][0]
                                     if target.get("models") else {})
                        models = []
                        for it in sec["models"]:
                            if not isinstance(it, dict):
                                continue
                            merged_item = dict(base_item)
                            for k, v in it.items():
                                if v is None or v == "":
                                    continue
                                if k not in spec.fields:
                                    continue
                                if is_secret_field(k) and is_masked(v):
                                    continue
                                merged_item[k] = v
                            models.append(merged_item)
                        if models:
                            target["models"] = models
                    if sec.get("active") is not None:
                        target["active"] = sec["active"]
                else:
                    try:
                        idx = int(target.get("active") or 0)
                    except (TypeError, ValueError):
                        idx = 0
                    models = target.get("models")
                    if (isinstance(models, list) and models
                            and 0 <= idx < len(models)):
                        item = models[idx]
                        for k, v in sec.items():
                            if v is None or v == "":
                                continue
                            if k not in spec.fields:
                                continue
                            if is_secret_field(k) and is_masked(v):
                                continue
                            item[k] = v
                continue
            for k, v in sec.items():
                if v is None or v == "":
                    continue
                sub_spec = spec.subsections.get(k)
                if sub_spec is not None and isinstance(v, dict):
                    sub_target = target.setdefault(k, {})
                    for rk, rv in v.items():
                        if rv is not None and rv != "":
                            sub_target[rk] = rv
                    continue
                target[k] = v
        return merged

    def create_profile(self, name: str, data: dict) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("档案名称不能为空")
        pid = uuid.uuid4().hex[:8]
        with self._lock:
            profile = {
                "id": pid,
                "name": name,
                "active": False,
                **self._coerce(self._merge_defaults(data)),
            }
            self._profiles[pid] = profile
            self._save()
        logger.info("创建配置档案: %s (%s)", name, pid)
        return self._public(profile)

    def update_profile(self, profile_id: str, data: dict) -> Optional[dict]:
        with self._lock:
            p = self._profiles.get(profile_id)
            if not p:
                return None
            if data.get("name"):
                p["name"] = str(data["name"]).strip()
            # 只覆盖传入的 section 字段；密钥字段传回脱敏值时保留原值
            # （段列表与字段语义全部由 SECTION_SCHEMA 驱动：on_null 空值语义 /
            # secret 脱敏保留 / range 范围校验）
            for section, spec in SECTION_SCHEMA.items():
                if not isinstance(data.get(section), dict):
                    continue
                target = p.setdefault(section, {})
                if spec.list_structure:
                    # llm 模型列表段：models 整体替换 + 校验 / active 切换 /
                    # 旧标量兼容（见 _update_llm_section）
                    self._update_llm_section(spec, target, data[section])
                    continue
                for k, v in data[section].items():
                    sub_spec = spec.subsections.get(k)
                    if sub_spec is not None and isinstance(v, dict):
                        # 子段（rerank）按字段合并（部分字段提交不丢默认值）
                        sub_target = target.setdefault(k, {})
                        for rk, rv in v.items():
                            if rv is None or rv == "":
                                continue
                            if is_secret_field(rk) and is_masked(rv):
                                continue
                            sub_target[rk] = rv
                        continue
                    fspec = spec.fields.get(k)
                    if v is None or v == "":
                        # chat 生成参数例外：显式传 null 表示"用 LLM 配置默认"，
                        # 允许清除已设值（on_null=clear）；system_prompt 例外：
                        # 显式传空串 = 恢复内置默认模板（on_null=restore）；
                        # 其余字段 None/空串=不修改（既有契约）
                        if fspec is not None and fspec.on_null == "clear":
                            target[k] = None
                        elif fspec is not None and fspec.on_null == "restore":
                            target[k] = ""
                        continue
                    # 脱敏回传不覆盖（schema.secret 叠加 is_secret_field；
                    # 未知字段也按 is_secret_field 判定，与历史一致）
                    secret_field = fspec.secret if fspec is not None else False
                    if (secret_field or is_secret_field(k)) and is_masked(v):
                        continue
                    if fspec is not None and fspec.range is not None:
                        # P2: chat.history_rounds 范围校验 1~20（越界 → 400）
                        lo, hi = fspec.range
                        try:
                            rounds = int(v)
                        except (TypeError, ValueError):
                            raise ValueError(f"历史轮数需为 {lo}~{hi} 的整数")
                        if not lo <= rounds <= hi:
                            raise ValueError(f"历史轮数需为 {lo}~{hi}")
                        target[k] = rounds
                        continue
                    target[k] = v
            p.update(self._coerce({s: p.get(s) for s in SECTION_SCHEMA}))
            self._save()
            if p.get("active"):
                self._apply_active()
        logger.info("更新配置档案: %s (%s)", p["name"], profile_id)
        return self._public(p)

    def _update_llm_section(self, spec: SectionSpec, target: dict,
                            sec: dict) -> None:
        """llm 模型列表段更新：models 整体替换 / active 切换 / 旧标量兼容

        - 新结构（含 models 或 active 键）：
          - models 整体替换（每次提交完整列表）：条目必填校验（name 唯一、
            base_url/model 非空）、至少保留 1 个；条目内 api_key 传回脱敏值
            按同索引保留原值；未提交字段继承原条目同索引值（条目保持完整）
          - active 切换：索引必须为合法整数（0 <= active < len(models)）
        - 旧标量字段（/api/settings/chat 超管快捷改，无 models/active 键）：
          字段级合并进**激活模型**条目（= 修改当前激活模型的字段，即时生效）
        """
        if "models" in sec or "active" in sec:
            if "models" in sec:
                raw = sec["models"]
                if not isinstance(raw, list):
                    raise ValueError("模型列表格式错误")
                orig = (target.get("models")
                        if isinstance(target.get("models"), list) else [])
                new_models: List[dict] = []
                names: set = set()
                for i, it in enumerate(raw):
                    if not isinstance(it, dict):
                        raise ValueError("模型条目格式错误")
                    name = str(it.get("name") or "").strip()
                    if not name:
                        raise ValueError("模型名称不能为空")
                    if name in names:
                        raise ValueError(f"模型名称不能重复: {name}")
                    names.add(name)
                    if not str(it.get("base_url") or "").strip():
                        raise ValueError(f"模型「{name}」的 API 地址不能为空")
                    if not str(it.get("model") or "").strip():
                        raise ValueError(f"模型「{name}」的模型标识不能为空")
                    new_item: dict = {}
                    for k, v in it.items():
                        if v is None or v == "":
                            continue
                        if k not in spec.fields and not is_secret_field(k):
                            continue  # 条目内未知字段忽略
                        if is_secret_field(k) and is_masked(v):
                            # 脱敏回传：同索引原值保留；无原值则忽略该字段
                            if (i < len(orig) and isinstance(orig[i], dict)
                                    and orig[i].get(k) is not None):
                                v = orig[i][k]
                            else:
                                continue
                        new_item[k] = v
                    # 未提交字段继承原条目同索引值（编辑单个字段不丢其余字段）
                    if i < len(orig) and isinstance(orig[i], dict):
                        for k, v in orig[i].items():
                            if k not in new_item and v is not None:
                                new_item[k] = v
                    new_models.append(new_item)
                if not new_models:
                    raise ValueError("至少保留一个模型")
                target["models"] = new_models
            if "active" in sec:
                active = sec["active"]
                try:
                    active = int(active)
                except (TypeError, ValueError):
                    raise ValueError("激活模型索引必须是整数")
                if not 0 <= active < len(target.get("models") or []):
                    raise ValueError("激活的模型不存在")
                target["active"] = active
            return
        # 旧标量提交：字段级合并进激活模型条目（super_admin /api/settings/chat）
        try:
            idx = int(target.get("active") or 0)
        except (TypeError, ValueError):
            idx = 0
        orig = target.get("models")
        if not isinstance(orig, list) or not 0 <= idx < len(orig):
            return
        item = orig[idx]
        if not isinstance(item, dict):
            return
        for k, v in sec.items():
            if v is None or v == "":
                continue
            if k not in spec.fields:
                continue
            if is_secret_field(k) and is_masked(v):
                continue
            item[k] = v

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            p = self._profiles.pop(profile_id, None)
            if not p:
                return False
            if self._active_id == profile_id:
                # 删除活跃档案后，激活剩余第一个
                if self._profiles:
                    self._active_id = next(iter(self._profiles))
                else:
                    self._active_id = None
            self._save()
            if self._active_id and self._active_id in self._profiles:
                self._apply_active()
        logger.info("删除配置档案: %s", profile_id)
        return True

    def activate(self, profile_id: str) -> Optional[dict]:
        with self._lock:
            if profile_id not in self._profiles:
                return None
            self._active_id = profile_id
            for p in self._profiles.values():
                p["active"] = (p["id"] == profile_id)
            self._save()
            self._apply_active()
        p = self._profiles[profile_id]
        logger.info("激活配置档案: %s (%s)", p["name"], profile_id)
        return self._public(p)

    # ================= 连接测试（探测逻辑统一在 services/probes.py） =================

    @staticmethod
    def _message(r: dict) -> dict:
        """probes 结果 {ok, latency_ms, reason} → 对外 {ok, latency_ms, message}"""
        return {"ok": r["ok"], "latency_ms": r["latency_ms"],
                "message": f"{r['reason']}（耗时 {r['latency_ms']}ms）"}

    async def test_connections(self, profile: dict) -> dict:
        """逐项测试 LLM / Embedding / MinerU / DeepDoc / MySQL / MinIO，
        统一 {ok, latency_ms, message}"""
        return {
            "llm": self._test_llm(profile.get("llm") or {}),
            "embedding": self._test_embedding(profile.get("embedding") or {}),
            "mineru": self._test_mineru(profile.get("mineru") or {}),
            "deepdoc": await self._test_deepdoc(profile.get("deepdoc") or {}),
            "mysql": await self._test_mysql(profile.get("mysql") or {}),
            "minio": await self._test_minio(profile.get("minio") or {}),
        }

    def _test_llm(self, llm: dict) -> dict:
        """对激活模型条目发最小 chat 请求（max_tokens=1），5s 超时（probes SDK 形态）"""
        return self._message(probe_llm_sdk(
            active_llm_item(llm), timeout=LLM_TEST_TIMEOUT, client_cls=OpenAI))

    def _test_embedding(self, embedding: dict) -> dict:
        """发 1 条 embed，5s 超时，返回实际维度（probes SDK 形态）"""
        return self._message(probe_embedding_sdk(
            embedding, timeout=EMBEDDING_TEST_TIMEOUT, client_cls=OpenAI))

    def _test_mineru(self, mineru: dict) -> dict:
        """健康探测（/health → /api/health → 根路径，≤3s，<400 可用）"""
        return self._message(probe_mineru_sync(
            mineru, timeout=min(
                MINERU_TEST_TIMEOUT,
                float(mineru.get("timeout") or MINERU_TEST_TIMEOUT)),
            ok_under=400))

    async def _test_deepdoc(self, deepdoc: dict) -> dict:
        """RAGFlow 登录探测（RSA 加密密码 POST /v1/user/login，≤8s）"""
        return self._message(probe_deepdoc_sync(
            deepdoc, timeout=min(
                DEEPDOC_TEST_TIMEOUT,
                float(deepdoc.get("timeout") or DEEPDOC_TEST_TIMEOUT))))

    async def _test_mysql(self, mysql: dict) -> dict:
        """aiomysql 异步 connect + ping，5s 超时"""
        return self._message(await probe_mysql(mysql, timeout=MYSQL_TEST_TIMEOUT))

    async def _test_minio(self, minio: dict) -> dict:
        """MinIO 桶探测（bucket_exists），5s 超时"""
        return self._message(await probe_minio(minio, timeout=MINIO_TEST_TIMEOUT))


# 模块导入即创建单例并加载活跃档案（服务启动自动生效，无需改 main.py）
_settings_service = SettingsService()


def get_settings_service() -> SettingsService:
    return _settings_service
