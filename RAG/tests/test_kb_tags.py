"""知识库标签 API 集成测试

覆盖：
- 设置/覆盖/清空标签（PUT /kbs/{id}/tags）、创建/更新时携带标签
- 校验：超 10 个 / 超长 / 空标签 400；去重去空白
- 权限：user 403、dept_admin 不可改全局库（403）、不存在 404（伪装）
- 按标签过滤列表（?tag 多值交集）、标签聚合计数（count 降序）
- 部门隔离：dept_admin/user 只见本部门 KB 的标签
- 存量表容错：无 tags 列的表补列迁移、tags 列脏数据读取容错
"""
from __future__ import annotations

import asyncio

from conftest import _find_dept_id, admin_headers_of, create_kb


def _set_tags(client, kb_id, tags, headers):
    """设置标签并断言成功，返回响应 JSON"""
    resp = client.put(f"/api/kbs/{kb_id}/tags", json={"tags": tags}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestSetTags:
    """设置/覆盖/清空标签"""

    def test_set_tags_and_response(self, client, admin_headers):
        kb = create_kb(client)
        data = _set_tags(client, kb["id"], ["制度", "运维", "产品文档"], admin_headers)
        assert data["tags"] == ["制度", "运维", "产品文档"]
        # 响应含实时统计字段（与列表/详情一致）
        assert "doc_count" in data and "chunk_count" in data
        # 详情/列表持久化
        detail = client.get(f"/api/kbs/{kb['id']}", headers=admin_headers).json()
        assert detail["tags"] == ["制度", "运维", "产品文档"]
        listed = client.get("/api/kbs", headers=admin_headers).json()
        assert listed[0]["tags"] == ["制度", "运维", "产品文档"]

    def test_overwrite_tags(self, client, admin_headers):
        kb = create_kb(client)
        _set_tags(client, kb["id"], ["a", "b"], admin_headers)
        data = _set_tags(client, kb["id"], ["c"], admin_headers)
        assert data["tags"] == ["c"]  # 覆盖式：旧标签全部替换

    def test_clear_tags(self, client, admin_headers):
        kb = create_kb(client)
        _set_tags(client, kb["id"], ["a"], admin_headers)
        data = _set_tags(client, kb["id"], [], admin_headers)
        assert data["tags"] == []

    def test_dedupe_and_trim(self, client, admin_headers):
        """去重 + 去空白（strip 后比较）"""
        kb = create_kb(client)
        data = _set_tags(client, kb["id"], [" 文档 ", "文档", " 运维 ", "文档"], admin_headers)
        assert data["tags"] == ["文档", "运维"]

    def test_create_with_tags(self, client, admin_headers):
        """创建知识库时直接带标签（同样去重去空白）"""
        resp = client.post("/api/kbs", json={"name": "带标签库", "tags": ["制度", " 运维 "]},
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["tags"] == ["制度", "运维"]
        # 不传 tags → 空列表
        resp = client.post("/api/kbs", json={"name": "无标签库"}, headers=admin_headers)
        assert resp.json()["tags"] == []

    def test_update_kb_with_tags(self, client, admin_headers):
        """编辑弹窗路径：PUT /kbs/{id} 携带 tags"""
        kb = create_kb(client)
        resp = client.put(f"/api/kbs/{kb['id']}", json={"tags": ["新标签"]},
                          headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["tags"] == ["新标签"]
        # 省略 tags → 不改标签
        resp = client.put(f"/api/kbs/{kb['id']}", json={"name": "改名"},
                          headers=admin_headers)
        assert resp.json()["tags"] == ["新标签"]
        # 传 [] → 清空
        resp = client.put(f"/api/kbs/{kb['id']}", json={"tags": []}, headers=admin_headers)
        assert resp.json()["tags"] == []


class TestTagValidation:
    """标签校验（400）"""

    def test_too_many_tags_400(self, client, admin_headers):
        kb = create_kb(client)
        resp = client.put(f"/api/kbs/{kb['id']}/tags",
                          json={"tags": [f"标签{i}" for i in range(11)]},
                          headers=admin_headers)
        assert resp.status_code == 400
        assert "最多" in resp.text

    def test_too_long_tag_400(self, client, admin_headers):
        kb = create_kb(client)
        resp = client.put(f"/api/kbs/{kb['id']}/tags",
                          json={"tags": ["长" * 21]}, headers=admin_headers)
        assert resp.status_code == 400
        assert "20 字符" in resp.text

    def test_empty_or_blank_tag_400(self, client, admin_headers):
        kb = create_kb(client)
        for bad in ([""], ["   "], ["合法", "  "]):
            resp = client.put(f"/api/kbs/{kb['id']}/tags",
                              json={"tags": bad}, headers=admin_headers)
            assert resp.status_code == 400, f"tags={bad!r} 应为 400"
        # 非字符串元素由 pydantic 类型层拒绝（422）
        resp = client.put(f"/api/kbs/{kb['id']}/tags",
                          json={"tags": [None]}, headers=admin_headers)
        assert resp.status_code == 422

    def test_create_too_many_tags_400(self, client, admin_headers):
        resp = client.post("/api/kbs",
                           json={"name": "x", "tags": [f"t{i}" for i in range(11)]},
                           headers=admin_headers)
        assert resp.status_code == 400

    def test_update_kb_tags_400(self, client, admin_headers):
        kb = create_kb(client)
        resp = client.put(f"/api/kbs/{kb['id']}", json={"tags": ["长" * 21]},
                          headers=admin_headers)
        assert resp.status_code == 400


class TestTagPermission:
    """标签修改权限（can_manage_kb）"""

    def test_user_forbidden_403(self, client, admin_headers, user_headers):
        kb = create_kb(client)
        resp = client.put(f"/api/kbs/{kb['id']}/tags", json={"tags": ["x"]},
                          headers=user_headers)
        assert resp.status_code == 403

    def test_dept_admin_cannot_tag_global_kb(self, client, admin_headers,
                                             dept_admin_headers):
        """dept_admin 改无部门（全局）库标签 → 403；改本部门库 → 200"""
        global_kb = create_kb(client, name="全局库")
        resp = client.put(f"/api/kbs/{global_kb['id']}/tags", json={"tags": ["x"]},
                          headers=dept_admin_headers)
        assert resp.status_code == 403
        dept_id = _find_dept_id(client, admin_headers, "测试部门")
        dept_kb = create_kb(client, name="部门库", department_id=dept_id)
        resp = client.put(f"/api/kbs/{dept_kb['id']}/tags", json={"tags": ["部门标签"]},
                          headers=dept_admin_headers)
        assert resp.status_code == 200

    def test_unknown_kb_404(self, client, admin_headers, user_headers):
        """不存在 → 404（对 user 同样伪装，防探测）"""
        assert client.put("/api/kbs/nonexist/tags", json={"tags": ["x"]},
                          headers=admin_headers).status_code == 404
        assert client.put("/api/kbs/nonexist/tags", json={"tags": ["x"]},
                          headers=user_headers).status_code == 404

    def test_aggregate_and_filter_login_only(self, client, admin_headers,
                                             user_headers):
        """标签聚合与列表过滤登录即可（user 可读）"""
        kb = create_kb(client, name="部门库", department_id=_find_dept_id(
            client, admin_headers, "测试部门"))
        # user 与 dept_admin 同部门，可直接看（先由 admin 打标签）
        _set_tags(client, kb["id"], ["部门标签"], admin_headers)
        agg = client.get("/api/kbs/tags", headers=user_headers)
        assert agg.status_code == 200
        assert agg.json()["tags"] == [{"name": "部门标签", "count": 1}]
        listed = client.get("/api/kbs", params=[("tag", "部门标签")], headers=user_headers)
        assert len(listed.json()) == 1


class TestListFilterByTag:
    """按标签过滤列表（多值 = 交集）"""

    def test_filter_and_intersection(self, client, admin_headers):
        kb1 = create_kb(client, name="库1")
        kb2 = create_kb(client, name="库2")
        create_kb(client, name="库3")  # 无标签
        _set_tags(client, kb1["id"], ["a", "b"], admin_headers)
        _set_tags(client, kb2["id"], ["b", "c"], admin_headers)

        by_a = client.get("/api/kbs", params=[("tag", "a")], headers=admin_headers).json()
        assert [k["id"] for k in by_a] == [kb1["id"]]
        by_b = client.get("/api/kbs", params=[("tag", "b")], headers=admin_headers).json()
        assert {k["id"] for k in by_b} == {kb1["id"], kb2["id"]}
        # 多值交集：同时含 a 与 b 的只有 kb1
        both = client.get("/api/kbs", params=[("tag", "a"), ("tag", "b")],
                          headers=admin_headers).json()
        assert [k["id"] for k in both] == [kb1["id"]]
        # 不存在的标签 → 空列表（不报错）
        assert client.get("/api/kbs", params=[("tag", "不存在")],
                          headers=admin_headers).json() == []
        # 无参数 → 全部
        assert len(client.get("/api/kbs", headers=admin_headers).json()) == 3

    def test_filter_includes_tags_field(self, client, admin_headers):
        """过滤后列表项仍带 tags（前端渲染用）"""
        kb = create_kb(client)
        _set_tags(client, kb["id"], ["制度"], admin_headers)
        listed = client.get("/api/kbs", params=[("tag", "制度")],
                            headers=admin_headers).json()
        assert listed[0]["tags"] == ["制度"]


class TestTagAggregate:
    """标签聚合计数（count 降序、同 count 按名称）"""

    def test_aggregate_counts_sorted(self, client, admin_headers):
        for name, tags in [("库1", ["ai", "文档"]), ("库2", ["ai", "数据"]),
                           ("库3", ["文档"])]:
            kb = create_kb(client, name=name)
            _set_tags(client, kb["id"], tags, admin_headers)
        resp = client.get("/api/kbs/tags", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == {"tags": [
            {"name": "ai", "count": 2},
            {"name": "文档", "count": 2},   # 同 count 按名称升序
            {"name": "数据", "count": 1},
        ]}

    def test_aggregate_empty(self, client, admin_headers):
        assert client.get("/api/kbs/tags", headers=admin_headers).json() == {"tags": []}


class TestTagDeptIsolation:
    """部门隔离：聚合与过滤只见本部门 KB 的标签"""

    def test_dept_admin_sees_only_own_dept_tags(self, client, admin_headers,
                                                dept_admin_headers, user_headers):
        dept_id = _find_dept_id(client, admin_headers, "测试部门")
        # admin 建全局库（无部门）+ 两个部门库
        global_kb = create_kb(client, name="全局库")
        _set_tags(client, global_kb["id"], ["全局标签"], admin_headers)
        dept_kb1 = create_kb(client, name="部门库1", department_id=dept_id)
        _set_tags(client, dept_kb1["id"], ["部门标签", "共享"], admin_headers)
        dept_kb2 = create_kb(client, name="部门库2", department_id=dept_id)
        _set_tags(client, dept_kb2["id"], ["部门标签"], admin_headers)

        # dept_admin 聚合：只有本部门标签（共享=部门库1 独有）
        agg = client.get("/api/kbs/tags", headers=dept_admin_headers).json()["tags"]
        assert agg == [{"name": "部门标签", "count": 2}, {"name": "共享", "count": 1}]

        # 列表过滤：本部门标签可命中；全局标签不可见（空）
        assert len(client.get("/api/kbs", params=[("tag", "部门标签")],
                              headers=dept_admin_headers).json()) == 2
        assert client.get("/api/kbs", params=[("tag", "全局标签")],
                          headers=dept_admin_headers).json() == []

        # 普通 user（同部门）登录即可见本部门标签
        agg_user = client.get("/api/kbs/tags", headers=user_headers).json()["tags"]
        assert {t["name"] for t in agg_user} == {"部门标签", "共享"}

        # super_admin 聚合全量
        agg_admin = client.get("/api/kbs/tags", headers=admin_headers).json()["tags"]
        assert {t["name"] for t in agg_admin} == {"全局标签", "部门标签", "共享"}


class TestLegacyTable:
    """存量表容错：无 tags 列补列迁移 + tags 列脏数据读取容错"""

    def test_legacy_table_add_column(self, tmp_path):
        """存量 MySQL 库（kbs 表无 tags 列）→ ensure_kb_tags_column 补列，旧数据不丢

        用独立 sqlite 文件库模拟存量表（避免与 TestClient 全局 engine 的
        event loop 冲突），直接驱动 db.ensure_kb_tags_column 迁移函数。
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from backend.db import ensure_kb_tags_column

        async def _run():
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
            try:
                async with engine.begin() as conn:
                    # 模拟旧版本建表：无 tags 列 + 一行旧数据
                    await conn.execute(text(
                        "CREATE TABLE kbs (id VARCHAR(32) PRIMARY KEY, "
                        "name VARCHAR(128), description TEXT, department_id VARCHAR(32), "
                        "owner_id VARCHAR(32), doc_count INTEGER NOT NULL DEFAULT 0, "
                        "chunk_count INTEGER NOT NULL DEFAULT 0, created_at VARCHAR(32))"))
                    await conn.execute(text(
                        "INSERT INTO kbs (id, name, doc_count, chunk_count, created_at) "
                        "VALUES ('legacy1', '存量库', 0, 0, '2026-01-01 00:00:00')"))
                # 迁移补列
                assert await ensure_kb_tags_column(engine) is True
                # 幂等：重复调用不报错
                assert await ensure_kb_tags_column(engine) is True
                async with engine.begin() as conn:
                    rows = (await conn.execute(text(
                        "SELECT tags FROM kbs WHERE id='legacy1'"))).fetchall()
                    assert rows[0][0] is None  # 旧数据 tags 为空，行不丢
            finally:
                await engine.dispose()
        asyncio.run(_run())

    def test_dirty_tags_value_tolerant(self, tmp_path):
        """tags 列脏数据（非 JSON / JSON 非字符串数组）→ 服务层读取容错为空

        用独立 sqlite 文件库 + 新结构全表建库，直接驱动 KBService 读取。
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

        from backend.db import Base
        from backend.models import user_models  # noqa: F401 注册 ORM 模型
        from backend.services.kb_service import KBService

        async def _run():
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{tmp_path / 'dirty.db'}")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                    for i, raw in enumerate(["{{{bad-json", '"[123]"', None]):
                        await conn.execute(text(
                            "INSERT INTO kbs (id, name, tags, doc_count, chunk_count, "
                            "created_at) VALUES (:id, :name, :tags, 0, 0, '2026-01-01 00:00:00')"),
                            {"id": f"k{i}", "name": f"库{i}", "tags": raw})
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    svc = KBService()
                    for i in range(3):
                        kb = await svc.get(db, f"k{i}")
                        assert kb is not None
                        assert kb.tags == []  # 脏数据容错为空，不 500
            finally:
                await engine.dispose()
        asyncio.run(_run())
