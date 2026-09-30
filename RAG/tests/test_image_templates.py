"""读图模板库测试：按名解析 / 自定义优先 / 入库与聊天共用同一套策略

模板是「**看图策略**」——该看什么、不该看什么——**不含输出格式**。这是入库
摘要与聊天识图能共用的前提：入库要字段行（回填 chunk），聊天要自然语言（注入
messages 且带用户问题），输出形态不同，但"看什么"完全一样。两边共用同一套
策略，生成的描述内容才同构，以图搜图时向量才靠得近。

本文件锁住：解析规则（按名/默认/回退/超管替代）+ 两个调用方确实都吃到了模板。
"""
from __future__ import annotations

from backend.services.chat_vision import _build_messages
from backend.services.image_summary import default_prompt
from backend.services.image_templates import (BUILTIN_IMAGE_TEMPLATES,
                                              DEFAULT_TEMPLATE_NAME,
                                              resolve_template_body,
                                              template_options)

OPTS = {"label_type": True, "read_text": True, "describe_scene": True,
        "describe_layout": False}


class TestTemplateOptions:
    def test_builtin_has_expected_set(self):
        """出厂内置几套模板，且每套都有名字与正文"""
        names = [t["name"] for t in BUILTIN_IMAGE_TEMPLATES]
        assert names == ["通用", "界面与截图", "文档与证照", "实物与照片"]
        assert all(t["prompt"].strip() for t in BUILTIN_IMAGE_TEMPLATES)

    def test_configured_replaces_builtin(self):
        """超管配了模板 → **整体替代**内置（不是合并）"""
        opts = template_options([{"name": "只有这套", "prompt": "看清就行"}])
        assert [t["name"] for t in opts] == ["只有这套"]

    def test_empty_configured_falls_back_to_builtin(self):
        assert template_options(None) == [dict(t) for t in BUILTIN_IMAGE_TEMPLATES]
        assert template_options([]) == [dict(t) for t in BUILTIN_IMAGE_TEMPLATES]

    def test_half_filled_entries_dropped(self):
        """名字或正文缺一个的条目视为半填，丢弃（选中也拿不到可用提示词）"""
        opts = template_options([{"name": "有名字没正文", "prompt": ""},
                                 {"name": "", "prompt": "有正文没名字"},
                                 {"name": "完整", "prompt": "正文"}])
        assert [t["name"] for t in opts] == ["完整"]


class TestResolveTemplateBody:
    def test_by_name(self):
        body = resolve_template_body("文档与证照")
        assert "文档、证明、合同或报表" in body
        assert "编号" in body and "印章" in body

    def test_empty_name_uses_default(self):
        """不选模板 → 用默认那套（内置的「通用」）"""
        assert resolve_template_body("") == resolve_template_body(
            DEFAULT_TEMPLATE_NAME)

    def test_unknown_name_falls_back_not_empty(self):
        """★ 名字失效（超管删了/改名）→ 回退默认，**不返回空串**

        返回空串会让调用方各自的硬编码兜底生效——那等于"部门选了个失效的
        名字，行为悄悄变了"，是最难查的一类问题。
        """
        body = resolve_template_body("已经被删掉的模板")
        assert body.strip()
        assert body == resolve_template_body(DEFAULT_TEMPLATE_NAME)

    def test_configured_used_when_given(self):
        """传了超管配置 → 从配置里解析"""
        body = resolve_template_body("自建", [{"name": "自建",
                                               "prompt": "只看编号"}])
        assert body == "只看编号"


class TestChatSideUsesTemplate:
    """聊天识图：模板 + 追加段（用户问题 + 自然语言要求）"""

    def _text(self, **kw):
        kw.setdefault("data_url", "data:image/png;base64,AAA")
        return _build_messages(**kw)[0]["content"][0]["text"]

    def test_template_body_included(self):
        body = resolve_template_body("文档与证照")
        text = self._text(prompt="", max_chars=200, question="讲讲",
                          template=body)
        assert body in text, "模板正文要原样进提示词"
        assert "不要描述头像" in text

    def test_tail_appended_with_question(self):
        text = self._text(prompt="", max_chars=200, question="这个怎么配置",
                          template=resolve_template_body("界面与截图"))
        assert "「这个怎么配置」" in text, "用户问题要进提示词"
        assert "自然语言" in text and "200" in text
        assert "{question}" not in text and "{max_chars}" not in text

    def test_custom_prompt_wins_over_template(self):
        """★ 自定义提示词非空 → 优先于模板（本部门专用出口）"""
        text = self._text(prompt="只认报错码", max_chars=200, question="看图",
                          template=resolve_template_body("文档与证照"))
        assert text.startswith("只认报错码")
        assert "文档、证明、合同或报表" not in text, "自定义生效时不该再叠加模板"

    def test_empty_template_falls_back_to_default(self):
        """调用方没传模板 → 用内置默认那套（不是空提示词）"""
        text = self._text(prompt="", max_chars=200, question="q",
                          template="")
        assert resolve_template_body("") in text


class TestIngestSideUsesTemplate:
    """入库摘要：同一套模板 + 字段输出段"""

    def test_fields_prompt_contains_template_first(self):
        body = resolve_template_body("文档与证照")
        p = default_prompt(OPTS, "fields", body)
        assert p.startswith(body), "模板排在最前（先讲怎么看，再讲怎么输出）"
        assert "简介：" in p and "类型：" in p
        assert p.index("简介：") > p.index(body[-10:])

    def test_prose_prompt_contains_template(self):
        body = resolve_template_body("界面与截图")
        p = default_prompt(OPTS, "prose", body)
        assert body in p
        assert "首句" in p and "概括" in p

    def test_brief_does_not_use_template(self):
        """★ brief 档**不用模板**：它明确"不读图中文字"，与模板的"逐字抄录'
        直接冲突，硬拼会得到自相矛盾的指令"""
        body = resolve_template_body("文档与证照")
        p = default_prompt(OPTS, "brief", body)
        assert "逐字抄录" not in p, "brief 不该被塞进模板的抄录要求"
        assert "不读" in p or "不要陈述图中的具体内容与文字" in p

    def test_both_sides_share_same_strategy(self):
        """★ 共用验证：同一个模板名，两边的提示词都含它的正文"""
        for name in ("通用", "界面与截图", "文档与证照", "实物与照片"):
            body = resolve_template_body(name)
            ingest = default_prompt(OPTS, "fields", body)
            chat = _build_messages(data_url="data:image/png;base64,AAA",
                                   prompt="", max_chars=200, question="q",
                                   template=body)[0]["content"][0]["text"]
            assert body in ingest, f"{name}: 入库侧没吃到模板"
            assert body in chat, f"{name}: 聊天侧没吃到模板"
