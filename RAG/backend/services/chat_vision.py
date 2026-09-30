"""聊天识图：发图 → 视觉模型读图 → 描述文本（检索与对话两处用）

**改读图行为只需要动这一个文件。** 提示词、占位符、消息组装、调用参数、
并发与错误归一全在这里；`chat_service` 只 import 两个符号（对外文案常量 +
`describe_images`）。想加对话历史、加图片类型分流、换采样参数，先看
`_build_messages()` —— 那是「给视觉模型看什么」的唯一出口。

设计要点（背景与取舍）：

- **一次生成、两处用**：同一段描述既并入当轮检索词（让"这张报错截图"能
  命中对应文档），又注入 messages（让主模型知道图里有什么）。拆成两份
  描述要多一次 VLM 调用，而读图是整条链路上最慢的一环，用户发完图干等
  的正是它——先按一份做，实测检索不准再拆（见 config.image_desc_max_chars）。
- **带问题读图**（`{question}` 占位符）：盲读会把整页界面当查询词，用户
  问的却是箭头指的那一个字段，模型根本不知道往哪儿看。实测不传问题时，
  它可能转去描述「卡通头像」「11:55」这类无关元素；传了问题并明确要求
  "箭头指向的元素要报出准确名称"，才认得出红色箭头所指的输入框。
- **`frequency_penalty`**：小模型读长表单会陷入复读（实测「备注/测试备注」
  重复 58 遍以上、输出 2047 字），提示词里写「严禁重复」治不住；加上惩罚
  参数后降到 520 字且不再复读。副作用是好的——模型会诚实承认"文字模糊
  看不清"，而不是编造一串字段名。
- **提示词里的长度约束是软约束**：模型经常不守（实测配置 800 字、真机输出
  1498 字），所以 `describe_images` 末尾还有一道硬截断兜底。

失败语义（与"图片摘要是增强功能、不该有否决权"不同——这里图是用户**主动
发的**，必须给明确交代）：
- 全部失败 → 返回非空失败原因，调用方下发 vision_error 且**不发消息**
- 部分失败 → 描述里标注哪几张没读出来，其余照常走（不为一张图废掉整轮）
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import List, Tuple

from backend.services.image_templates import resolve_template_body
from backend.services.llm_client import get_llm_client, llm_completion

logger = logging.getLogger(__name__)

# 视觉模型不可用时的**统一文案**：探活（/api/chat/vision-status）与发送时
# 兜底（vision_error 事件）共用同一句，避免两个入口说法不一致让用户困惑。
# 前端 ChatInput.tsx 有一份同文案副本（注释标注以此为准）。
VISION_UNAVAILABLE_MSG = "视觉模型当前无法使用，无法识图"

# 读图的「看图策略」由**模板**提供（`image_templates`，入库摘要与聊天共用同一套
# ——两边关注同样的东西、排除同样的东西，生成的描述才同构、向量才靠得近）。
# 本模块只负责在模板之后追加**聊天侧独有**的那段：用户问题 + 自然语言输出要求。
#
# 两个占位符，用 str.replace 替换（不用 str.format——模板正文里的花括号会因
# 未知占位符抛 KeyError）：
# - `{max_chars}`：运行时替换为 image_desc_max_chars
# - `{question}`：用户就这张图提的问题；只发图不打字时替换为 _NO_QUESTION 占位
_CHAT_TAIL = (
    "\n\n用户就这张图提的问题是：「{question}」。\n"
    "请用自然语言描述这张图片，供知识库检索与问答使用，"
    "不超过 {max_chars} 字。直接输出描述内容本身，不要开场白、不要总结。"
)

# 只发图不打字时的占位：不写这句，`{question}` 位置会留一对空引号，
# 模型可能理解成"用户问了个空问题"而输出莫名其妙的东西
_NO_QUESTION = "（用户未提问，请完整描述这张图片）"

# 读图超时（秒）：比 LLM 对话超时（默认 120s）短——用户发完图正在干等，
# 读图不该独占整个超时窗口；到点即判失败，前端提示"无法识图"让用户重试
_IMAGE_DESC_TIMEOUT = 60.0

# 采样参数：抑制复读（见模块注释的实测数据）。
# 走 extra_body 透传——llm_completion 签名里没有这个参数，而 extra_body 会
# 原样并入请求 JSON。frequency_penalty 是 OpenAI 标准参数，兼容服务即使不
# 支持也只会忽略，不会报错（这是它比 vLLM 私有的 repetition_penalty 更稳
# 的地方，所以选它）。
_VISION_EXTRA_BODY = {"frequency_penalty": 0.6}


def render_default_prompt(template_body: str = "") -> str:
    """当前默认会发给视觉模型的提示词（**占位符原样保留**）

    供前端 placeholder 展示"不填自定义的话，实际发的是什么"。走这条路而不是
    在前端硬编码一份副本：超管换了默认模板，placeholder 跟着变，不会像静态
    副本那样悄悄过期（过期后用户照着它改，改出来的是上一版的行为）。
    """
    body = (template_body or "").strip() or resolve_template_body("")
    return body + _CHAT_TAIL


def _image_mime(name: str) -> str:
    """按扩展名猜 MIME（未知回退 image/jpeg，与 image_summary 同口径）"""
    ext = (name.rsplit(".", 1)[-1] or "").lower()
    return {"png": "image/png", "gif": "image/gif",
            "webp": "image/webp", "bmp": "image/bmp"}.get(ext, "image/jpeg")


def _build_messages(*, data_url: str, prompt: str, max_chars: int,
                    question: str = "", template: str = "") -> list:
    """★ 组装喂给视觉模型的消息 —— **改读图行为只动这一个函数**

    所有"给视觉模型看什么"的决策都收在这里：用哪份提示词、占位符怎么换、
    消息长什么样。想加对话历史、加图片类型分流、改多图对比方式，都从这儿下手。

    - data_url：`data:image/png;base64,...` 形式的图片地址
    - prompt：**自定义**读图提示词（手写的那份，可经配置档案下发）。非空时
      **优先**，用户自己写全——占位符用不用由他决定，这是「选模板」之外的出口
    - template：**选中模板的正文**（`image_templates`，入库摘要与聊天共用）。
      prompt 为空时用它，再追加聊天侧独有的那段（用户问题 + 自然语言输出要求）
    - question：用户就这张图提的问题；空 = 只发图不打字（用 _NO_QUESTION 占位）
    """
    q = (question or "").strip() or _NO_QUESTION
    custom = (prompt or "").strip()
    if custom:
        text = (custom.replace("{max_chars}", str(max_chars))
                      .replace("{question}", q))
    else:
        body = (template or "").strip() or resolve_template_body("")
        tail = (_CHAT_TAIL.replace("{max_chars}", str(max_chars))
                         .replace("{question}", q))
        text = body + tail
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]


async def _describe_chat_image(client, model_cfg, key: str, max_chars: int,
                               storage, prompt: str = "",
                               question: str = "",
                               template: str = "") -> str:
    """读一张聊天图片 → 描述文本（异常原样抛出，由 describe_images 归一）

    图从对象存储按 key 取，**不信任前端传的 base64**——那等于把大小/格式
    校验权全交给客户端。key 是 upload-image 阶段校验后落下的。
    """
    data = await storage.read_bytes(key)
    payload = base64.b64encode(data).decode()
    messages = _build_messages(
        data_url=f"data:{_image_mime(key)};base64,{payload}",
        prompt=prompt, max_chars=max_chars, question=question,
        template=template)
    timeout = min(float(model_cfg.timeout or 60), _IMAGE_DESC_TIMEOUT)
    resp = await llm_completion(
        client, model=model_cfg.model, messages=messages,
        # 中文按 1 字 ≈ 1.6 token 留余量；超长由提示词的 max_chars 约束
        max_tokens=max(512, int(max_chars * 1.6)),
        temperature=0.1,  # 读图要稳定可复现，不吃创造性
        extra_body=dict(_VISION_EXTRA_BODY),
        timeout=timeout)
    try:
        return (resp.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError):
        return ""


async def describe_images(images: List[str], model_cfg, max_chars: int,
                          storage, prompt: str = "",
                          question: str = "",
                          template: str = "") -> Tuple[str, str]:
    """并发读多张图 → 合并描述。返回 (描述文本, 失败原因)

    本模块对外的**唯一入口**（chat_service 只 import 它和文案常量）。

    prompt：**自定义**读图提示词（部门覆盖 → 全局 → 空串）；非空时优先于
    模板。见 config.ChatConfig.image_prompt。
    template：**选中模板的正文**（见 config.ChatConfig.image_template 与
    services/image_templates）；prompt 为空时用它 + 聊天侧追加段。
    question：用户就这批图提的问题（同一句配所有图）。传了才会带问题读图，
    见 _build_messages 与模块注释里的实测对比。
    """
    client = get_llm_client(
        model_cfg.model_dump(),
        timeout=min(float(model_cfg.timeout or 60), _IMAGE_DESC_TIMEOUT))
    results = await asyncio.gather(
        *[_describe_chat_image(client, model_cfg, k, max_chars, storage, prompt,
                               question, template)
          for k in images],
        return_exceptions=True)

    parts: List[str] = []
    ok = 0
    last_err = ""
    for i, r in enumerate(results, 1):
        if isinstance(r, BaseException) or not r:
            if isinstance(r, BaseException):
                last_err = str(r)[:200]
            parts.append(f"（第 {i} 张图片未能识别）")
        else:
            ok += 1
            # **硬截断**到 max_chars：提示词里那句"不超过 N 字"只是软约束，
            # 实测模型会超（配置 800 字，真机输出 1498 字）——而这段描述是要
            # 并进检索词的，超长会把原问题淹没（检索模型对超长 query 效果下降）
            text = r if len(r) <= max_chars else r[:max_chars] + "…"
            parts.append(f"【图片{i}】{text}" if len(images) > 1 else text)
    if ok == 0:
        return "", (last_err or "视觉模型未返回内容")
    return "\n".join(parts), ""
