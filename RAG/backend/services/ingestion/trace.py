"""入库轨迹计时 mixin（方法实现原样搬移自原 ingestion_service）

- _TraceMixin: _set_stage / _finalize_trace / _mark_trace_failed / _clear_stage
- 状态字段（_trace / _stage / _stage_since / _stage_ms / _task_started_at）由
  IngestionService.__init__ 初始化，mixin 只提供方法
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional


class _TraceMixin:
    """入库轨迹计时（方法实现见原 ingestion_service，原样搬移）"""

    def _set_stage(self, doc_id: str, stage: str) -> None:
        """更新任务阶段（记录开始时间；切换时累计上一阶段耗时）
        - 任务结束由 _clear_stage 清理；trace 由 _finalize_trace 落文档
        """
        # trace 初始化（首次调用）：后续切换直接 append
        if doc_id not in self._trace:
            self._trace[doc_id] = []
            # 任务开始时间（仅首次阶段记录；展示用）——start 时刻即首次进入阶段
            if doc_id not in self._task_started_at:
                self._task_started_at[doc_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 阶段切换：先结掉上一阶段耗时（perf_counter 差 → ms；无上一段起点
        # （首次）跳过——首个阶段"准备中"由任务启动处 _set_stage 开启）
        prev_start = self._stage_ms.pop(doc_id, None)
        if prev_start is not None:
            self._trace[doc_id].append({
                "stage": self._stage.get(doc_id, "准备中"),
                "ms": int(round((time.perf_counter() - prev_start) * 1000)),
            })
        self._stage[doc_id] = stage
        self._stage_since[doc_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._stage_ms[doc_id] = time.perf_counter()

    def _finalize_trace(self, doc_id: str) -> tuple[list, int, Optional[dict], str, str]:
        """任务结束生成入库轨迹（成功=当前阶段结掉；失败=当前阶段标记 failed）

        返回 (trace, total_ms, last_stage_err, started_at, finished_at)：
        - trace: [{stage, ms, status?}]，status 仅失败阶段的当前阶段有
        - total_ms: 总耗时（所有阶段求和）
        - last_stage_err: 用户取消/失败标记（异常路径填）
        - started_at: 任务开始时间（HH:mm:ss）
        - finished_at: 任务结束时间（HH:mm:ss）
        """
        prev_start = self._stage_ms.pop(doc_id, None)
        if prev_start is not None and doc_id in self._trace:
            self._trace[doc_id].append({
                "stage": self._stage.get(doc_id, "准备中"),
                "ms": int(round((time.perf_counter() - prev_start) * 1000)),
            })
        trace = self._trace.pop(doc_id, [])
        total = int(sum(s.get("ms", 0) for s in trace))
        started_at = self._task_started_at.pop(doc_id, "")
        finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return trace, total, None, started_at, finished_at

    def _mark_trace_failed(self, doc_id: str) -> None:
        """失败路径：当前阶段标记 failed（状态列可追溯：失败在哪个阶段）"""
        trace = self._trace.get(doc_id)
        if not trace:
            return
        for s in reversed(trace):
            s["status"] = "failed"
            break

    def _clear_stage(self, doc_id: str) -> None:
        """任务结束（完成/失败/取消/待确认）清理阶段状态（避免悬停残留）"""
        self._stage.pop(doc_id, None)
        self._stage_since.pop(doc_id, None)
        self._stage_ms.pop(doc_id, None)
        self._trace.pop(doc_id, None)
        self._task_started_at.pop(doc_id, None)
