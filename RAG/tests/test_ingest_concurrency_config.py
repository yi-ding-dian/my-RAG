"""入库并发数系统配置 测试

背景：后台入库并发上限原为环境变量 INGEST_CONCURRENCY（模块导入时读取，
改环境变量需重启）。改造为系统配置段 ingestion.concurrency（默认 3，
范围 1~10，超管在 Settings 页可调，即时生效）：
- settings_service SECTION_SCHEMA 加 ingestion 段（range 校验 + 旧档案补段）
- ingestion_service 信号量按配置值惰性重建（每次 acquire 前比对配置，
  变化 → 按新值重建，见 _get_ingest_semaphore）

覆盖：
- 默认并发 3（出厂配置 + 配置档案段齐全）
- PUT /api/settings/profiles/{id} 越界（0/11/非数字）→ 400 中文文案
- 合法更新（边界 1/10、中间 5）→ 200 且全局活跃配置即时生效
- 信号量动态重建：配置值变化 → 重建信号量（新上限生效）
- 旧档案缺 ingestion 段自动补默认段（fill_section）
"""
from __future__ import annotations

from backend.config import (build_default_config, get_active_config,
                            set_active_config)


class TestIngestConcurrencyConfig:

    def _put(self, client, headers, concurrency):
        profiles = client.get("/api/settings/profiles",
                              headers=headers).json()
        pid = profiles[0]["id"]
        return client.put(f"/api/settings/profiles/{pid}",
                          json={"ingestion": {"concurrency": concurrency}},
                          headers=headers)

    def test_default_concurrency_is_3(self):
        """出厂配置默认并发 3（get_active_config 实时读取）"""
        assert get_active_config().ingestion.concurrency == 3

    def test_profile_has_ingestion_section(self, client, admin_headers):
        """配置档案含 ingestion 段且默认 3（.env 出厂值）"""
        profiles = client.get("/api/settings/profiles",
                              headers=admin_headers).json()
        assert profiles[0]["ingestion"]["concurrency"] == 3

    def test_under_1_400(self, client, admin_headers):
        """0 → 400（文案带字段中文标签）"""
        resp = self._put(client, admin_headers, 0)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "入库并发数需为 1~10"

    def test_over_10_400(self, client, admin_headers):
        """11 → 400"""
        resp = self._put(client, admin_headers, 11)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "入库并发数需为 1~10"

    def test_non_numeric_400(self, client, admin_headers):
        """非数字 → 400"""
        resp = self._put(client, admin_headers, "abc")
        assert resp.status_code == 400
        assert "入库并发数需为 1~10" in resp.json()["detail"]

    def test_valid_update_effective(self, client, admin_headers):
        """合法更新 5 → 200，档案返回 5 且全局活跃配置即时生效（无需重启）"""
        resp = self._put(client, admin_headers, 5)
        assert resp.status_code == 200, resp.text
        assert resp.json()["ingestion"]["concurrency"] == 5
        assert get_active_config().ingestion.concurrency == 5

    def test_boundary_1_and_10_ok(self, client, admin_headers):
        """边界 1 与 10 → 200 且生效"""
        for value in (1, 10):
            resp = self._put(client, admin_headers, value)
            assert resp.status_code == 200, resp.text
            assert resp.json()["ingestion"]["concurrency"] == value
            assert get_active_config().ingestion.concurrency == value

    def test_semaphore_rebuilt_on_config_change(self):
        """信号量动态重建：配置值变化 → 重建信号量（新上限生效，并发语义保持）

        信号量创建后不可变，_get_ingest_semaphore 每次 acquire 前比对
        当前系统配置，变化则按新值重建（旧任务按各自获取时计数继续）。
        """
        from backend.services import ingestion_service

        orig_cfg = get_active_config()
        try:
            # 基线：首次获取绑定默认 3
            s1 = ingestion_service._get_ingest_semaphore()
            assert ingestion_service._ingest_semaphore_value == 3
            assert s1._value == 3

            # 配置改为 5 → 重建
            cfg5 = build_default_config()
            cfg5.ingestion.concurrency = 5
            set_active_config(cfg5)
            s2 = ingestion_service._get_ingest_semaphore()
            assert s2 is not s1, "配置变化后应重建信号量"
            assert s2._value == 5
            assert ingestion_service._ingest_semaphore_value == 5

            # 配置未变 → 复用同一信号量（不重建）
            s3 = ingestion_service._get_ingest_semaphore()
            assert s3 is s2

            # 改回默认 3 → 再次重建（回落）
            set_active_config(build_default_config())
            s4 = ingestion_service._get_ingest_semaphore()
            assert s4 is not s2
            assert s4._value == 3
        finally:
            set_active_config(orig_cfg)

    def test_old_profile_fills_ingestion_section(self):
        """旧档案缺 ingestion 段：_coerce 自动补默认段（fill_section）"""
        from backend.services.settings_service import SettingsService

        coerced = SettingsService._coerce({
            "id": "old",
            "name": "旧档案",
            "active": True,
            "chat": {"history_rounds": 8},
        })
        assert coerced["ingestion"]["concurrency"] == 3

    def test_old_profile_fills_missing_concurrency(self):
        """旧档案 ingestion 段缺字段：自动补默认（fill_missing）"""
        from backend.services.settings_service import SettingsService

        coerced = SettingsService._coerce({
            "id": "old2",
            "name": "旧档案2",
            "active": True,
            "ingestion": {},
        })
        assert coerced["ingestion"]["concurrency"] == 3
