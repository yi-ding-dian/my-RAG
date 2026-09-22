"""全局配置管理

设计要点（阶段2 预留）：
- LLM / Embedding / MinerU / 检索 / 切块 / 会话 / MySQL / MinIO 配置集中在一个
  ServiceConfig pydantic 模型里，.env 提供出厂默认值；
- 阶段2 settings.service 实现"配置档案"后，只需替换 _active_config（或调用
  set_active_config），各 service 均通过 get_active_config() 运行时动态读取，
  无需改动任何调用方代码。

多租户+团队协作阶段新增（Agent 1）：
- MySQL（users/departments/kbs 三表）与 MinIO（对象存储）配置段，.env 默认 +
  配置档案可覆盖；MYSQL_URL 用于测试覆盖（如 sqlite+aiosqlite://，离线跑）；
- JWT_SECRET 仅 .env 注入（安全材料，不进配置档案 UI）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# 项目根目录（backend 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """.env 驱动的出厂默认配置（字段名大写平铺，便于 .env 书写）"""

    # 服务
    HOST: str = "0.0.0.0"
    PORT: int = 8091
    # CORS 白名单（逗号分隔的具体前端来源，如
    # CORS_ORIGINS=https://rag.example.com,http://127.0.0.1:3002）：
    # 生产/公网部署**必须**配置具体前端来源（不要留 *）；未配置（空串）时
    # 拒绝跨域（不带 CORS 头——前端同源/nginx 代理不受影响，真实跨域来源
    # 需显式加白名单）；"*" 仅限本地开发调试（任意来源可跨域，生产有安全
    # 风险）；前端域名变化需同步更新，改动后需重启后端生效（启动时读取）
    # 注意：Bearer token 场景下 "*" 不能与 allow_credentials 同时用
    CORS_ORIGINS: str = ""

    # 数据目录（默认在项目下 data/，Docker 中可用环境变量覆盖为挂载卷）
    DATA_DIR: Path = BASE_DIR / "data"

    # ---- 登录限速（防爆破，IP 维度失败计数窗口）----
    # 同一 IP 在窗口内登录失败次数 >= 上限 → 锁定（返回 429）；登录成功即清零。
    # 只对**失败**计数（不限总请求数）——避免误伤同 NAT 多设备的正常登录。
    # 内存实现（单 worker 足够，重启清零）；默认开启，测试环境关闭
    # （conftest 设置 LOGIN_RATE_LIMIT_ENABLED=false，避免同 IP 多测试污染）
    LOGIN_RATE_LIMIT_ENABLED: bool = True
    # 窗口（秒）：在此窗口内累计失败 LOGIN_MAX_FAILURES 次触发锁定
    LOGIN_RATE_WINDOW: int = 60
    # 窗口内失败上限（次数）
    LOGIN_MAX_FAILURES: int = 5
    # 触发后锁定时长（秒）
    LOGIN_LOCK_SECONDS: int = 60

    # LLM（生产环境通过 .env / 配置档案注入真实地址与密钥）
    LLM_BASE_URL: str = "http://127.0.0.1:1234/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "qwen3.6-35b-a3b-apex-quality"
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_TOKENS: int = 4096
    LLM_TIMEOUT: float = 120.0

    # Embedding（生产环境通过 .env / 配置档案注入真实地址与密钥）
    EMBEDDING_BASE_URL: str = "http://127.0.0.1:8300/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = "bge-m3"
    EMBEDDING_BATCH_SIZE: int = 32
    EMBEDDING_MAX_CHARS: int = 8000
    EMBEDDING_TIMEOUT: float = 60.0

    # MinerU（独立部署，默认本机 8001，服务名 mineru-api）
    MINERU_API_URL: str = "http://127.0.0.1:8001"
    MINERU_TIMEOUT: float = 300.0

    # DeepDoc（ragflow-server 默认本机 9380，RAGFlow API；
    # DeepDoc 是 RAGFlow 内置进程内解析器，表格输出为可检索 HTML）
    DEEPDOC_BASE_URL: str = "http://127.0.0.1:9380"
    DEEPDOC_EMAIL: str = ""
    DEEPDOC_PASSWORD: str = ""
    DEEPDOC_TIMEOUT: float = 300.0
    # 临时数据集命名前缀（解析完成后自动清理，前缀便于排查残留）
    DEEPDOC_DATASET_PREFIX: str = "myrag-tmp-"

    # 检索
    RETRIEVAL_TOP_K: int = 5

    # 切块
    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 100

    # 会话
    CHAT_HISTORY_ROUNDS: int = 8

    # 入库并发数（后台解析任务并发上限的出厂默认，配置档案可覆盖）
    INGEST_CONCURRENCY: int = 3

    # ---- 数据库（MySQL，多租户 users/departments/kbs 三表）----
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 5455
    MYSQL_USER: str = "ragflow"
    MYSQL_PASSWORD: str = ""
    MYSQL_DATABASE: str = "my_rag"
    # URL 覆盖字段：正常为空走 host/port/user/password/database 组装；
    # 测试时注入 sqlite+aiosqlite://（内存库）离线跑
    MYSQL_URL: str = ""

    # ---- 对象存储（MinIO，原始文档 + 解析图片）----
    MINIO_ENDPOINT: str = "127.0.0.1:9000"
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "my-rag"
    MINIO_SECURE: bool = False
    MINIO_REGION: str = ""

    # 存储后端: minio / local（local 存本地 data/uploads，测试离线用）
    STORAGE_BACKEND: str = "minio"

    # 向量存储后端: chroma（嵌入式文件，默认）/ milvus（服务化，未接入）
    # 接口已做可插拔抽象（backend/services/vector_store.py VectorBackend），
    # 选择 milvus 时 get_vector_store() 报错提示待接入，不影响 chroma 启动
    VECTOR_BACKEND: str = "chroma"
    # Milvus 服务地址（VECTOR_BACKEND=milvus 时使用；预留配置）
    MILVUS_URI: str = "http://127.0.0.1:19530"

    # 认证（JWT 签名密钥，必须通过 .env 注入强随机值 ≥16 字符，否则拒绝启动）
    JWT_SECRET: str = ""

    model_config = {
        "env_file": str(BASE_DIR / ".env"),
        "env_file_encoding": "utf-8",
    }


# ---------------- 路径常量（全部基于 DATA_DIR 绝对路径，Docker 化友好） ----------------

def _paths(data_dir: Path):
    return {
        "DATA_DIR": data_dir,
        "UPLOAD_DIR": data_dir / "uploads",
        "PARSED_DIR": data_dir / "parsed",
        "KBS_DIR": data_dir / "kbs",
        "DOCUMENTS_DIR": data_dir / "documents",
        "CHAT_DIR": data_dir / "chat",
        # 已删除会话的归档目录（软删除：用户侧不可见，仅超管回溯反馈现场可读）
        "CHAT_DELETED_DIR": data_dir / "chat_deleted",
        "USER_MEMORY_DIR": data_dir / "user_memory",
        "CHROMA_DIR": data_dir / "chroma",
        # 本地存储后端（STORAGE_BACKEND=local）对象存放目录，与 MinIO 桶 key 同构
        "STORAGE_DIR": data_dir / "storage",
    }


# ---------------- 服务配置（集中定义，阶段2 可被配置档案整体覆盖） ----------------

class LLMConfig(BaseModel):
    """LLM 对话模型配置"""
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int
    timeout: float
    # Top P 采样范围（模型级，0~1）：外部查询留空时即取此值；
    # 默认 0.9——留 None 会让"跟随全局"无处可跟，采样范围随各服务端默认漂移，
    # 同一个知识库换模型后回答风格会莫名其妙地变
    top_p: float | None = 0.9
    # 思考控制方式（模型级，见 services/thinking_strategy）：
    #   none    不处理 —— 模型本身不思考，或部署端已关闭（如 vLLM 启动参数
    #           关了思考）；系统层不再做任何事
    #   prefill 注入 <think></think> 跳过思考 —— 仅适用于 Qwen 系
    #           （chat template 含 <think> 结构）且部署端忽略 API 参数的场景
    #           （如 LM Studio）；对不思考的模型注入会把回答压短、丢图片，
    #           对 DeepSeek 等模型实测无效
    #   api     传 thinking 参数（{"thinking":{"type":"disabled"}}）——
    #           支持该参数的在线 API（DeepSeek 等）
    # 默认 none（安全）：新加的模型不会被误注入——漏注入只是慢，误注入会伤回答
    thinking_control: str = "none"

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "LLMConfig":
        """dict → LLMConfig 兼容转换（历史裸 dict 调用点的类型化入口）

        - dict 可能含 LLMConfig 没有的扩展字段（部门合并产物/模型列表条目）
          → 只取已知字段，忽略未知
        - base_url/api_key/model 缺省或 None → ""（客户端构造语义与裸 dict
          的 .get(key, "") 一致）；temperature/max_tokens/timeout 缺省或
          None/0 → 出厂默认（0.3/4096/60，与历史 `float(x or 60)` 兜底一致）
        - thinking_control 缺省/空 → "none"（旧配置升级后不误注入）
        - None/空 dict 输入 → 出厂默认（防御脏数据）
        """
        data = data or {}
        return cls(
            base_url=data.get("base_url") or "",
            api_key=data.get("api_key") or "",
            model=data.get("model") or "",
            temperature=float(data.get("temperature") or 0.3),
            max_tokens=int(data.get("max_tokens") or 4096),
            timeout=float(data.get("timeout") or 60.0),
            # 注意不能用 `or`：0 是合法的 top_p（只取最高概率 token）
            top_p=(float(data["top_p"])
                   if data.get("top_p") is not None else 0.9),
            thinking_control=data.get("thinking_control") or "none",
        )


class EmbeddingConfig(BaseModel):
    """Embedding 模型配置"""
    base_url: str
    api_key: str
    model: str
    batch_size: int
    max_chars: int
    timeout: float


class MinerUConfig(BaseModel):
    """MinerU 解析服务配置

    engines_enabled / default_engine：由超管按服务端资源（GPU、显存）声明
    "这台机器能跑哪些解析后端"——只有声明可用的才出现在解析配置的下拉里，
    避免用户选中实际跑不动的档（如无 GPU 时选 hybrid-engine 会直接失败）；
    default_engine 是不显式指定时的默认档，**必须**在 engines_enabled 内。
    """
    api_url: str
    timeout: float
    # 可用解析后端（默认只开 pipeline：它无 GPU 也能跑，是"总能出结果"的兜底档）
    engines_enabled: List[str] = Field(default_factory=lambda: ["pipeline"])
    # 默认解析后端（必须在 engines_enabled 里；后端保存配置时校验）
    default_engine: str = "pipeline"


class DeepDocConfig(BaseModel):
    """DeepDoc 解析服务配置（RAGFlow API 默认本机 9380 ragflow-server）

    DeepDoc 是 ragflow-server 内置进程内解析器（ONNX OCR + 版面识别 +
    表格结构识别），通过 RAGFlow API 调用；核心价值：表格输出为
    HTML <table> 文本可检索（vs MinerU 表格为图片不可检索）。
    """
    base_url: str = "http://127.0.0.1:9380"
    email: str = ""
    password: str = ""
    timeout: float = 300.0
    # 临时数据集命名前缀（解析完成后 finally 自动清理）
    dataset_prefix: str = "myrag-tmp-"


class RerankConfig(BaseModel):
    """Rerank 重排序配置（OpenAI 兼容 /rerank 协议：POST {base_url}/rerank）

    - enabled 为 False 或 base_url/model 任一为空时跳过重排（严格降级，不报错）
    - api_key: 云端服务（如 SiliconFlow）需要 Bearer 鉴权；本机 vLLM 无鉴权
      时留空即可
    - top_n: 参与重排的候选条数（大于等于最终 top_k 才有意义）
    """
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    top_n: int = 10


class RetrievalConfig(BaseModel):
    """检索参数"""
    top_k: int
    # 相似度阈值：低于该分数的检索结果过滤（0=不过滤，默认）
    similarity_threshold: float = 0.0
    # 混合检索开关：BM25 关键词 + 向量 RRF 融合（默认开启；关闭=纯向量原逻辑）
    enable_hybrid: bool = True
    # Rerank 重排序（默认关闭，企业用户可开启）
    rerank: RerankConfig = Field(default_factory=RerankConfig)


class ChunkingConfig(BaseModel):
    """切块参数"""
    chunk_size: int
    chunk_overlap: int
    # 标题分层模型（可选）：层级聚合切块（hierarchical）且解析产物层级不可靠
    # （非 docx 结构解析）时，用于给标题重新分层的 LLM 模型——带编号的标题走
    # 规则、无编号的交 LLM（见 normative.heading_llm.hybrid_levels）。值为
    # 激活档案 LLM 模型列表里的标识（name 或 model 字符串，见
    # settings.service.llm_cfg_for_parser）；空 = 不做 LLM 分层，只用规则
    # 分层（运行时动态读取，改配置即生效）
    heading_llm_model: str = ""


class ChatConfig(BaseModel):
    """会话参数（生成参数 None=用 LLM 配置默认值）"""
    history_rounds: int
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    # 多轮对话开关：False 时不带历史（只发 system + 当前问题）
    enable_multi_turn: bool = True
    # 自定义系统提示词：空串 = 使用内置默认模板（chat_service._SYSTEM_PROMPT_TEMPLATE）
    system_prompt: str = ""
    # 引用的提示词库条目名（配置档案 prompts 段）：非空时**优先于** system_prompt。
    # 部门可覆盖（白名单）——部门管理员从库里选一条给自己部门用，存名字不存副本
    system_prompt_ref: str = ""
    # 单条输入（问题/检索 query）最大长度（字，默认 2000）；超出返回 400
    # 友好提示——防超长粘贴耗尽上下文/费用
    max_query_len: int = 2000
    # 知识图谱增强：查询时 LLM 抽实体 → 图谱匹配 → 1-hop 邻接扩展，
    # 图谱上下文作为"知识图谱"来源引用注入回答（默认开；无图谱自动跳过零成本）
    kg_enhance: bool = True
    # 查询改写：多轮对话时 LLM 结合历史把问题改写为独立检索查询（消除
    # "它/上面那个"等指代，口语→正式书面化，检索命中率提升）。默认开——
    # 仅在命中触发条件时才调用（见 query_rewriter._needs_rewrite：口语词必
    # 触发、不依赖历史；指代词需有历史，无历史消不掉不白费调用），每轮最多
    # 一次 LLM 调用（短超时 8s，失败自动降级用原问题）；想省调用可手动关闭
    query_rewrite: bool = True
    # 查询改写用的历史轮数（默认 3，部门可覆盖）：指代消解只需就近上下文，
    # 与 history_rounds（喂给 LLM 对话的历史，默认 8）解耦——改写输入按
    # token 计费，轮数越多越贵；范围 1~10 由 settings schema 限制
    query_rewrite_rounds: int = 3
    # 引用摘要字数（默认 600，部门可覆盖）：鼠标悬停在回答里的引用标 [n] 上时，
    # 浮层显示的字数上限。**是"窗口大小"而非"从头截断长度"**——回答用到的
    # 内容常落在块的中后段（表格块的有效数字都在表格下方），前端会先在全量
    # 文本上定位命中、再围绕命中开窗（见 MessageList 的 buildSnippet），
    # 从头硬截会让命中整段落在窗口外、浮层里什么也标不出来。
    # 范围 100~2000 由 settings schema 限制
    citation_snippet_chars: int = 600
    # 思考模式（聊天问答 LLM 调用）：disabled=关闭思考（默认，更快更省 token）
    # | enabled_low/enabled_high/enabled_max=开启思考并指定强度。注入方式按
    # 服务商区分（见 thinking_strategy）：在线 API（api.deepseek.com 等）经
    # extra_body 控制；本地 Qwen 思考模型 disabled 时注入空 <think> prefill
    # 跳过思考（LM Studio 忽略 extra_body）
    thinking_mode: str = "disabled"


class PromptItem(BaseModel):
    """系统提示词库条目（外部查询下拉里的一个选项）"""
    # 显示名：外部查询按它匹配引用（改名会让已引用的链接回退到全局默认）
    name: str = ""
    # 提示词正文；留空视为无效条目（下拉里不展示）
    content: str = ""


class PromptLibraryConfig(BaseModel):
    """系统提示词库（供外部查询引用；不写进运行时全局配置）

    放在配置档案里：切换档案时提示词库随之切换，与 LLM 模型列表同理。
    外部查询存的是**条目名**（引用）而非内容副本——改库里的正文，所有
    引用它的链接立刻生效，不会出现"N 条链接各存一份过期副本"。
    """
    items: list = Field(default_factory=list)


class ContextualRetrievalConfig(BaseModel):
    """上下文检索增强配置（入库切块后处理，运行时动态读取）

    - max_full_doc_chars：完整文档视角阈值（字符，默认 20000）。解析文本
      <= 阈值时，摘要生成把完整文档作为上下文（全局视角，替代文档名+前
      1500 字符截断）；超过阈值 → 提示效果不佳，任务失败建议换用其他切块
      方式或关闭增强（见 contextual_retriever.DocTooLongError）
    """
    max_full_doc_chars: int = 20000


class IngestionConfig(BaseModel):
    """入库并发/配额配置（后台解析任务并发上限 + 知识库文档数/上传大小护栏，
    超管在系统配置页可调，即时生效）

    - concurrency：同时解析入库的文档数上限（默认 3，范围 1~10）。
      超出上限的任务在信号量队列等待，避免批量解析打爆 MinerU/embedding；
      运行时由 backend/services/ingestion/service.py 每次 acquire 前实时读取，改动即生效
      （信号量按配置值惰性重建，见 _get_ingest_semaphore）
    - image_summary_concurrency：图片摘要调多模态模型的**全局**并发上限
      （默认 4，范围 1~16）。注意这是**跨文档共享**的池子：单篇文档解析时
      能吃满，多篇同时解析时自动分摊，在飞请求总数恒定——若做成"每篇文档
      各自并发"，3 篇 × N 会把模型打爆。默认值取自实测（2026-09-14 压测：
      1→4 吞吐 ×2.8；4→8 仅 +11% 但延迟翻倍；8→16 仅 +15%，吞吐天花板
      ≈3.4 张/秒是模型侧物理上限）。配 1 等价于改造前的串行行为
    - kb_doc_limit：单知识库最大文档数，0=不限；上传/URL 导入时校验，
      超限返回友好 400（防单库无限膨胀/多用户上传耗尽磁盘）
    - max_upload_mb：单文件上传上限（MB，默认 100）；上传时校验，
      超过返回 413。nginx 反代层上限需 >= 该配置（deploy 文档已提示）
    """
    concurrency: int = 3
    image_summary_concurrency: int = 4
    kb_doc_limit: int = 0
    max_upload_mb: int = 100


class AgenticConfig(BaseModel):
    """Agentic 检索增强（LangGraph 检索决策层，聊天设置可配，默认关闭）

    开启后问答链路在"检索 → 生成"之间插入决策循环：
    检索结果按最高相似度分数（0~1，cos 相似度）分档：
    - 分数 >= recheck_threshold → 直接回答（不改写不重试）
    - abstain_threshold <= 分数 < recheck_threshold 且 重试未超上限
      → LLM 改写查询 + 重新检索 + 再分档
    - 分数 < abstain_threshold，或重试用尽仍不达标 → 拒答（沿用
      "未检索到相关内容"提示），不改写不重试——乱问/知识库无相关内容
      时零额外成本
    默认关闭（enabled=False）：问答链路与未接入时完全一致。
    """
    # 总开关（默认关；开启后聊天设置可调）
    enabled: bool = False
    # 查询改写重试上限（改写后重检索；0 = 仅分档不改写）
    max_retries: int = 1
    # 直接回答下限（相似度 >= 该值不进入改写循环）
    recheck_threshold: float = 0.55
    # 拒答阈值（相似度 < 该值直接拒答；超管可调）
    abstain_threshold: float = 0.25


class MySQLConfig(BaseModel):
    """MySQL 连接配置（url 非空时优先使用，测试覆盖 sqlite 用）"""
    host: str = "127.0.0.1"
    port: int = 5455
    user: str = "ragflow"
    password: str = ""
    database: str = "my_rag"
    url: str = ""


class MinIOConfig(BaseModel):
    """MinIO 连接配置"""
    endpoint: str = "127.0.0.1:9000"
    access_key: str = ""
    secret_key: str = ""
    bucket: str = "my-rag"
    secure: bool = False
    region: str = ""


class VectorStorageConfig(BaseModel):
    """向量存储后端配置（配置档案新增段，替代纯 env 控制）

    - backend: chroma（嵌入式文件，默认）/ milvus（服务化，需 milvus_uri）
    - milvus_uri: milvus 服务地址（backend=milvus 时使用）
    """
    backend: str = "chroma"
    milvus_uri: str = ""


class VisionModelConfig(BaseModel):
    """图片摘要使用的多模态模型条目

    超管在「系统配置 → 图片解析模型」里可配**多个**（复用 llm 段的模型列表
    结构：{models: [...], active: 索引}，第一个为默认）；部门管理员从列表里
    **选一个**（按 name 匹配），但看不到也改不了连接信息与密钥。

    - name: 显示名，部门据此选择（同段内应唯一）
    - 其余字段与 LLMConfig 同构，走 OpenAI 兼容的多模态接口
    """
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 60.0


class ImageSummaryConfig(BaseModel):
    """图片摘要生成配置（部门可覆盖模型选择与提示词）

    - model:  选中的模型 name（空 = 用列表第一个）
    - prompt: 提示词（空 = 用内置默认模板）
    - options: 结构化选项，勾选后自动生成 prompt；键为
      read_text / describe_scene / label_type / describe_layout
    - output_format: fields（固定字段，默认）/ prose（自然段）
    - text_max_chars: 「文字」字段长度上限（仅 fields 模式，超出截断）
    - max_images: 单文档摘要张数上限（0 = 不限）
    """
    model: str = ""
    prompt: str = ""
    options: Dict[str, bool] = Field(default_factory=dict)
    output_format: str = "fields"
    text_max_chars: int = 200
    max_images: int = 50


class GotenbergConfig(BaseModel):
    """Gotenberg 文档转换服务配置（Office → PDF）

    用途：老版 .doc / .docx 等先转成 PDF，再交 MinerU 做版面识别——MinerU 的
    主场是 PDF（按字号/字体/位置还原标题层级），而它处理 docx 时只提取文本、
    不做版面分析，**标题层级会全丢**（实测同一份文档：直接给 docx → 一个标题
    都识别不出；转成 PDF 再给 → 标题层级完整还原）。

    为什么用容器版而非宿主机直接跑 LibreOffice：容器可以加内存硬上限，转换
    大文档内存暴涨时**只有容器被杀**，不拖垮整机（实测裸跑 soffice 在
    105MB / 795 张扫描图的 .doc 上吃到 27.9GB，把机器拖到 OOM 卡死数分钟）。

    未配置（base_url 为空）→ 不做转换，.doc 走原有路径。
    """
    base_url: str = "http://127.0.0.1:3000"
    timeout: float = 120.0


class ServiceConfig(BaseModel):
    """全部服务配置集合（阶段2 配置档案的完整形态）"""
    llm: LLMConfig
    embedding: EmbeddingConfig
    mineru: MinerUConfig
    deepdoc: DeepDocConfig = Field(default_factory=DeepDocConfig)
    gotenberg: GotenbergConfig = Field(default_factory=GotenbergConfig)
    retrieval: RetrievalConfig
    chunking: ChunkingConfig
    chat: ChatConfig
    contextual_retrieval: ContextualRetrievalConfig = Field(
        default_factory=ContextualRetrievalConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    agentic: AgenticConfig = Field(default_factory=AgenticConfig)
    mysql: MySQLConfig
    minio: MinIOConfig
    vector_store: VectorStorageConfig = Field(default_factory=VectorStorageConfig)
    vision: VisionModelConfig = Field(default_factory=VisionModelConfig)
    image_summary: ImageSummaryConfig = Field(default_factory=ImageSummaryConfig)
    # 系统提示词库（外部查询引用的可选项来源；内容不参与运行时行为，
    # 挂在 ServiceConfig 上只是为了让配置档案的段能按 dataclass 名反射）
    prompts: PromptLibraryConfig = Field(default_factory=PromptLibraryConfig)


settings = Settings()

# 数据目录自动创建（含 uploads/parsed/kbs/documents/chat/chat_deleted/chroma）
_path_map = _paths(settings.DATA_DIR)
for _dir in _path_map.values():
    _dir.mkdir(parents=True, exist_ok=True)

DATA_DIR = _path_map["DATA_DIR"]
UPLOAD_DIR = _path_map["UPLOAD_DIR"]
PARSED_DIR = _path_map["PARSED_DIR"]
KBS_DIR = _path_map["KBS_DIR"]
DOCUMENTS_DIR = _path_map["DOCUMENTS_DIR"]
CHAT_DIR = _path_map["CHAT_DIR"]
CHAT_DELETED_DIR = _path_map["CHAT_DELETED_DIR"]
USER_MEMORY_DIR = _path_map["USER_MEMORY_DIR"]
CHROMA_DIR = _path_map["CHROMA_DIR"]
STORAGE_DIR = _path_map["STORAGE_DIR"]


def build_default_config() -> ServiceConfig:
    """从 .env 出厂配置构造 ServiceConfig（阶段2 初始化配置档案时复用此函数）"""
    return ServiceConfig(
        llm=LLMConfig(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL,
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
            timeout=settings.LLM_TIMEOUT,
        ),
        embedding=EmbeddingConfig(
            base_url=settings.EMBEDDING_BASE_URL,
            api_key=settings.EMBEDDING_API_KEY,
            model=settings.EMBEDDING_MODEL,
            batch_size=settings.EMBEDDING_BATCH_SIZE,
            max_chars=settings.EMBEDDING_MAX_CHARS,
            timeout=settings.EMBEDDING_TIMEOUT,
        ),
        mineru=MinerUConfig(
            api_url=settings.MINERU_API_URL,
            timeout=settings.MINERU_TIMEOUT,
        ),
        deepdoc=DeepDocConfig(
            base_url=settings.DEEPDOC_BASE_URL,
            email=settings.DEEPDOC_EMAIL,
            password=settings.DEEPDOC_PASSWORD,
            timeout=settings.DEEPDOC_TIMEOUT,
            dataset_prefix=settings.DEEPDOC_DATASET_PREFIX,
        ),
        # Gotenberg 无 .env 出厂项：默认本机 3000，实际地址在系统配置页填
        # （它通常与 MinerU 同机部署，地址因环境而异）
        gotenberg=GotenbergConfig(),
        retrieval=RetrievalConfig(top_k=settings.RETRIEVAL_TOP_K),
        chunking=ChunkingConfig(
            chunk_size=settings.CHUNK_SIZE,
            chunk_overlap=settings.CHUNK_OVERLAP,
        ),
        chat=ChatConfig(history_rounds=settings.CHAT_HISTORY_ROUNDS),
        # 入库并发数：.env INGEST_CONCURRENCY 出厂默认（配置档案可覆盖）
        ingestion=IngestionConfig(
            concurrency=max(1, int(settings.INGEST_CONCURRENCY))),
        mysql=MySQLConfig(
            host=settings.MYSQL_HOST,
            port=settings.MYSQL_PORT,
            user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD,
            database=settings.MYSQL_DATABASE,
            url=settings.MYSQL_URL,
        ),
        minio=MinIOConfig(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            bucket=settings.MINIO_BUCKET,
            secure=settings.MINIO_SECURE,
            region=settings.MINIO_REGION,
        ),
        # 图片摘要：出厂不预设模型（由超管在「系统配置 → 图片解析模型」里添加
        # 多模态模型），部门从列表选一个并配提示词；未配置时解析不生成摘要
        vision=VisionModelConfig(),
        image_summary=ImageSummaryConfig(),
    )


# 活跃配置（阶段1：默认值来自 .env；阶段2：settings.service 启动时替换为档案）
_active_config: ServiceConfig = build_default_config()


def get_active_config() -> ServiceConfig:
    """各 service 运行时动态读取活跃配置（不缓存启动值）"""
    return _active_config


def set_active_config(config: ServiceConfig):
    """阶段2 settings.service 覆盖活跃配置（本阶段仅默认实现）"""
    global _active_config
    _active_config = config
