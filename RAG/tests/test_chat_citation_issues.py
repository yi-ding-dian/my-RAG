"""引用溯源合规：越界编号标记（S1）+ 命中窗口引用组装（S2）测试

背景（实测发现）：
- S1：LLM 偶发自造越界编号（实测 sources 仅 2 条却输出 [34]）——后端只
  标记不改文本，SSE done 事件附 citation_issues、消息落盘携带；前端据
  此渲染"失效引用"灰标（渲染判定在本文件外的前端 renderCitationContent）。
- S2：parent_child 父块超长时旧逻辑 refs = parent_text[:6000]，命中词在
  父块 6000~8000（入库 _PARENT_TEXT_META_LIMIT=8000 展示区）或 8000 之外
  （入库截断区）时模型看不见命中词 → 回答"未找到"而引用面板却有原文。
  修复：_REF_TEXT_MAX_LEN 提到 8000（对齐入库展示上限）；命中组装改为
  「命中子块全文 + 相邻前后各 1 子块」窗口优先（doc_chunks 由文档
  chunks_meta 提供），命中词最早可见，入库截断外的命中子块同样可覆盖。

本文件：纯函数（_citation_issues / _ref_snippet / _build_refs）+ 全链路
集成（真实入库 parent_child + 伪检索固定命中块 → prompt/done 断言）。
"""
from __future__ import annotations

import json

import pytest

from backend.models.rag_models import ChatMessage, Source
from backend.services.chat_service import (ChatService, _REF_TEXT_MAX_LEN)
from conftest import create_kb, upload_and_ingest

# parent_child 大章文档：6 段各约 100 字（chunk_size=80 时每段独立成块，
# 产出 >=3 子块供邻块窗口验证；第 3 段含关键词供命中块定位）
_PARENT_DOC = """# 设备维护总章

北极星科技设备维护流程规定,每台数控机床每日开工前需完成润滑点检,检查主轴温度与导轨油位数据是否正常,点检记录需要登记在车间数字化看板上面,异常情况须在两小时内上报设备主管处理。

第2段。备件库实行先进先出原则管理,润滑油类备件保质期为两年,过期批次一律报废处理并且登记台账,每月盘点一次核对账实是否相符。

第3段。量子谐振阻尼器标定周期为每季度一次,标定工作由计量中心统一执行,标定结果需要录入设备档案系统,逾期未标定的设备不得投入生产运行。

第4段。磁悬浮导轨专用润滑脂型号为LUBE-X7,每台机床每月补充一次,加注量不得超过标准刻度线,加注后需要擦拭干净残留油渍防止污染工件。

第5段。检修作业须执行停电挂牌制度,严禁带电拆装传动部件,完工后由班组长复核签字确认,安全工器具使用前需要检查外观与试验合格标签。

第6段。特种设备操作证每两年复审一次,复审不合格者暂停上岗资格直至补考通过,复审报名由车间安全员统一组织并留存培训记录备查。
"""


def _src(text: str, document_id: str, chunk_index: int, name: str,
         parent_text: str | None = None, context: str | None = None) -> Source:
    return Source(
        id=f"{document_id}_{chunk_index}",
        text=text,
        score=0.8,
        document_id=document_id,
        document_name=name,
        kb_id="kb-test",
        chunk_index=chunk_index,
        parent_text=parent_text,
        context=context,
        char_start=chunk_index * 100,
        char_end=chunk_index * 100 + len(text),
    )


