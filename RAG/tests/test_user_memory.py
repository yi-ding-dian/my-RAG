"""用户画像/偏好记忆测试：存储 CRUD/隔离、提取合并（mock LLM）、
频率控制、注入组装、聊天集成、权限矩阵

覆盖（设计决策点 1~5）：
- 存储：按 user_id 隔离、目录自动创建、损坏兜底、路径穿越防御
- 提取：LLM 新条目/同内容加权更新/未提及衰减移除/敏感过滤 prompt 断言/
  失败静默/JSON 围栏解析
- 频率控制：每 5 轮或 30 分钟；并发防护
- 注入：开关关/无条目/有条目；内置模板与自定义模板的注入位置
- 聊天集成：system prompt 注入画像段（仅聊天问答）+ done 后触发异步提取
- 权限矩阵：本人/超管/dept_admin 本部门/他部门/无部门/未登录/用户不存在
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest

from backend.services import user_memory_service as ums
from backend.services.chat_service import (ChatService, _CITATION_RULE,
                                           _SYSTEM_PROMPT_TEMPLATE)
from conftest import FakeLLMClient, create_kb, upload_and_ingest

svc = lambda: ums.get_user_memory_service()  # noqa: E731


@pytest.fixture(autouse=True)
def _clean_extract_running():
    """每测试清理提取并发集合（服务层单例无状态，仅模块级集合跨测试残留）"""
    ums._extract_running.clear()
    yield
    ums._extract_running.clear()


@pytest.fixture()
def mock_extract_llm(monkeypatch):
    """替换 user_memory_service 的 LLM 客户端为离线伪客户端

    返回 state（{"parts": ..., "instances": []}）；parts 缺省 None 时
    FakeLLMClient 默认流式文本——提取调用走非流式（stream=False 缺省），
    parts 应为完整响应文本（如 json.dumps 的数组）。
    """
    state = {"parts": None, "instances": []}

    def _get_client(llm_cfg=None):
        inst = FakeLLMClient(mode="ok", parts=state["parts"])
        state["instances"].append(inst)
        return inst

    monkeypatch.setattr(ums, "get_llm_client", _get_client)
    return state


def _extract_json(items):
    """提取响应 JSON 文本（LLM 返回的内容）"""
    return json.dumps(items, ensure_ascii=False)


# ==================== 存储 CRUD / 隔离 ====================

class TestStorageCRUD:

    def test_dir_auto_created_and_roundtrip(self, tmp_path=None):
        """save 自动建目录；load 读回一致"""
        s = svc()
        s.update_memory("u_a", items=[{"type": "profile",
                                       "content": "从事电力行业"}])
        data = s.get_memory("u_a")
        assert data["user_id"] == "u_a"
        assert data["memory_enabled"] is False
        assert len(data["items"]) == 1
        assert data["items"][0]["content"] == "从事电力行业"
        assert data["items"][0]["type"] == "profile"
        assert data["items"][0]["confidence"] == 0.8
        assert data["items"][0]["id"]
        assert data["items"][0]["created_at"]

    def test_isolated_by_user(self):
        """按 user_id 隔离：A 的条目不影响 B"""
        s = svc()
        s.update_memory("u_a", items=[{"type": "profile", "content": "A 的行业"}])
        s.update_memory("u_b", items=[{"type": "preference", "content": "B 的偏好"}])
        a = s.get_memory("u_a")
        b = s.get_memory("u_b")
        assert [i["content"] for i in a["items"]] == ["A 的行业"]
        assert [i["content"] for i in b["items"]] == ["B 的偏好"]

    def test_corrupt_file_fallback(self):
        """文件损坏 → 兜底默认结构（不崩溃），且可继续写入"""
        s = svc()
        s.update_memory("u_c", items=[{"type": "profile", "content": "正常"}])
        from backend.config import USER_MEMORY_DIR
        (USER_MEMORY_DIR / "u_c.json").write_text("{ 坏 JSON", encoding="utf-8")
        data = s.get_memory("u_c")
        assert data["items"] == []
        assert data["memory_enabled"] is False
        # 损坏后可继续正常写
        s.update_memory("u_c", items=[{"type": "profile", "content": "恢复"}])
        assert s.get_memory("u_c")["items"][0]["content"] == "恢复"

    def test_invalid_user_id_rejected(self):
        """路径穿越防御：非法 user_id → ValueError"""
        s = svc()
        for bad in ("../etc", "a/b", "..%2Fetc", "", "中文用户"):
            with pytest.raises(ValueError):
                s._path(bad)

    def test_update_items_replace_and_keep_identity(self):
        """全量替换：无 id 新增；带 id 更新保留 created_at/confidence"""
        s = svc()
        s.update_memory("u_d", items=[{"type": "profile", "content": "旧内容",
                                       "confidence": 0.6}])
        item = s.get_memory("u_d")["items"][0]
        item_id, created_at = item["id"], item["created_at"]
        # updated_at 为秒级精度：跨秒等待验证"更新会刷新时间"
        time.sleep(1.1)
        s.update_memory("u_d", items=[
            {"id": item_id, "type": "preference", "content": "新内容"},
            {"type": "profile", "content": "新增条目"},
        ])
        items = s.get_memory("u_d")["items"]
        assert len(items) == 2
        updated = next(i for i in items if i["id"] == item_id)
        assert updated["content"] == "新内容"
        assert updated["type"] == "preference"
        assert updated["created_at"] == created_at      # 保留原创建时间
        assert updated["confidence"] == 0.6             # 保留原置信度
        assert updated["updated_at"] != updated["created_at"]
        added = next(i for i in items if i["content"] == "新增条目")
        assert added["confidence"] == 0.8

    def test_delete_item_and_clear(self):
        """删单条（不存在返回 False）；清空全部"""
        s = svc()
        s.update_memory("u_e", items=[
            {"type": "profile", "content": "甲"},
            {"type": "preference", "content": "乙"},
        ])
        items = s.get_memory("u_e")["items"]
        assert s.delete_item("u_e", items[0]["id"]) is True
        assert [i["content"] for i in s.get_memory("u_e")["items"]] == ["乙"]
        assert s.delete_item("u_e", "no_such_id") is False
        s.clear("u_e")
        assert s.get_memory("u_e")["items"] == []
        assert s.get_memory("u_e")["memory_enabled"] is False  # 开关保留（默认关）

    def test_update_enabled_only(self):
        """只改开关不动条目"""
        s = svc()
        s.update_memory("u_f", items=[{"type": "profile", "content": "x"}])
        s.update_memory("u_f", enabled=False)
        data = s.get_memory("u_f")
        assert data["memory_enabled"] is False
        assert len(data["items"]) == 1
        assert data["items"][0]["content"] == "x"


# ==================== 提取合并（mock LLM） ====================

class TestExtractMerge:

    def test_extract_new_items(self, mock_extract_llm):
        """新条目提取：类型/内容/置信度正确，记录提取时间"""
        mock_extract_llm["parts"] = [_extract_json([
            {"type": "profile", "content": "从事电力行业，SCA 系统调试",
             "confidence": 0.9},
            {"type": "preference", "content": "回答希望简洁",
             "confidence": 0.7},
        ])]
        messages = [{"role": "user", "content": "我是做电力调试的"},
                    {"role": "assistant", "content": "了解。"}]
        ok = asyncio.run(svc().extract_and_merge("u1", messages))
        assert ok is True
        items = svc().get_memory("u1")["items"]
        assert len(items) == 2
        by_content = {i["content"]: i for i in items}
        assert by_content["从事电力行业，SCA 系统调试"]["type"] == "profile"
        assert by_content["从事电力行业，SCA 系统调试"]["confidence"] == 0.9
        assert by_content["回答希望简洁"]["type"] == "preference"
        data = svc().get_memory("u1")
        assert data["last_extract_at"]
        assert data["last_extract_round"] == 1

    def test_extract_merge_same_content_weighted(self, mock_extract_llm):
        """同内容再次提取：不新增条目，置信度加权（0.7 旧 + 0.3 新）"""
        s = svc()
        s.update_memory("u2", items=[
            {"type": "profile", "content": "从事电力行业", "confidence": 0.5}])
        old_id = s.get_memory("u2")["items"][0]["id"]
        mock_extract_llm["parts"] = [_extract_json([
            {"type": "profile", "content": "从事电力行业", "confidence": 1.0}])]
        messages = [{"role": "user", "content": "我是做电力行业的"}]
        asyncio.run(s.extract_and_merge("u2", messages))
        items = s.get_memory("u2")["items"]
        assert len(items) == 1
        item = items[0]
        assert item["id"] == old_id                    # 同条目更新
        assert item["confidence"] == round(0.5 * 0.7 + 1.0 * 0.3, 3)  # 0.65
        assert item["created_at"]  # 保留

    def test_extract_decay_and_remove(self, mock_extract_llm):
        """长期未提及：置信度 ×0.9 衰减；低于 0.3 移除"""
        s = svc()
        # 0.3 × 0.9 = 0.27 < 0.3 → 一次未提及即移除
        s.update_memory("u3", items=[
            {"type": "profile", "content": "旧信息", "confidence": 0.3},
            {"type": "profile", "content": "新信息", "confidence": 0.9},
        ])
        mock_extract_llm["parts"] = [_extract_json([
            {"type": "profile", "content": "新信息", "confidence": 0.9}])]
        messages = [{"role": "user", "content": "继续聊新信息"}]
        asyncio.run(s.extract_and_merge("u3", messages))
        items = s.get_memory("u3")["items"]
        assert [i["content"] for i in items] == ["新信息"]
        # 被提及的"新信息"不衰减（保持 0.9；衰减仅针对本轮未提及条目）
        assert items[0]["confidence"] == 0.9

    def test_extract_prompt_contains_sensitive_filter(self, mock_extract_llm):
        """敏感过滤提示词送达：prompt 含密码/账号/身份证等禁止项"""
        mock_extract_llm["parts"] = [_extract_json([])]
        messages = [{"role": "user", "content": "我的账号是 abc123"}]
        asyncio.run(svc().extract_and_merge("u4", messages))
        inst = mock_extract_llm["instances"][0]
        sys_prompt = inst.last_kwargs["messages"][0]["content"]
        assert "密码" in sys_prompt and "账号" in sys_prompt
        assert "身份证" in sys_prompt and "银行卡" in sys_prompt
        assert "稳定" in sys_prompt and "偏好" in sys_prompt
        # 对话原文在 user 消息
        assert "我的账号是 abc123" in inst.last_kwargs["messages"][1]["content"]

    def test_extract_llm_failure_silent(self, monkeypatch, mock_extract_llm):
        """LLM 失败静默：返回 False、不抛异常、条目不变、不记录提取时间"""
        s = svc()
        s.update_memory("u5", items=[{"type": "profile", "content": "保留"}])

        class Boom:
            def __init__(self):
                self.chat = type("c", (), {
                    "completions": type("cc", (), {
                        "create": self._create})})()

            async def _create(self, **kwargs):
                raise RuntimeError("LLM 挂了")

        monkeypatch.setattr(ums, "get_llm_client", lambda llm_cfg=None: Boom())
        messages = [{"role": "user", "content": "聊聊"}]
        ok = asyncio.run(s.extract_and_merge("u5", messages))
        assert ok is False
        assert s.get_memory("u5")["items"][0]["content"] == "保留"
        assert s.get_memory("u5")["last_extract_at"] == ""  # 未记录 → 下次可重试

    def test_extract_non_json_output_silent(self, mock_extract_llm):
        """LLM 输出非 JSON → 静默失败（下次可重试）"""
        mock_extract_llm["parts"] = ["抱歉，我无法完成该请求。"]
        messages = [{"role": "user", "content": "聊聊"}]
        ok = asyncio.run(svc().extract_and_merge("u6", messages))
        assert ok is False
        assert svc().get_memory("u6")["items"] == []

    def test_extract_json_fence_tolerated(self, mock_extract_llm):
        """LLM 输出带 ```json 围栏 → 正常解析"""
        mock_extract_llm["parts"] = [
            "```json\n" + _extract_json(
                [{"type": "profile", "content": "电力行业", "confidence": 0.8}])
            + "\n```"]
        messages = [{"role": "user", "content": "我搞电力的"}]
        ok = asyncio.run(svc().extract_and_merge("u7", messages))
        assert ok is True
        assert svc().get_memory("u7")["items"][0]["content"] == "电力行业"

    def test_extract_empty_conversation_skipped(self, mock_extract_llm):
        """无对话消息 → 不调 LLM、返回 False"""
        ok = asyncio.run(svc().extract_and_merge("u8", []))
        assert ok is False
        assert mock_extract_llm["instances"] == []

    def test_extract_empty_result_updates_frequency_only(self, mock_extract_llm):
        """LLM 合法返回空数组：条目不变但更新提取记录（防每次对话重试）"""
        s = svc()
        s.update_memory("u9", items=[{"type": "profile", "content": "已有",
                                      "confidence": 0.6}])
        mock_extract_llm["parts"] = [_extract_json([])]
        messages = [{"role": "user", "content": "今天天气不错"}]
        ok = asyncio.run(s.extract_and_merge("u9", messages))
        assert ok is True
        data = s.get_memory("u9")
        assert data["items"][0]["confidence"] == round(0.6 * 0.9, 3)  # 衰减
        assert data["last_extract_at"] != ""
        # 衰减后仍保留（0.54 ≥ 0.3）
        assert len(data["items"]) == 1


