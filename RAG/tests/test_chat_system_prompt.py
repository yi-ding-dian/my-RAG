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
                                           _SYSTEM_PROMPT_TEMPLATE)
from conftest import _FakeStream, create_kb, upload_and_ingest

REFS = "[引用 1]（来源：文档A）\n内容一\n\n[引用 2]（来源：文档B）\n内容二"


def _make_knowledge_sources() -> list:
    """构造 {knowledge} 测试用 sources：父块（含图片/表格）+ 无父块子块 + 纯空白"""
    return [
        Source(
            id="docA_0", text="子块文本A",
            parent_text="父块A：工程文档说明\n\n"
                        "![示意图](/api/files/images/docA/img1.jpg)\n\n"
                        "| 列1 | 列2 |\n| --- | --- |\n| 值甲 | 值乙 |",
            document_id="docA", document_name="文档A", kb_id="kb1",
        ),
        Source(id="docB_1", text="子块文本B", parent_text=None,
               document_id="docB", document_name="文档B", kb_id="kb1"),
        Source(id="docC_2", text="   ", parent_text="\n  \n",
               document_id="docC", document_name="文档C", kb_id="kb1"),
    ]


KNOWLEDGE = ChatService._build_knowledge(_make_knowledge_sources())


class TestBuildSystemContent:
    """system 内容组装规则（纯函数单测）"""

    def test_default_uses_builtin_template(self):
        """system_prompt="" → 内置默认模板（现有行为零变化）"""
        assert ChatService._build_system_content("", REFS) == \
            _SYSTEM_PROMPT_TEMPLATE.format(refs=REFS)

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
        """自定义含 {refs} → 替换为引用内容，模板其余原样"""
        prompt = "你是我的专属助手。\n{refs}\n请直接回答。"
        result = ChatService._build_system_content(prompt, REFS)
        assert result == "你是我的专属助手。\n" + REFS + "\n请直接回答。"

    def test_custom_without_refs_appends_ref_section(self):
        """自定义不含 {refs} → 末尾自动追加引用段与行内标注规则"""
        result = ChatService._build_system_content("你是我的专属助手。", REFS)
        assert result == ("你是我的专属助手。\n\n" + _CITATION_RULE
                          + "\n[引用]\n" + REFS)
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
                _SYSTEM_PROMPT_TEMPLATE.format(refs=REFS)

    def test_none_system_prompt_falls_back_to_default(self):
        """None 防御（异常数据不崩溃）"""
        assert ChatService._build_system_content(None, REFS) == \
            _SYSTEM_PROMPT_TEMPLATE.format(refs=REFS)

    def test_custom_other_braces_no_error(self):
        """str.replace 而非 str.format：用户模板中其他花括号不触发 KeyError"""
        prompt = "你是助手 {xyz}。\n{refs}"
        assert ChatService._build_system_content(prompt, REFS) == \
            "你是助手 {xyz}。\n" + REFS


class TestBuildKnowledge:
    """{knowledge} 纯知识文本组装：原文逐字、无来源包装、父块优先"""

    def test_no_citation_wrapper(self):
        """不出现 "[引用" / "（来源：" 字样（无任何来源包装）"""
        k = ChatService._build_knowledge(_make_knowledge_sources())
        assert "[引用" not in k
        assert "（来源：" not in k

    def test_parent_text_preferred_blank_line_joined(self):
        """父块优先；多片段 \n\n 空行拼接；纯空白片段跳过"""
        k = ChatService._build_knowledge(_make_knowledge_sources())
        assert k.startswith("父块A：工程文档说明")
        assert k == ("父块A：工程文档说明\n\n"
                     "![示意图](/api/files/images/docA/img1.jpg)\n\n"
                     "| 列1 | 列2 |\n| --- | --- |\n| 值甲 | 值乙 |\n\n"
                     "子块文本B")
        assert "子块文本A" not in k          # 有父块用父块，子块不出现
        assert not k.endswith("\n\n")        # 纯空白片段跳过，末尾无多余空行

    def test_markdown_image_table_kept_verbatim(self):
        """图片标签 ![]() / 表格逐字保留（用户模板要求原文输出含图片）"""
        k = ChatService._build_knowledge(_make_knowledge_sources())
        assert "![示意图](/api/files/images/docA/img1.jpg)" in k
        assert "| 列1 | 列2 |" in k and "值甲" in k

    def test_empty_sources(self):
        assert ChatService._build_knowledge([]) == ""


