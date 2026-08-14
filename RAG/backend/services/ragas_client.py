"""RAGAS 评估系统对接客户端（httpx 异步）

只读操作（探测/报告/任务状态，3s 超时）：
- GET {base}/api/evaluations            任务列表（探测可用性）
- GET {base}/api/evaluations/{id}       任务状态
- GET {base}/api/evaluations/{id}/results  任务报告
- 任何异常（超时/连接拒绝/非 2xx）→ {"available": false}，不影响自身服务

写操作（创建数据集/批量样本/创建评估任务，30s 超时，失败抛中文异常）：
- POST {base}/api/datasets/create        创建数据集
- POST {base}/api/datasets/{id}/samples  逐条添加样本
- POST {base}/api/evaluations            创建评估任务（支持 llm 覆盖字段：
  {base_url, api_key, model, temperature, max_tokens}，RAGAS 侧按此契约消费；
  其中 max_tokens 缺省 None=用 RAGAS 任务级 llm_max_tokens）
- POST {base}/api/evaluations/{id}/cancel 取消任务

RAGAS_BASE_URL 来自环境变量（默认 http://localhost:8090）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

RAGAS_BASE_URL = os.environ.get("RAGAS_BASE_URL", "http://localhost:8090").rstrip("/")
RAGAS_TIMEOUT = 3.0  # 探测/报告统一 3s 超时
RAGAS_POST_TIMEOUT = 30.0  # 写操作超时（创建/上传样本，大样本集需更长）
RAGAS_SAMPLE_BATCH = 50  # 批量上传样本的每批条数


class RagasApiError(Exception):
    """RAGAS 写操作失败（连接不可达/超时/非 2xx），message 为中文提示

    status_code: RAGAS 返回的 HTTP 状态码（连接失败/超时等无响应场景为 None），
    路由层据此区分"任务不存在(404)"与"其他失败(400)"。
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class RagasClient:

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=RAGAS_TIMEOUT)
        return self._client

    # ---------------- 只读（3s 超时，失败返回 None） ----------------

    async def _get(self, path: str) -> Optional[dict]:
        """GET 并返回 JSON；任何异常/非 2xx 返回 None"""
        try:
            resp = await self._get_client().get(f"{RAGAS_BASE_URL}{path}")
            if resp.status_code < 400:
                try:
                    return resp.json()
                except Exception:
                    return None
            logger.warning("RAGAS %s 返回 %s", path, resp.status_code)
            return None
        except Exception as e:
            logger.info("RAGAS %s 不可达: %s", path, e)
            return None

    async def probe(self) -> dict:
        """探测 RAGAS 可用性 + 拉取任务列表

        返回: {"available": bool, "base_url": str, "tasks": [...], "message": str}
        """
        data = await self._get("/api/evaluations")
        if data is None:
            return {
                "available": False,
                "base_url": RAGAS_BASE_URL,
                "tasks": [],
                "message": f"无法连接 RAGAS 评估系统（{RAGAS_BASE_URL}），"
                           "请先启动 RAGAS 服务（端口 8090）",
            }
        tasks = data if isinstance(data, list) else data.get("tasks", [])
        return {
            "available": True,
            "base_url": RAGAS_BASE_URL,
            "tasks": tasks,
            "message": "",
        }

    async def get_task(self, task_id: str) -> Optional[dict]:
        """获取评估任务状态（GET /api/evaluations/{id}）；不可达返回 None"""
        return await self._get(f"/api/evaluations/{task_id}")

    async def get_report(self, task_id: str) -> dict:
        """获取评估任务报告

        返回: {"available": bool, "report": {...} | None, "message": str}
        报告结构（RAGAS 真实字段）:
          aggregate: {scores: {metric: number}, count: int}
          results: [{question, answer, scores: {metric: number|null}, ...}]
        """
        data = await self._get(f"/api/evaluations/{task_id}/results")
        if data is None:
            return {
                "available": False,
                "report": None,
                "message": f"无法获取 RAGAS 任务报告（{RAGAS_BASE_URL}/api/evaluations/{task_id}/results）",
            }
        return {"available": True, "report": data, "message": ""}

    # ---------------- 写操作（30s 超时，失败抛中文异常） ----------------

    async def _post(self, path: str, json_body=None) -> dict:
        """POST 并返回 JSON；连接失败/超时/非 2xx 抛 RagasApiError（中文提示）"""
        try:
            resp = await self._get_client().post(
                f"{RAGAS_BASE_URL}{path}", json=json_body, timeout=RAGAS_POST_TIMEOUT)
        except Exception as e:
            raise RagasApiError(
                f"无法连接 RAGAS 评估系统（{RAGAS_BASE_URL}）："
                f"{e.__class__.__name__}，请确认服务已启动（端口 8090）") from e
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict) and body.get("detail"):
                    detail = f"：{body['detail']}"
            except Exception:
                pass
            raise RagasApiError(
                f"RAGAS 请求失败（{path}，HTTP {resp.status_code}）{detail}",
                status_code=resp.status_code)
        try:
            return resp.json()
        except Exception:
            return {}

    async def create_dataset(self, name: str, description: str = "") -> str:
        """创建空数据集，返回 dataset_id"""
        data = await self._post("/api/datasets/create",
                                json_body={"name": name, "description": description})
        dataset_id = data.get("id") if isinstance(data, dict) else None
        if not dataset_id:
            raise RagasApiError("RAGAS 创建数据集失败：响应缺少数据集 ID")
        return dataset_id

    async def add_samples(self, dataset_id: str,
                          samples: list, batch_size: int = RAGAS_SAMPLE_BATCH) -> None:
        """批量添加样本（每批 batch_size 条并发 POST /samples；失败即中断抛中文异常）"""
        for i in range(0, len(samples), batch_size):
            batch = samples[i:i + batch_size]
            results = await asyncio.gather(*[
                self._post(f"/api/datasets/{dataset_id}/samples", json_body=s)
                for s in batch
            ])
            # 任一失败时 gather 已抛 RagasApiError（首个异常直接传播，其余完成不等待）
            logger.info("RAGAS 样本已上传 %d/%d", min(i + len(batch), len(samples)),
                        len(samples))
            _ = results

    async def upload_dataset(self, samples: list, name: str,
                             description: str = "") -> str:
        """创建数据集 + 批量写入样本，返回 dataset_id"""
        dataset_id = await self.create_dataset(name, description)
        if samples:
            await self.add_samples(dataset_id, samples)
        return dataset_id

    async def create_evaluation(self, dataset_id: str, metrics: list,
                                llm_cfg: dict, name: str,
                                top_k: int = 3) -> dict:
        """创建评估任务（use_retrieval=false，contexts 由知识库侧检索提供）

        llm_cfg: {base_url, api_key, model, temperature, max_tokens}——RAGAS
        评估任务级 LLM 覆盖契约（不传则 RAGAS 用自身 profiles active 配置）。
        max_tokens 取知识库活跃配置（如 2048），覆盖 RAGAS 任务默认 256，
        避免 judge 输出因长度截断被判定不完整（LLMDidNotFinish）。
        """
        llm_body = {
            "base_url": llm_cfg.get("base_url", ""),
            "api_key": llm_cfg.get("api_key", ""),
            "model": llm_cfg.get("model", ""),
            "temperature": llm_cfg.get("temperature", 0.0),
        }
        if llm_cfg.get("max_tokens") is not None:
            llm_body["max_tokens"] = llm_cfg["max_tokens"]
        return await self._post("/api/evaluations", json_body={
            "dataset_id": dataset_id,
            "metrics": metrics,
            "use_retrieval": False,
            "retrieval_top_k": top_k,
            "name": name,
            "llm": llm_body,
        })

    async def cancel_task(self, task_id: str) -> None:
        """取消评估任务；失败抛 RagasApiError（中文提示，status_code 为 RAGAS
        响应状态码：任务不存在 404 / 已完成等业务拒绝 400 / 连接失败 None）"""
        await self._post(f"/api/evaluations/{task_id}/cancel")


_ragas_client: Optional[RagasClient] = None


def get_ragas_client() -> RagasClient:
    global _ragas_client
    if _ragas_client is None:
        _ragas_client = RagasClient()
    return _ragas_client
