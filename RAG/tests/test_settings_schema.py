"""声明式 SECTION_SCHEMA 重构测试（架构审查高优先项）

覆盖：
1. schema 与 config.py dataclass 一致性（反射来源验证：段/字段/默认值）
2. 白名单从 schema 派生（settings_service 与 routers 双份消除；未知字段 400 仍生效）
3. 默认值补全 / coerce / merge / to_service_config 边界行为与重构前一致
4. 加装演练：临时注册一个测试段 → 全部处理函数自动支持（"一处定义"的证明）
"""
from __future__ import annotations

import pytest

from backend.config import (ChatConfig, ChunkingConfig, DeepDocConfig,
                            EmbeddingConfig, LLMConfig, MinerUConfig,
                            MinIOConfig, MySQLConfig, RerankConfig,
                            RetrievalConfig, ServiceConfig,
                            build_default_config)
from backend.services import settings_service as ss
from backend.services.settings_service import (SECTION_SCHEMA, FieldSpec,
                                               SectionSpec,
                                               get_settings_service,
                                               is_secret_field)

# dataclass 与 schema 的差异清单（schema 相对 dataclass 的增删——改这里须同步 schema）
_EXTRAS = {
    "embedding": {"dimension"},   # 档案专用字段（固定 1024，不写全局配置）
    "llm": {"name"},              # 模型条目显示名（const="默认"，无 dataclass 来源）
}
_DROPS = {
    "embedding": {"batch_size", "max_chars", "timeout"},
}
_MODEL_CLS = {
    "llm": LLMConfig, "embedding": EmbeddingConfig, "mineru": MinerUConfig,
    "deepdoc": DeepDocConfig, "retrieval": RetrievalConfig,
    "chunking": ChunkingConfig, "chat": ChatConfig, "mysql": MySQLConfig,
    "minio": MinIOConfig,
}
_SUBSECTIONS = {"retrieval": {"rerank": RerankConfig}}


# ==================== 1. schema 与 dataclass 一致性 ====================

