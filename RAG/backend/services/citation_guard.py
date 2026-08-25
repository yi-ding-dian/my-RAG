"""引用溯源程序性保障：流式引用标校验 + 覆盖率统计

背景：引用标注 [n] 此前仅靠 prompt 约束（chat_service cite_note），模型
不遵守则无标注或编号越界——前端（MessageList renderCitationContent）对
越界编号的兜底只是"原样渲染为普通文本，不报错"，零程序性保障。本模块把
"依赖模型听话"升级为"系统校验 + 披露"：

- CitationGuard 流式管道：识别 [n] 引用标（与前端完全同规则），
  编号越界（n > max_ref，max_ref = 来源条数）的**剥离**——保证到达前端
  的每一个引用标都指向真实来源；合法编号原样通过；跨 chunk 拆分（"[3"
  与 "]。" 分属两个 delta）由待定缓冲处理。
- 流结束后统计引用覆盖率（引用了引用标的句子占比），随 done 事件下发，
  前端据此展示"未标注引用来源"等提示（诚实披露，不假装强制）。
- 前端"越界原样渲染"兜底保留：双保险，过滤遗漏时前端不会崩。

规则（与 frontend/src/components/MessageList.tsx renderCitationContent
同源，改动必须双端同步）：
- 引用标 = [n]（纯数字）后跟行尾/空白/标点（句尾特征）。"[n] 前不限"
  （句末紧贴形式"…应用[2]。"）；"见[3]附录"（[n] 后接汉字）不识别，
  整段原文保留。
- markdown 链接 [text](url)：text 非纯数字不命中；[1](url) 极端形式里
  [1] 后接 "("（不在句尾特征集）同样不识别。
- 多编号 [1,2]：前端正则不含逗号，不识别，原文保留（模型受句末 [n]
  指令约束一般不生成，可接受）。
- 句尾特征字符集（与前端正则同源）：
  空白 + ,.;:!?，。；：！？、%．％~～）)]】」"'’
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# 数字串检测（ASCII `\d`，与 JS 正则语义一致，不匹配全角数字等 unicode 数字）
_NUM_RE = re.compile(r"[0-9]+")
# 句尾特征单字符（[n] 后必须跟行尾/空白/标点；$ 行尾由 flush/eos 处理）
_TAIL_CHAR_RE = re.compile(r"[\s,.;:!?，。；：！？、%．％~～）)\]】」\"'’]")
# 完整引用标匹配（stats 句子统计与最终整文校验用；行尾 $ 含流结束场景）
_REF_RE = re.compile(
    r"\[[0-9]+\](?=$|[\s,.;:!?，。；：！？、%．％~～）)\]】」\"'’])")


@dataclass
class CitationStats:
    """引用标注统计（随 done 事件下发，前端据此提示）"""
    refs: int = 0             # 有效引用标数量（到达前端的）
    refs_invalid: int = 0     # 越界剥离数量
    sentences: int = 0        # 句子总数（按 。！？!?. 切分）
    cited_sentences: int = 0  # 含有效引用标的句子数
    coverage: float = 0.0     # 引用覆盖率 cited_sentences / sentences（0~1，无句子为 0）


class CitationGuard:
    """流式引用标过滤器

    用法：
        guard = CitationGuard(max_ref=len(sources))
        async for chunk in stream:
            clean = guard.feed(content)   # 返回可输出的 sanitized 增量
            if clean: yield delta(clean)
        tail = guard.flush()              # 流结束释放待定缓冲
        stats = guard.stats()             # 覆盖率统计（基于 sanitized 全文）

    语义：
    - feed 返回的文本去掉了越界 [n]；合法 [n] 与普通文本原样输出。
    - 待定缓冲未释放前，跨 chunk 的引用标不会提前输出（最长延迟到
      句尾特征字符或流结束，语义不受影响）。
    """

    def __init__(self, max_ref: int):
        self.max_ref = max_ref
        self._pending = ""                # 待定缓冲（可能构成引用标的尾段）
        self._refs = 0                    # 有效引用标计数
        self._refs_invalid = 0            # 越界剥离计数
        self._output_parts: List[str] = []  # sanitized 输出累计（stats 统计用）

    def feed(self, text: str) -> str:
        """输入一段流式文本，返回可立即输出的 sanitized 片段"""
        if not text:
            return ""
        self._pending += text
        return self._drain(eos=False)

    def flush(self) -> str:
        """流结束：待定缓冲按行尾（$ 也属句尾特征）判定后释放

        - "[n]" 后无字符（流结束）→ 视为行尾句尾特征 → 正常判定/剥离
        - "[n" / "[" 不完整 → 原文释放（不可能再构成合法引用标）
        """
        return self._drain(eos=True)

    def stats(self) -> CitationStats:
        """基于 sanitized 全文统计（引用标均为已校验合法）"""
        text = "".join(self._output_parts)
        stats = CitationStats(refs=self._refs, refs_invalid=self._refs_invalid)
        if not text:
            return stats
        sentences = [s for s in re.split(r"[。！？!?.]+", text) if s.strip()]
        stats.sentences = len(sentences)
        stats.cited_sentences = sum(
            1 for s in sentences if _REF_RE.search(s))
        if stats.sentences:
            stats.coverage = round(stats.cited_sentences / stats.sentences, 2)
        return stats

    # ---------- 内部扫描 ----------

    def _drain(self, eos: bool) -> str:
        """扫描待定缓冲：确定的部分输出（或剥离），不定的保留

        eos=True（流结束）："[" / "[n" 不完整尾部按原文释放；"[n]" 结尾
        视为行尾句尾特征（$ 匹配），正常判定。
        """
        out: List[str] = []
        txt = self._pending
        n = len(txt)
        pos = 0
        while pos < n:
            caret = txt.find("[", pos)
            if caret < 0:
                out.append(txt[pos:])
                pos = n
                break
            if caret > pos:
                out.append(txt[pos:caret])
                pos = caret
            # [ 开始 → 判定是否引用标
            m = _NUM_RE.match(txt, caret + 1)
            if not m:
                # [ 后无数字 → 不是引用标
                if caret + 1 >= n and not eos:
                    # "[" 是待定尾部（下一 chunk 可能是 "[1]"，如 "" + "[1]"）
                    self._pending = txt[caret:]
                    self._emit(out)
                    return "".join(out)
                # 原文输出到下一个 [ 或文本结束
                nxt = txt.find("[", caret + 1)
                end = nxt if nxt >= 0 else n
                out.append(txt[caret:end])
                pos = end
                continue
            num_end = m.end()
            if num_end >= n:
                if not eos:
                    # "[数字" 尾部待定（"[" 后数字串未到达 ]）
                    self._pending = txt[caret:]
                    self._emit(out)
                    return "".join(out)
                # eos：不完整（数字后无字符）→ 不可能构成引用标，原文释放
                out.append(txt[caret:])
                pos = n
                continue
            c = txt[num_end]
            if c != "]":
                # 数字后非 ]（"[1,2]" / "[12x" 等）→ 不是引用标，原文保留
                nxt = txt.find("[", num_end)
                end = nxt if nxt >= 0 else n
                out.append(txt[caret:end])
                pos = end
                continue
            # 数字后是 ]：检查其后字符（句尾特征判定）
            after = num_end + 1
            if after >= n:
                if not eos:
                    # "[n]" 后字符未到达（可能是句尾标点或汉字）→ 待定
                    self._pending = txt[caret:]
                    self._emit(out)
                    return "".join(out)
                # eos：行尾（$）视为句尾特征，正常判定
                self._commit_ref(int(m.group(0)), out, txt, caret, after)
                pos = after
                continue
            c2 = txt[after]
            if not _TAIL_CHAR_RE.match(c2):
                # ] 后非句尾特征（汉字/数字/字母等，如"见[3]附录"）
                # → 不是引用标，原文保留到下一个 [ 或文本结束
                nxt = txt.find("[", after)
                end = nxt if nxt >= 0 else n
                out.append(txt[caret:end])
                pos = end
                continue
            # 句尾特征 → 引用标：合法输出 / 越界剥离
            self._commit_ref(int(m.group(0)), out, txt, caret, after)
            pos = after
        self._pending = ""
        self._emit(out)
        return "".join(out)

    def _commit_ref(self, num: int, out: List[str], txt: str,
                    caret: int, after: int):
        """引用标判定提交：合法 → 输出 [n]；越界 → 剥离（计数不输出）"""
        if 1 <= num <= self.max_ref:
            self._refs += 1
            out.append(txt[caret:after])
        else:
            self._refs_invalid += 1

    def _emit(self, out: List[str]):
        """把本轮输出记入累计（供 stats）"""
        if out:
            self._output_parts.extend(out)
