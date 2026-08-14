"""pytest 全局配置：数据完全隔离 + 服务单例重置 + TestClient + 离线 mock

核心思路：
1. 本模块顶层（任何 backend.* 导入之前）设置全部环境变量：
   - DATA_DIR 指向 tempfile 临时目录（pydantic-settings 环境变量优先级高于
     .env，保证测试绝不触碰项目真实 data/，session 结束自动清理）；
   - MYSQL_URL=sqlite+aiosqlite://（内存库，离线跑，多连接实测共享同库）、
     STORAGE_BACKEND=local（本地存储后端，不连 MinIO）、
     JWT_SECRET=test-secret-test-secret（认证签名，≥16 字符满足启动强度校验，
     与 .env 出厂值隔离）；
   - LLM/Embedding/MinerU 指向本机不可达端口（测试内全部 mock，防意外误连）；
2. 每个测试前清空临时目录并重置全部服务单例（含 db engine、存储后端、
   admin token 缓存、模块导入即实例化的 settings_service 非惰性单例），
   实现用例间完全隔离（每次 TestClient 启动时 lifespan 重新 init_db 种子）；
3. mock embedding（字符直方图向量，离线可跑且相似文本可命中）与
   mock LLM（伪流式客户端）通过 monkeypatch 注入，测试不依赖外部网络；
4. client fixture 提供进程内 FastAPI TestClient（不占 8091 端口），
   lifespan 完整执行：init_db（建表 + 种子 admin/admin123 + 默认部门）
   + 存储桶 ensure（local 后端恒 True，无网络）；
5. 认证 fixtures：admin_headers / dept_admin_headers / user_headers；
   业务辅助函数（create_kb/upload_doc/...）默认自动带 admin 登录态
   （bcrypt 慢，测试内缓存 token），也可显式传 headers 覆盖。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# 关键：必须在任何 backend.* 模块导入之前设置
# （pytest 先加载本模块顶层代码，再收集/导入各测试文件）
# ---------------------------------------------------------------------------
TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="myrag_test_"))
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
# 数据库：sqlite 内存库（离线）；对象存储：本地目录（离线）；JWT：独立密钥
os.environ["MYSQL_URL"] = "sqlite+aiosqlite://"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["JWT_SECRET"] = "test-secret-test-secret"
# 测试专用口令/密钥（config.py 出厂默认已清空，此处注入固定测试值，
# 供配置档案脱敏/回传等断言使用；与 .env 出厂值完全隔离）
os.environ["LLM_API_KEY"] = "test-llm-api-key"
os.environ["EMBEDDING_API_KEY"] = "test-embed-key"
os.environ["MYSQL_PASSWORD"] = "mysql-test-pass"
os.environ["MINIO_ACCESS_KEY"] = "test-access-key"
os.environ["MINIO_SECRET_KEY"] = "minio-test-secret"
os.environ["DEEPDOC_EMAIL"] = "test@example.com"
os.environ["DEEPDOC_PASSWORD"] = "test-password"
# 双保险：外部服务地址指向本机不可达端口（测试内全部 mock，仅防意外误连）
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:59999/v1"
os.environ["EMBEDDING_BASE_URL"] = "http://127.0.0.1:59999/v1"
os.environ["MINERU_API_URL"] = "http://127.0.0.1:59999"
os.environ["DEEPDOC_BASE_URL"] = "http://127.0.0.1:59997"
os.environ["RAGAS_BASE_URL"] = "http://127.0.0.1:59998"

# 测试文档样例（中文，含两级标题，ingest 后应切成多块）
SAMPLE_TEXT = """# Python 简介

Python 是一种高级编程语言，由 Guido van Rossum 于 1991 年发布，强调代码可读性。

## 主要特性

Python 语法简洁优雅，支持面向对象、函数式等多种编程范式。

## 适用场景

