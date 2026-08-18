"""服务配置档案管理（多 profile 持久化 + 活跃切换 + 连接测试）——编排层

- 持久化到 data/settings.json，多档案（每档案含 LLM/Embedding/MinerU/DeepDoc/
  检索/切块/MySQL/MinIO 全量配置）
- 活跃档案切换时调用 config.set_active_config()，各 service 运行时动态读取 → 即时生效
  （MySQL 配置变更 → db.get_engine() key 比对自动重建，无需重启）
- 服务启动（模块导入）时自动加载活跃档案
- 连接测试：LLM 发最小 chat 请求、Embedding 发 1 条 embed、MinerU 健康探测、
  DeepDoc 登录探测（RSA 加密密码 POST /v1/user/login）、MySQL aiomysql ping、
  MinIO bucket 探测 → 返回 {ok, latency_ms, message} 结构（实现见 settings_test）
- 密钥字段脱敏泛化（is_secret_field: endswith api_key/password/secret_key）：
  GET 返回脱敏；保存时传回脱敏值不覆盖原值

======== 按职责拆分（本文件为编排层，纯逻辑按职责下沉到同目录模块） ========
- settings_schema.py：SECTION_SCHEMA 声明式反射 / 字段白名单 / 密钥与类型辅助 /
  默认值构造 / coerce（类型规范化 + 旧档案兼容）——**新增配置段改这里**
  （维护者指南见该文件 docstring）
- settings_merge.py：chat_payload / merge_chat_config / merge_department_llm /
  active_llm_item 等纯合并函数
- settings_validate.py：_FIELD_LABELS / validate_range（update_profile 范围校验）
- settings_test.py：SettingsTester mixin（连接测试，含 LLM_TEST_TIMEOUT 等常量）
- 本文件保留对外主入口 / 档案 CRUD / 活跃配置获取 / 合并入口（re-export）/
  依赖单例的查询（find_llm_item / llm_cfg_for_parser）——全部原符号照常
  可从 backend.services.settings_service import（routers/chat_service/
  contextual_retriever/agentic_chunker/knowledge_graph_service/documents 等
  既有引用零改动）。
===============================================================================
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Dict, List, Optional

from openai import OpenAI

from backend.config import (DATA_DIR, ServiceConfig,
                            build_default_config, set_active_config)
from backend.services.settings_merge import (active_llm_item, chat_payload,
                                             merge_chat_config,
                                             merge_department_llm)
from backend.services.settings_schema import (CHAT_FIELD_NAMES,
                                              CHAT_RETRIEVAL_FIELD_NAMES,
                                              CHAT_SECTIONS, FieldSpec,
                                              LLM_FIELD_NAMES, SECTION_SCHEMA,
                                              SectionSpec, _MISSING,
                                              _build_default_profile,
                                              _build_default_section,
                                              _cast_value, _coerce_bool,
                                              _passes_condition,
                                              coerce_profile, is_masked,
                                              is_secret_field, mask_api_key)
from backend.services.settings_test import (DEEPDOC_TEST_TIMEOUT,
                                            EMBEDDING_TEST_TIMEOUT,
                                            LLM_TEST_TIMEOUT,
                                            MINERU_TEST_TIMEOUT,
                                            MINIO_TEST_TIMEOUT,
                                            MYSQL_TEST_TIMEOUT,
                                            SettingsTester)
from backend.services.settings_validate import _FIELD_LABELS, validate_range

logger = logging.getLogger(__name__)

SETTINGS_FILE = DATA_DIR / "settings.json"


class SettingsService(SettingsTester):

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
        return coerce_profile(data)

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
                        # range 范围校验（越界 → ValueError，文案带字段中文标签；
                        # 如 chat.history_rounds 1~20 / ingestion.concurrency 1~10）
                        target[k] = validate_range(fspec, k, v)
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


# ================= 依赖单例的查询（连接测试/合并函数见 settings_test / settings_merge） =================

def find_llm_item(ident: str) -> Optional[dict]:
    """按标识从当前激活档案的 LLM 模型列表查完整条目（name 优先，model 次之）

    供解析配置 parse_llm_model（上下文摘要/知识图谱抽取指定模型）使用：
    返回条目含 api_key（内部使用，绝不对外）；未指定标识 / 无激活档案 /
    未找到 → None（调用方回退激活模型）。标识匹配用 str 严格相等。
    """
    if not ident:
        return None
    profile = get_settings_service().get_active()
    if not profile:
        return None
    llm = profile.get("llm")
    if not isinstance(llm, dict):
        return None
    models = llm.get("models")
    if not isinstance(models, list):
        return None
    for m in models:
        if not isinstance(m, dict):
            continue
        if str(m.get("name") or "") == ident:
            return dict(m)
    for m in models:
        if not isinstance(m, dict):
            continue
        if str(m.get("model") or "") == ident:
            return dict(m)
    return None


def llm_cfg_for_parser(ident) -> Optional[dict]:
    """解析配置指定模型 → 完整 LLM 配置 dict（含 api_key，供上下文摘要/图谱
    抽取客户端覆盖激活模型）；未指定（None/空串）/查不到 → None（回退激活）"""
    if not ident:
        return None
    return find_llm_item(str(ident))


# 模块导入即创建单例并加载活跃档案（服务启动自动生效，无需改 main.py）
_settings_service = SettingsService()


def get_settings_service() -> SettingsService:
    return _settings_service
