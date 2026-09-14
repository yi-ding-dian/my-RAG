"""图片摘要生成：用多模态模型读图，把内容写进 markdown

**动机**：规范文档的关键信息常常只以像素形式存在——证照、扫描件、签章、
表格截图、现场照片。它们的文字提取不出来（本身就是图片），切块文本里只剩
一行 `![](images/xxx.jpg)`，于是"珠海南方软件园的产权证"这种问题**永远命中
不了**那张图。本模块让多模态模型把图读出来、写成一段描述回填 markdown，
从而参与切块与检索。

**调用时机**：ingestion 解析完成后、切块之前——必须在切块前，否则摘要进不了
chunk 文本，就只剩展示价值了。

**回填格式**（图片**下方**，引用块）：

    ![](images/image5.jpg)
    > 图片说明：
    > 类型：房地产权证内页
    > 文字：权属人 珠海南方软件园发展有限公司；终止日期 2044-03-17
    > 画面：表格版式，右下角盖有红色圆形公章

放图下方是图注惯例；写成引用块是为了让 chunking 的保护区好识别
（见 chunking/common.py 的 _find_image_ranges），把「图 + 摘要」绑成
不可切分区间。

**失败语义**：单张失败（超时/格式不支持/被拒答）→ 跳过并记一笔，**不阻塞
主流程**。图片摘要是增强功能，不该有否决权。
"""
from __future__ import annotations

import base64
import logging
import re
from typing import Dict, List, Optional, Tuple

from backend.config import ImageSummaryConfig, VisionModelConfig
from backend.services.llm_client import (LLMRequestError, LLMTimeoutError,
                                         get_llm_client, llm_completion)

logger = logging.getLogger(__name__)

# 图片引用：**两种形态都要认**
#   1) 解析器原始产物：![](images/xxx.jpg)
#   2) 入库阶段 _upload_images 替换后：![](/api/files/images/{doc_id}/xxx.jpg)
# 摘要步骤跑在两者之间（解析后、切块前），拿到的通常是**已替换**的形态——
# 只认第一种会导致"一张都没处理"（refs 为空，静默返回）。
# 捕获组恒为图片名（basename），与 images 列表的 name 对齐。
_IMG_REF_RE = re.compile(
    r"!\[\]\((?:images/|/api/files/images/[^/]+/)([^)]+)\)")

# 回填块的起始标记（chunking 保护区据此识别整块）
SUMMARY_PREFIX = "> 图片说明："

# 固定字段的输出顺序与白名单（模型多吐的行按白名单丢弃，缺的补"无"）
_FIELD_ORDER: Tuple[str, ...] = ("类型", "文字", "画面", "版式")
# 字段行正则：`类型：xxx`（中英文冒号都收）
_FIELD_LINE_RE = re.compile(r"^\s*([^：:]{1,8})\s*[：:]\s*(.*)$")

# 结构化选项 → 提示词片段（顺序即默认输出顺序）
_OPTION_ORDER: Tuple[str, ...] = ("label_type", "read_text",
                                  "describe_scene", "describe_layout")
_OPTION_LINES: Dict[str, str] = {
    "label_type": "类型：这是什么（文件类型或场景）",
    "read_text": "文字：图中可见的关键文字（标题、单位名称、编号、日期、金额、规格等）",
    "describe_scene": "画面：画面主要对象与特征（设备、场地、人物、签章等）",
    "describe_layout": "版式：版式结构（表格行列、签章位置、分区布局等）",
}
# 默认勾选项（与设计文档一致：版式默认关，它对证照/照片无用却费 token）
DEFAULT_OPTIONS: Dict[str, bool] = {"label_type": True, "read_text": True,
                                    "describe_scene": True,
                                    "describe_layout": False}

# 固定约束：不随选项变、也不允许部门改——这是防止模型编内容的关键
_FIXED_TAIL = """
要求：
- 只描述你确实看到的内容，不要推测、不要评价、不要补充常识
- 某个字段确实没有内容时，写"无"
- 不要开场白，不要总结
"""

# 过小图片的跳过阈值（像素面积）：取**文档内最大图**的该比例。印章/图标这类
# 对检索没有价值，摘要只会产出"这是一个红色圆形印章"式的废话，白费一次调用。
# 用相对判定而非绝对阈值的原因：实测同一份文档里权证图 109 万像素、裸章 7.4 万
# 像素，绝对阈值（如 4 万）根本挡不住；相对判定才有效。绝对下限兜底"整篇都是
# 小图"的文档，避免全跳。阈值刻意保守——误跳一张有用的小图（签名、二维码）
# 比省一次调用代价大得多。
_SMALL_RATIO = 0.15
_MIN_PIXELS = 200 * 200

