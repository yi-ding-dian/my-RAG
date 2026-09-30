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

from backend.services.llm_client import get_llm_client, llm_completion

logger = logging.getLogger(__name__)

# 视觉模型不可用时的**统一文案**：探活（/api/chat/vision-status）与发送时
# 兜底（vision_error 事件）共用同一句，避免两个入口说法不一致让用户困惑。
# 前端 ChatInput.tsx 有一份同文案副本（注释标注以此为准）。
VISION_UNAVAILABLE_MSG = "视觉模型当前无法使用，无法识图"

# 读图提示词：**不复用 image_summary 那份**——它是给"入库摘要"设计的三段式
# 结构化输出（类型/文字/画面），回填进 chunk 文本参与检索；聊天识图的这段
# 描述要同时喂给检索与对话回答，需要自然语言叙述 + 高密度关键信息。
#
# 这是**内置默认**：超管可在「设置 → 聊天设置 → 聊天识图 → 读图提示词」覆盖
# （配置档案 chat.image_prompt），部门管理员可在「部门配置 → 对话增强」再覆盖
# 一层（schema 白名单）。配置为空时回退到本常量。
#
# 两个占位符，用 str.replace 替换（不用 str.format——用户提示词里的 JSON 示例
# 花括号会因未知占位符抛 KeyError，整轮识图挂掉）：
# - `{max_chars}`：运行时替换为 image_desc_max_chars；用户自定义提示词若不写它，
#   长度约束就只剩 describe_images 的硬截断兜底（不报错，但模型更容易超发）
# - `{question}`：用户就这张图提的问题。**写了才会带问题读图**；不写 = 盲读
#   （老档案行为逐字节不变）。用户只发图不打字时替换为 _NO_QUESTION 占位
#
# 第 5 条（排除无关元素）是实测加出来的：用户从聊天软件发来的截图大多带聊天
# 界面的装饰（头像、昵称、时间戳），不加这条时模型会把它们当描述对象——实测
# 一张带公章的证明文档，输出里近一半篇幅是「右上角卡通头像」「13:35」，正文
# 反倒只抄了个零头；加上之后同类图片一句废话都没有，关键信息（标题/单位名/
# 日期/印章）全部保住。注意：单纯调整条目顺序没用（试过把"标记优先"提到第一
# 句，模型会满图找标记，反倒不抄文字了），得有明确"抄什么、不抄什么"的指引。
_CHAT_IMAGE_PROMPT = (
    "请描述这张图片，供知识库检索与问答使用。用户就这张图提的问题是："
    "「{question}」。\n"
    "要求：\n"
    "1. **逐字抄录图中所有文字**：标题、单位名、编号、字段名、按钮名、型号、"
    "参数、报错信息、日期。这些是拿去知识库检索的关键词，必须一字不差，"
    "不得概括或改写。每项只列一次，不要重复；\n"
    "2. 图中有**箭头、红框、圈注、高亮**等标记时，明确说明它指向哪个元素"
    "（报出该元素的准确名称）——用户的问题通常就针对这个元素；\n"
    "3. 图中有**印章、签字、表格、图表**时，说明其内容与数量；若是界面或"
    "报错截图，说明是什么系统、什么页面、什么操作、什么提示；\n"
    "4. **只描述真实看到的内容**：不推测、不补充常识、不回答图片之外的问题。"
    "文字模糊看不清时，明确说明看不清，**绝对不要猜测或编造**任何编号与数字；\n"
    "5. **不要描述头像、昵称、时间戳、聊天气泡、背景装饰**等与内容无关的"
    "元素——它们不是检索线索，只会挤占篇幅；\n"
    "6. 简洁中文，不超过 {max_chars} 字。"
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


def _image_mime(name: str) -> str:
    """按扩展名猜 MIME（未知回退 image/jpeg，与 image_summary 同口径）"""
    ext = (name.rsplit(".", 1)[-1] or "").lower()
    return {"png": "image/png", "gif": "image/gif",
            "webp": "image/webp", "bmp": "image/bmp"}.get(ext, "image/jpeg")


def _build_messages(*, data_url: str, prompt: str, max_chars: int,
                    question: str = "") -> list:
    """★ 组装喂给视觉模型的消息 —— **改读图行为只动这一个函数**

    所有"给视觉模型看什么"的决策都收在这里：提示词取哪份、占位符怎么换、
    消息长什么样。想加对话历史、加图片类型分流（截图/照片走不同模板）、
    改多图对比方式，都从这儿下手，不必翻调用链。

    - data_url：`data:image/png;base64,...` 形式的图片地址
    - prompt：用户配置的提示词（部门覆盖 → 全局 → 空串）；空 = 用内置默认
    - question：用户就这张图提的问题；空 = 只发图不打字（用 _NO_QUESTION 占位）
    """
    template = (prompt or "").strip() or _CHAT_IMAGE_PROMPT
    text = (template
            .replace("{max_chars}", str(max_chars))
            .replace("{question}", (question or "").strip() or _NO_QUESTION))
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]


async def _describe_chat_image(client, model_cfg, key: str, max_chars: int,
                               storage, prompt: str = "",
                               question: str = "") -> str:
    """读一张聊天图片 → 描述文本（异常原样抛出，由 describe_images 归一）

    图从对象存储按 key 取，**不信任前端传的 base64**——那等于把大小/格式
    校验权全交给客户端。key 是 upload-image 阶段校验后落下的。
    """
    data = await storage.read_bytes(key)
    payload = base64.b64encode(data).decode()
    messages = _build_messages(
        data_url=f"data:{_image_mime(key)};base64,{payload}",
        prompt=prompt, max_chars=max_chars, question=question)
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
                          question: str = "") -> Tuple[str, str]:
    """并发读多张图 → 合并描述。返回 (描述文本, 失败原因)

    本模块对外的**唯一入口**（chat_service 只 import 它和文案常量）。

    prompt：读图提示词（调用方传合并后的最终值：部门覆盖 → 全局 → 空串）；
    空 = 用内置默认 _CHAT_IMAGE_PROMPT。见 config.ChatConfig.image_prompt。
    question：用户就这批图提的问题（同一句配所有图）。传了才会带问题读图，
    见 _build_messages 与模块注释里的实测对比。
    """
    client = get_llm_client(
        model_cfg.model_dump(),
        timeout=min(float(model_cfg.timeout or 60), _IMAGE_DESC_TIMEOUT))
    results = await asyncio.gather(
        *[_describe_chat_image(client, model_cfg, k, max_chars, storage, prompt,
                               question)
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