class TestSchemaReflectsDataclass:

    def test_sections_match_service_config(self):
        """段列表 == ServiceConfig 全部字段（加新段后此断言自动失效提示补 schema）"""
        assert set(SECTION_SCHEMA) == set(ServiceConfig.model_fields)

    def test_section_fields_match_dataclass(self):
        """每段 schema 字段均有 dataclass 来源；dataclass 字段全部进入 schema

        （改名映射 source 计为来源；embedding 的 batch_size/max_chars/timeout
        按 _DROPS 显式排除；dimension 按 _EXTRAS 显式追加）
        """
        for sec_name, model_cls in _MODEL_CLS.items():
            spec = SECTION_SCHEMA[sec_name]
            dataclass_fields = set(model_cls.model_fields)
            for fname in set(spec.fields) - _EXTRAS.get(sec_name, set()):
                fs = spec.fields[fname]
                assert fname in dataclass_fields or (
                    fs.source and fs.source in dataclass_fields), \
                    f"{sec_name}.{fname} 无 dataclass 来源"
            renamed_sources = {fs.source for fs in spec.fields.values()
                               if fs.source and fs.source != fs.name}
            for dname in dataclass_fields:
                assert (dname in spec.fields
                        or dname in spec.subsections
                        or dname in renamed_sources
                        or dname in _DROPS.get(sec_name, set())), \
                    f"{sec_name} 的 dataclass 字段 {dname} 未进入 schema"

    def test_subsection_matches_dataclass(self):
        """retrieval.rerank 子段与 RerankConfig 一致"""
        spec = SECTION_SCHEMA["retrieval"].subsections["rerank"]
        assert set(spec.fields) == set(RerankConfig.model_fields)
        assert spec.dataclass_name == "rerank"

    def test_inferred_cast_from_dataclass_type(self):
        """cast 从 dataclass 类型推断（str/int/float/bool，Optional 解包）"""
        chat = SECTION_SCHEMA["chat"].fields
        assert chat["temperature"].cast == "float"
        assert chat["top_p"].cast == "float"
        assert chat["max_tokens"].cast == "int"
        assert chat["history_rounds"].cast == "int"
        assert chat["enable_multi_turn"].cast == "bool"
        assert chat["system_prompt"].cast == "str"
        assert SECTION_SCHEMA["llm"].fields["temperature"].cast == "float"

    def test_secret_flag_matches_is_secret_field(self):
        """secret 标记与 is_secret_field 规则自动一致（无漏标）"""
        for spec in SECTION_SCHEMA.values():
            for fname, fspec in spec.fields.items():
                assert fspec.secret == is_secret_field(fname), \
                    f"{spec.name}.{fname} secret 标记与 is_secret_field 不一致"

    def test_default_profile_matches_factory(self):
        """默认档案值 == .env 出厂配置（build_default_config）对应段/字段"""
        d = get_settings_service()._default_profile_data()
        cfg = build_default_config()
        # llm 段为模型列表：默认单条目（.env 出厂值）+ active=0
        assert d["llm"]["active"] == 0
        assert d["llm"]["models"][0]["name"] == "默认"   # 显示名常量
        assert d["llm"]["models"][0]["base_url"] == cfg.llm.base_url
        assert d["llm"]["models"][0]["api_key"] == cfg.llm.api_key
        assert d["llm"]["models"][0]["temperature"] == cfg.llm.temperature
        assert d["llm"]["models"][0]["timeout"] == cfg.llm.timeout
        assert d["embedding"]["base_url"] == cfg.embedding.base_url
        assert d["embedding"]["dimension"] == 1024  # 档案专用常量
        assert "batch_size" not in d["embedding"]   # 不进档案
        assert "timeout" not in d["llm"]            # 段级不再有标量字段（timeout 在条目内）
        assert d["mineru"]["url"] == cfg.mineru.api_url      # 字段改名映射
        assert d["chunking"]["overlap"] == cfg.chunking.chunk_overlap
        assert d["retrieval"]["rerank"]["top_n"] == cfg.retrieval.rerank.top_n
        assert d["chat"]["system_prompt"] == cfg.chat.system_prompt
        assert d["mysql"]["host"] == cfg.mysql.host
        assert d["minio"]["bucket"] == cfg.minio.bucket

    def test_all_fields_have_default_source(self):
        """每个 schema 字段默认值均有来源（const 或 dataclass 字段）"""
        cfg = build_default_config()
        for spec in SECTION_SCHEMA.values():
            cfg_sec = getattr(cfg, spec.dataclass_name)
            for fspec in spec.fields.values():
                if not fspec.in_profile:
                    continue
                if fspec.const is not ss._MISSING:
                    continue
                assert hasattr(cfg_sec, fspec.source or fspec.name), \
                    f"{spec.name}.{fspec.name} 默认值来源不存在"


# ==================== 2. 白名单从 schema 派生 ====================

