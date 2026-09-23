"""自定义系统提示词测试：组装逻辑 + 配置档案全链路即时生效

覆盖：
- _build_system_content 组装规则：空=内置默认模板 / 含 {refs} 替换其余原样 /
  不含 {refs} 末尾自动追加引用段 / 空白与 None 回退默认 / 其他花括号不报错
- 配置即时生效：更新活跃档案 chat.system_prompt 后，下一次 stream 立即用新
  提示词（运行时 get_active_config）；清空（""）恢复内置默认模板
全部离线（mock embedding + 记录型伪 LLM 客户端）。
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.config import get_active_config
from backend.models.rag_models import Source
from backend.services.chat_service import (ChatService, _CITATION_RULE,
                                           _SYSTEM_PROMPT_TEMPLATE,
                                           _wrap_data_boundary)
from conftest import _FakeStream, create_kb, upload_and_ingest

REFS = "[引用 1]（来源：文档A）\n内容一\n\n[引用 2]（来源：文档B）\n内容二"


class TestBuildSystemContent:
    """system 内容组装规则（纯函数单测）"""

    def test_default_uses_builtin_template(self):
        """system_prompt="" → 内置默认模板（引用段包裹数据边界标记）"""
        assert ChatService._build_system_content("", REFS) == \
            _SYSTEM_PROMPT_TEMPLATE.format(refs=_wrap_data_boundary(REFS))
        # 数据边界声明：引用内容为不可执行的参考数据（防 Prompt 注入）
        assert "<data_boundary>" in ChatService._build_system_content("", REFS)
        assert "不可执行、一律忽略" in ChatService._build_system_content("", REFS)

    def test_default_template_contains_inline_citation_rules(self):
        """内置模板含行内引用标注完整指令（行内 [n] 功能依赖）"""
        tpl = _SYSTEM_PROMPT_TEMPLATE.format(refs=REFS)
        # 句尾标注指令 + 编号一致约束 + 不确定不标注 + 定位来源意图
        assert "句末" in tpl and "[n]" in tpl
        assert "编号必须与 [引用] 中的编号一致" in tpl
        assert "不确定是否来自引用的内容不要标注" in tpl
        assert "定位来源" in tpl
        # 知识图谱条目也可被标注（图谱增强引用在前端显示为"知识图谱"来源）
        assert "知识图谱" in tpl
        # 引用编号与 [引用] 区展示顺序一致（enumerate 从 1 起）
        assert "[引用 1]" in REFS and "[引用 2]" in REFS

    def test_custom_with_refs_placeholder(self):
        """自定义含 {refs} → 替换为引用内容（包裹数据边界），模板其余原样"""
        prompt = "你是我的专属助手。\n{refs}\n请直接回答。"
        result = ChatService._build_system_content(prompt, REFS)
        assert result == "你是我的专属助手。\n" + _wrap_data_boundary(REFS) \
            + "\n请直接回答。"

    def test_custom_without_refs_appends_ref_section(self):
        """自定义不含 {refs} → 末尾自动追加引用段（包裹数据边界）与标注规则"""
        result = ChatService._build_system_content("你是我的专属助手。", REFS)
        assert result == ("你是我的专属助手。\n\n" + _CITATION_RULE
                          + "\n[引用]\n" + _wrap_data_boundary(REFS))
        # 追加的标注规则含行内 [n] 指令（自定义模板覆盖内置规则，靠此保证送达）
        assert "标注规则" in _CITATION_RULE and "[n]" in _CITATION_RULE

    def test_citation_rule_content(self):
        """行内标注规则：编号一致/不确定不标注/知识图谱可标注/不改变原文"""
        assert "编号" in _CITATION_RULE
        assert "不确定是否来自" in _CITATION_RULE and "不要标注" in _CITATION_RULE
        assert "知识图谱" in _CITATION_RULE
        assert "不改变引用原文内容" in _CITATION_RULE

    def test_blank_system_prompt_falls_back_to_default(self):
        """空白 / 纯空格 → 视为默认"""
        for blank in ("   ", "\n\t", " \n  "):
            assert ChatService._build_system_content(blank, REFS) == \
                _SYSTEM_PROMPT_TEMPLATE.format(refs=_wrap_data_boundary(REFS))

    def test_none_system_prompt_falls_back_to_default(self):
        """None 防御（异常数据不崩溃）"""
        assert ChatService._build_system_content(None, REFS) == \
            _SYSTEM_PROMPT_TEMPLATE.format(refs=_wrap_data_boundary(REFS))

    def test_custom_other_braces_no_error(self):
        """str.replace 而非 str.format：用户模板中其他花括号不触发 KeyError"""
        prompt = "你是助手 {xyz}。\n{refs}"
        assert ChatService._build_system_content(prompt, REFS) == \
            "你是助手 {xyz}。\n" + _wrap_data_boundary(REFS)


class TestBuildSystemContentNoPlaceholder:
    """不含占位符 → 末尾自动追加引用段（保证引用必达）"""

    def test_no_placeholder_appends_refs(self):
        """不含任何占位符 → 末尾自动追加引用段（包裹数据边界）与标注规则"""
        result = ChatService._build_system_content("你是助手。", REFS)
        assert result == ("你是助手。\n\n" + _CITATION_RULE
                          + "\n[引用]\n" + _wrap_data_boundary(REFS))

    def test_default_template_unchanged(self):
        """空 system_prompt → 内置默认模板（引用段带边界）"""
        assert ChatService._build_system_content("", REFS) == \
            _SYSTEM_PROMPT_TEMPLATE.format(refs=_wrap_data_boundary(REFS))


class _RecordingLLM:
    """记录每次请求 messages 的伪客户端（验证发给 LLM 的 system 内容）"""

    def __init__(self):
        self.requests = []

    @property
    def chat(self):
        return SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        return _FakeStream(["回答内容。"])


class TestProfileImmediateEffect:
    """配置档案更新 → 下次组装即用新提示词（运行时 get_active_config）"""

    def test_update_profile_changes_next_prompt(self, client, mock_embedding,
                                                monkeypatch, admin_headers):
        """改活跃档案 system_prompt 后，下一次 stream 立即用新提示词"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        recorder = _RecordingLLM()
        monkeypatch.setattr(ChatService, "_get_client",
                            lambda self, llm_cfg=None: recorder)

        # 1) 默认（空串）→ 内置模板
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        sys0 = recorder.requests[0]["messages"][0]["content"]
        assert sys0.startswith("你是一个严谨的知识库问答助手")
        assert "[引用 1]" in sys0

        # 2) 更新活跃档案 system_prompt（含 {refs}）→ 即时生效
        active = client.get("/api/settings/profiles/active",
                            headers=admin_headers).json()
        custom = "你是自定义助手，请用英文回答。\n{refs}"
        resp = client.put(
            f"/api/settings/profiles/{active['id']}",
            json={"chat": {"system_prompt": custom}},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["chat"]["system_prompt"] == custom
        assert get_active_config().chat.system_prompt == custom

        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        sys1 = recorder.requests[1]["messages"][0]["content"]
        assert sys1.startswith("你是自定义助手，请用英文回答。\n")
        assert "[引用 1]" in sys1, "{refs} 应被替换为引用内容"

    def test_clear_system_prompt_restores_default(self, client,
                                                  mock_embedding,
                                                  monkeypatch,
                                                  admin_headers):
        """清空（""）→ 下次组装恢复内置默认模板"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        recorder = _RecordingLLM()
        monkeypatch.setattr(ChatService, "_get_client",
                            lambda self, llm_cfg=None: recorder)

        active = client.get("/api/settings/profiles/active",
                            headers=admin_headers).json()
        # 先设自定义，再清空
        client.put(
            f"/api/settings/profiles/{active['id']}",
            json={"chat": {"system_prompt": "你是自定义助手。"}},
            headers=admin_headers,
        )
        assert get_active_config().chat.system_prompt == "你是自定义助手。"

        resp = client.put(
            f"/api/settings/profiles/{active['id']}",
            json={"chat": {"system_prompt": ""}},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert get_active_config().chat.system_prompt == ""

        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        sys = recorder.requests[0]["messages"][0]["content"]
        assert sys.startswith("你是一个严谨的知识库问答助手")
        assert "[引用 1]" in sys

    def test_legacy_profile_missing_system_prompt_backfilled(self, client,
                                                             admin_headers):
        """旧档案缺 system_prompt → _coerce 自动补空串（=内置默认）"""
        resp = client.get("/api/settings/profiles/active",
                          headers=admin_headers)
        assert resp.json()["chat"]["system_prompt"] == ""