_MAX_TOKENS = 500
_TEMPERATURE = 0.1  # 摘要要稳定可复现，不吃创造性


# ---- 提示词构造（结构化选项 → 提示词；部门可微调后 cfg.prompt 优先） ----

def default_prompt(options: Optional[Dict[str, bool]] = None,
                   output_format: str = "fields") -> str:
    """内置默认模板（部门未配提示词时使用）

    三种输出格式：
    - fields：固定字段（类型/文字/画面/…），检索命中率最高（默认）
    - prose：自然段，读起来自然
    - brief：一句话简介，**不读图中文字** —— 适合"整页全是文字但不需要
      理解含义"的图（如合同条款扫描件）；代价是图里的字检索不到，
      故不适合证照类
    """
    if output_format == "brief":
        return ("用一句话说明这张图片大致是什么、用来做什么的"
                "（如「某公司生产厂房外景照片」「设备接线示意图」）。\n\n"
                "要求：\n"
                "- 只描述你确实看到的，不要推测、不要评价、不要陈述图中的"
                "具体内容与文字\n"
                "- 不超过 30 字，写成一句话\n"
                "- 直接输出这句话，不要加「这张图片」之类的开场白，不要换行"
                )
    opts = options if options else DEFAULT_OPTIONS
    fields = [_OPTION_LINES[k] for k in _OPTION_ORDER
              if opts.get(k) and k in _OPTION_LINES]
    if not fields:  # 全不勾的极端配置：退回"只读文字"，至少还有检索价值
        fields = [_OPTION_LINES["read_text"]]
    if output_format == "prose":
        head = ("请查看这张图片，用中文写一段 2~4 句的客观描述，用于文档检索。\n\n"
                "要点：\n")
        return head + "\n".join(f"- {f}" for f in fields) + "\n" + _FIXED_TAIL
    head = ("请查看这张图片，按下面的字段输出中文描述，用于文档检索。\n\n"
            "每行一个字段，只输出这几行，不要加其他说明：\n")
    return head + "\n".join(fields) + "\n" + _FIXED_TAIL


def build_prompt(cfg: ImageSummaryConfig) -> str:
    """按配置生成提示词（部门自定义过则直接用，否则用默认模板）"""
    custom = (cfg.prompt or "").strip()
    if custom:
        return custom
    return default_prompt(cfg.options, cfg.output_format)


# ---- 输出校验 ----

def _normalize(raw: str, cfg: ImageSummaryConfig) -> str:
    """模型原始输出 → 干净的摘要文本（三种格式各自处理）

    - fields 模式：按白名单过滤字段行（模型多吐的说明文字丢弃）、缺失字段
      补"无"、`文字` 字段按 text_max_chars 截断
    - prose 模式：折叠成一段（去掉换行），按 text_max_chars × 4 兜底限长
    - brief 模式：同样折叠成一行，但限长固定 50 字（"一句话"的语义）
    """
    text = (raw or "").strip()
    if not text:
        return ""
    fmt = cfg.output_format or "fields"
    if fmt == "brief":
        return _truncate(" ".join(text.split()), 50)
    if fmt == "prose":
        return _truncate(" ".join(text.split()), max(200, cfg.text_max_chars * 4))

    found: Dict[str, str] = {}
    for line in text.split("\n"):
        m = _FIELD_LINE_RE.match(line)
        if not m:
            continue  # 非字段行（开场白/总结/空行）丢弃
        name, value = m.group(1).strip(), m.group(2).strip()
        if name in _FIELD_ORDER and name not in found:
            found[name] = value or "无"
    if not found:
        # 模型没按字段格式回（有可能）：退化为整段，好过丢掉
        return _truncate(" ".join(text.split()), max(200, cfg.text_max_chars * 4))
    lines = []
    for name in _FIELD_ORDER:
        if name not in found:
            continue  # 未启用的字段不补——保持与"选项"一致
        value = found[name]
        if name == "文字":
            value = _truncate(value, cfg.text_max_chars or 200)
        lines.append(f"{name}：{value}")
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _render_block(text: str, output_format: str) -> str:
    """摘要文本 → 回填用的引用块（首行带「图片说明」标记）

    brief / prose 都是一行文字，直接跟在标记后；fields 逐行加引用前缀。
    """
    if (output_format or "fields") in ("prose", "brief"):
        return f"{SUMMARY_PREFIX}{text}"
    lines = [SUMMARY_PREFIX] + [f"> {l}" for l in text.split("\n") if l.strip()]
    return "\n".join(lines)