class TestWhitelistFromSchema:

    def test_settings_service_whitelists(self):
        """部门合并函数用的白名单 == schema whitelist 标记"""
        assert set(ss.CHAT_FIELD_NAMES) == {
            "temperature", "top_p", "max_tokens", "enable_multi_turn",
            "history_rounds", "system_prompt"}
        assert set(ss.CHAT_RETRIEVAL_FIELD_NAMES) == {
            "top_k", "similarity_threshold"}
        assert set(ss.LLM_FIELD_NAMES) == {
            "base_url", "api_key", "model", "temperature",
            "max_tokens", "timeout"}
        # 白名单字段必在 schema 且 whitelist=True（无手写漂移）
        for fname in ss.CHAT_FIELD_NAMES:
            assert SECTION_SCHEMA["chat"].fields[fname].whitelist

    def test_routers_whitelist_same_source(self):
        """routers/settings.py 白名单与 settings_service 派生一致（双份已消除）"""
        from backend.routers import settings as router_settings
        assert router_settings.CHAT_SECTION_FIELDS == set(ss.CHAT_FIELD_NAMES)
        assert router_settings.CHAT_RETRIEVAL_FIELDS == set(
            ss.CHAT_RETRIEVAL_FIELD_NAMES)
        assert router_settings.CHAT_LLM_FIELDS == set(ss.LLM_FIELD_NAMES)
        assert router_settings.CHAT_SECTIONS == ss.CHAT_SECTIONS == {
            "chat", "retrieval", "llm"}

    def test_unknown_chat_field_still_400(self, client, admin_headers):
        """白名单派生后：chat 段未知字段仍 400（防越权语义不变）"""
        resp = client.post("/api/settings/chat",
                           json={"chat": {"api_key": "sk-evil"}},
                           headers=admin_headers)
        assert resp.status_code == 400
        assert "不允许修改聊天设置字段" in resp.json()["detail"]

    def test_infra_section_still_400(self, client, admin_headers):
        """白名单段集合派生后：基础设施段仍 400"""
        resp = client.post("/api/settings/chat",
                           json={"mineru": {"url": "http://evil:1"}},
                           headers=admin_headers)
        assert resp.status_code == 400
        assert "不允许修改配置段" in resp.json()["detail"]


# ==================== 3. 处理函数行为与重构前一致 ====================

class TestCoerceBehavior:

    def test_coerce_empty_keeps_legacy_shapes(self):
        """_coerce({}) 各段形态与重构前逐段一致（缺段部分补全契约）"""
        out = ss.SettingsService._coerce({})
        # 不补全段（仅补段内 fill_missing 字段）
        assert out["llm"] == {}
        assert out["embedding"] == {}
        assert out["mineru"] == {}
        assert out["chunking"] == {}
        # retrieval 缺段 → 只补 enable_hybrid + rerank 整段（历史契约）
        assert out["retrieval"] == {
            "enable_hybrid": build_default_config().retrieval.enable_hybrid,
            "rerank": {"enabled": False, "base_url": "", "model": "",
                       "top_n": 10},
        }
        # chat 缺段 → 只补 system_prompt=""（历史契约，不补全段）
        assert out["chat"] == {"system_prompt": ""}
        # mysql/minio/deepdoc 缺段 → 补整段默认（fill_section）
        cfg = build_default_config()
        assert out["mysql"] == {
            "host": cfg.mysql.host, "port": cfg.mysql.port,
            "user": cfg.mysql.user, "password": cfg.mysql.password,
            "database": cfg.mysql.database, "url": cfg.mysql.url,
        }
        assert out["minio"]["endpoint"] == cfg.minio.endpoint
        assert out["deepdoc"]["base_url"] == cfg.deepdoc.base_url
        assert out["deepdoc"]["dataset_prefix"] == cfg.deepdoc.dataset_prefix

    def test_coerce_numeric_types(self):
        """字符串数值按 cast 规范化（int/float/bool），str 字段保持原值"""
        out = ss.SettingsService._coerce({
            "llm": {"temperature": "0.5", "max_tokens": "100", "model": "m1"},
            "retrieval": {"top_k": "7", "similarity_threshold": "0.3",
                          "enable_hybrid": "false"},
            "chunking": {"chunk_size": "600", "overlap": "50"},
            "chat": {"temperature": "0.9", "enable_multi_turn": "true"},
            "minio": {"secure": "on"},
            "mysql": {"port": "3306"},
        })
        # 旧单对象 llm 迁移为 models[0]+active=0，条目内数值类型规范化
        assert out["llm"]["active"] == 0
        assert out["llm"]["models"][0]["temperature"] == 0.5
        assert out["llm"]["models"][0]["max_tokens"] == 100
        assert out["llm"]["models"][0]["model"] == "m1"  # str 不转换
        assert out["retrieval"]["top_k"] == 7
        assert out["retrieval"]["similarity_threshold"] == 0.3
        assert out["retrieval"]["enable_hybrid"] is False  # "false" 宽松转换
        assert out["chunking"]["chunk_size"] == 600
        assert out["chunking"]["overlap"] == 50
        assert out["chat"]["temperature"] == 0.9
        assert out["chat"]["enable_multi_turn"] is True
        assert out["minio"]["secure"] is True
        assert out["mysql"]["port"] == 3306

    def test_coerce_chunk_overlap_legacy(self):
        """旧数据 chunk_overlap → overlap 迁移保留"""
        out = ss.SettingsService._coerce(
            {"chunking": {"chunk_size": 500, "chunk_overlap": 100}})
        assert out["chunking"] == {"chunk_size": 500, "overlap": 100}
        # 同时有 overlap 时旧键移除
        out2 = ss.SettingsService._coerce(
            {"chunking": {"overlap": 30, "chunk_overlap": 100}})
        assert out2["chunking"] == {"overlap": 30}

    def test_coerce_rerank_partial_fill(self):
        """rerank 缺字段补默认；存在字段转换类型"""
        out = ss.SettingsService._coerce(
            {"retrieval": {"rerank": {"enabled": "true", "top_n": "5"}}})
        rr = out["retrieval"]["rerank"]
        assert rr["enabled"] is True
        assert rr["top_n"] == 5
        assert rr["base_url"] == ""  # 缺字段补默认
        assert rr["model"] == ""

    def test_coerce_keeps_chat_nulls(self):
        """chat 生成参数 null 合法（保持 None 不转换）"""
        out = ss.SettingsService._coerce(
            {"chat": {"temperature": None, "top_p": None, "max_tokens": None,
                      "history_rounds": 5}})
        assert out["chat"]["temperature"] is None
        assert out["chat"]["history_rounds"] == 5


