"""入库轨迹计时 mixin（方法实现原样搬移自原 ingestion_service）

- _TraceMixin: _set_stage / _finalize_trace / _mark_trace_failed /
  _mark_stage_warn / _clear_stage
- 状态字段（_trace / _stage / _stage_since / _stage_ms / _task_started_at /
  _trace_warn）由 IngestionService.__init__ 初始化，mixin 只提供方法
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
            entry = {
                "stage": self._stage.get(doc_id, "准备中"),
                "ms": int(round((time.perf_counter() - prev_start) * 1000)),
            }
            self._attach_warn(doc_id, entry)
            self._trace[doc_id].append(entry)
        self._stage[doc_id] = stage
        self._stage_since[doc_id] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._stage_ms[doc_id] = time.perf_counter()

    def _update_stage_text(self, doc_id: str, stage: str) -> None:
        """只更新阶段**展示文案**（悬停进度可见），**不结算上一阶段耗时**

        高频进度刷新专用（如"图片摘要（3/87）"）：若走 _set_stage，每刷新一次
        都会往 trace 里 append 一条记录——几十上百张图会把入库轨迹撑爆。
        阶段起始时间保持不变（悬停看到的仍是该阶段开始时刻），整个阶段的耗时
        仍只记一条。
        """
        if doc_id in self._running:
            self._stage[doc_id] = stage

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
            entry = {
                "stage": self._stage.get(doc_id, "准备中"),
                "ms": int(round((time.perf_counter() - prev_start) * 1000)),
            }
            self._attach_warn(doc_id, entry)
            self._trace[doc_id].append(entry)
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

    def _mark_stage_warn(self, doc_id: str, msg: str) -> None:
        """标记当前阶段有警告（非失败，任务继续跑完）

        与 _mark_trace_failed 的区别：失败会终止任务、把**已结算**的最后一条
        置为 failed；警告是过程里发现问题但结果仍可用（如文档转换没成功、
        文档仍按原文件解析了），任务照常走完、状态仍是"已入库"。
        故此处只暂存文案，等该阶段被 _set_stage / _finalize_trace 结算时由
        _attach_warn 挂到对应条目上——前端据此把该阶段标黄 + 悬浮展示原因。
        """
        self._trace_warn[doc_id] = msg

    def _attach_warn(self, doc_id: str, entry: dict) -> None:
        """把暂存的阶段警告挂到 trace 条目（无则不动，条目保持原样）"""
        warn = self._trace_warn.pop(doc_id, None)
        if warn:
            entry["status"] = "warn"
            entry["warn"] = warn

    def _clear_stage(self, doc_id: str) -> None:
        """任务结束（完成/失败/取消/待确认）清理阶段状态（避免悬停残留）"""
        self._stage.pop(doc_id, None)
        self._stage_since.pop(doc_id, None)
        self._stage_ms.pop(doc_id, None)
        self._trace.pop(doc_id, None)
        self._task_started_at.pop(doc_id, None)
        self._trace_warn.pop(doc_id, None)
