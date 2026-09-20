"""外部查询（知识库对外开放）测试

覆盖：
- 管理 API（仅 super_admin）：创建（含 token/kb_names）/列表/校验（空名、
  库数 1~10、库存在性、config 白名单与范围）/权限 403/404/编辑（token 不变）/
  重置 token（旧链接失效）/停用启用/删除/有效期设置与续期
- 外部 API（公开 token 鉴权）：info 校验、错 token/停用/**已过期**/不存在
  统一 401、SSE 事件流（meta→delta→done）、无命中不调 LLM、system_prompt
  覆盖与 {knowledge} 占位符、默认模板、多库检索 kb_name、多轮上下文、
  限流 429、记录落库（含来源 IP）
- 图片代理：鉴权 / 越权（非暴露库）/ 开关 / 路径穿越 / 正常读取
- 记录接口：筛选（链接、IP、时间段）/ 分页 / 总览统计
全部离线（mock embedding + LLM）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from conftest import create_kb, upload_and_ingest

# 到期时间格式（与实现约定一致；此处写字面量避免与实现常量耦合）
EXPIRES_FMT = "%Y-%m-%d %H:%M:%S"


def create_ext(client, headers, name="外部查询", kb_ids=None, config=None,
               expires_at=None):
    """创建外部查询（默认 admin 登录态），断言 201 并返回完整配置"""
    payload = {"name": name, "kb_ids": kb_ids or [], "config": config}
    if expires_at is not None:
        payload["expires_at"] = expires_at
    resp = client.post("/api/ext-queries", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestAdminCRUD:
    """管理 API：CRUD / 权限 / 校验"""

    def test_create_list_with_token_masked(self, client, admin_headers):
        """创建返回完整 token + kb_names；列表仅回传打码 token（防批量泄露），
        完整凭证经 GET /ext-queries/{id}/token 单独取回（带审计）"""
        kb = create_kb(client, name="制度库")
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"temperature": 0.5, "top_k": 3})
        assert item["token"] and len(item["token"]) >= 32
        assert item["enabled"] is True
        assert item["kb_names"] == [{"id": kb["id"], "name": "制度库",
                                     "department_id": None}]
        assert item["config"]["temperature"] == 0.5
        assert item["config"]["top_k"] == 3
        assert item["config"]["enable_multi_turn"] is False, \
            "多轮对话默认关闭（外部以一次性问答为主）"
        items = client.get("/api/ext-queries", headers=admin_headers).json()
        assert len(items) == 1
        # 列表打码：与完整 token 不同且为"前4****后3"掩码格式
        assert items[0]["token"] != item["token"]
        assert items[0]["token"] == f"{item['token'][:4]}****{item['token'][-3:]}"
        assert "****" in items[0]["token"]
        # 完整 token 单独接口取回（超管可复制分发）
        res = client.get(f"/api/ext-queries/{item['id']}/token",
                         headers=admin_headers)
        assert res.status_code == 200
        assert res.json()["token"] == item["token"]

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
        """user / dept_admin 访问管理 API → 404 伪装"""
        kb = create_kb(client)
        for hdrs in (user_headers, dept_admin_headers):
            resp = client.get("/api/ext-queries", headers=hdrs)
            assert resp.status_code == 404
            resp = client.post("/api/ext-queries",
                               json={"name": "x", "kb_ids": [kb["id"]]},
                               headers=hdrs)
            assert resp.status_code == 404

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
        assert got["token"] != item["token"]  # 编辑返回也不带明文（打码回传）
        assert "****" in got["token"]
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
        """外部查询记录落库：命中与未命中都记录（命中数区分，含来源 IP）"""
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
        r = client.get("/api/ext-queries/logs", headers=admin_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total"] == 2
        logs = data["items"]  # 时间倒序：后查的空库在前
        assert logs[0]["config_id"] == item_miss["id"]
        assert logs[0]["config_name"] == "空库", "配置名冗余存储"
        assert logs[0]["hit_count"] == 0, "空库应记未命中"
        assert logs[1]["config_id"] == item_hit["id"]
        assert logs[1]["query"] == "Python 是什么？"
        assert logs[1]["hit_count"] >= 1, "命中库应有命中数"
        assert logs[1]["source"] == "chat"
        assert logs[1]["client_ip"], "应记录来源 IP（外部无账号，IP 是追溯线索）"
        assert logs[1]["created_at"], "应记录发生时间"

    def test_chat_sources_image_rewritten(self, client, admin_headers,
                                          mock_embedding, mock_llm):
        """meta 下发的 sources 图片链接已改写为 ext 端点（外部页据此渲染图片）

        内部 /api/files/images/ 按登录用户 + 库权限鉴权，外部用户没有账号，
        原链接在外部页只会加载失败——故必须在下发前改写。
        """
        content = "# Python\n\n![图1](/api/files/images/doc001/图1.png) 说明。"
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"], content=content)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        mock_llm()
        r = self._chat(client, item["id"], item["token"], "Python 是什么？")
        assert r.status_code == 200, r.text
        meta_block = r.text.split("event: meta", 1)[1].split("\n\n", 1)[0]
        meta = json.loads(meta_block.split("data: ", 1)[1].strip())
        text = meta["sources"][0]["text"]
        assert f"/api/ext/{item['id']}/images/doc001/图1.png" in text
        assert f"token={item['token']}" in text
        assert "/api/files/images/" not in text, "内部链接必须全部改写"


class TestExtImages:
    """外部图片代理 /api/ext/{id}/images/{doc_id}/{name}

    外部用户与 Agent 都没有系统账号，内部图片接口一律 401，故对外提供本端点；
    鉴权与防探测口径同 info：错 token/停用/不存在 → 401「链接无效或已失效」，
    越权（文档不属于暴露的库）/开关关闭/文件名非法 → 404「图片不存在」。
    """

    def _get(self, client, config_id, doc_id, name, token=None):
        params = {"token": token} if token is not None else {}
        return client.get(f"/api/ext/{config_id}/images/{doc_id}/{name}",
                          params=params)

    def test_auth_failures(self, client, admin_headers, mock_embedding):
        """无 token / 错 token / 配置不存在 / 停用 → 统一 401（防探测）"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        assert self._get(client, item["id"], doc["id"],
                         "a.png").status_code == 401
        assert self._get(client, item["id"], doc["id"], "a.png",
                         token="wrong").status_code == 401
        assert self._get(client, "nonexist", doc["id"], "a.png",
                         token=item["token"]).status_code == 401
        client.post(f"/api/ext-queries/{item['id']}/toggle",
                    headers=admin_headers)
        r = self._get(client, item["id"], doc["id"], "a.png",
                      token=item["token"])
        assert r.status_code == 401
        assert "链接无效" in r.json()["detail"]

    def test_cross_kb_denied(self, client, admin_headers, mock_embedding):
        """文档不属于本配置暴露的库 → 404 伪装（越权读其他库图片在此拦截）"""
        kb_a = create_kb(client, name="库A")
        kb_b = create_kb(client, name="库B")
        doc_b = upload_and_ingest(client, kb_b["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb_a["id"]])
        r = self._get(client, item["id"], doc_b["id"], "a.png",
                      token=item["token"])
        assert r.status_code == 404
        assert "图片不存在" in r.json()["detail"]

    def test_disabled_returns_404(self, client, admin_headers, mock_embedding):
        """关闭图片展示 → 404（与"图片不存在"同款伪装，不泄露开关状态）"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"enable_images": False})
        r = self._get(client, item["id"], doc["id"], "a.png",
                      token=item["token"])
        assert r.status_code == 404

    def test_bad_name_returns_404(self, client, admin_headers, mock_embedding):
        """文件名非法（路径穿越 / 特殊字符）→ 404 伪装"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        for bad in ("a b", "a*b", "a;b"):
            r = self._get(client, item["id"], doc["id"], bad,
                          token=item["token"])
            assert r.status_code == 404, f"{bad} 应 404"

    def test_unknown_doc_returns_404(self, client, admin_headers,
                                     mock_embedding):
        """文档不存在 → 404 伪装"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        r = self._get(client, item["id"], "nonexistent-doc", "a.png",
                      token=item["token"])
        assert r.status_code == 404

    def test_image_ok(self, client, admin_headers, mock_embedding,
                      monkeypatch):
        """正常读取：200 + image/* content_type（存储以假实现注入，全程离线）"""
        kb = create_kb(client)
        doc = upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        from backend.routers import ext_query as ext_router

        class _FakeStorage:
            async def download_to(self, key, dest_path):
                assert key == f"images/{doc['id']}/a.png"
                with open(dest_path, "wb") as f:
                    f.write(b"\x89PNG\r\n\x1a\n")

        monkeypatch.setattr(ext_router, "get_storage_service",
                            lambda: _FakeStorage())
        r = self._get(client, item["id"], doc["id"], "a.png",
                      token=item["token"])
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("image/")
        assert r.content.startswith(b"\x89PNG")


class TestExpiry:
    """链接有效期：到期失效 / 续期 / 防探测口径"""

    def test_expired_returns_401(self, client, admin_headers, mock_embedding):
        """已过期 → 外部请求统一 401（与错 token 同文案，不暴露"已过期"状态）"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        past = (datetime.now() - timedelta(days=1)).strftime(EXPIRES_FMT)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          expires_at=past)
        assert item["expires_at"] == past
        r = client.post(f"/api/ext/{item['id']}/chat", json={"query": "x"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 401
        assert "链接无效" in r.json()["detail"], "不得暴露是过期还是不存在"
        # 其余外部端点口径一致
        assert client.get(f"/api/ext/{item['id']}/info",
                          params={"token": item["token"]}).status_code == 401
        assert client.get(
            f"/api/ext/{item['id']}/images/{kb['id']}/a.png",
            params={"token": item["token"]}).status_code == 401

    def test_not_expired_usable(self, client, admin_headers, mock_embedding,
                                mock_llm):
        """未到期 → 正常可用"""
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        future = (datetime.now() + timedelta(days=30)).strftime(EXPIRES_FMT)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          expires_at=future)
        mock_llm()
        r = client.post(f"/api/ext/{item['id']}/chat",
                        json={"query": "Python 是什么？"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 200, r.text

    def test_default_permanent(self, client, admin_headers):
        """不传有效期 = 永久有效（存量配置零迁移）"""
        kb = create_kb(client)
        assert create_ext(client, admin_headers,
                          kb_ids=[kb["id"]])["expires_at"] is None

    def test_bad_format_rejected(self, client, admin_headers):
        """格式非法 → 400 中文提示"""
        kb = create_kb(client)
        r = client.post("/api/ext-queries", json={
            "name": "x", "kb_ids": [kb["id"]], "expires_at": "2026/12/31",
        }, headers=admin_headers)
        assert r.status_code == 400
        assert "到期时间" in r.json()["detail"]

    def test_renew_extends_from_original_expiry(self, client, admin_headers):
        """续期：未过期时从**原到期时间**顺延，不损失剩余天数"""
        kb = create_kb(client)
        future = (datetime.now() + timedelta(days=10)).strftime(EXPIRES_FMT)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          expires_at=future)
        r = client.post(f"/api/ext-queries/{item['id']}/renew",
                        json={"days": 30}, headers=admin_headers)
        assert r.status_code == 200, r.text
        expected = (datetime.strptime(future, EXPIRES_FMT)
                    + timedelta(days=30)).strftime(EXPIRES_FMT)
        assert r.json()["expires_at"] == expected, "应从原到期时间顺延"

    def test_renew_expired_from_now(self, client, admin_headers):
        """续期：已过期时从当前时间重新起算"""
        kb = create_kb(client)
        past = (datetime.now() - timedelta(days=100)).strftime(EXPIRES_FMT)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          expires_at=past)
        r = client.post(f"/api/ext-queries/{item['id']}/renew",
                        json={"days": 7}, headers=admin_headers)
        assert r.status_code == 200
        left = (datetime.strptime(r.json()["expires_at"], EXPIRES_FMT)
                - datetime.now()).days
        assert 6 <= left <= 7, f"应从当前起算约 7 天，实际 {left}"

    def test_renew_days_out_of_range(self, client, admin_headers):
        """续期天数越界 → 400"""
        kb = create_kb(client)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        for days in (0, -1, 9999):
            r = client.post(f"/api/ext-queries/{item['id']}/renew",
                            json={"days": days}, headers=admin_headers)
            assert r.status_code == 400, f"days={days} 应被拒"

    def test_update_can_clear_expiry(self, client, admin_headers):
        """编辑不传 expires_at = 保持不变；传空串 = 清除（改永久）"""
        kb = create_kb(client)
        future = (datetime.now() + timedelta(days=10)).strftime(EXPIRES_FMT)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          expires_at=future)
        r = client.put(f"/api/ext-queries/{item['id']}", json={"name": "改名"},
                       headers=admin_headers)
        assert r.json()["expires_at"] == future, "不传应保持不变"
        r = client.put(f"/api/ext-queries/{item['id']}",
                       json={"expires_at": ""}, headers=admin_headers)
        assert r.json()["expires_at"] is None, "空串应清除有效期"


class TestLogsApi:
    """记录接口：总览统计 / 筛选 / 分页"""

    def _produce(self, client, config_id, token, query="问题"):
        """产生一条记录（空库查询无命中也会记录，无需 mock LLM）"""
        return client.post(f"/api/ext/{config_id}/chat", json={"query": query},
                           headers={"Authorization": f"Bearer {token}"})

    def test_overview(self, client, admin_headers, mock_embedding):
        """总览：链接维度与记录维度统计正确"""
        kb = create_kb(client)
        future = (datetime.now() + timedelta(days=3)).strftime(EXPIRES_FMT)
        past = (datetime.now() - timedelta(days=1)).strftime(EXPIRES_FMT)
        a = create_ext(client, admin_headers, kb_ids=[kb["id"]], name="甲")
        create_ext(client, admin_headers, kb_ids=[kb["id"]], name="乙",
                   expires_at=future)   # 3 天后到期 → 计入"即将到期"
        create_ext(client, admin_headers, kb_ids=[kb["id"]], name="丙",
                   expires_at=past)     # 已过期
        # 先查询产生记录（停用后请求会 401、不再记录），再停用
        self._produce(client, a["id"], a["token"])
        client.post(f"/api/ext-queries/{a['id']}/toggle", headers=admin_headers)
        r = client.get("/api/ext-queries/overview", headers=admin_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["links"]["total"] == 3
        assert d["links"]["enabled"] == 2, "甲已停用"
        assert d["links"]["expiring_soon"] == 1
        assert d["links"]["expired"] == 1
        assert d["logs"]["today"] >= 1
        assert d["logs"]["total"] >= 1
        assert d["logs"]["last_at"], "应给出最近一次访问时间"
        assert d["logs"]["retain_days"] == 90

    def test_filter_by_config(self, client, admin_headers, mock_embedding):
        """按链接筛选；config_name 冗余存储供展示"""
        kb = create_kb(client)
        a = create_ext(client, admin_headers, kb_ids=[kb["id"]], name="甲")
        b = create_ext(client, admin_headers, kb_ids=[kb["id"]], name="乙")
        self._produce(client, a["id"], a["token"], "甲的问题")
        self._produce(client, b["id"], b["token"], "乙的问题")
        d = client.get("/api/ext-queries/logs", params={"config_id": a["id"]},
                       headers=admin_headers).json()
        assert d["total"] == 1
        assert d["items"][0]["query"] == "甲的问题"
        assert d["items"][0]["config_name"] == "甲"

    def test_filter_by_ip(self, client, admin_headers, mock_embedding):
        """按来源 IP 模糊筛选（测试客户端的 IP 为 testclient）"""
        kb = create_kb(client)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        self._produce(client, item["id"], item["token"])
        hit = client.get("/api/ext-queries/logs", params={"ip": "testclient"},
                         headers=admin_headers).json()
        assert hit["total"] == 1
        miss = client.get("/api/ext-queries/logs", params={"ip": "10.99.99"},
                          headers=admin_headers).json()
        assert miss["total"] == 0

    def test_pagination(self, client, admin_headers, mock_embedding):
        """分页：total 为筛选后总数，items 按时间倒序"""
        kb = create_kb(client)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        for i in range(5):
            self._produce(client, item["id"], item["token"], f"问题{i}")
        d = client.get("/api/ext-queries/logs",
                       params={"page": 1, "page_size": 2},
                       headers=admin_headers).json()
        assert d["total"] == 5
        assert len(d["items"]) == 2
        assert d["items"][0]["query"] == "问题4", "应时间倒序"
        d2 = client.get("/api/ext-queries/logs",
                        params={"page": 3, "page_size": 2},
                        headers=admin_headers).json()
        assert len(d2["items"]) == 1

    def test_non_admin_denied(self, client, user_headers):
        """普通用户访问记录/总览接口 → 404 伪装（与其余管理接口一致）"""
        assert client.get("/api/ext-queries/logs",
                          headers=user_headers).status_code == 404
        assert client.get("/api/ext-queries/overview",
                          headers=user_headers).status_code == 404


class TestLlmModel:
    """外部查询指定 LLM 模型：缺省跟随全局，指定则用该模型（不存在时回退）"""

    def test_field_saved_default_empty(self, client, admin_headers):
        """llm_model 可保存；缺省为空串 = 跟随全局"""
        kb = create_kb(client)
        a = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        assert a["config"]["llm_model"] == "", "缺省应为空串（跟随全局）"
        b = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                       config={"llm_model": "model-b"})
        assert b["config"]["llm_model"] == "model-b"
        # 列表回读一致（该字段必须真的落库，不能被 pydantic 静默丢弃）
        items = client.get("/api/ext-queries", headers=admin_headers).json()
        got = next(x for x in items if x["id"] == b["id"])
        assert got["config"]["llm_model"] == "model-b"

    def test_resolve_uses_specified(self, monkeypatch):
        """指定存在的模型 → 解析出该模型自己的连接与参数"""
        import backend.services.ext_query_service as eqs
        monkeypatch.setattr(
            "backend.services.settings.service.find_llm_item",
            lambda ident: {"name": ident, "model": f"m-{ident}",
                           "base_url": "http://specified/v1", "api_key": "k",
                           "temperature": 0.9, "max_tokens": 123},
        )
        cfg = eqs.resolve_llm_config({"llm_model": "model-a"})
        assert cfg["model"] == "m-model-a"
        assert cfg["base_url"] == "http://specified/v1"
        assert cfg["temperature"] == 0.9
        assert cfg["max_tokens"] == 123

    def test_resolve_falls_back_to_global(self, monkeypatch):
        """未指定 / 指定但已不存在 → 都回退全局激活模型（不报错）"""
        import backend.services.ext_query_service as eqs
        monkeypatch.setattr(
            "backend.services.settings.service.find_llm_item",
            lambda ident: None,   # 模拟模型已被改名/删除
        )
        none_cfg = eqs.resolve_llm_config({})
        missing_cfg = eqs.resolve_llm_config({"llm_model": "已删除的模型"})
        assert none_cfg["model"] == missing_cfg["model"], \
            "两种情况都应回退到同一个全局激活模型"
        assert none_cfg.get("base_url"), "回退后应带出可用的连接配置"

    def test_external_query_runs_with_specified_model(self, client, admin_headers,
                                                      mock_embedding, mock_llm,
                                                      monkeypatch):
        """端到端：指定模型后 /query 仍正常应答（模型切换不影响链路）"""
        import backend.services.ext_query_service as eqs
        monkeypatch.setattr(
            "backend.services.settings.service.find_llm_item",
            lambda ident: {"name": ident, "model": "specified-model",
                           "base_url": "http://specified/v1", "api_key": "k",
                           "temperature": 0.5, "max_tokens": 512},
        )
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"llm_model": "specified-model"})
        mock_llm()
        r = client.post(f"/api/ext/{item['id']}/query",
                        json={"query": "Python 是什么？"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 200, r.text
        assert r.json()["answer"]


class TestConfigDefaults:
    """配置项精简与"跟随全局"默认值接口"""

    def test_history_rounds_removed(self, client, admin_headers):
        """历史轮数已从白名单移除：提交也被忽略（固定跟随全局聊天设置）

        该参数对外部场景是纯噪音——没人会去调"保留几轮"，且 Agent 接入
        本就无会话概念，留着只会让配置面变复杂。
        """
        kb = create_kb(client)
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"history_rounds": 10})
        assert "history_rounds" not in item["config"]
        # 存量数据里的该字段也会在读取时被 coerce_config 丢弃
        items = client.get("/api/ext-queries", headers=admin_headers).json()
        assert all("history_rounds" not in x["config"] for x in items)

    def test_defaults_endpoint(self, client, admin_headers):
        """默认值接口：让表单能显示「跟随全局（实际值）」而非空泛的"跟随全局" """
        r = client.get("/api/ext-queries/defaults", headers=admin_headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert {"llm_model", "temperature", "max_tokens", "top_p", "top_k",
                "similarity_threshold", "enable_multi_turn",
                "enable_images"} <= set(d)
        assert d["enable_multi_turn"] is False, "多轮默认关"
        assert d["enable_images"] is True, "图片默认开"
        assert isinstance(d["top_k"], int) and d["top_k"] >= 1
        assert isinstance(d["similarity_threshold"], (int, float))

    def test_defaults_requires_admin(self, client, user_headers):
        """默认值接口同样仅超管（与其余管理接口一致）"""
        assert client.get("/api/ext-queries/defaults",
                          headers=user_headers).status_code == 404


class TestPromptLibrary:
    """系统提示词库：外部链接引用其中条目（存名字 → 改库正文即全生效）"""

    def test_ref_field_saved(self, client, admin_headers):
        """system_prompt_ref 可保存；缺省为空串（= 不引用库条目）"""
        kb = create_kb(client)
        a = create_ext(client, admin_headers, kb_ids=[kb["id"]])
        assert a["config"]["system_prompt_ref"] == ""
        b = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                       config={"system_prompt_ref": "严谨引用"})
        assert b["config"]["system_prompt_ref"] == "严谨引用"
        items = client.get("/api/ext-queries", headers=admin_headers).json()
        got = next(x for x in items if x["id"] == b["id"])
        assert got["config"]["system_prompt_ref"] == "严谨引用"

    def test_resolve_chain(self, monkeypatch):
        """解析链：自定义 → 引用库条目 → 全局聊天设置 → 空（内部落内置模板）"""
        import backend.services.ext_query_service as eqs
        # 库读取已归位到 settings 层，patch 那里（外部查询通过它解析引用）
        monkeypatch.setattr(
            "backend.services.settings.service.resolve_prompt_ref",
            lambda ref: "库里的正文" if ref == "严谨引用" else None)
        # 自定义优先于引用
        assert eqs.resolve_system_prompt(
            {"system_prompt": "自定义的", "system_prompt_ref": "严谨引用"},
            "全局的") == "自定义的"
        assert eqs.resolve_system_prompt(
            {"system_prompt_ref": "严谨引用"}, "全局的") == "库里的正文"
        # 引用的条目不存在（改名/删除）→ 回退全局，不报错
        assert eqs.resolve_system_prompt(
            {"system_prompt_ref": "已删除的"}, "全局的") == "全局的"
        assert eqs.resolve_system_prompt({}, "全局的") == "全局的"
        # 都没有 → 空串，由 _build_system_content 落到内置模板
        assert eqs.resolve_system_prompt({}, "") == ""

    def test_list_prompt_items_filters_invalid(self, monkeypatch):
        """库条目读取：名或正文为空的半成品条目被过滤掉"""
        import backend.services.ext_query_service as eqs

        class _Svc:
            @staticmethod
            def get_active():
                return {"prompts": {"items": [
                    {"name": "有效", "content": "正文"},
                    {"name": "", "content": "没名字"},
                    {"name": "没正文", "content": "  "},
                    "不是字典",
                ]}}

        monkeypatch.setattr(
            "backend.services.settings.service.get_settings_service",
            lambda: _Svc())
        from backend.services.settings.service import list_prompt_items
        items = list_prompt_items()
        assert items == [{"name": "有效", "content": "正文"}]

    def test_defaults_include_prompt_options(self, client, admin_headers):
        """defaults 带 prompt_options（下拉数据源）与 default_system_prompt"""
        d = client.get("/api/ext-queries/defaults",
                       headers=admin_headers).json()
        assert isinstance(d.get("prompt_options"), list)
        assert d.get("default_system_prompt"), "应给出默认提示词全文"

    def test_endpoint_uses_referenced_prompt(self, client, admin_headers,
                                             mock_embedding, mock_llm,
                                             monkeypatch):
        """端到端：引用库条目后 /query 正常应答（提示词换来源不影响链路）"""
        monkeypatch.setattr(
            "backend.services.settings.service.resolve_prompt_ref",
            lambda ref: "你是严谨的助手，只依据引用回答。" if ref else None)
        kb = create_kb(client)
        upload_and_ingest(client, kb["id"])
        item = create_ext(client, admin_headers, kb_ids=[kb["id"]],
                          config={"system_prompt_ref": "严谨引用"})
        mock_llm()
        r = client.post(f"/api/ext/{item['id']}/query",
                        json={"query": "Python 是什么？"},
                        headers={"Authorization": f"Bearer {item['token']}"})
        assert r.status_code == 200, r.text
        assert r.json()["answer"]