class TestMergeDefaults:

    def test_merge_fills_missing_sections(self):
        """_merge_defaults 以默认档案为基底，缺段自动补齐"""
        svc = get_settings_service()
        # 旧标量 llm 提交 → 字段级合并进激活条目（models[0]），其余字段默认补齐
        merged = svc._merge_defaults({"llm": {"model": "m9"}})
        assert merged["llm"]["models"][0]["model"] == "m9"
        assert merged["llm"]["models"][0]["base_url"]  # 默认补齐
        assert merged["llm"]["active"] == 0
        # 新结构 models 整体替换（条目缺字段用默认补齐）
        merged2 = svc._merge_defaults(
            {"llm": {"models": [{"name": "新", "model": "m2"}], "active": 0}})
        assert merged2["llm"]["models"][0]["name"] == "新"
        assert merged2["llm"]["models"][0]["model"] == "m2"
        assert merged2["llm"]["models"][0]["base_url"]  # 默认补齐
        assert merged["mysql"]["host"]    # 缺段补默认
        assert merged["chat"]["system_prompt"] == ""

    def test_merge_rerank_fieldwise(self):
        """retrieval.rerank 子段按字段合并（部分字段提交不丢默认值）"""
        svc = get_settings_service()
        merged = svc._merge_defaults(
            {"retrieval": {"rerank": {"enabled": True}}})
        assert merged["retrieval"]["rerank"]["enabled"] is True
        assert merged["retrieval"]["rerank"]["top_n"] == 10  # 默认保留
        assert merged["retrieval"]["rerank"]["base_url"] == ""


