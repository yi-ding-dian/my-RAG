"""配置引用检测：删配置项之前，查清谁在用它。

四类可删对象与各自的引用方：

| 对象 | 引用方 | 引用失效后 |
|---|---|---|
| 提示词库条目（按名字） | 本档案 `chat.system_prompt_ref`、部门 `chat.system_prompt_ref`、外部查询 `config.system_prompt_ref` | 回退全局默认提示词 |
| LLM 对话模型（按名字） | 本档案 `llm.active`（激活的那条）、外部查询 `config.llm_model` | 回退全局激活模型 |
| 图片解析模型（按名字） | 本档案 `vision.active`、部门 `image_summary.model` | 回退全局激活模型 |
| 配置档案本身 | 只有**当前激活**那份有依赖——部门与外部查询引用的名字都来自它 | 自动切到剩余第一个档案，全局配置整体改变 |

两点决定了文案怎么写：

1. 引用失效一律**回退默认、不报错**（见 `resolve_prompt_ref` / `find_llm_item`
   / `resolve_llm_config`，它们只记 warning 不让调用方失败）。所以弹窗该说
   "会自动回退到哪"，说"会报错"是在吓唬用户。
2. 部门配置里**没有**对 LLM 模型名的引用：部门只能覆盖"默认"那一个模型的
   字段（schema 里 llm 段 name 是 `const="默认"`），不存在"某部门引用了某个
   具名模型"。图片模型不同，部门是**按名字**选（`image_summary.model`）。

检测结果由前端在打开配置弹窗时拉一次、删除时本地查表，所以这里一次性
把三类关系都算出来。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

# 引用方类型
SELF = "self"              # 当前激活档案自己（全局默认值所在地）
DEPARTMENT = "department"  # 某个部门的配置
EXT_QUERY = "ext_query"    # 某条外部查询链接


@dataclass(frozen=True)
class Reference:
    """一个引用方：谁在用这个配置项"""

    kind: str
    id: str
    label: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "label": self.label}


def _ref_text(value) -> str:
    """取引用名（空串/None = 没引用）"""
    return str(value or "").strip()


def _active_model_name(section: dict) -> str:
    """模型列表段（llm / vision）当前激活条目的名字

    active 越界时返回空串——部门列表被改小后索引可能越界，此时运行时
    也是按"没指定"处理（回退全局激活），检测口径保持一致。
    """
    models = section.get("models")
    if not isinstance(models, list) or not models:
        return ""
    idx = section.get("active", 0)
    if not isinstance(idx, int) or not (0 <= idx < len(models)):
        return ""
    item = models[idx]
    return _ref_text(item.get("name")) if isinstance(item, dict) else ""


async def collect_references(db: AsyncSession) -> dict:
    """一次算清全部引用关系

    返回::

        {
          "prompts":        {"条目名": [Reference.as_dict(), ...]},
          "llm_models":     {"模型名": [...]},
          "vision_models":  {"模型名": [...]},
          "active_profile": {"id", "name", "department_count",
                             "ext_query_count"} | None,
        }

    `active_profile` 给"删除配置档案"用：删激活档案时后端会自动激活剩余的
    第一个，**所有**部门与外部查询链接的配置都会跟着变——所以这里给的是
    总数（带计数），而不是"引用了它的人"。
    """
    # 延迟导入：本模块被 settings 路由引用，而这三个服务又反向依赖 settings，
    # 顶层导入会成环
    from backend.services.department_service import list_department_configs
    from backend.services.ext_query_service import get_ext_query_service
    from backend.services.settings.service import get_settings_service

    active = get_settings_service().get_active() or {}
    active_id = str(active.get("id") or "")
    active_name = str(active.get("name") or "")

    prompts: Dict[str, List[Reference]] = defaultdict(list)
    llm_models: Dict[str, List[Reference]] = defaultdict(list)
    vision_models: Dict[str, List[Reference]] = defaultdict(list)

    self_label = f"本档案（{active_name}）" if active_name else "本档案"

    # ---- 1) 当前激活档案自己（全局默认值的来源）----
    chat = active.get("chat") or {}
    name = _ref_text(chat.get("system_prompt_ref"))
    if name:
        prompts[name].append(
            Reference(SELF, active_id, f"{self_label}的「聊天设置」"))

    name = _active_model_name(active.get("llm") or {})
    if name:
        llm_models[name].append(
            Reference(SELF, active_id, f"{self_label}当前激活的模型"))

    name = _active_model_name(active.get("vision") or {})
    if name:
        vision_models[name].append(
            Reference(SELF, active_id, f"{self_label}当前激活的图片模型"))

    # ---- 2) 各部门配置 ----
    dept_configs = await list_department_configs(db)
    for dept_id, dept_name, cfg in dept_configs:
        label = f"部门「{dept_name}」"
        name = _ref_text((cfg.get("chat") or {}).get("system_prompt_ref"))
        if name:
            prompts[name].append(Reference(DEPARTMENT, dept_id, label))
        name = _ref_text((cfg.get("image_summary") or {}).get("model"))
        if name:
            vision_models[name].append(Reference(DEPARTMENT, dept_id, label))

    # ---- 3) 外部查询链接 ----
    queries = get_ext_query_service().list()
    for q in queries:
        conf = q.get("config") or {}
        label = f"外部查询「{q.get('name') or q.get('id')}」"
        name = _ref_text(conf.get("system_prompt_ref"))
        if name:
            prompts[name].append(
                Reference(EXT_QUERY, str(q.get("id") or ""), label))
        name = _ref_text(conf.get("llm_model"))
        if name:
            llm_models[name].append(
                Reference(EXT_QUERY, str(q.get("id") or ""), label))

    def _dump(src: Dict[str, List[Reference]]) -> Dict[str, List[dict]]:
        return {k: [r.as_dict() for r in v] for k, v in src.items()}

    return {
        "prompts": _dump(prompts),
        "llm_models": _dump(llm_models),
        "vision_models": _dump(vision_models),
        "active_profile": {
            "id": active_id,
            "name": active_name,
            "department_count": len(dept_configs),
            "ext_query_count": len(queries),
        } if active_id else None,
    }
