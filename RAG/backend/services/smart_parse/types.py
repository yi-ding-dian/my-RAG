"""共享常量与数据类（本包的地基，不依赖包内其它模块）

- 文件类型归类：本包所有"按文件类型分派"的判断都读这里，不各写一份
- METHOD_LABELS：切块方式显示名。**键集必须与 chunking.common.VALID_METHODS
  一致**——tests/test_parse_plan.py 有断言锁死这条。历史上这里漏过
  hierarchical，导致"推荐层级聚合切块"时查不到标签直接 KeyError
- 数据类：ParsePlan 是"推荐 → 入库配置"的唯一出口，config 可直接作为
  POST /{doc_id}/ingest 的请求体
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

_TEXT_FILE_TYPES = {"txt", "md"}
_PARSER_FILE_TYPES = {"pdf", "docx"}
_SPREADSHEET_FILE_TYPES = {"xlsx", "xls", "csv"}
# docx/doc：结构化读取（doc 先经 LibreOffice 转 docx，见 extract.extract_docx_like）——
# doc 不能走纯文本提取（python-docx 读不了老二进制格式），单列一类
_DOC_LIKE_FILE_TYPES = {"docx", "doc"}

# 需要走解析器（而非本地直读）的文档类型：据此决定是否把引擎建议写进入库配置
_PARSED_FILE_TYPES = _PARSER_FILE_TYPES | {"doc"}


# 切块方式显示名（与 backend.chunking.common.VALID_METHODS 键集一致，有断言锁）
METHOD_LABELS: dict[str, str] = {
    "naive": "通用切块",
    "title": "按标题切块",
    "regex": "正则切块",
    "parent_child": "父子分块",
    "qa": "QA 问答",
    "agentic": "Agentic 智能分块",
    "hierarchical": "层级聚合切块",
}


def label_for(method: str) -> str:
    """切块方式显示名（未登记时回退原值，与前端 `?? method` 同语义）"""
    return METHOD_LABELS.get(method, method)


def is_spreadsheet(file_type: str) -> bool:
    return file_type in _SPREADSHEET_FILE_TYPES


def is_text(file_type: str) -> bool:
    return file_type in _TEXT_FILE_TYPES


def is_doc_like(file_type: str) -> bool:
    return file_type in _DOC_LIKE_FILE_TYPES


def is_parsed(file_type: str) -> bool:
    """是否走解析器（pdf/docx/doc）——决定 parser_engine 是否有意义"""
    return file_type in _PARSED_FILE_TYPES


@dataclass(frozen=True)
class Decision:
    """一条决策：config 中一个字段的取值、来源与中文理由

    - key 与 config 键一一对应（tests 有断言：每条 config 键都有决策）
    - source: rule=规则命中 / engine=引擎联动 / profile=画像 / default=缺省 / limit=上限回退
    - billable=True 的决策是**花钱的**（LLM 环节），规则引擎只给"开启后会怎样"，
      是否开启由用户按预算决定
    """
    key: str
    value: Any
    reason: str
    source: str = "rule"
    billable: bool = False


@dataclass(frozen=True)
class CostItem:
    """单个 LLM 环节的成本（原始单位，不做 token/金额换算）

    - calls：该环节的 LLM 调用次数（**主计量**）；未开启时也给"开启后会是多少"，
      前端做开关预览时直接取，无需再请求后端
    - input_chars / output_chars：合计字符数（如实反映"每块重发全文"的放大）
    - images：送多模态模型的图片张数（仅图片摘要非 0）
    - detail：估算口径（前端 tooltip 原样展示，如"每块重发全文 ≤2.0 万字 × 26 块"）
    - available：False = 该环节对本文档不可开启（如超阈值的上下文检索会致入库失败）
    """
    key: str
    label: str
    calls: int
    input_chars: int
    output_chars: int
    images: int
    detail: str
    available: bool = True


@dataclass(frozen=True)
class CostEstimate:
    """成本预估：total_* 只对**已开启**项求和（未开启项保留"开启后"的量供预览）

    不做 token 换算——各模型词表不同（同一个汉字，中文词表覆盖率高的模型约
    0.6 token，GPT 系可达 1.5~2，差 3 倍），换算留到展示层用"本模型实测系数"。
    """
    chunks: int
    enabled: dict[str, bool]
    items: list[CostItem]
    total_calls: int
    total_input_chars: int
    total_output_chars: int
    total_images: int
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MethodRecommendation:
    """切块方式推荐项（主推荐 recommended=True，其余为备选/可选）"""
    method: str
    label: str
    recommended: bool
    reason: str
    badge: str = "备选"          # 推荐 / 可选 / 备选


@dataclass(frozen=True)
class ParsePlan:
    """入库方案（唯一真相源）：推荐 → 入库配置的唯一出口

    三处曾各自为政的数据（Step1 面板 / Step2 徽标 / Step2 默认选中）现在都从这里
    派生，结构上不可能再打架。
    """
    config: dict                             # 可直接作为 ingest 请求体
    decisions: list[Decision]                # 每个 config 键一条理由
    cost: CostEstimate
    alternatives: list[MethodRecommendation]
    switches: dict[str, bool]                # 增强开关推荐值（前端 Step3 初值）
    engine: str                              # 生效解析引擎
    warnings: list[str] = field(default_factory=list)

    @property
    def method(self) -> str:
        return str(self.config.get("method", "naive"))

    def to_dict(self) -> dict:
        """序列化为接口响应（dataclass → dict，嵌套一并转换）"""
        return asdict(self)

    def to_recommendations(self) -> dict:
        """兼容投影：旧 recommendations 结构（**弃用**，消费点迁移完成后删除）

        由 plan 派生 —— 唯一真相源不靠约定，而是靠"另一份数据是从它派生的"。
        禁止任何调用方独立计算或回写本结构：三处矛盾正是旧结构各自为政造成的。
        """
        main = next(
            (a for a in self.alternatives if a.recommended),
            self.alternatives[0] if self.alternatives else None)
        ctx = next(d for d in self.decisions
                   if d.key == "contextual_retrieval")
        return {
            "chunk_method": {
                "method": main.method, "label": main.label,
                "recommended": True, "reason": main.reason,
            } if main else {},
            "alternatives": [
                {"method": a.method, "label": a.label,
                 "recommended": a.recommended, "reason": a.reason,
                 "badge": a.badge}
                for a in self.alternatives if not a.recommended
            ],
            "contextual_retrieval": {
                "recommended": bool(self.switches.get("contextual_retrieval")),
                "reason": ctx.reason,
            },
            "enable_heading_in_content": bool(
                self.switches.get("enable_heading_in_content")),
        }


@dataclass(frozen=True)
class AnalyzeReport:
    """analyze 接口的完整产物（路由只做权限与路径拼装）

    plan 为 None = 决策矩阵失败（画像照常返回，前端按"无推荐"处理）。
    """
    doc_id: str
    file_type: str
    extracted: bool
    extract_warning: Optional[str]
    engine_suggestion: dict
    profile: dict[str, dict]        # length / structure / qa / reference_density / spreadsheet
    plan: Optional[ParsePlan]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """接口响应：保持既有键形状一字不改，新增 parse_plan"""
        out: dict = {
            "doc_id": self.doc_id,
            "file_type": self.file_type,
            "extracted": self.extracted,
            "extract_warning": self.extract_warning,
            "engine_suggestion": self.engine_suggestion,
            "recommendations": (self.plan.to_recommendations()
                                if self.plan else {}),
            "parse_plan": self.plan.to_dict() if self.plan else None,
            "warnings": self.warnings,
        }
        # 画像各段保持原键（表格文档的 length/structure/qa/reference_density 为 {}，
        # 前端 DocumentPortrait 依赖这个分支切换，不能省键）
        for key in ("length", "structure", "qa", "reference_density",
                    "spreadsheet"):
            if key in self.profile:
                out[key] = self.profile[key]
        return out