Python 被广泛应用于 Web 开发、数据分析、人工智能等领域。
"""


# ==================== 服务单例重置 ====================

def reset_services():
    """重置服务单例（vector_store 除外），使下次 get_xxx_service() 重新加载

    - settings_service 是模块导入即实例化的非惰性单例，需重建实例；
    - 数据库 engine 需重置：TestClient 每次启动新 event loop，旧连接
      跨 event loop 不可用（reset_db_engine 同步 dispose，异常可忽略）；
    - 存储后端单例重置（STORAGE_BACKEND=local，新目录句柄）；
    - admin token 缓存重置：每测试 sqlite 重新种子 → admin 用户 id 变化
      → 旧 token 的 sub 查不到用户（401），必须清除；
    - vector_store 的 PersistentClient 打开着 chroma 目录的 sqlite 句柄，
      删除/重建目录会使其失效（readonly 错误），故 session 级保留同一实例，
      collection 按随机 kb_id 命名天然隔离，无需重置。
    """
    from backend.services import (chat_service, document_service,
                                  embedding_service, ext_query_service,
                                  ingestion_service, kb_service,
                                  parser_client, ragas_client,
                                  retrieval_service, storage_service)
    kb_service._kb_service = None
    document_service._document_service = None
    embedding_service._embedding_service = None
    parser_client._parser_client = None
    ingestion_service._ingestion_service = None
    # 后台任务并发信号量惰性绑定首次使用的事件循环，跨测试 loop 串用会
    # 报 "Future attached to a different loop" → 每测试重建
    ingestion_service._ingest_semaphore = None
    from backend.services import dim_check
    dim_check._rebuild_semaphore = None
    retrieval_service._retrieval_service = None
    chat_service._chat_service = None
    ext_query_service._ext_query_service = None
    ragas_client._ragas_client = None
    storage_service._storage = None
    storage_service._storage_key = None

    from backend import db as db_module
    db_module.reset_db_engine()

    from backend.services import settings_service
    from backend.services.settings_service import SettingsService
    settings_service._settings_service = SettingsService()

    _ADMIN_AUTH_CACHE["token"] = None


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp_dir():
    """session 结束删除临时数据目录（跑完不留垃圾）"""
    yield
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


_PARSER_PROBE_ALL_OK = {
    "mineru": {"available": True, "reason": ""},
    "deepdoc": {"available": True, "reason": ""},
    "plain": {"available": True, "reason": ""},
}


@pytest.fixture(autouse=True)
def _mock_parser_probe(monkeypatch):
    """默认 mock 解析器探测为全部可用（离线测试不连真实服务）

    - 探测涉及真实网络（MinerU/ragflow-server），且探测不可用会触发
      ingestion 自动降级（engine 变化），mock 全可用保证现有测试行为不变；
    - 探测降级类测试（test_parser_probe.py）用 monkeypatch 覆盖
      backend.services.parser_probe.probe_parsers 或直接测其内部函数。
    """
    async def _all_ok(cfg=None, **kw):
        return _PARSER_PROBE_ALL_OK

    # 只 patch 调用方（from-import 复制的引用）；parser_probe 模块本身保留
    # 原函数，供 test_parser_probe.py 直接测探测逻辑
    for module in ("backend.routers.knowledge_bases",
                   "backend.routers.documents",
                   "backend.services.ingestion_service"):
        monkeypatch.setattr(module + ".probe_parsers", _all_ok)


@pytest.fixture(autouse=True)
def _isolated_env():
    """每个测试前：清空 JSON 元数据与文件目录 + 重置服务单例

    - uploads/parsed/kbs/documents/chat/storage 清空内容（保留目录本身）；
    - settings.json 等根目录 JSON 删除（settings 单例重建后重新初始化默认档案）；
    - chroma 目录保留：vector_store 单例 session 级复用，kb_id 随机
      collection 天然隔离。
    """
    from backend.config import (CHAT_DIR, DATA_DIR, DOCUMENTS_DIR, KBS_DIR,
                                PARSED_DIR, STORAGE_DIR, UPLOAD_DIR)
    for d in (UPLOAD_DIR, PARSED_DIR, KBS_DIR, DOCUMENTS_DIR, CHAT_DIR,
              STORAGE_DIR):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    for p in list(DATA_DIR.glob("*.json")) + list(DATA_DIR.glob("*.jsonl")):
        p.unlink()
    reset_services()
    yield


# ==================== TestClient fixture ====================

@pytest.fixture()
def client():
    """进程内 FastAPI TestClient（不占 8091 端口，lifespan 完整执行）

    - with 进入时 lifespan 运行 init_db：建表 + 种子
      （默认部门 dept_default + 超级管理员 admin/admin123）；
    - 每次测试的 _isolated_env 已先重置 db engine 与全部服务单例，
      因此每个测试获得全新空库（用例完全隔离，无跨 event loop 残留）。
    """
    from backend.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


# ==================== 认证 fixtures ====================

_ADMIN_AUTH_CACHE: dict = {"token": None}


def admin_token(client) -> str:
    """admin 登录拿 token（惰性 + 每测试缓存）

    缓存原因：bcrypt 单次约 0.2~0.3s，大量用例共用；_isolated_env 每测试
    清缓存（内存库重新种子后 admin id 变化，旧 token 的 sub 失效 → 401）。
    """
    if _ADMIN_AUTH_CACHE["token"] is None:
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin123"})
        assert resp.status_code == 200, resp.text
        _ADMIN_AUTH_CACHE["token"] = resp.json()["access_token"]
    return _ADMIN_AUTH_CACHE["token"]


def login_headers(client, username, password) -> dict:
    """登录并返回 Authorization headers"""
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def admin_headers_of(client) -> dict:
    """admin 的 Authorization headers（复用缓存 token）"""
    return {"Authorization": f"Bearer {admin_token(client)}"}


def _resolve_headers(client, headers) -> dict:
    """headers 显式传入则用之，否则默认 admin 登录态"""
    return headers if headers is not None else admin_headers_of(client)


def create_department_and_admin(client, admin_headers, dept_name, username,
                                password, display_name):
    """建部门 + 建 dept_admin 用户 + 登录，返回 (dept_id, dept_admin_headers)"""
    resp = client.post("/api/departments",
                       json={"name": dept_name, "description": "测试用部门"},
                       headers=admin_headers)
    assert resp.status_code == 201, resp.text
    dept_id = resp.json()["id"]
    resp = client.post("/api/users", json={
        "username": username,
        "password": password,
        "display_name": display_name,
        "role": "dept_admin",
        "department_id": dept_id,
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    return dept_id, login_headers(client, username, password)


def create_user(client, admin_headers, dept_id, username,
                password="user123456", display_name="普通用户") -> dict:
    """建 user 角色用户并登录，返回 headers"""
    resp = client.post("/api/users", json={
        "username": username,
        "password": password,
        "display_name": display_name,
        "role": "user",
        "department_id": dept_id,
    }, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    return login_headers(client, username, password)


def _find_dept_id(client, admin_headers, dept_name) -> str:
    """按名称查部门 id（fixture 复用已建部门）"""
    depts = client.get("/api/departments", headers=admin_headers).json()
    return next(d["id"] for d in depts if d["name"] == dept_name)


@pytest.fixture()
def admin_headers(client):
    """超级管理员登录态（admin/admin123，种子账号）"""
    return admin_headers_of(client)


@pytest.fixture()
def dept_admin_headers(client, admin_headers):
    """部门管理员登录态：先建"测试部门"，再创建 dept_admin 用户并登录"""
    _, headers = create_department_and_admin(
        client, admin_headers, "测试部门", "dept_admin_test",
        "dept123456", "部门管理员")
    return headers


@pytest.fixture()
def user_headers(client, admin_headers, dept_admin_headers):
    """普通用户登录态：与 dept_admin_headers 同部门（测试部门）"""
    dept_id = _find_dept_id(client, admin_headers, "测试部门")
    return create_user(client, admin_headers, dept_id,
                       "user_test", "user123456", "普通用户")


# ==================== 离线 mock：embedding ====================

def char_vector(text: str, dim: int = 64) -> list:
    """字符直方图向量（按 ord 取模，避免 hash 随机种子），归一化后余弦相似

    相似文本共享字符 → 向量相近 → 离线检索可命中；dim=64 保证不同文本
    向量有区分度（bge-m3 为 1024 维，测试不校验维度）。
    """
    vec = [0.0] * dim
    for ch in text:
        vec[ord(ch) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeEmbeddingService:
    """离线伪 embedding：字符直方图向量，无网络依赖"""

    async def embed(self, texts):
        return [char_vector(t) for t in texts]


@pytest.fixture()
def mock_embedding(monkeypatch):
    """替换 embedding 服务为离线实现

    ingestion/retrieval 两个模块顶部是 `from ... import get_embedding_service`
    （引用复制），因此除源模块外必须同时替换这两处的已复制引用。
    """
    fake_getter = lambda: FakeEmbeddingService()  # noqa: E731
    for module in ("backend.services.embedding_service",
                   "backend.services.ingestion_service",
                   "backend.services.retrieval_service"):
        monkeypatch.setattr(module + ".get_embedding_service", fake_getter)


# ==================== 离线 mock：LLM ====================

class _FakeChunk:
    """伪 OpenAI 流式 chunk（chat_service 只读取 choices[0].delta.content）"""

    def __init__(self, text: str):
        self.choices = [SimpleNamespace(delta=SimpleNamespace(content=text))]


class _FakeStream:
    """异步迭代器：按段依次产出伪 chunk"""

    def __init__(self, parts):
        self._parts = list(parts)
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._parts):
            raise StopAsyncIteration
        chunk = _FakeChunk(self._parts[self._i])
        self._i += 1
        return chunk


class FakeLLMClient:
    """伪 OpenAI 客户端：mode=ok 返回固定流式文本；mode=error 抛异常

    结构对齐 chat_service 的调用形态：
    `stream = await client.chat.completions.create(...)`，create 为协程。
    llm_cfg 记录注入客户端时的合并后 LLM 配置（部门 LLM 生效断言用）。
    stream=False 时（外部同步查询 /query）返回一次性的非流式响应
    （choices[0].message.content = parts 拼接），供同步接口断言。
    """

    def __init__(self, mode: str = "ok", parts=None, llm_cfg=None):
        self.mode = mode
        self.parts = parts if parts is not None else [
            "好的，根据[引用1]内容，", "Python 是一门编程语言。", "[1]"]
        self.call_count = 0
        self.llm_cfg = llm_cfg
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.call_count += 1
        self.last_kwargs = kwargs
        if self.mode == "error":
            raise RuntimeError("mock LLM 调用失败（测试构造）")
        if kwargs.get("stream") is False:
            # 非流式：一次返回完整消息内容
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="".join(self.parts)))])
        return _FakeStream(self.parts)


class _LLMMockState:
    """mock_llm 工厂状态：mode/parts 可在测试中调整，instances 记录注入的客户端"""

    def __init__(self):
        self.mode = "ok"
        self.parts = None
        self.instances = []


@pytest.fixture()
def mock_llm(monkeypatch):
    """替换 chat_service 的 LLM 客户端为离线伪客户端

    用法:
        mock_llm()                 # 默认成功流式
        mock_llm(mode="error")     # 模拟 LLM 调用失败（走 error 事件）
        mock_llm(parts=[...])      # 自定义流式文本
    返回 state（instances 列表记录已注入客户端，可断言 LLM 调用次数）
    """
    from backend.services import chat_service
    state = _LLMMockState()

    def _get_client(self, llm_cfg=None):
        inst = FakeLLMClient(mode=state.mode, parts=state.parts,
                             llm_cfg=llm_cfg)
        state.instances.append(inst)
        return inst

    monkeypatch.setattr(chat_service.ChatService, "_get_client", _get_client)

    def factory(mode="ok", parts=None):
        state.mode = mode
        state.parts = parts
        return state

    return factory


# ==================== 业务辅助函数 ====================

def create_kb(client, name="测试知识库", description="单测知识库",
              headers=None, **extra):
    """创建知识库，返回 KnowledgeBase dict（默认 admin 登录态）

    - headers 显式传入（如 user_headers）时以传入者身份调用；
    - extra 透传请求体字段（如 department_id 指定部门）。
    """
    resp = client.post(
        "/api/kbs",
        json={"name": name, "description": description, **extra},
        headers=_resolve_headers(client, headers),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def upload_doc(client, kb_id, filename="测试文档.txt", content=None,
               mime="text/plain", headers=None):
    """上传文档（默认 SAMPLE_TEXT），返回 DocumentItem dict（status=uploaded）"""
    if content is None:
        content = SAMPLE_TEXT
    if isinstance(content, str):
        content = content.encode("utf-8")
    resp = client.post(
        f"/api/kbs/{kb_id}/documents/upload",
        files={"file": (filename, content, mime)},
        headers=_resolve_headers(client, headers),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def upload_and_ingest(client, kb_id, filename="测试文档.txt", content=None,
                      headers=None, ingest_body=None):
    """上传并等待 ingested，返回最终 DocumentItem dict

    ingest_body: 可选切块参数（method/chunk_size/overlap/delimiter/
    split_level/regex_pattern），None 等价于不传 body（沿用默认/已有配置）。
    """
    doc = upload_doc(client, kb_id, filename=filename, content=content,
                     headers=headers)
    resp = client.post(f"/api/kbs/{kb_id}/documents/{doc['id']}/ingest",
                       json=ingest_body,
                       headers=_resolve_headers(client, headers))
    assert resp.status_code == 200, resp.text
    return wait_for_status(client, kb_id, doc["id"], headers=headers)


def wait_for_status(client, kb_id, doc_id, status="ingested", timeout=30.0,
                    headers=None):
    """轮询文档直到指定状态；超时或 failed 抛 AssertionError"""
    hdrs = _resolve_headers(client, headers)
    deadline = time.monotonic() + timeout
    while True:
        resp = client.get(f"/api/kbs/{kb_id}/documents/{doc_id}", headers=hdrs)
        doc = resp.json()
        if doc["status"] == status:
            return doc
        if doc["status"] == "failed":
            raise AssertionError(f"文档入库失败: {doc.get('error')}")
        if time.monotonic() > deadline:
            raise AssertionError(
                f"等待 {status} 超时（{timeout}s），当前状态: {doc['status']}")
        time.sleep(0.2)


def extract_session_id(sse_text: str) -> str:
    """从 SSE 文本提取 done 事件的 session_id"""
    for block in sse_text.split("\n\n"):
        if block.startswith("event: done"):
            data_line = block.split("data: ", 1)[1]
            return json.loads(data_line)["session_id"]
    raise AssertionError("SSE 文本中未找到 done 事件")