class TestToServiceConfigEdges:
    """to_service_config 覆盖条件边界（truthy vs not_none，与重构前逐条核对）"""

    def _cfg(self, profile):
        return get_settings_service().to_service_config(profile)

    def test_truthy_vs_not_none_zero(self):
        base = build_default_config()
        cfg = self._cfg({
            "llm": {"models": [{"max_tokens": 0, "temperature": 0}]},
            "chat": {"max_tokens": 0, "temperature": 0},
        })
        # 激活条目：max_tokens 真值判断 0 不覆盖；temperature not_none 0 覆盖
        assert cfg.llm.max_tokens == base.llm.max_tokens
        assert cfg.llm.temperature == 0.0
        # chat 生成参数 not_none：0 覆盖
        assert cfg.chat.max_tokens == 0
        assert cfg.chat.temperature == 0.0

    def test_empty_string_overrides(self):
        """not_none 条件字段空串也覆盖（system_prompt 恢复默认 / url 清空）"""
        cfg = self._cfg({
            "chat": {"system_prompt": ""},
            "mysql": {"url": ""},
            "minio": {"region": ""},
        })
        assert cfg.chat.system_prompt == ""
        assert cfg.mysql.url == ""
        assert cfg.minio.region == ""

    def test_secret_masked_not_overwrite(self):
        """密钥字段回传脱敏值 → 不覆盖原值（secret_truthy，条目内同样生效）"""
        base = build_default_config()
        cfg = self._cfg({"llm": {"models": [{"api_key": "sk-a****7890"}]}})
        assert cfg.llm.api_key == base.llm.api_key

    def test_profile_only_fields_not_applied(self):
        """档案专用字段（embedding.dimension）不写配置；
        标量 llm.timeout 提交被忽略（段为模型列表结构，仅条目内字段生效）"""
        cfg = self._cfg({"embedding": {"dimension": 2048},
                         "llm": {"timeout": 1.0}})
        assert cfg.llm.timeout == build_default_config().llm.timeout
        # 条目内 timeout 生效（多模型条目是完整配置）
        cfg2 = self._cfg({"llm": {"models": [{"timeout": 1.5}]}})
        assert cfg2.llm.timeout == 1.5

    def test_masked_field_renames(self):
        """mineru.url → api_url、chunking.overlap → chunk_overlap 写入目标正确"""
        cfg = self._cfg({"mineru": {"url": "  http://new:8001  "},
                         "chunking": {"overlap": 42}})
        assert cfg.mineru.api_url == "http://new:8001"
        assert cfg.chunking.chunk_overlap == 42


class TestUpdateProfileSemantics:
    """update_profile 边界语义（null 清空 / 空串恢复 / 脱敏保留 / 未知字段）"""

    def test_null_clears_chat_generation_params(self):
        svc = get_settings_service()
        p = svc.create_profile("t", {"chat": {"temperature": 0.9}})
        r = svc.update_profile(p["id"], {"chat": {"temperature": None}})
        assert r["chat"]["temperature"] is None

    def test_empty_system_prompt_restores(self):
        svc = get_settings_service()
        p = svc.create_profile("t", {"chat": {"system_prompt": "x"}})
        r = svc.update_profile(p["id"], {"chat": {"system_prompt": ""}})
        assert r["chat"]["system_prompt"] == ""

    def test_null_other_fields_noop(self):
        """非 chat 段 null/空串 = 不修改（既有契约）；
        llm 旧标量提交合并进激活条目，null 同样不修改"""
        svc = get_settings_service()
        p = svc.create_profile("t", {"llm": {"model": "m1"},
                                     "minio": {"region": "r1"}})
        r = svc.update_profile(p["id"], {"llm": {"model": None},
                                         "minio": {"region": ""}})
        assert r["llm"]["models"][0]["model"] == "m1"
        assert r["minio"]["region"] == "r1"

    def test_masked_secret_keeps_original(self):
        svc = get_settings_service()
        p = svc.create_profile("t", {"llm": {"api_key": "sk-original-123"}})
        r = svc.update_profile(p["id"],
                               {"llm": {"api_key": "sk-or****-123"}})
        assert r["llm"]["models"][0]["api_key"] == "sk-o****-123"  # 公开形态 = 原值脱敏
        raw = svc.get_profile(p["id"])
        assert raw["llm"]["models"][0]["api_key"] == "sk-original-123"  # 原值保留

    def test_unknown_field_written_as_is(self):
        """update_profile 无字段白名单（档案管理 API）：非 llm 段未知字段直接写入；
        llm 段为模型列表结构：未知键忽略（仅接受 models/active/条目字段）"""
        svc = get_settings_service()
        p = svc.create_profile("t", {})
        r = svc.update_profile(p["id"], {"minio": {"custom_extra": 7}})
        assert r["minio"]["custom_extra"] == 7
        r2 = svc.update_profile(p["id"], {"llm": {"custom_extra": 9}})
        assert "custom_extra" not in r2["llm"]

    def test_history_rounds_range(self):
        """history_rounds 范围校验（schema.range 驱动，消息不变）"""
        svc = get_settings_service()
        p = svc.create_profile("t", {})
        with pytest.raises(ValueError, match="历史轮数需为 1~20"):
            svc.update_profile(p["id"], {"chat": {"history_rounds": 0}})
        r = svc.update_profile(p["id"], {"chat": {"history_rounds": 15}})
        assert r["chat"]["history_rounds"] == 15


