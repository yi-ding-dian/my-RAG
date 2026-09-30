"""连接测试的按段过滤 / 并发 / 异常隔离 + 新建档案的草稿测试接口

覆盖（对应配置弹窗面板标题上「测试」按钮的后端契约）：
- `sections` 只测点名的段（点「向量存储」不该把 LLM / DeepDoc 也等一遍）
- 各段**并发**探测：总耗时 ≈ 最慢那段，而非各段之和
- 单段抛异常不拖垮整次测试（其余段照常返回结论，接口不 500）
- POST /settings/test-draft：新建档案还没有 id 时也能按当前表单值测
"""
from __future__ import annotations

import asyncio
import time

from backend.services.settings.service import get_settings_service

ALL_SECTIONS = {"llm", "embedding", "mineru", "gotenberg", "deepdoc",
                "mysql", "minio", "vector_store", "rerank", "vision"}

# 同步探测 / 协程探测各取几个：两条路径都要覆盖（_run 对它们走不同分支）
_SYNC_PROBES = ("_test_llm", "_test_embedding", "_test_mineru",
                "_test_gotenberg", "_test_rerank", "_test_vision")
_ASYNC_PROBES = ("_test_deepdoc", "_test_mysql", "_test_minio",
                 "_test_vector_store")


def _make_profile(client, admin_headers, name="按段测试", **sections):
    resp = client.post("/api/settings/profiles", json={"name": name, **sections},
                       headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestSectionsFilter:
    """sections 参数：只测点名的段"""

    def test_single_section(self, client, admin_headers):
        profile = _make_profile(client, admin_headers)
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           params={"sections": "vector_store"},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert set(resp.json()) == {"vector_store"}, resp.json()

    def test_multiple_sections(self, client, admin_headers):
        profile = _make_profile(client, admin_headers)
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           params={"sections": "minio,vector_store"},
                           headers=admin_headers)
        assert set(resp.json()) == {"minio", "vector_store"}, resp.json()

    def test_absent_means_all(self, client, admin_headers):
        """不传 sections = 全测（卡片级「测试连接」沿用这个语义）"""
        profile = _make_profile(client, admin_headers)
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           headers=admin_headers)
        assert set(resp.json()) == ALL_SECTIONS, resp.json()

    def test_blank_means_all(self, client, admin_headers):
        """空串 / 只有逗号 = 没点名任何段 → 全测"""
        profile = _make_profile(client, admin_headers)
        resp = client.post(f"/api/settings/profiles/{profile['id']}/test",
                           params={"sections": " , "},
                           headers=admin_headers)
        assert set(resp.json()) == ALL_SECTIONS, resp.json()


class TestConcurrency:
    """各段并发探测"""

    @staticmethod
    def _slow_sync(*_args, **_kwargs):
        time.sleep(0.2)
        return {"ok": True, "latency_ms": 200, "message": "ok"}

    @staticmethod
    async def _slow_async(*_args, **_kwargs):
        await asyncio.sleep(0.2)
        return {"ok": True, "latency_ms": 200, "message": "ok"}

    def test_probes_run_concurrently(self, monkeypatch):
        """10 段各睡 0.2s：串行要 2s 上下，并发应远小于它"""
        svc = get_settings_service()
        for name in _SYNC_PROBES:
            monkeypatch.setattr(svc, name, self._slow_sync)
        for name in _ASYNC_PROBES:
            monkeypatch.setattr(svc, name, self._slow_async)

        t0 = time.time()
        result = asyncio.run(svc.test_connections({}))
        elapsed = time.time() - t0

        assert set(result) == ALL_SECTIONS, result
        assert all(r["ok"] for r in result.values()), result
        assert elapsed < 1.0, f"各段似乎没有并发（耗时 {elapsed:.2f}s）"

    def test_single_probe_exception_isolated(self, monkeypatch):
        """一段同步抛异常 → 该段记失败，同批其它段照常出结论"""
        svc = get_settings_service()

        def boom(*_args, **_kwargs):
            raise RuntimeError("mock: 探测炸了")

        monkeypatch.setattr(svc, "_test_minio", boom)
        monkeypatch.setattr(svc, "_test_llm", self._slow_sync)

        result = asyncio.run(svc.test_connections(
            {}, sections=["llm", "minio"]))

        assert result["llm"]["ok"] is True, result
        assert result["minio"]["ok"] is False, result
        assert "探测异常" in result["minio"]["message"], result

    def test_sections_skips_other_probes(self, monkeypatch):
        """没点名的段根本不跑（拿掉它的探测也不会出错）"""
        svc = get_settings_service()

        def boom(*_args, **_kwargs):
            raise RuntimeError("这一段不该被调用")

        for name in _SYNC_PROBES + _ASYNC_PROBES:
            monkeypatch.setattr(svc, name, boom)
        monkeypatch.setattr(svc, "_test_llm", self._slow_sync)

        result = asyncio.run(svc.test_connections({}, sections=["llm"]))
        assert set(result) == {"llm"}, result


class TestDraftConnection:
    """POST /settings/test-draft：新建档案（无 id）也能测"""

    def test_draft_uses_posted_values(self, client, admin_headers):
        """draft 接口纯按请求体探测：只回点名的段，且用的是请求体里的值"""
        resp = client.post("/api/settings/test-draft",
                           params={"sections": "vector_store"},
                           json={"name": "尚未保存的档案",
                                 "vector_store": {"backend": "chroma"}},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert set(data) == {"vector_store"}, data
        assert data["vector_store"]["ok"] is True, data
        # 测试环境用的是本地 Chroma 目录
        assert "Chroma" in data["vector_store"]["message"], data

    def test_draft_requires_admin(self, client, user_headers):
        """普通用户无权测连接：与 /profiles/{id}/test 同一道门槛，
        且按项目约定越权一律 404 伪装（不暴露"这个接口存在"）"""
        resp = client.post("/api/settings/test-draft",
                           params={"sections": "vector_store"},
                           json={"name": "x"}, headers=user_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "资源不存在"

    def test_draft_does_not_create_profile(self, client, admin_headers):
        """只测不写：调完 draft 接口不该多出一个档案"""
        before = client.get("/api/settings/profiles",
                            headers=admin_headers).json()
        client.post("/api/settings/test-draft",
                    params={"sections": "vector_store"},
                    json={"name": "不该被创建", "vector_store":
                          {"backend": "chroma"}},
                    headers=admin_headers)
        after = client.get("/api/settings/profiles",
                           headers=admin_headers).json()
        assert len(after) == len(before)
        assert "不该被创建" not in [p["name"] for p in after]