class TestBuildSystemContentKnowledge:
    """{knowledge} 占位符组装规则（与 {refs} 可并存）"""

    def test_knowledge_placeholder_replaced(self):
        """含 {knowledge} → 替换为纯知识内容；只含 {knowledge} 不追加 [引用] 段"""
        prompt = ("工程文档助手。\n<knowledge_base>\n"
                  "{knowledge}\n</knowledge_base>\n只输出原文，未找到说'未找到'")
        result = ChatService._build_system_content(prompt, REFS, KNOWLEDGE)
        assert result.startswith("工程文档助手。\n<knowledge_base>\n")
        assert result.endswith("\n</knowledge_base>\n只输出原文，未找到说'未找到'")
        assert KNOWLEDGE in result
        assert "[引用" not in result        # 模板完整掌控，无兜底追加

    def test_knowledge_and_refs_coexist(self):
        """{knowledge} 与 {refs} 并存 → 各自替换"""
        prompt = "知识：{knowledge}\n引用：{refs}"
        result = ChatService._build_system_content(prompt, REFS, KNOWLEDGE)
        assert result == f"知识：{KNOWLEDGE}\n引用：{REFS}"

    def test_knowledge_only_with_empty_knowledge(self):
        """knowledge 为空串（防御）→ 替换为空，不报错"""
        result = ChatService._build_system_content("模板：{knowledge}", REFS, "")
        assert result == "模板："

    def test_no_placeholder_appends_refs(self):
        """{knowledge} 与 {refs} 都不含 → 末尾自动追加引用段与标注规则"""
        result = ChatService._build_system_content("你是助手。", REFS, KNOWLEDGE)
        assert result == ("你是助手。\n\n" + _CITATION_RULE
                          + "\n[引用]\n" + REFS)

    def test_default_template_unchanged(self):
        """空 system_prompt → 内置默认模板（knowledge 参数不影响）"""
        assert ChatService._build_system_content("", REFS, KNOWLEDGE) == \
            _SYSTEM_PROMPT_TEMPLATE.format(refs=REFS)


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

    def test_profile_with_knowledge_placeholder(self, client, mock_embedding,
                                                monkeypatch, admin_headers):
        """profile system_prompt 含 {knowledge} → 发给 LLM 的 system 为纯知识原文

        模板结构（<knowledge_base> 块 + 固定话术）逐字保留，{knowledge}
        替换为检索片段原文（无 "[引用 n]（来源：xxx）" 包装）。
        """
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        recorder = _RecordingLLM()
        monkeypatch.setattr(ChatService, "_get_client",
                            lambda self, llm_cfg=None: recorder)

        active = client.get("/api/settings/profiles/active",
                            headers=admin_headers).json()
        custom = ("工程文档助手。\n<knowledge_base>\n{knowledge}\n"
                  "</knowledge_base>\n只输出知识库原文，未找到说'未找到'")
        resp = client.put(
            f"/api/settings/profiles/{active['id']}",
            json={"chat": {"system_prompt": custom}},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert get_active_config().chat.system_prompt == custom

        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        sys0 = recorder.requests[0]["messages"][0]["content"]
        assert sys0.startswith("工程文档助手。\n<knowledge_base>\n")
        assert sys0.endswith(
            "\n</knowledge_base>\n只输出知识库原文，未找到说'未找到'")
        assert "[引用" not in sys0      # 纯知识注入，无来源包装
        assert "Python" in sys0         # 检索片段原文确实注入