# ---- 图片尺寸 / MIME ----

def _pixel_area(data: bytes) -> int:
    """图片像素面积（PIL 只读头部不解全图；解码失败 → 0，按"过小"跳过）

    解码失败的通常是 EMF/WMF 等矢量格式——多模态模型本来也读不了。
    """
    try:
        import io as _io

        from PIL import Image
        with Image.open(_io.BytesIO(data)) as im:
            return int(im.width) * int(im.height)
    except Exception:
        return 0


def _skip_threshold(areas: List[int]) -> int:
    """跳过阈值 = 文档内最大图面积 × _SMALL_RATIO，且不低于绝对下限"""
    biggest = max(areas) if areas else 0
    return max(int(biggest * _SMALL_RATIO), _MIN_PIXELS)


def _guess_mime(name: str) -> str:
    ext = (name.rsplit(".", 1)[-1] or "").lower()
    return {"png": "image/png", "gif": "image/gif",
            "webp": "image/webp", "bmp": "image/bmp"}.get(ext, "image/jpeg")


# ---- 单张调用 ----

async def _describe_one(client, model_cfg: VisionModelConfig, prompt: str,
                        data: bytes, name: str) -> str:
    """送一张图给多模态模型，返回规范化后的摘要文本（空串=无有效内容）"""
    payload = base64.b64encode(data).decode()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:{_guess_mime(name)};base64,{payload}"}},
        ],
    }]
    resp = await llm_completion(
        client, model=model_cfg.model, messages=messages,
        max_tokens=_MAX_TOKENS, temperature=_TEMPERATURE,
        timeout=model_cfg.timeout)
    raw = ""
    try:
        raw = resp.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError):
        raw = ""
    return raw


# ---- 对外主入口 ----

async def resolve_config(db, dept_id: Optional[str]) -> Optional[dict]:
    """解析生效的图片摘要配置（部门覆盖 → 全局）；不可用 → None

    返回 {"model": VisionModelConfig, "summary": ImageSummaryConfig}。

    模型来自**超管配的模型列表**（配置档案 vision 段的 models），部门只选
    "用哪一个"（按 name 匹配）——部门看不到也改不了连接信息与密钥。部门没选、
    或选中的名字已被超管删掉 → 回退列表第一个（与 LLM 段的 active 语义一致）。

    任一环节缺失（没有模型列表 / 模型字段不全）→ None，调用方据此跳过生成、
    或在前端预检处提示"请管理员先配置图片解析模型"。
    """
    from backend.services.department_service import get_department_config
    from backend.services.settings.service import get_settings_service

    profile = get_settings_service().get_active() or {}
    models = ((profile.get("vision") or {}).get("models")) or []
    models = [m for m in models if isinstance(m, dict)]
    if not models:
        return None

    # 部门配置覆盖全局档案的 image_summary 段（提示词/格式/上限都走这条链）
    merged = dict(profile.get("image_summary") or {})
    if dept_id:
        dept_cfg = await get_department_config(db, dept_id)
        merged.update(dept_cfg.get("image_summary") or {})

    want = (merged.get("model") or "").strip()
    entry = next((m for m in models if (m.get("name") or "") == want),
                 None) if want else None
    if entry is None:
        entry = models[0]  # 未选 / 选中的已不存在 → 第一个

    model_cfg = VisionModelConfig(
        name=entry.get("name") or "",
        base_url=entry.get("base_url") or "",
        api_key=entry.get("api_key") or "",
        model=entry.get("model") or "",
        timeout=float(entry.get("timeout") or 60),
    )
    if not model_cfg.base_url or not model_cfg.model:
        return None  # 条目残缺（地址/模型名没填）→ 视为未配置
    return {"model": model_cfg,
            "summary": ImageSummaryConfig(**{
                k: v for k, v in merged.items()
                if k in ImageSummaryConfig.model_fields})}