class TestRefSnippetWindow:
    """S2：_ref_snippet 命中窗口优先组装（命中块+前后邻块，命中词最早可见）"""

    def test_window_puts_hit_block_first_and_includes_neighbors(self):
        """命中子块文本排最前，前/后邻块跟随；命中词必在输出内"""
        chunks = ["前邻块内容。", "命中块。量子谐振阻尼器标定周期为每季度一次。",
                  "后邻块内容。", "远邻块内容。"]
        s = _src(chunks[1], "doc1", 1, "文档A",
                 parent_text="父块截断文本不含命中词。" * 600)  # >8000 模拟
        out = ChatService._ref_snippet(s, {"doc1": chunks})
        assert "量子谐振阻尼器" in out, "命中词必须在窗口内"
        assert out.startswith(chunks[1]), "命中块必须排最前（命中词最早可见）"
        assert "前邻块内容" in out and "后邻块内容" in out, "前后邻块应包含"
        assert "远邻块内容" not in out, "只取相邻各 1 块"
        assert "父块截断文本" not in out, "窗口模式不再拼接父块头文本"

    def test_window_first_and_last_chunk_skip_missing_neighbors(self):
        """首块无前邻、末块无后邻：只拼存在侧，不越界（父子命中语义）"""
        chunks = ["首块内容。", "次块内容。"]
        parent = "父" * 9000  # 超长父块（触发窗口分支的前提）
        out0 = ChatService._ref_snippet(
            _src(chunks[0], "d", 0, "A", parent_text=parent), {"d": chunks})
        assert out0 == chunks[0] + "\n\n" + chunks[1]
        out1 = ChatService._ref_snippet(
            _src(chunks[1], "d", 1, "A", parent_text=parent), {"d": chunks})
        assert out1 == chunks[1] + "\n\n" + chunks[0]

    def test_hit_beyond_parent_8000_cut_visible_via_window(self):
        """入库 parent_text 8000 截断之外的命中子块：窗口用子块/邻块文本覆盖
        （parent_text 中不含命中词，旧逻辑必丢；窗口路径必须可见）"""
        chunks = [f"第{i}段。北极星设备维护例行内容。" + "辅。" * 30 for i in range(30)]
        hit = "第15段。量子谐振阻尼器标定周期为每季度一次。" + "辅。" * 30
        chunks[15] = hit
        s = _src(hit, "doc2", 15, "文档B", parent_text="序" * 8000)  # 截断不带词
        out = ChatService._ref_snippet(s, {"doc2": chunks})
        assert "量子谐振阻尼器" in out
        assert out.startswith(hit), "命中块最前"

    def test_no_doc_chunks_falls_back_to_parent_text(self):
        """无邻块数据（读取失败/非父子）：父块截断兜底（上限对齐 8000）"""
        par = "普通父块内容。" * 900  # > 8000
        s = _src("子块", "d", 2, "A", parent_text=par)
        out = ChatService._ref_snippet(s, None)
        assert len(out) <= _REF_TEXT_MAX_LEN
        assert out == par[:_REF_TEXT_MAX_LEN], "兜底=父块按上限截断"

    def test_parent_text_within_limit_unchanged(self):
        """父块未超上限且无邻块数据：全量下发（现状行为零变化）"""
        par = "短父块。内容完整。"
        s = _src("子", "d", 0, "A", parent_text=par)
        assert ChatService._ref_snippet(s, None) == par

    def test_no_parent_text_uses_child_text(self):
        """非父子模式（无父块）：子块文本截断（现状行为零变化）"""
        s = _src("普通块文本。" * 200, "d", 0, "A")
        out = ChatService._ref_snippet(s, {"d": ["x"]})
        assert out == (s.text or "")[:_REF_TEXT_MAX_LEN]

    def test_build_refs_window_keeps_numbering_and_context_prefix(self):
        """_build_refs 集成：编号顺序保持、窗口生效、context 前缀仍拼接"""
        chunks = ["邻A。", "命中块。北极星科技住宿上限550元。", "邻B。"]
        s1 = _src(chunks[1], "doc1", 1, "制度.txt", parent_text="父" * 9000)
        s2 = _src("图谱内容。", "doc2", 0, "知识图谱",
                  context="上下文摘要")
        refs = ChatService._build_refs([s1, s2], doc_chunks={"doc1": chunks})
        assert "[引用 1]（来源：制度.txt）" in refs
        assert "[引用 2]（来源：知识图谱）" in refs
        assert "【上下文】上下文摘要" in refs, "context 前缀拼接不受窗口影响"
        assert refs.index("[引用 1]（来源") < refs.index("[引用 2]（来源")


class TestCitationIssuesPure:
    """S1：_citation_issues 越界编号解析（与前端 renderCitationContent 同构）"""

    def test_collects_out_of_range_numbers(self):
        assert ChatService._citation_issues(
            "上限550元[1]。另有未知[34]。", 2) == [34]
        assert ChatService._citation_issues(
            "越界[99]。以及[0]。", 1) == [99, 0]

    def test_valid_numbers_ignored(self):
        assert ChatService._citation_issues(
            "标准[1]与补充[2]以及合并[1,2]。", 2) == []

    def test_zero_and_boundary(self):
        """n=0 越界；n=len(sources) 恰好有效"""
        assert ChatService._citation_issues("编号[0]。", 2) == [0]
        assert ChatService._citation_issues("编号[2]。", 2) == []

    def test_no_sources_all_numbers_out_of_range(self):
        assert ChatService._citation_issues("回答[1]。", 0) == [1]
        assert ChatService._citation_issues("无引用回答。", 0) == []

    def test_deduplicated_in_order(self):
        assert ChatService._citation_issues(
            "首次[34]。再次[34]。后来[7]。", 2) == [34, 7]

    def test_number_followed_by_chinese_not_citation(self):
        """正文 "[2024]年"（后接汉字）不是句尾引用标注，不误报"""
        assert ChatService._citation_issues("数据于[2024]年发布。", 3) == []

    def test_unclosed_bracket_not_matched(self):
        assert ChatService._citation_issues("流式半截[3", 2) == []


