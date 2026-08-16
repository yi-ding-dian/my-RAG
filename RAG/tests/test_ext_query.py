"""外部查询（知识库对外开放）测试

覆盖：
- 管理 API（仅 super_admin）：创建（含 token/kb_names）/列表/校验（空名、
  库数 1~10、库存在性、config 白名单与范围）/权限 403/404/编辑（token 不变）/
  重置 token（旧链接失效）/停用启用/删除
- 外部 API（公开 token 鉴权）：info 校验、错 token/停用/不存在统一 401、
  SSE 事件流（meta→delta→done）、无命中不调 LLM、system_prompt 覆盖与
  {knowledge} 占位符、默认模板、多库检索 kb_name、多轮上下文、限流 429、
  审计日志落盘
全部离线（mock embedding + LLM）。
"""
from __future__ import annotations

import json

from conftest import create_kb, upload_and_ingest
from backend.config import DATA_DIR


def create_ext(client, headers, name="外部查询", kb_ids=None, config=None):
    """创建外部查询（默认 admin 登录态），断言 201 并返回完整配置"""
    resp = client.post("/api/ext-queries", json={
        "name": name, "kb_ids": kb_ids or [], "config": config,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestAdminCRUD:
    """管理 API：CRUD / 权限 / 校验"""

    def test_create_list_with_token_and_kb_names(self, client, admin_headers):
        """创建返回完整 token + kb_names；列表同样含完整 token（内网管理端）"""
        kb = create_kb(client, name="制度库")
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"temperature": 0.5, "top_k": 3})
        assert item["token"] and len(item["token"]) >= 32
        assert item["enabled"] is True
        assert item["kb_names"] == [{"id": kb["id"], "name": "制度库",
                                     "department_id": None}]
        assert item["config"]["temperature"] == 0.5
        assert item["config"]["top_k"] == 3
        assert item["config"]["enable_multi_turn"] is True
        items = client.get("/api/ext-queries", headers=admin_headers).json()
        assert len(items) == 1
        assert items[0]["token"] == item["token"]

    def test_create_validation(self, client, admin_headers):
        """名称/库数/库存在性/config 白名单与范围校验"""
        kb = create_kb(client)
        # 空名称
        resp = client.post("/api/ext-queries", json={"name": "  ",
                                                     "kb_ids": [kb["id"]]},
                           headers=admin_headers)
        assert resp.status_code == 400
        # kb_ids 为空
        resp = client.post("/api/ext-queries", json={"name": "x", "kb_ids": []},
                           headers=admin_headers)
        assert resp.status_code == 400
        # 超过 10 个
        kbs = [create_kb(client, name=f"库{i}") for i in range(11)]
        resp = client.post("/api/ext-queries",
                           json={"name": "x", "kb_ids": [k["id"] for k in kbs]},
                           headers=admin_headers)
        assert resp.status_code == 400
        # 不存在的库 → 400 指明库 id
        resp = client.post("/api/ext-queries",
                           json={"name": "x", "kb_ids": ["nonexist"]},
                           headers=admin_headers)
        assert resp.status_code == 400
        assert "nonexist" in resp.json()["detail"]
        # config 范围越界 → 400
        resp = client.post("/api/ext-queries",
                           json={"name": "x", "kb_ids": [kb["id"]],
                                 "config": {"top_k": 999}},
                           headers=admin_headers)
        assert resp.status_code == 400
        # 未知字段被丢弃，合法字段保留
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"bad_field": 1, "temperature": 0.7})
        assert "bad_field" not in item["config"]
        assert item["config"]["temperature"] == 0.7

    def test_permissions(self, client, admin_headers, dept_admin_headers,
                         user_headers):
        """user / dept_admin 访问管理 API → 403"""
        kb = create_kb(client)
        for hdrs in (user_headers, dept_admin_headers):
            resp = client.get("/api/ext-queries", headers=hdrs)
            assert resp.status_code == 403
            resp = client.post("/api/ext-queries",
                               json={"name": "x", "kb_ids": [kb["id"]]},
                               headers=hdrs)
            assert resp.status_code == 403

    def test_update(self, client, admin_headers):
        """编辑名称/库/config；token 保持不变（链接继续有效）；不存在 404"""
        kb_a = create_kb(client, name="库A")
        kb_b = create_kb(client, name="库B")
        item = create_ext(client, admin_headers, kb_ids=[kb_a["id"]],
                          config={"top_k": 3})
        resp = client.put(f"/api/ext-queries/{item['id']}", json={
            "name": "改名", "kb_ids": [kb_b["id"]],
            "config": {"temperature": 0.9},
        }, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        got = resp.json()
        assert got["name"] == "改名"
        assert got["kb_ids"] == [kb_b["id"]]
        assert got["config"]["temperature"] == 0.9
        assert got["token"] == item["token"]
        # 编辑到不存在的库 → 400
        resp = client.put(f"/api/ext-queries/{item['id']}",
                          json={"kb_ids": ["nonexist"]}, headers=admin_headers)
        assert resp.status_code == 400
        # 不存在的 id → 404
        resp = client.put("/api/ext-queries/nonexist", json={"name": "x"},
                          headers=admin_headers)
        assert resp.status_code == 404

    def test_reset_token(self, client, admin_headers, mock_embedding):
        """重置 token：旧链接立即失效，新 token 可正常使用"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        # 重置前旧 token 可用
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "Python 是什么？"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 200, r.text
        resp = client.post(f"/api/ext-queries/{item['id']}/reset-token",
                           headers=admin_headers)
        assert resp.status_code == 200, resp.text
        new_token = resp.json()["token"]
        assert new_token and new_token != item["token"]
        # 旧 token 失效
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "x"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 401
        # 新 token 可用
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "Python 是什么？"},
                        headers={"Authorization": f"Bearer {new_token}"})
        assert r.status_code == 200, r.text

    def test_toggle_and_delete(self, client, admin_headers, mock_embedding):
        """停用 → 外部 401；启用恢复；删除后外部 401 + 管理 404"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        # 停用
        resp = client.post(f"/api/ext-queries/{item['id']}/toggle",
                           headers=admin_headers)
        assert resp.status_code == 200 and resp.json()["enabled"] is False
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "x"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 401
        # 启用恢复
        resp = client.post(f"/api/ext-queries/{item['id']}/toggle",
                           headers=admin_headers)
        assert resp.json()["enabled"] is True
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "x"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 200, r.text
        # 删除
        resp = client.delete(f"/api/ext-queries/{item['id']}",
                             headers=admin_headers)
        assert resp.status_code == 200
        assert client.get("/api/ext-queries", headers=admin_headers).json() == []
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "x"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 401
        # 管理端操作已删除配置 → 404（伪装）
        resp = client.delete(f"/api/ext-queries/{item['id']}",
                             headers=admin_headers)
        assert resp.status_code == 404


class TestExtChat:
    """外部查询 API：鉴权 / SSE 流式 / 配置覆盖 / 多库 / 日志 / 限流"""

    def _chat(self, client, config_id, token, query, session_id=None):
        body = {"query": query}
        if session_id:
            body["session_id"] = session_id
        return client.post(f"/api/ext/{config_id}/chat", json=body,
                           headers={"Authorization": f"Bearer {token}"})

    def test_auth_failures(self, client, admin_headers, mock_embedding):
        """无 token / 错 token / 配置不存在 / 停用 → 统一 401（防探测）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        # 无 token
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "x"})
        assert r.status_code == 401
        # 错 token
        r = self._chat(client, item["id"], "wrong-token-xxx", "x")
        assert r.status_code == 401
        # 配置不存在（与 token 错误同文案同状态码）
        r = client.post("/api/ext/nonexist/chat", json={"query": "x"},
                        headers={"Authorization": "Bearer whatever"})
        assert r.status_code == 401
        assert "链接无效" in r.json()["detail"]
        # 停用
        client.post(f"/api/ext-queries/{item['id']}/toggle", headers=admin_headers)
        r = self._chat(client, item["id"], item["token"], "x")
        assert r.status_code == 401

    def test_info_endpoint(self, client, admin_headers):
        """info：页面挂载校验 {name, kb_names}；错 token/缺 token → 401"""
        kb = create_kb(client, name="制度库")
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        r = client.get(f"/api/ext/{item['id']}/info",
                       params={"token": item["token"]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["name"] == "外部查询"
        assert data["kb_names"][0]["name"] == "制度库"
        r = client.get(f"/api/ext/{item['id']}/info", params={"token": "bad"})
        assert r.status_code == 401
        r = client.get(f"/api/ext/{item['id']}/info")
        assert r.status_code == 401

    def test_stream_events(self, client, admin_headers, mock_embedding,
                           mock_llm):
        """SSE 事件顺序 meta → delta → done；meta 携带 sources（含 kb_name）"""
        kb = create_kb(client, name="测试知识库")
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        r = self._chat(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 200, r.text
        text = r.text
        for ev in ("meta", "delta", "done"):
            assert f"event: {ev}" in text, f"缺少事件 {ev}"
        assert (text.index("event: meta") < text.index("event: delta")
                < text.index("event: done")), "事件顺序必须是 meta→delta→done"
        meta_block = text.split("event: meta", 1)[1].split("\n\n", 1)[0]
        meta = json.loads(meta_block.split("data: ", 1)[1].strip())
        assert meta["sources"], "应至少命中一条来源"
        assert meta["sources"][0]["kb_name"] == "测试知识库"
        assert meta["sources"][0]["kb_id"] == kb["id"]
        # done 事件
        assert "event: done" in text

    def test_no_hit_without_llm(self, client, admin_headers, mock_embedding,
                                mock_llm):
        """空库无命中：直接告知 + done，不调用 LLM"""
        state = mock_llm(mode="error")  # 若被调用则会抛异常/记录实例
        kb = create_kb(client)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        r = self._chat(client, item["id"], item["token"], "完全不相关的问题")
        assert r.status_code == 200
        assert "未检索到相关内容" in r.text
        assert "event: delta" in r.text and "event: done" in r.text
        assert not state.instances, "无命中时不应创建 LLM 客户端"

    def test_system_prompt_override(self, client, admin_headers,
                                    mock_embedding, mock_llm):
        """config.system_prompt 覆盖默认模板；{knowledge} 占位符替换为检索原文"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"system_prompt": "你是测试助手，请逐字输出原文。\n{knowledge}"})
        state = mock_llm()
        r = self._chat(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 200, r.text
        sys_content = state.instances[0].last_kwargs["messages"][0]["content"]
        assert sys_content.startswith("你是测试助手，请逐字输出原文。")
        assert "Python" in sys_content, "{knowledge} 占位符应替换为检索原文"
        assert "[引用 1]" not in sys_content, \
            "knowledge 是原文逐字拼接，不应带引用包装"

    def test_default_template_when_prompt_empty(self, client, admin_headers,
                                                mock_embedding, mock_llm):
        """system_prompt 空/缺省 → 内置默认模板（含 [引用] 与来源标注规则）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        state = mock_llm()
        self._chat(client, item["id"], item["token"], "Python 是什么？")
        sys_content = state.instances[0].last_kwargs["messages"][0]["content"]
        assert "[引用" in sys_content, "默认模板应注入 [引用] 内容"
        # 行内引用标注指令（内置模板规则 2：句末紧贴句尾标注 [n]，编号与引用一致）
        assert "句末" in sys_content and "[n]" in sys_content
        assert "编号必须与 [引用] 中的编号一致" in sys_content

    def test_generation_params_override(self, client, admin_headers,
                                        mock_embedding, mock_llm):
        """config.temperature/top_p/max_tokens 覆盖全局 LLM 配置"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"temperature": 0.11, "top_p": 0.55,
                                  "max_tokens": 777})
        state = mock_llm()
        self._chat(client, item["id"], item["token"], "Python 是什么？")
        kwargs = state.instances[0].last_kwargs
        assert kwargs["temperature"] == 0.11
        assert kwargs["top_p"] == 0.55
        assert kwargs["max_tokens"] == 777

    def test_multi_kb_sources(self, client, admin_headers, mock_embedding,
                              mock_llm):
        """多库暴露：meta sources 覆盖两个库，kb_name/kb_id 归属正确"""
        kb_a = create_kb(client, name="知识库A")
        upload_and_ingest(client, kb_a["id"])
        kb_b = create_kb(client, name="知识库B")
        upload_and_ingest(client, kb_b["id"])
        item = create_ext(client, admin_headers,
                          kb_ids=[kb_a["id"], kb_b["id"]])
        mock_llm()
        r = self._chat(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 200, r.text
        meta_block = r.text.split("event: meta", 1)[1].split("\n\n", 1)[0]
        meta = json.loads(meta_block.split("data: ", 1)[1].strip())
        kb_ids = {s["kb_id"] for s in meta["sources"]}
        assert kb_ids == {kb_a["id"], kb_b["id"]}, "多库检索应覆盖两个库"
        name_of = {kb_a["id"]: "知识库A", kb_b["id"]: "知识库B"}
        for s in meta["sources"]:
            assert s["kb_name"] == name_of[s["kb_id"]]

    def test_multi_turn_context(self, client, admin_headers, mock_embedding,
                                mock_llm):
        """同 session_id 续上下文（历史轮数截断）；不同 session 不共享；
        enable_multi_turn=False 不带历史"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"enable_multi_turn": True,
                                  "history_rounds": 1})
        state = mock_llm()
        self._chat(client, item["id"], item["token"], "问题一", session_id="s1")
        self._chat(client, item["id"], item["token"], "问题二", session_id="s1")
        msgs = state.instances[1].last_kwargs["messages"]
        assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
        assert msgs[1]["content"] == "问题一"
        # 不同 session_id 不共享上下文
        self._chat(client, item["id"], item["token"], "问题三", session_id="s2")
        msgs2 = state.instances[2].last_kwargs["messages"]
        assert [m["role"] for m in msgs2] == ["system", "user"]
        # enable_multi_turn=False：即使同 session 也不带历史
        item2 = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                           config={"enable_multi_turn": False})
        self._chat(client, item2["id"], item2["token"], "问题四", session_id="s1")
        msgs3 = state.instances[3].last_kwargs["messages"]
        assert [m["role"] for m in msgs3] == ["system", "user"]

    def test_rate_limit(self, client, admin_headers, mock_embedding,
                        monkeypatch):
        """限流：每 config 每分钟超过阈值 → 429"""
        import backend.services.ext_query_service as eqs_mod
        monkeypatch.setattr(eqs_mod, "RATE_LIMIT_PER_MIN", 2)
        kb = create_kb(client)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        r1 = self._chat(client, item["id"], item["token"], "x")
        r2 = self._chat(client, item["id"], item["token"], "x")
        assert r1.status_code == 200 and r2.status_code == 200
        r3 = self._chat(client, item["id"], item["token"], "x")
        assert r3.status_code == 429
        # 不影响其他 config
        item2 = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                           name="另一个")
        r = self._chat(client, item2["id"], item2["token"], "x")
        assert r.status_code == 200

    def test_query_log_written(self, client, admin_headers, mock_embedding,
                               mock_llm):
        """外部查询审计日志落盘 jsonl：命中与未命中都记录（命中数区分）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        empty_kb = create_kb(client, name="空库")
        item_hit = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                              name="命中库")
        item_miss = create_ext(client, admin_headers, kb_ids=[empty_kb["id"]],
                               name="空库")
        mock_llm()
        self._chat(client, item_hit["id"], item_hit["token"], "Python 是什么？")
        self._chat(client, item_miss["id"], item_miss["token"], "任何问题")
        log_path = DATA_DIR / "ext_query_logs.jsonl"
        assert log_path.exists()
        lines = [json.loads(l) for l in
                 log_path.read_text(encoding="utf-8").strip().splitlines()]
        assert len(lines) == 2
        assert all(l["ts"] for l in lines)
        assert lines[0]["config_id"] == item_hit["id"]
        assert lines[0]["query"] == "Python 是什么？"
        assert lines[0]["hit_count"] >= 1, "命中库应有命中数"
        assert lines[1]["config_id"] == item_miss["id"]
        assert lines[1]["hit_count"] == 0, "空库应记未命中"