async def check_ready(db, dept_id: Optional[str]) -> Tuple[bool, str]:
    """解析前可用性预检（后端兜底；前端另有轻量预检）

    返回 (是否可用, 不可用原因)。原因文案面向**普通用户**——他们看到配置项
    名只会一脸懵，必须说清"找谁处理"。
    """
    cfg = await resolve_config(db, dept_id)
    if cfg is None:
        return False, "图片摘要需要管理员先配置「图片解析模型」，请联系系统管理员"
    reason = await probe_model(cfg["model"])
    if reason:
        return False, f"图片解析模型当前不可用：{reason}，请联系系统管理员"
    return True, ""


async def probe_model(model_cfg: VisionModelConfig) -> str:
    """轻量探活：GET {base_url}/models（3s 超时）

    可用 → 空串；不可用 → 面向用户的原因（不暴露内部细节，够定位即可）。
    只探活不试推——试推要传图，代价大；真正可用性由生成阶段的实际结果兜底。
    """
    import httpx

    url = model_cfg.base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                url, headers={"Authorization":
                              f"Bearer {model_cfg.api_key or 'EMPTY'}"})
    except Exception as e:
        return f"连接失败（{type(e).__name__}）"
    if resp.status_code >= 400:
        return f"服务返回 HTTP {resp.status_code}"
    return ""


async def summarize_images(
    markdown: str,
    images: List[dict],
    *,
    model_cfg: VisionModelConfig,
    summary_cfg: ImageSummaryConfig,
    client=None,
    on_progress=None,
) -> Tuple[str, dict]:
    """逐张生成摘要并回填 markdown

    - markdown / images：解析产物（与 MinerU 同构）
    - model_cfg：选定的多模态模型（连接信息，超管配）
    - summary_cfg：部门侧配置（提示词/格式/上限）
    - client：可选注入（测试用）；None 时按 model_cfg 构造并缓存
    - on_progress：可选同步回调 (已处理张数, 总张数)，每张处理后调一次——
      调用方据此把进度写进文档状态（如「图片摘要（3/8）」）

    返回 (回填后的 markdown, stats)；stats 含 total/done/skipped_small/
    skipped_limit/failed（失败图片名列表），供上层记录"哪些图没摘要成功"。

    单张串行：模型对多图并发的稳定性未知，且"一次多个送"容易对应错位——
    省下的调用次数不值这个风险。
    """
    refs = list(_IMG_REF_RE.finditer(markdown))
    stats: dict = {"total": len(refs), "done": 0, "skipped_small": 0,
                   "skipped_limit": 0, "failed": []}
    if not refs:
        return markdown, stats

    prompt = build_prompt(summary_cfg)
    output_format = summary_cfg.output_format or "fields"
    limit = max(0, int(summary_cfg.max_images or 0))
    by_name = {im.get("name"): im.get("data") for im in images}
    # 小图判定用**相对**阈值：先算全部图面积，阈值 = 最大图 × _SMALL_RATIO
    areas = {im.get("name"): _pixel_area(im["data"])
             for im in images if im.get("data")}
    threshold = _skip_threshold(list(areas.values()))
    if client is None:
        client = get_llm_client(model_cfg.model_dump(),
                                timeout=model_cfg.timeout)

    done: Dict[str, str] = {}
    used = 0
    for idx, m in enumerate(refs, start=1):
        name = m.group(1)
        if on_progress is not None:
            try:
                on_progress(idx, len(refs))
            except Exception:  # 进度回调失败不该影响摘要本身
                logger.debug("图片摘要进度回调失败", exc_info=True)
        if limit and used >= limit:
            stats["skipped_limit"] += 1
            continue
        data = by_name.get(name)
        if not data:
            stats["failed"].append(name)  # 解析产物里没有这张图（异常）
            continue
        if areas.get(name, 0) < threshold:
            stats["skipped_small"] += 1
            continue
        used += 1
        try:
            raw = await _describe_one(client, model_cfg, prompt, data, name)
        except (LLMTimeoutError, LLMRequestError) as e:
            logger.warning("图片摘要失败 %s: %s", name, e)
            stats["failed"].append(name)
            continue
        except Exception as e:  # 兜底：单图异常绝不冒泡拖垮整篇解析
            logger.warning("图片摘要异常 %s: %s", name, e)
            stats["failed"].append(name)
            continue
        text = _normalize(raw, summary_cfg)
        if text:
            done[name] = text
            stats["done"] += 1

    if not done:
        return markdown, stats

    def _sub(m: re.Match) -> str:
        text = done.get(m.group(1))
        if not text:
            return m.group(0)
        return f"{m.group(0)}\n{_render_block(text, output_format)}"

    return _IMG_REF_RE.sub(_sub, markdown), stats