# ==================== 频率控制 ====================

class TestFrequencyControl:

    def test_first_extract_allowed(self):
        """从未提取过 → 允许（首次对话即提取）"""
        assert svc().should_extract("f1", 1) is True

    def test_round_gate(self, mock_extract_llm):
        """不足 5 轮跳过；≥5 轮放行"""
        s = svc()
        mock_extract_llm["parts"] = [_extract_json(
            [{"type": "profile", "content": "x", "confidence": 0.8}])]
        # 第 6 轮提取成功 → last=6
        messages = [{"role": "user", "content": "m"}]
        assert asyncio.run(s.extract_and_merge("f2", messages,
                                               current_round=6)) is True
        assert s.get_memory("f2")["last_extract_round"] == 6
        # 还差 2 轮 → 跳过
        assert s.should_extract("f2", 8) is False
        # 达到 5 轮 → 放行
        assert s.should_extract("f2", 11) is True

    def test_round_gate_skips_llm_call(self, mock_extract_llm):
        """频率拦截时不调 LLM"""
        s = svc()
        mock_extract_llm["parts"] = [_extract_json(
            [{"type": "profile", "content": "x", "confidence": 0.8}])]
        messages = [{"role": "user", "content": "m"}]
        asyncio.run(s.extract_and_merge("f3", messages, current_round=6))
        assert mock_extract_llm["instances"][0].call_count == 1
        asyncio.run(s.extract_and_merge("f3", messages, current_round=8))
        assert mock_extract_llm["instances"][0].call_count == 1  # 未再调

    def test_time_gate(self):
        """距上次 ≥30 分钟 → 放行（即使轮数不足）"""
        s = svc()
        s.update_memory("f4", items=[{"type": "profile", "content": "x"}])
        data = s.get_memory("f4")
        old = datetime.now() - timedelta(minutes=31)
        data["last_extract_at"] = old.strftime("%Y-%m-%d %H:%M:%S")
        data["last_extract_round"] = 5
        ums.USER_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        (ums.USER_MEMORY_DIR / "f4.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        assert s.should_extract("f4", 7) is True    # 7-5=2 <5 但时间满足
        # 未到 30 分钟 → 仅轮数判定
        data["last_extract_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        (ums.USER_MEMORY_DIR / "f4.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        assert s.should_extract("f4", 7) is False

    def test_concurrent_skip(self, mock_extract_llm):
        """同一用户已有提取任务在跑 → 跳过"""
        ums._extract_running.add("f5")
        messages = [{"role": "user", "content": "m"}]
        ok = asyncio.run(svc().extract_and_merge("f5", messages))
        assert ok is False
        assert mock_extract_llm["instances"] == []

    def test_extract_records_round(self, mock_extract_llm):
        """提取成功后 last_extract_round 记录当前轮次"""
        s = svc()
        mock_extract_llm["parts"] = [_extract_json(
            [{"type": "profile", "content": "x", "confidence": 0.8}])]
        messages = [{"role": "user", "content": "m"}]
        asyncio.run(s.extract_and_merge("f6", messages, current_round=20))
        assert s.get_memory("f6")["last_extract_round"] == 20


# ==================== 注入组装 ====================

class TestBuildMemoryContext:

    def test_no_file_empty(self):
        """无画像文件 → 空串（不注入）"""
        assert svc().build_memory_context("nobody") == ""

    def test_disabled_empty(self):
        """开关关 → 空串"""
        s = svc()
        s.update_memory("m1", items=[{"type": "profile", "content": "x"}])
        s.update_memory("m1", enabled=False)
        assert s.build_memory_context("m1") == ""

    def test_no_items_empty(self):
        """有文件无条目 → 空串"""
        s = svc()
        s.update_memory("m2", enabled=True)
        assert s.build_memory_context("m2") == ""

    def test_grouped_by_type(self):
        """条目按类型分组：画像在前、偏好在后"""
        s = svc()
        s.update_memory("m3", items=[
            {"type": "preference", "content": "喜欢简洁回答"},
            {"type": "profile", "content": "电力行业"},
            {"type": "preference", "content": "用中文"},
        ], enabled=True)
        ctx = s.build_memory_context("m3")
        assert "【用户画像" in ctx
        assert ctx.index("用户画像：") < ctx.index("偏好：")
        assert "电力行业" in ctx and "喜欢简洁回答" in ctx and "用中文" in ctx

    def test_profile_only(self):
        """只有画像无偏好 → 不输出偏好行"""
        s = svc()
        s.update_memory("m4", items=[{"type": "profile", "content": "x"}],
                        enabled=True)
        ctx = s.build_memory_context("m4")
        assert "偏好：" not in ctx and "用户画像：x" in ctx


class TestBuildSystemContentMemory:
    """_build_system_content 注入位置（内置模板/自定义模板）"""

    REFS = "[引用 1]（来源：文档A）\n内容一"
    MEM = "【用户画像】\n用户画像：电力行业"

    def test_default_injects_before_refs(self):
        """内置模板：画像段在引用内容之前；无 memory 行为零变化"""
        out = ChatService._build_system_content("", self.REFS, memory=self.MEM)
        # 注意：模板规则文本本身含 "[引用]" 字样，须以引用内容（REFS 原文）定位
        assert out.index(self.MEM) < out.index(self.REFS)
        assert out.endswith("\n\n" + self.REFS)
        assert ChatService._build_system_content("", self.REFS) == \
            _SYSTEM_PROMPT_TEMPLATE.format(refs=self.REFS)

    def test_custom_memory_placeholder(self):
        """自定义模板 {memory} 占位符 → 原位替换"""
        out = ChatService._build_system_content(
            "你是助手。\n{memory}\n{refs}", self.REFS, memory=self.MEM)
        assert out == f"你是助手。\n{self.MEM}\n{self.REFS}"

    def test_custom_without_memory_placeholder_no_inject(self):
        """自定义含 {refs} 但无 {memory} → 不注入（模板自行掌控）"""
        out = ChatService._build_system_content(
            "你是助手。\n{refs}", self.REFS, memory=self.MEM)
        assert self.MEM not in out
        assert out == f"你是助手。\n{self.REFS}"

    def test_no_placeholder_injects_before_refs(self):
        """无占位符自动追加引用段：画像插在引用段前"""
        out = ChatService._build_system_content(
            "你是助手。", self.REFS, memory=self.MEM)
        assert out == ("你是助手。\n\n" + _CITATION_RULE
                       + f"\n{self.MEM}\n\n[引用]\n{self.REFS}")

    def test_memory_empty_unchanged(self):
        """memory 空串 → 与历史完全一致"""
        assert ChatService._build_system_content("你是助手。", self.REFS) == \
            ("你是助手。\n\n" + _CITATION_RULE + "\n[引用]\n" + self.REFS)


# ==================== 聊天集成（system 注入 + 异步提取触发） ====================

class _RecordingLLM:
    """记录请求 messages 的伪 LLM（复用 chat 测试模式）"""

    def __init__(self):
        self.requests = []

    @property
    def chat(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        from conftest import _FakeStream
        self.requests.append(kwargs)
        return _FakeStream(["回答内容。"])


class TestChatIntegration:

    def _build(self, client, mock_embedding, monkeypatch):
        """建库入库 + 记录 LLM + 记录提取触发"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        recorder = _RecordingLLM()
        monkeypatch.setattr(ChatService, "_get_client",
                            lambda self, llm_cfg=None: recorder)
        extract_calls = []

        def _record_extract(user_id, messages):
            extract_calls.append((user_id, len(messages)))

        monkeypatch.setattr(ChatService, "_schedule_memory_extract",
                            staticmethod(_record_extract))
        return kb, recorder, extract_calls

    def test_system_injects_memory_and_triggers_extract(
            self, client, mock_embedding, monkeypatch, admin_headers):
        """有画像条目：聊天 system 注入画像段；done 后异步触发提取"""
        kb, recorder, extract_calls = self._build(client, mock_embedding,
                                                  monkeypatch)
        # admin 本人先写画像
        me_id = client.get("/api/auth/me", headers=admin_headers).json()["id"]
        resp = client.put(f"/api/users/{me_id}/memory", json={
            "enabled": True,
            "items": [
                {"type": "profile", "content": "从事电力行业，SCA 系统调试"},
            ],
        }, headers=admin_headers)
        assert resp.status_code == 200

        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        sys0 = recorder.requests[0]["messages"][0]["content"]
        assert "用户画像" in sys0 and "从事电力行业" in sys0
        # 画像段在引用段之前
        assert sys0.index("从事电力行业") < sys0.index("[引用 1]")
        # done 后触发异步提取（含本次会话消息）
        assert len(extract_calls) == 1
        assert extract_calls[0][0] == me_id
        assert extract_calls[0][1] == 2  # user + assistant 两条消息

    def test_no_memory_no_inject(self, client, mock_embedding, monkeypatch,
                                  admin_headers):
        """无画像条目 → system 不注入画像段（行为与历史一致）"""
        kb, recorder, extract_calls = self._build(client, mock_embedding,
                                                  monkeypatch)
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200 and "event: done" in resp.text
        sys0 = recorder.requests[0]["messages"][0]["content"]
        assert "用户画像" not in sys0
        assert sys0.startswith("你是一个严谨的知识库问答助手")
        assert len(extract_calls) == 1  # 提取照常触发（首轮允许）

    def test_disabled_memory_no_inject(self, client, mock_embedding,
                                       monkeypatch, admin_headers):
        """开关关 → 不注入"""
        kb, recorder, _ = self._build(client, mock_embedding, monkeypatch)
        me_id = client.get("/api/auth/me", headers=admin_headers).json()["id"]
        client.put(f"/api/users/{me_id}/memory", json={"items": [
            {"type": "profile", "content": "电力行业"},
        ]}, headers=admin_headers)
        client.put(f"/api/users/{me_id}/memory", json={"enabled": False},
                   headers=admin_headers)
        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "Python 是什么？",
        }, headers=admin_headers)
        assert resp.status_code == 200
        sys0 = recorder.requests[0]["messages"][0]["content"]
        assert "用户画像" not in sys0 and "电力行业" not in sys0


# ==================== 权限矩阵 ====================

def _user_id_of(client, admin_headers, username):
    users = client.get("/api/users", headers=admin_headers).json()
    return next(u["id"] for u in users if u["username"] == username)


class TestPermissions:

    def test_owner_crud(self, client, admin_headers, user_headers):
        """本人（普通用户）：GET/PUT/DELETE 全部可用"""
        uid = _user_id_of(client, admin_headers, "user_test")
        resp = client.get(f"/api/users/{uid}/memory", headers=user_headers)
        assert resp.status_code == 200
        assert resp.json()["memory_enabled"] is False
        assert resp.json()["items"] == []
        # PUT 条目
        resp = client.put(f"/api/users/{uid}/memory", json={"items": [
            {"type": "profile", "content": "我是电力调试的"},
            {"type": "preference", "content": "喜欢简洁"},
        ]}, headers=user_headers)
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2
        # 编辑单条（全量替换 + id）
        item = resp.json()["items"][0]
        resp = client.put(f"/api/users/{uid}/memory", json={"items": [
            {"id": item["id"], "type": "profile", "content": "改后的内容"},
            {"id": resp.json()["items"][1]["id"],
             "type": "preference", "content": "喜欢简洁"},
        ]}, headers=user_headers)
        assert resp.status_code == 200
        edited = next(i for i in resp.json()["items"]
                      if i["id"] == item["id"])
        assert edited["content"] == "改后的内容"
        # 删单条
        resp = client.delete(f"/api/users/{uid}/memory?item_id={item['id']}",
                             headers=user_headers)
        assert resp.status_code == 200
        assert len(client.get(f"/api/users/{uid}/memory",
                              headers=user_headers).json()["items"]) == 1
        # 清空
        resp = client.delete(f"/api/users/{uid}/memory", headers=user_headers)
        assert resp.status_code == 200
        assert client.get(f"/api/users/{uid}/memory",
                          headers=user_headers).json()["items"] == []

    def test_unauthenticated_401(self, client):
        """未登录 → 401"""
        assert client.get("/api/users/xxx/memory").status_code == 401
        assert client.put("/api/users/xxx/memory",
                          json={"enabled": False}).status_code == 401
        assert client.delete("/api/users/xxx/memory").status_code == 401

    def test_super_admin_read_any_write_other_403(self, client, admin_headers,
                                                  user_headers):
        """超管读任意人 200；写非本人 403"""
        uid = _user_id_of(client, admin_headers, "user_test")
        resp = client.get(f"/api/users/{uid}/memory", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["user_id"] == uid
        resp = client.put(f"/api/users/{uid}/memory",
                          json={"enabled": False}, headers=admin_headers)
        assert resp.status_code == 403
        assert "只读" in resp.json()["detail"]
        assert client.delete(f"/api/users/{uid}/memory",
                             headers=admin_headers).status_code == 403

    def test_dept_admin_read_same_dept_write_403(self, client, admin_headers,
                                                 dept_admin_headers,
                                                 user_headers):
        """dept_admin 读本部门成员 200；写本部门成员（非本人）403"""
        uid = _user_id_of(client, admin_headers, "user_test")
        assert client.get(f"/api/users/{uid}/memory",
                          headers=dept_admin_headers).status_code == 200
        resp = client.put(f"/api/users/{uid}/memory",
                          json={"enabled": False},
                          headers=dept_admin_headers)
        assert resp.status_code == 403
        assert client.delete(f"/api/users/{uid}/memory",
                             headers=dept_admin_headers).status_code == 403

    def test_dept_admin_read_other_dept_404(self, client, admin_headers,
                                            dept_admin_headers):
        """dept_admin 读他部门用户 → 404 伪装"""
        # 建第二个部门 + 普通用户
        resp = client.post("/api/departments",
                           json={"name": "第二部门", "description": ""},
                           headers=admin_headers)
        dept2 = resp.json()["id"]
        resp = client.post("/api/users", json={
            "username": "user_other", "password": "user123456",
            "display_name": "他部门用户", "role": "user",
            "department_id": dept2,
        }, headers=admin_headers)
        other_id = resp.json()["id"]
        resp = client.get(f"/api/users/{other_id}/memory",
                          headers=dept_admin_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "用户不存在"  # 防探测文案

    def test_dept_admin_without_dept_404(self, client, admin_headers):
        """dept_admin 无部门归属 → 访问任意人 404 伪装"""
        resp = client.post("/api/users", json={
            "username": "admin_nodep", "password": "admin123456",
            "display_name": "无部门管理员", "role": "dept_admin",
        }, headers=admin_headers)
        assert resp.status_code == 201
        login = client.post("/api/auth/login", json={
            "username": "admin_nodep", "password": "admin123456"})
        h = {"Authorization": f"Bearer {login.json()['access_token']}"}
        admin_id = _user_id_of(client, admin_headers, "admin")
        assert client.get(f"/api/users/{admin_id}/memory",
                          headers=h).status_code == 404

    def test_normal_user_read_other_404(self, client, admin_headers,
                                        user_headers):
        """普通用户读他人 → 404 伪装（GET/PUT/DELETE）"""
        admin_id = _user_id_of(client, admin_headers, "admin")
        assert client.get(f"/api/users/{admin_id}/memory",
                          headers=user_headers).status_code == 404
        assert client.put(f"/api/users/{admin_id}/memory",
                          json={"enabled": False},
                          headers=user_headers).status_code == 404
        assert client.delete(f"/api/users/{admin_id}/memory",
                             headers=user_headers).status_code == 404

    def test_target_not_found_404(self, client, admin_headers, user_headers):
        """目标用户不存在 → 404"""
        assert client.get("/api/users/no_such_user/memory",
                          headers=admin_headers).status_code == 404
        assert client.put("/api/users/no_such_user/memory",
                          json={"enabled": False},
                          headers=user_headers).status_code == 404

    def test_put_requires_field(self, client, admin_headers, user_headers):
        """PUT 空载荷（无 enabled/items）→ 400"""
        uid = _user_id_of(client, admin_headers, "user_test")
        resp = client.put(f"/api/users/{uid}/memory",
                          json={}, headers=user_headers)
        assert resp.status_code == 400
        assert "至少传一个" in resp.json()["detail"]

    def test_delete_item_not_found_404(self, client, admin_headers,
                                       user_headers):
        """删不存在的条目 → 404"""
        uid = _user_id_of(client, admin_headers, "user_test")
        resp = client.delete(f"/api/users/{uid}/memory?item_id=no_such_id",
                             headers=user_headers)
        assert resp.status_code == 404
        assert "条目不存在" in resp.json()["detail"]

    def test_delete_user_cleans_memory_file(self, client, admin_headers):
        """删除用户 → 连带清理其画像文件（不存在静默）"""
        from backend.config import USER_MEMORY_DIR
        created = client.post("/api/users", json={
            "username": "um_delete_me", "password": "pass123456",
            "display_name": "将删", "role": "user",
        }, headers=admin_headers).json()
        uid = created["id"]
        login = client.post("/api/auth/login", json={
            "username": "um_delete_me", "password": "pass123456"})
        hdrs = {"Authorization": f"Bearer {login.json()['access_token']}"}
        client.put(f"/api/users/{uid}/memory", json={"items": [
            {"type": "profile", "content": "删除前的画像"},
        ]}, headers=hdrs)
        assert (USER_MEMORY_DIR / f"{uid}.json").exists()
        # admin 删除该用户 → 画像文件连带清理
        assert client.delete(f"/api/users/{uid}",
                             headers=admin_headers).status_code == 200
        assert not (USER_MEMORY_DIR / f"{uid}.json").exists()
        # 删除不存在用户 → 静默成功（不报错）
        assert client.delete("/api/users/no_such_user2",
                             headers=admin_headers).status_code == 404