class TestStreamCitationIntegration:
    """全链路集成：真实入库 parent_child + 伪检索固定命中块
    （绕过检索不确定性，聚焦 引用组装 → prompt / done 标记 链路）"""

    def _ingest_parent_doc(self, client, admin_headers):
        kb = create_kb(client)
        doc = upload_and_ingest(
            client, kb["id"], filename="维保规程.txt", content=_PARENT_DOC,
            headers=admin_headers,
            ingest_body={"method": "parent_child", "chunk_size": 80,
                         "overlap": 0})
        return kb, doc

    def _hit_idx(self, chunks: list) -> int:
        """定位含关键词的命中块（测试文档第 3 段）"""
        for i, c in enumerate(chunks):
            if "量子谐振阻尼器" in c["text"]:
                return i
        raise AssertionError(f"关键词应存在于某子块: {len(chunks)} 块")

    def _detail_chunks(self, client, admin_headers, kb, doc):
        resp = client.get(
            f"/api/kbs/{kb['id']}/documents/{doc['id']}", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["chunks"]

    @staticmethod
    def _patch_retrieve(monkeypatch, sources):
        from backend.services.retrieval_service import RetrievalService

        async def fake_retrieve(self, kb_id, query, top_k=None, min_score=None,
                                **kw):
            return sources

        monkeypatch.setattr(RetrievalService, "retrieve", fake_retrieve)

    def test_prompt_refs_contain_hit_block_via_window(
            self, client, mock_embedding, mock_llm, admin_headers, monkeypatch):
        """S2 端到端：命中块（父块截断不可见位置）经窗口进入 prompt refs，
        且 refs 以命中块开头、含邻块"""
        kb, doc = self._ingest_parent_doc(client, admin_headers)
        chunks = self._detail_chunks(client, admin_headers, kb, doc)
        assert len(chunks) >= 3, f"文档应至少 3 子块: {len(chunks)}"
        hit_idx = self._hit_idx(chunks)
        hit_text = chunks[hit_idx]["text"]
        # 命中块位于父块 8000 截断之外（父块文本不含它——窗口必须覆盖）
        src = _src(hit_text, doc["id"], hit_idx, doc["original_name"],
                   parent_text="父块截断内容。" * 600)
        self._patch_retrieve(monkeypatch, [src])
        mock_llm(parts=["好的，我根据引用回答。"])

        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "量子谐振阻尼器标定周期是多久？",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        prompt_block = resp.text.split("event: prompt", 1)[1].split("\n\n", 1)[0]
        prompt_data = json.loads(prompt_block.split("data: ", 1)[1].strip())
        system = prompt_data["prompt"][0]["content"]
        assert hit_text in system, "命中块全文必须在 refs（窗口优先）"
        assert "父块截断内容" not in system, "窗口模式不拼父块头"
        # 命中块在引用条目内排最前（编号头部后直接是命中块文本）
        entry = system.split("[引用 1]（来源：", 1)[1]
        assert entry.startswith(f"{doc['original_name']}）\n{hit_text}"), \
            "引用 1 条目应以命中块开头"

    def test_done_and_history_carry_citation_issues(
            self, client, mock_embedding, mock_llm, admin_headers, monkeypatch):
        """S1 端到端：回答含越界 [34] → done 附 citation_issues + 落盘携带"""
        kb, doc = self._ingest_parent_doc(client, admin_headers)
        chunks = self._detail_chunks(client, admin_headers, kb, doc)
        srcs = [
            _src(chunks[0]["text"], doc["id"], 0, doc["original_name"]),
            _src(chunks[1]["text"], doc["id"], 1, doc["original_name"]),
        ]
        self._patch_retrieve(monkeypatch, srcs)
        # 模型输出：句尾有效 [1] + 越界 [34]（sources 仅 2 条）
        mock_llm(parts=["量子谐振阻尼器标定周期为每季度一次[1]。"
                        "以及另一个编号[34]。"])

        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "标定周期是多久？",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        done_block = resp.text.split("event: done", 1)[1].split("\n\n", 1)[0]
        done_data = json.loads(done_block.split("data: ", 1)[1].strip())
        assert done_data.get("citation_issues") == [34], \
            f"done 应携带越界编号: {done_data}"

        # 会话落盘（历史重放同样可标失效引用）
        session_id = done_data["session_id"]
        hist = client.get(f"/api/chat/history/{session_id}",
                          headers=admin_headers)
        assert hist.status_code == 200, hist.text
        last = hist.json()["messages"][-1]
        assert last["role"] == "assistant"
        assert last.get("citation_issues") == [34], \
            f"落盘消息应携带 citation_issues: {last}"

    def test_done_omits_field_when_no_issues(
            self, client, mock_embedding, mock_llm, admin_headers, monkeypatch):
        """无越界编号：done 不带 citation_issues 字段（向后兼容缺省）"""
        kb, doc = self._ingest_parent_doc(client, admin_headers)
        chunks = self._detail_chunks(client, admin_headers, kb, doc)
        src = _src(chunks[0]["text"], doc["id"], 0, doc["original_name"])
        self._patch_retrieve(monkeypatch, [src])
        mock_llm(parts=["标定周期为每季度一次[1]。"])

        resp = client.post("/api/chat/stream", json={
            "kb_id": kb["id"], "query": "标定周期是多久？",
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        done_block = resp.text.split("event: done", 1)[1].split("\n\n", 1)[0]
        done_data = json.loads(done_block.split("data: ", 1)[1].strip())
        assert "citation_issues" not in done_data, \
            f"无越界时不应带字段: {done_data}"
