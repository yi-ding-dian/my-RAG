"""视觉模型候选池：识图/图片摘要可以从「LLM 模型管理」里挑模型

背景：多模态模型常常就是同一台推理服务上的一个模型，超管已经在「LLM 模型
管理」里配过连接信息了，没道理为了读图再抄一份到「图片解析模型」里。候选池
把 **图片解析模型 + LLM 模型** 合并，选哪边都能被解析到。

但"没指定用哪个"时的回退**仍只在图片解析模型里做**：候选池里混着纯文本
模型，若没选也去抓第一个，没配视觉模型的档案会悄悄拿文本模型去读图，报出来
的是一句看不懂的调用错误，而不是"未配置图片解析模型"。
"""
from __future__ import annotations

from backend.services import image_summary as img_summ
from backend.services.settings.service import get_settings_service

VISION_MODEL = {"name": "qwen-vl", "base_url": "http://vision:8000/v1",
                "api_key": "", "model": "Qwen-VL", "timeout": 60}
LLM_MODEL = {"name": "本地 Qwen", "base_url": "http://llm:8000/v1",
             "api_key": "", "model": "Qwen3.5", "temperature": 0.3,
             "max_tokens": 4096, "timeout": 120}


def _patch_active(monkeypatch, profile: dict) -> None:
    """把"当前激活档案"换成受控数据（候选池就是从这里读的）"""
    monkeypatch.setattr(get_settings_service(), "get_active", lambda: profile)


def _profile(vision=None, llm=None) -> dict:
    p: dict = {}
    if vision is not None:
        p["vision"] = {"models": vision, "active": 0}
    if llm is not None:
        p["llm"] = {"models": llm, "active": 0}
    return p


class TestCandidatePool:
    """候选池 = 图片解析模型 + LLM 模型"""

    def test_merges_both_lists(self, monkeypatch):
        _patch_active(monkeypatch, _profile([VISION_MODEL], [LLM_MODEL]))
        names = [m["name"] for m in img_summ._candidate_models()]
        assert names == ["qwen-vl", "本地 Qwen"]

    def test_vision_wins_on_same_name(self, monkeypatch):
        """同名时留 vision 那份：它是专为视觉配的，超时/密钥可能与对话不同"""
        twin = {**LLM_MODEL, "name": "qwen-vl",
                "base_url": "http://llm-same:8000/v1"}
        _patch_active(monkeypatch, _profile([VISION_MODEL], [twin]))
        pool = img_summ._candidate_models()
        assert len(pool) == 1, pool
        assert pool[0]["base_url"] == "http://vision:8000/v1"

    def test_skips_dirty_entries(self, monkeypatch):
        """非 dict、缺名字的条目一律丢弃"""
        _patch_active(monkeypatch, _profile(
            [VISION_MODEL, "不是字典", {"name": ""}], [LLM_MODEL]))
        assert len(img_summ._candidate_models()) == 2

    def test_empty_profile(self, monkeypatch):
        _patch_active(monkeypatch, {})
        assert img_summ._candidate_models() == []


class TestResolveEntry:
    """按名字挑出生效条目"""

    def test_llm_model_can_be_selected(self, monkeypatch):
        """**本次新增的能力**：显式选中的名字可以来自 LLM 模型列表"""
        _patch_active(monkeypatch, _profile([VISION_MODEL], [LLM_MODEL]))
        entry = img_summ._resolve_entry("本地 Qwen")
        assert entry is not None
        assert entry["base_url"] == "http://llm:8000/v1"

    def test_vision_model_selected(self, monkeypatch):
        _patch_active(monkeypatch, _profile([VISION_MODEL], [LLM_MODEL]))
        entry = img_summ._resolve_entry("qwen-vl")
        assert entry is not None
        assert entry["base_url"] == "http://vision:8000/v1"

    def test_blank_falls_back_to_vision_first(self, monkeypatch):
        """没指定 → 图片解析模型第一个（LLM 列表不参与回退）"""
        _patch_active(monkeypatch, _profile([VISION_MODEL], [LLM_MODEL]))
        entry = img_summ._resolve_entry("")
        assert entry is not None
        assert entry["name"] == "qwen-vl"

    def test_deleted_name_falls_back_to_vision_first(self, monkeypatch):
        """指定的名字已被超管删掉 → 回退，不让功能直接不可用"""
        _patch_active(monkeypatch, _profile([VISION_MODEL], [LLM_MODEL]))
        entry = img_summ._resolve_entry("早就删掉的模型")
        assert entry is not None
        assert entry["name"] == "qwen-vl"

    def test_llm_only_without_selection_is_none(self, monkeypatch):
        """只有 LLM 模型、又没指定用哪个 → None

        否则会抓一个纯文本模型去读图，报出的错看不出根因。
        """
        _patch_active(monkeypatch, _profile(None, [LLM_MODEL]))
        assert img_summ._resolve_entry("") is None

    def test_llm_only_but_explicitly_selected(self, monkeypatch):
        """只有 LLM 模型，但显式选了它 → 可用（档案没建 vision 段也行）"""
        _patch_active(monkeypatch, _profile(None, [LLM_MODEL]))
        entry = img_summ._resolve_entry("本地 Qwen")
        assert entry is not None
        assert entry["model"] == "Qwen3.5"

    def test_nothing_configured(self, monkeypatch):
        _patch_active(monkeypatch, {})
        assert img_summ._resolve_entry("任意名字") is None
