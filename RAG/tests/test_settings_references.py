"""配置引用检测：删配置项前查"谁在用它"（GET /api/settings/references）

覆盖四类可删对象的引用方识别：
- 提示词条目 → 本档案 / 部门 / 外部查询
- LLM 模型   → 本档案激活项 / 外部查询（部门**不**按名字引用模型）
- 图片模型   → 本档案激活项 / 部门 image_summary.model
- 激活档案   → 影响面统计（部门数 / 外部查询数）
"""
from conftest import create_department_and_admin, create_kb

import itertools

# 外部查询要求至少暴露一个知识库；每次建库换个名字避免重名
_kb_seq = itertools.count(1)


def _refs(client, headers):
    r = client.get("/api/settings/references", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _create_ext(client, headers, name, config):
    kb = create_kb(client, name=f"引用检测库{next(_kb_seq)}")
    r = client.post("/api/ext-queries",
                    json={"name": name, "kb_ids": [kb["id"]], "config": config},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _dept_admin_headers(client, admin_headers, dept_name, username):
    _, hdrs = create_department_and_admin(
        client, admin_headers, dept_name, username, "pw123456", dept_name)
    return hdrs


class TestAccessAndEmpty:
    def test_requires_super_admin(self, client, admin_headers):
        """部门管理员读不到（超管专属；非超管一律 404 不暴露资源存在性）"""
        hdrs = _dept_admin_headers(client, admin_headers, "引用权限部", "refauth")
        assert client.get("/api/settings/references",
                          headers=hdrs).status_code == 404

    def test_nothing_configured(self, client, admin_headers):
        """没有任何引用时三类表为空（self 类引用只在档案真配了才算）"""
        data = _refs(client, admin_headers)
        assert data["prompts"] == {}
        assert data["vision_models"] == {}
        # 激活档案若配了 LLM 模型，llm_models 会有一条 self 引用——只允许 self
        for refs in data["llm_models"].values():
            assert {x["kind"] for x in refs} == {"self"}


class TestPromptReferences:
    def test_self_department_and_ext(self, client, admin_headers):
        """同一个提示词被三处引用时，三处都要能列出来"""
        prompt = "工程文档提示词"
        client.post("/api/settings/chat",
                    json={"chat": {"system_prompt_ref": prompt}},
                    headers=admin_headers)
        dept_hdrs = _dept_admin_headers(client, admin_headers, "引用测试部", "refd1")
        client.post("/api/settings/chat",
                    json={"chat": {"system_prompt_ref": prompt}},
                    headers=dept_hdrs)
        _create_ext(client, admin_headers, "引用测试链接",
                    {"system_prompt_ref": prompt})

        refs = _refs(client, admin_headers)["prompts"][prompt]
        assert sorted(x["kind"] for x in refs) == ["department", "ext_query", "self"]
        assert any("引用测试部" in x["label"] for x in refs)
        assert any("引用测试链接" in x["label"] for x in refs)

    def test_unreferenced_prompt_absent(self, client, admin_headers):
        """没被引用的条目不出现在表里（前端据此走"无引用"的简版确认）"""
        _create_ext(client, admin_headers, "无关链接",
                    {"system_prompt_ref": "别的提示词"})
        assert "没人引用的提示词" not in _refs(client, admin_headers)["prompts"]


class TestLlmModelReferences:
    def test_ext_query_references_model(self, client, admin_headers):
        _create_ext(client, admin_headers, "模型引用链接",
                    {"llm_model": "本地 Qwen"})
        refs = _refs(client, admin_headers)["llm_models"]["本地 Qwen"]
        assert [x["kind"] for x in refs] == ["ext_query"]
        assert "模型引用链接" in refs[0]["label"]

    def test_department_model_override_is_not_a_reference(self, client, admin_headers):
        """部门覆盖 llm 的 model 字段≠按名字引用（部门只覆盖"默认"那条的字段）"""
        dept_hdrs = _dept_admin_headers(client, admin_headers, "模型部", "refd2")
        client.post("/api/settings/chat",
                    json={"llm": {"model": "部门自己的模型名"}},
                    headers=dept_hdrs)
        assert "部门自己的模型名" not in _refs(client, admin_headers)["llm_models"]


class TestVisionModelReferences:
    def test_department_references_vision_model(self, client, admin_headers):
        dept_hdrs = _dept_admin_headers(client, admin_headers, "图片部", "refd3")
        client.post("/api/settings/chat",
                    json={"image_summary": {"model": "qwen-vl"}},
                    headers=dept_hdrs)
        refs = _refs(client, admin_headers)["vision_models"]["qwen-vl"]
        assert [x["kind"] for x in refs] == ["department"]
        assert "图片部" in refs[0]["label"]


class TestActiveProfile:
    def test_counts_departments_and_ext_queries(self, client, admin_headers):
        _dept_admin_headers(client, admin_headers, "统计部", "refd4")
        _create_ext(client, admin_headers, "统计链接", {})

        ap = _refs(client, admin_headers)["active_profile"]
        assert ap["id"] and ap["name"]
        assert ap["department_count"] >= 1
        assert ap["ext_query_count"] >= 1
