"""引用编号顺序规范化测试：编号 = sources 显示顺序 = 1..N 连续

全链路一致性约定（注释固化于 chat_service.py / routers/chat.py）：
- 普通检索引用按相关度降序（retrieval_service 内排序，图谱不参与）
- 图谱引用（score=0）固定追加在末尾，作为补充引用
- _build_refs / meta 事件 / 导出 Markdown 均按同一 sources 列表顺序编号，
  前端行内 [n]（sources[n-1]）与面板角标（index+1）同源
- 任何环节不得对 sources 重排/去重，否则编号与前端 [n] 错位

本文件只测编号与顺序的纯函数部分；集成侧（stream meta / retrieve 多库
+图谱）的"图谱恒在末尾"断言见 test_kg_enhance.py / test_retrieve_multi_kb.py。
"""
from __future__ import annotations

from backend.models.rag_models import ChatMessage, ChatSession, Source
from backend.services.chat_service import ChatService


def _src(i: int, name: str = "文档A", score: float | None = None) -> Source:
    """第 i 条普通引用（分数递减，模拟检索相关度降序）"""
    if score is None:
        score = 0.8 - i * 0.1
    return Source(
        id=f"d{i}_{i}",
        text=f"第{i}条引用内容（相关度 {score:.3f}）",
        score=score,
        document_id=f"d{i}",
        document_name=name,
        chunk_index=i,
    )


def _kg() -> Source:
    """图谱引用（score=0，document_name="知识图谱"）"""
    return Source(
        id="kg:kb1",
        text="【知识图谱实体】…",
        score=0.0,
        document_name="知识图谱",
        chunk_index=-1,
    )


class TestBuildRefsNumbering:
    """_build_refs：编号 = 列表顺序，1..N 连续无跳跃无重复"""

    def test_refs_numbered_in_list_order(self):
        """编号 1..N 按列表顺序递增，与检索相关度排序一致"""
        sources = [_src(1), _src(2), _src(3)]
        refs = ChatService._build_refs(sources)
        for i in (1, 2, 3):
            assert f"[引用 {i}]（来源：文档A）" in refs
        assert (refs.index("[引用 1]（来源")
                < refs.index("[引用 2]（来源")
                < refs.index("[引用 3]（来源"))

    def test_kg_last_gets_last_number(self):
        """图谱引用在末尾 → 编号为最后一个且连续（不与普通引用穿插）"""
        sources = [_src(1), _src(2), _kg()]
        refs = ChatService._build_refs(sources)
        assert "[引用 3]（来源：知识图谱）" in refs
        assert refs.index("[引用 3]（来源：知识图谱）") > refs.index("[引用 2]（来源")
        # 编号 1..3 完整无跳跃（防图谱占位导致跳过）
        for i in (1, 2, 3):
            assert f"[引用 {i}]（来源：" in refs
        assert "[引用 4]" not in refs

    def test_number_continuous_even_with_kg_mixed(self):
        """图谱混在中间（调用方不遵守约定的场景兜底）：编号仍按位置 1..N 连续"""
        sources = [_src(1), _kg(), _src(2)]
        refs = ChatService._build_refs(sources)
        assert "[引用 2]（来源：知识图谱）" in refs
        assert "[引用 3]（来源：文档A）" in refs
        assert "[引用 4]" not in refs

    def test_single_source_number_one(self):
        """单条引用编号为 1"""
        refs = ChatService._build_refs([_src(1)])
        assert "[引用 1]（来源：文档A）" in refs
        assert "[引用 2]" not in refs


class TestExportNumbering:
    """build_export_markdown：导出引用编号与 sources 顺序一致（1..N 连续）"""

    def test_export_numbering_matches_sources_order(self):
        sources = [_src(1), _src(2), _kg()]
        session = ChatSession(
            id="s1",
            kb_id="kb1",
            user_id="u1",
            title="测试会话",
            messages=[
                ChatMessage(role="user", content="问题"),
                ChatMessage(role="assistant", content="回答[3]。",
                            sources=sources),
            ],
            created_at="2026-01-01 00:00:00",
            updated_at="2026-01-01 00:00:00",
        )
        md = ChatService.build_export_markdown(session)
        for i in (1, 2, 3):
            assert f"### 引用 {i}：" in md
        assert "### 引用 4" not in md
        # 图谱（列表末位）在导出中也是最后一个引用块
        assert (md.index("### 引用 3：知识图谱")
                > md.index("### 引用 2：文档A"))
