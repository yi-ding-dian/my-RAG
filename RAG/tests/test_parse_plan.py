"""入库方案（决策矩阵 + 成本预估）单测：纯函数直测，不起 app、不发请求

覆盖：
- **方案 A 回归锁**：引擎建议是 docx_struct 且有标题 → 主推荐层级聚合切块，
  父子分块降为备选、包含父标题推荐 False。这条锁住"推荐与默认选中打架"那个
  bug——它曾经的形态是：后端矩阵只会推父子分块，前端自己叠特判选层级聚合，
  于是 Step1 面板/Step2 徽标/默认选中三处各说各话
- 决策矩阵五条分支（QA / 层级聚合 / 父子分块 / 通用切块 / 表格）
- 契约硬断言：config 键都在入库请求字段内、每条配置都有决策理由、
  标签表与 VALID_METHODS 键集一致
- 成本预估：调用次数为主计量、"每块重发全文"的放大效应、三个门槛
- 口径常量与实现模块同源（改了一处不改另一处会红）
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.chunking.common import VALID_METHODS
from backend.models.rag_models import IngestRequest
from backend.services.smart_parse import cost as cost_mod
from backend.services.smart_parse.plan import PlanInput, build_plan
from backend.services.smart_parse.types import METHOD_LABELS, label_for


# ---- 假配置：测试确定性（不读真实 settings.json），字段路径与 config.py 一致 ----
@dataclass
class _Chunking:
    chunk_size: int = 800
    chunk_overlap: int = 100
    heading_llm_model: str = ""


@dataclass
class _Ctx:
    max_full_doc_chars: int = 20000


@dataclass
class _Img:
    max_images: int = 50


@dataclass
class _Cfg:
    chunking: _Chunking
    contextual_retrieval: _Ctx
    image_summary: _Img


@pytest.fixture
def cfg():
    return _Cfg(chunking=_Chunking(), contextual_retrieval=_Ctx(),
                image_summary=_Img())


def _plan(cfg, **kw):
    """文本文档方案的快捷构造（默认：txt、无标题、小篇幅）"""
    base = dict(kind="text", file_type="txt", engine="plain")
    base.update(kw)
    return build_plan(PlanInput(**base), cfg=cfg)


def _sheet_plan(cfg, **kw):
    base = dict(kind="spreadsheet", file_type="xlsx", engine="spreadsheet")
    base.update(kw)
    return build_plan(PlanInput(**base), cfg=cfg)


def _item(plan, key: str):
    return next(i for i in plan.cost.items if i.key == key)


# ==================== 方案 A（回归锁） ====================

class TestDocxStructHierarchical:
    """规范标题样式 + 结构解析 → 层级聚合切块（三处一致性的根）"""

    def _docx(self, cfg, **kw):
        base = dict(file_type="docx", engine="docx_struct",
                    has_headings=True, heading_count=56,
                    numbered_headings=10, is_normative=True, doc_chars=89000)
        base.update(kw)
        return _plan(cfg, **base)

    def test_main_recommendation_is_hierarchical(self, cfg):
        plan = self._docx(cfg)
        assert plan.config["method"] == "hierarchical"
        main = plan.alternatives[0]
        assert main.method == "hierarchical" and main.recommended is True
        assert main.badge == "推荐"
        # 理由要说清"为什么是它"——引擎精确是前提
        assert "结构解析" in main.reason and "标题" in main.reason

    def test_parent_child_demoted_to_alternative(self, cfg):
        """父子分块降为备选（原先它是主推荐，与前端默认选中打架）"""
        plan = self._docx(cfg)
        methods = [a.method for a in plan.alternatives]
        assert methods[0] == "hierarchical"
        assert "parent_child" in methods
        pc = next(a for a in plan.alternatives if a.method == "parent_child")
        assert pc.recommended is False and pc.badge == "备选"

    def test_heading_in_content_off_for_hierarchical(self, cfg):
        """包含父标题推荐 False：层级聚合的块自带标题链，再拼一遍会重复

        service 侧对该方式本就显式跳过这个开关——推荐 True 等于推荐一个空开关。
        """
        plan = self._docx(cfg)
        assert plan.config["enable_heading_in_content"] is False
        assert plan.switches["enable_heading_in_content"] is False

    def test_heading_in_content_on_for_other_heading_methods(self, cfg):
        """非层级聚合的有标题文档仍推荐包含父标题（行为未变）"""
        plan = _plan(cfg, file_type="pdf", engine="mineru",
                     has_headings=True, heading_count=20)
        assert plan.config["method"] == "parent_child"
        assert plan.config["enable_heading_in_content"] is True

    def test_docx_without_headings_stays_naive(self, cfg):
        """docx_struct 但没标题：退回通用切块（层级聚合要有树可聚合）"""
        plan = self._docx(cfg, has_headings=False, heading_count=0,
                          numbered_headings=0)
        assert plan.config["method"] == "naive"


# ==================== 决策矩阵全枚举 ====================

class TestDecisionMatrix:

    def test_qa_document(self, cfg):
        plan = _plan(cfg, is_qa=True, qa_pairs=12, qa_ratio=0.8)
        assert plan.config["method"] == "qa"
        assert "问答对" in plan.alternatives[0].reason
        # QA 自带上下文，不该推上下文检索
        assert plan.config["contextual_retrieval"] is False
        # QA 无切块参数（参数对问答对整块无意义）
        assert "chunk_size" not in plan.config

    def test_headings_parent_child(self, cfg):
        plan = _plan(cfg, file_type="pdf", engine="mineru",
                     has_headings=True, heading_count=30)
        assert plan.config["method"] == "parent_child"
        assert plan.config["chunk_size"] == 512
        assert plan.config["overlap"] == 50
        assert plan.config["retrieval_mode"] == "parent"
        assert plan.config["parent_split_level"] == 2

    def test_no_headings_under_threshold(self, cfg):
        plan = _plan(cfg, doc_chars=5000, threshold_chars=20000)
        assert plan.config["method"] == "naive"
        # 无标题且不超阈值 → 推荐上下文检索（给孤立块补全局背景）
        assert plan.config["contextual_retrieval"] is True

    def test_no_headings_over_threshold(self, cfg):
        """超阈值：不是"不建议"而是"不可开启"（开了会让整文档入库失败）"""
        plan = _plan(cfg, doc_chars=30000, threshold_chars=20000,
                     over_threshold=True)
        assert plan.config["contextual_retrieval"] is False
        ctx_item = _item(plan, "contextual_retrieval")
        assert ctx_item.available is False and ctx_item.calls == 0
        assert "不可开启" in ctx_item.detail

    def test_spreadsheet(self, cfg):
        plan = _sheet_plan(cfg, sheet_count=3, total_rows=1200)
        assert plan.config["method"] == "title"
        assert plan.config["contextual_retrieval"] is False
        assert "3 个 Sheet" in plan.alternatives[0].reason

    def test_parser_engine_only_for_parsed_types(self, cfg):
        """只有走解析器的类型才写 parser_engine（txt 写了没意义）"""
        assert "parser_engine" not in _plan(cfg, file_type="txt",
                                            engine="plain").config
        assert _plan(cfg, file_type="docx", engine="docx_struct",
                     has_headings=True, heading_count=5
                     ).config["parser_engine"] == "docx_struct"

    def test_default_chunk_size_follows_active_config(self, cfg):
        """泛用切块的大小跟随活跃配置（修掉前端硬编码 800 的漂移）"""
        cfg.chunking.chunk_size = 1200
        cfg.chunking.chunk_overlap = 150
        plan = _plan(cfg)
        assert plan.config["chunk_size"] == 1200
        assert plan.config["overlap"] == 150


# ==================== 契约硬断言 ====================

class TestContract:

    def _plans(self, cfg):
        return [
            _plan(cfg),
            _plan(cfg, file_type="docx", engine="docx_struct",
                  has_headings=True, heading_count=66, is_normative=True),
            _plan(cfg, is_qa=True, qa_pairs=5, qa_ratio=0.8),
            _sheet_plan(cfg, sheet_count=2),
            _plan(cfg, file_type="pdf", engine="mineru",
                  has_headings=True, heading_count=9),
        ]

    def test_config_keys_are_valid_ingest_fields(self, cfg):
        """config 的每个键都必须是入库请求的合法字段（构造期就该拦住，而非 422）"""
        for plan in self._plans(cfg):
            assert set(plan.config) <= set(IngestRequest.model_fields)

    def test_every_config_key_has_a_decision(self, cfg):
        """每条配置都有中文理由（前端直接展示，用户能看懂为什么这么定）"""
        for plan in self._plans(cfg):
            reasoned = {d.key for d in plan.decisions}
            assert set(plan.config) <= reasoned
            assert all(d.reason.strip() for d in plan.decisions)

    def test_method_is_never_regex(self, cfg):
        """矩阵只推可自动配置的方式：regex 需要用户给正则，不自动选"""
        for plan in self._plans(cfg):
            assert plan.config["method"] in VALID_METHODS
            assert plan.config["method"] != "regex"

    def test_method_labels_cover_all_methods(self):
        """标签表键集 == VALID_METHODS

        历史上这里漏过 hierarchical：矩阵一旦推层级聚合就查不到标签直接 KeyError。
        """
        assert set(METHOD_LABELS) == set(VALID_METHODS)
        assert label_for("hierarchical") == "层级聚合切块"
        assert label_for("未登记的方式") == "未登记的方式"   # 回退原值

    def test_paid_switches_have_cost_items(self, cfg):
        """**花钱的**开关必须在成本表里有对应项（前端按开关查成本项做预算预览）

        免费的开关（包含父标题等）不该有成本项——它不调 LLM。
        """
        paid = {"contextual_retrieval", "knowledge_graph", "image_summary"}
        for plan in self._plans(cfg):
            assert paid <= set(plan.switches)
            assert paid <= {i.key for i in plan.cost.items}
        # 免费开关不进成本表
        assert "enable_heading_in_content" not in {
            i.key for i in self._plans(cfg)[0].cost.items}

    def test_recommendations_projection_shape(self, cfg):
        """兼容投影：旧结构形状冻结（4 个未迁移的消费点还在读它）"""
        plan = _plan(cfg, file_type="docx", engine="docx_struct",
                     has_headings=True, heading_count=66, is_normative=True)
        rec = plan.to_recommendations()
        assert set(rec) == {"chunk_method", "alternatives",
                            "contextual_retrieval", "enable_heading_in_content"}
        assert rec["chunk_method"]["method"] == "hierarchical"
        assert rec["chunk_method"]["recommended"] is True
        assert isinstance(rec["contextual_retrieval"]["recommended"], bool)
        assert all("method" in a and "reason" in a for a in rec["alternatives"])
        # 投影与 plan 同源：主推荐永远不出现在备选里
        assert all(a["method"] != "hierarchical" for a in rec["alternatives"])


# ==================== 成本预估 ====================

class TestCostEstimate:

    def test_chunk_count_uses_effective_step(self, cfg):
        """块数按有效步长（chunk_size - overlap）估算"""
        plan = _plan(cfg, doc_chars=18000)
        # ceil(18000 / (800-100)) = 26
        assert plan.cost.chunks == 26

    def test_contextual_retrieval_resends_whole_doc(self, cfg):
        """上下文检索每次调用都重发全文——这个放大效应必须如实反映

        它是最贵的环节（O(块数 × 文档长度)），预估若按"只发块文本"算会低估
        一个数量级，用户就没法据此做预算判断。
        """
        plan = _plan(cfg, doc_chars=18000, threshold_chars=20000)
        ctx = _item(plan, "contextual_retrieval")
        assert ctx.calls == 26
        # 每块 ≈ min(18000, 20000) + 1000 块文本
        assert ctx.input_chars >= 26 * 18000
        assert "重发全文" in ctx.detail

    def test_knowledge_graph_is_far_cheaper_than_context(self, cfg):
        """图谱只发块文本，比上下文检索便宜得多（同块数下差一个数量级）"""
        plan = _plan(cfg, doc_chars=18000, threshold_chars=20000)
        ctx = _item(plan, "contextual_retrieval")
        kg = _item(plan, "knowledge_graph")
        assert kg.calls == ctx.calls == 26
        assert kg.input_chars < ctx.input_chars / 10

    def test_agentic_reads_once_but_writes_back_whole_text(self, cfg):
        plan = _plan(cfg, doc_chars=12000)
        ag = _item(plan, "agentic")
        assert ag.calls == 1
        # 逐字拷贝回填 → 输出≈输入
        assert ag.output_chars == ag.input_chars == 12000

    def test_agentic_unavailable_over_hard_limit(self, cfg):
        plan = _plan(cfg, doc_chars=60000)
        ag = _item(plan, "agentic")
        assert ag.available is False and ag.calls == 0

    def test_totals_only_count_enabled(self, cfg):
        """合计只算已开启的：全关时调用次数为 0（用户据此判断"这次要不要花钱"）"""
        plan = _plan(cfg, doc_chars=5000, threshold_chars=20000)
        assert plan.switches["contextual_retrieval"] is True
        assert plan.cost.total_calls == plan.cost.chunks
        # 有标题 → 不推上下文检索，图谱/图片摘要默认关 → 合计归零
        plan2 = _plan(cfg, doc_chars=5000, threshold_chars=20000,
                      has_headings=True, heading_count=10)
        assert plan2.switches["contextual_retrieval"] is False
        assert plan2.cost.total_calls == 0

    def test_notes_state_the_unit_convention(self, cfg):
        """口径说明必须带上：不做 token 换算的理由（各模型词表不同）"""
        plan = _plan(cfg, doc_chars=5000, threshold_chars=20000)
        joined = " ".join(plan.cost.notes)
        assert "调用次数" in joined and "token" in joined


# ==================== 成本口径常量与实现同源 ====================

class TestCostConstantsMatchImplementation:
    """预估用的口径常量必须与实现模块一致

    这些数字决定预估准不准；实现改了而这里没改，预估会静默失真。所以在这里
    断言同源——不直接 import 是因为那些模块 import 很重（contextual_retriever
    会拖入 chat_service），不宜进生产路径。
    """

    def test_context_chars(self):
        from backend.services.contextual_retriever import (
            _CHUNK_INPUT_CHARS, _CONTEXT_MAX_CHARS)
        assert cost_mod.CTX_CHUNK_CHARS == _CHUNK_INPUT_CHARS
        assert cost_mod.CTX_OUTPUT_CHARS == _CONTEXT_MAX_CHARS

    def test_kg_chars(self):
        from backend.services.knowledge_graph_service import _CHUNK_INPUT_CHARS
        assert cost_mod.KG_CHUNK_CHARS == _CHUNK_INPUT_CHARS

    def test_agentic_limit(self):
        from backend.services.agentic_chunker import _MAX_TEXT_CHARS
        assert cost_mod.AGENTIC_MAX_TEXT_CHARS == _MAX_TEXT_CHARS

    def test_heading_batch(self):
        from backend.normative.heading_llm import _BATCH_SIZE
        assert cost_mod.HEADING_BATCH == _BATCH_SIZE


# ==================== 标题分层（仅层级聚合 + 非结构解析） ====================

class TestHeadingLevelsCost:
    """标题分层只对**无编号**标题调 LLM，且结构解析下完全不需要"""

    def _estimate(self, cfg, **kw):
        # 默认配好标题分层模型（不配则该环节根本不启用，见 test_zero_without_*）
        cfg.chunking.heading_llm_model = kw.get("model", "qwen-plus")
        profile = {
            "length": {"doc_chars": 89000, "threshold_chars": 20000,
                       "image_refs": 0},
            "structure": {"heading_count": kw.get("heading_count", 301)},
            "qa": {}, "spreadsheet": {},
        }
        config = {"method": kw.get("method", "hierarchical"),
                  "chunk_size": 800, "overlap": 100}
        return cost_mod.estimate(kind="text", profile=profile, config=config,
                                 switches={}, engine=kw.get("engine", "mineru"),
                                 cfg=cfg)

    def _heading(self, cost):
        return next(i for i in cost.items if i.key == "heading_levels")

    def test_batches_by_150(self, cfg):
        assert self._heading(self._estimate(cfg, heading_count=301)).calls == 3

    def test_zero_for_docx_struct(self, cfg):
        """结构解析的层级由 OOXML 直读、精确 → 不该调 LLM（这正是推层级聚合的底气）"""
        hd = self._heading(self._estimate(cfg, engine="docx_struct"))
        assert hd.calls == 0 and hd.available is False

    def test_zero_without_model_configured(self, cfg):
        hd = self._heading(self._estimate(cfg, model=""))
        assert hd.calls == 0
        assert "未配置" in hd.detail

    def test_zero_for_other_methods(self, cfg):
        assert self._heading(self._estimate(cfg, method="naive")).calls == 0

    def test_calls_within_one_batch(self, cfg):
        """不足一批也算一批（150 个标题以内只调一次）"""
        assert self._heading(self._estimate(cfg, heading_count=100)).calls == 1


# ==================== agentic 文案由常量拼出 ====================

class TestAgenticReasonUsesConstants:

    def test_reason_reflects_params_constants(self, cfg):
        """字数上限来自 ingestion.params，改常量不改文案就会红（消灭硬编码副本）"""
        from backend.services.ingestion.params import (
            _MAX_AGENTIC_TEXT_CHARS, _MAX_AGENTIC_TEXT_CHARS_HARD)
        from backend.services.smart_parse.profiling import format_chars

        plan = _plan(cfg)
        ag = next(a for a in plan.alternatives if a.method == "agentic")
        assert format_chars(_MAX_AGENTIC_TEXT_CHARS) in ag.reason
        assert format_chars(_MAX_AGENTIC_TEXT_CHARS_HARD) in ag.reason
        assert ag.badge == "可选"
