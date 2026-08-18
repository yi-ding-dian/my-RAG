"""配置档案的声明式 Schema（SECTION_SCHEMA 单一来源）

从 backend/services/settings_service.py 按职责拆分而来（行为零变化）：
本模块承载 schema 反射 / 字段白名单 / 密钥与类型辅助 / 默认值构造 /
档案数据 coerce（类型规范化 + 旧档案兼容补齐）。

======== 维护者指南：如何新增一个配置段（总共 3 处，其中 2 处是 schema 一行） ========
1. backend/config.py：定义 dataclass（含默认值）并挂到 ServiceConfig；build_default_config()
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
3. 前端 Settings.tsx 表单。routers/settings.py 白名单、_public 脱敏、coerce 类型
   规范化、update_profile 语义、_merge_defaults、to_service_config 全部自动支持，
   **无需任何改动**（它们全部由 SECTION_SCHEMA 驱动）。
   /api/settings/chat 的读写白名单 = 各段 whitelist=True 的字段。
===============================================================================
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from backend.config import (ChatConfig, ChunkingConfig, ContextualRetrievalConfig,
                            DeepDocConfig, EmbeddingConfig, IngestionConfig,
                            LLMConfig, MinerUConfig, MinIOConfig, MySQLConfig,
                            RerankConfig, RetrievalConfig, ServiceConfig,
                            build_default_config)

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
# 各处理函数（_build_default_profile / coerce / _merge_defaults / to_service_config /
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
    - fill_missing: coerce 时字段缺失（None）自动补默认值（旧档案兼容）
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
    - fill_section:    coerce 时缺段/空段自动补整段默认值（旧档案兼容）
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
    # - 旧单对象档案在 coerce 自动迁移为 models[0]+active=0；
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
    # 逻辑保留在 coerce_profile，schema 只管新字段名）
    "chunking": _reflect_section("chunking", ChunkingConfig,
        drops=("chunk_overlap",),
        overrides={
            "chunk_size": {"condition": "truthy"},
            "overlap": {"source": "chunk_overlap", "target": "chunk_overlap",
                        "cast": "int", "condition": "not_none"},
        }),
    # 上下文检索增强（入库切块后处理专用配置）：完整文档视角阈值
    # （max_full_doc_chars，默认 20000）。仅档案 CRUD（超管）可配，不进
    # /api/settings/chat 白名单；入库任务每次调用 enrich_chunks 时经
    # get_active_config().contextual_retrieval 动态读取（不缓存，改配置即
    # 生效）；fill_section/fill_missing：旧档案自动补默认段与默认字段
    "contextual_retrieval": _reflect_section(
        "contextual_retrieval", ContextualRetrievalConfig,
        overrides={
            "max_full_doc_chars": {"condition": "truthy",
                                   "fill_missing": True,
                                   "range": (1000, 1000000)},
        },
        fill_section=True),
    # 入库并发配置（后台解析任务并发上限，默认 3，范围 1~10）：仅档案 CRUD
    # （超管）可配，不进 /api/settings/chat 白名单；入库任务每次 acquire 信号量
    # 前实时读 get_active_config().ingestion.concurrency（不缓存，改配置即生效，
    # 信号量按配置值惰性重建，见 ingestion_service._get_ingest_semaphore）；
    # fill_section/fill_missing：旧档案自动补默认段与默认字段
    "ingestion": _reflect_section(
        "ingestion", IngestionConfig,
        overrides={
            "concurrency": {"condition": "truthy", "fill_missing": True,
                            "range": (1, 10)},
        },
        fill_section=True),
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
            "kg_enhance": {"condition": "not_none", "whitelist": True,
                           "fill_missing": True},
            # 思考模式（聊天问答）：disabled=关闭思考（默认）| enabled_low/
            # enabled_high/enabled_max=开启并指定强度。部门可覆盖（whitelist），
            # 旧档案缺字段 coerce 时补默认（fill_missing）
            "thinking_mode": {"condition": "not_none", "whitelist": True,
                              "fill_missing": True},
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
    """按 schema 从 .env 出厂配置构造全量默认档案（默认档案数据源）"""
    out: Dict[str, dict] = {}
    for sec_name, spec in SECTION_SCHEMA.items():
        out[sec_name] = _build_default_section(
            getattr(cfg, spec.dataclass_name), spec)
    return out


# ==================== coerce（类型规范化 + 旧档案兼容，schema 驱动） ====================

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
        _coerce_section_types(
            sub, sub_spec, getattr(cfg_sec, sub_spec.dataclass_name))


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
            _fill_missing_section(sub, sub_spec, cfg_sub)
        elif sub_spec.fill_section:
            sec[sub_name] = _build_default_section(cfg_sub, sub_spec)


def coerce_profile(data: dict) -> dict:
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
        _coerce_section_types(sec, spec, cfg_sec)
        _fill_missing_section(sec, spec, cfg_sec)
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