# ==================== 4. 加装演练：新增配置段"一处定义" ====================

class TestSchemaAddSection:
    """加装演练：临时注册测试段 → 5 个处理函数自动支持（无需改任何函数）"""

    @pytest.fixture()
    def new_section(self, monkeypatch):
        """注册一个测试段 probe（复用 chat dataclass 取值，避免改 ServiceConfig）"""
        spec = SectionSpec(
            name="probe",
            dataclass_name="chat",  # 默认值/写入目标复用 chat 段（演练隔离）
            fields={
                "history_rounds": FieldSpec("history_rounds", cast="int",
                                            condition="truthy"),
                "top_p": FieldSpec("top_p", cast="float",
                                   condition="not_none", on_null="clear"),
                "level": FieldSpec("level", const=3, cast="int",
                                   condition="truthy"),
            },
            fill_section=True,
            pass_null=True,
        )
        monkeypatch.setitem(SECTION_SCHEMA, "probe", spec)
        yield spec
        # 清理：本轮测试内的 SettingsService 实例在 conftest 重置后即弃用

    def test_default_profile_supports_new_section(self, new_section):
        svc = get_settings_service()
        d = svc._default_profile_data()
        assert d["probe"] == {
            "history_rounds": build_default_config().chat.history_rounds,
            "top_p": None, "level": 3,
        }

    def test_to_service_config_supports_new_section(self, new_section):
        svc = get_settings_service()
        cfg = svc.to_service_config({"probe": {"top_p": 0.8}})
        assert cfg.chat.top_p == 0.8  # dataclass_name=chat → 写入 chat 段

    def test_coerce_supports_new_section(self, new_section):
        out = ss.SettingsService._coerce({"probe": {"top_p": "0.7"}})
        assert out["probe"]["top_p"] == 0.7
        # fill_section：缺段补整段默认
        out2 = ss.SettingsService._coerce({})
        assert out2["probe"]["level"] == 3
        assert out2["probe"]["history_rounds"] == \
            build_default_config().chat.history_rounds

    def test_merge_defaults_supports_new_section(self, new_section):
        svc = get_settings_service()
        merged = svc._merge_defaults({"probe": {"top_p": 0.6}})
        assert merged["probe"]["top_p"] == 0.6
        assert merged["probe"]["level"] == 3  # 默认保留

    def test_update_profile_supports_new_section(self, new_section, client,
                                                 admin_headers):
        """新段经档案管理 API 全链路生效（create → update → public）"""
        resp = client.post("/api/settings/profiles",
                           json={"name": "演练", "probe": {"top_p": 0.5}},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["probe"]["top_p"] == 0.5
        # 空值语义（on_null=clear）自动支持
        pid = resp.json()["id"]
        resp2 = client.put(f"/api/settings/profiles/{pid}",
                           json={"probe": {"top_p": None}},
                           headers=admin_headers)
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["probe"]["top_p"] is None
        assert resp2.json()["probe"]["level"] == 3

    def test_public_masks_new_secret_field(self, new_section, client,
                                           admin_headers):
        """新段密钥字段（api_key 后缀）自动脱敏（_public 由 schema 段驱动）"""
        resp = client.post("/api/settings/profiles",
                           json={"name": "演练密钥",
                                 "probe": {"api_key": "sk-abcdef123456"}},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["probe"]["api_key"] == "sk-a****3456"
