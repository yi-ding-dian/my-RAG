"""用户画像/偏好记忆服务

- 存储：data/user_memory/{user_id}.json（按用户隔离；user_id 格式白名单
  防路径穿越；文件损坏/缺字段兜底默认结构）
- 提取 extract_and_merge：对话结束后异步调用 LLM 从最近对话提取用户
  稳定事实与偏好（职位/行业/关注领域/沟通风格/格式偏好/禁忌，禁止敏感信息）；
  频率控制（每 EXTRACT_MIN_ROUNDS 轮或 EXTRACT_MIN_MINUTES 分钟一次，
  记录在文件内 last_extract_round/last_extract_at）；同内容更新（置信度
  加权）、长期未提及衰减（×0.9，<0.3 移除）；任何失败静默（warning，
  不阻塞对话）；并发防护（同用户已有任务在跑则跳过）
- 注入 build_memory_context：组装"用户画像：…偏好：…"文本段，供
  chat_service 组装 system prompt（仅聊天问答；memory_enabled 关/无条目
  返回空串跳过注入）
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from backend.config import USER_MEMORY_DIR, get_active_config
from backend.services.llm_client import (get_llm_client, llm_completion,
                                         llm_to_dict)
from backend.services.thinking_strategy import get_thinking_strategy

logger = logging.getLogger(__name__)

# ---- 提取频率控制 ----
# 同一用户两次提取间隔：≥ EXTRACT_MIN_ROUNDS 轮，或 ≥ EXTRACT_MIN_MINUTES 分钟
EXTRACT_MIN_ROUNDS = 5
EXTRACT_MIN_MINUTES = 30
# 提取时取最近消息条数（10 条 = 5 轮）
EXTRACT_MESSAGE_LIMIT = 10

# ---- 合并策略 ----
# 长期未提及的条目 confidence 衰减系数（每次提取未提及 ×0.9）
CONFIDENCE_DECAY = 0.9
# 衰减后低于该阈值移除
CONFIDENCE_REMOVE_THRESHOLD = 0.3
# 同内容条目合并：新置信度 = 旧×(1-w) + 新×w（加权）
CONFIDENCE_MERGE_WEIGHT_NEW = 0.3
# 手动添加/编辑条目的默认置信度
DEFAULT_CONFIDENCE = 0.8

_ITEM_TYPES = ("profile", "preference")

# user_id 允许字符（防路径穿越：只允许字母数字 _ -）
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 提取进行中的 user_id 集合（并发防护：已有提取在跑则跳过本次）
_extract_running: set = set()

_EXTRACT_SYSTEM_PROMPT = (
    "你是用户画像分析师。请从下方的对话中提取关于「用户」（提问方）的稳定事实与偏好，"
    "包括但不限于：职位/职业、行业、关注领域、沟通风格、格式偏好、禁忌等。\n"
    "规则：\n"
    "1. 只提取稳定、长期有效的信息，忽略临时性内容（如一次性问题、随口提及）；\n"
    "2. 禁止提取敏感信息：密码、账号、身份证号、手机号、银行卡、住址等一律不提取；\n"
    "3. 每条内容使用简洁的中文陈述句（不超过 50 字）；\n"
    "4. 输出严格为 JSON 数组，不要输出其他文字，格式：\n"
    '[{"type": "profile"|"preference", "content": "…", "confidence": 0-1}]\n'
    "其中 type=profile 表示身份/背景类事实，type=preference 表示偏好类信息；\n"
    "confidence 表示你对该条信息的把握程度（0~1，没有把握的不要输出）。"
)

_EXTRACT_USER_TEMPLATE = "对话如下：\n{conversation}"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _gen_id() -> str:
    return uuid.uuid4().hex[:12]


def _empty_memory(user_id: str) -> dict:
    """默认画像结构（无提取记录）"""
    return {
        "user_id": user_id,
        "memory_enabled": False,
        "updated_at": "",
        "last_extract_round": 0,
        "last_extract_at": "",
        "items": [],
    }


class UserMemoryService:

    # ---------- 存储层 ----------

    def _path(self, user_id: str) -> Path:
        if not _USER_ID_RE.match(user_id or ""):
            raise ValueError("非法的用户 ID")
        return USER_MEMORY_DIR / f"{user_id}.json"

    def _load(self, user_id: str) -> dict:
        """读取画像文件（不存在 → 默认结构；损坏/非 dict → 兜底默认并 warning）"""
        path = self._path(user_id)
        if not path.exists():
            return _empty_memory(user_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("读取用户画像 %s 失败（重置为空）: %s", user_id, e)
            return _empty_memory(user_id)
        if not isinstance(data, dict):
            logger.warning("用户画像 %s 结构异常（重置为空）", user_id)
            return _empty_memory(user_id)
        default = _empty_memory(user_id)
        for key in default:
            data.setdefault(key, default[key])
        return data

    def _save(self, data: dict) -> None:
        USER_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = _now_str()
        self._path(data["user_id"]).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- CRUD（接口与设置页） ----------

    def get_memory(self, user_id: str) -> dict:
        """读取完整画像（含 items/last_extract_*），不存在返回默认结构"""
        return self._load(user_id)

    def update_memory(self, user_id: str, enabled: Optional[bool] = None,
                      items: Optional[list] = None) -> dict:
        """更新开关 enabled 与/或条目列表（items 非 None 全量替换：
        带 id 的匹配更新既有条目，无 id 新增；非法条目跳过）"""
        data = self._load(user_id)
        if enabled is not None:
            data["memory_enabled"] = bool(enabled)
        if items is not None:
            data["items"] = self._normalize_items(items, data["items"])
        self._save(data)
        return data

    def delete_item(self, user_id: str, item_id: str) -> bool:
        """删除单条画像（不存在返回 False）"""
        data = self._load(user_id)
        before = len(data["items"])
        data["items"] = [i for i in data["items"] if i.get("id") != item_id]
        if len(data["items"]) == before:
            return False
        self._save(data)
        return True

    def clear(self, user_id: str) -> None:
        """清空全部条目（保留开关与元数据）"""
        data = self._load(user_id)
        data["items"] = []
        self._save(data)

    def delete_file(self, user_id: str) -> None:
        """删除用户画像文件（用户删除时连带清理；不存在/删除失败静默）"""
        try:
            path = self._path(user_id)
            if path.exists():
                path.unlink()
        except Exception as e:
            logger.warning("删除用户画像文件失败 %s: %s", user_id, str(e)[:150])

    @staticmethod
    def _normalize_items(items: list, old_items: list) -> list:
        """条目列表归一化：带 id 匹配更新（created_at/confidence 保留），
        无 id 新增（生成 id、默认置信度）；空内容/非法条目跳过"""
        now = _now_str()
        old_by_id = {i.get("id"): i for i in old_items if i.get("id")}
        result = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            itype = raw.get("type") if raw.get("type") in _ITEM_TYPES else "profile"
            item_id = raw.get("id")
            if item_id and item_id in old_by_id:
                old = old_by_id[item_id]
                old["type"] = itype
                old["content"] = content
                old["updated_at"] = now
                result.append(old)
            else:
                try:
                    confidence = (float(raw.get("confidence"))
                                  if raw.get("confidence") is not None
                                  else DEFAULT_CONFIDENCE)
                except (TypeError, ValueError):
                    confidence = DEFAULT_CONFIDENCE
                result.append({
                    "id": _gen_id(),
                    "type": itype,
                    "content": content,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "created_at": now,
                    "updated_at": now,
                })
        return result

    # ---------- 注入组装（仅聊天问答） ----------

    def build_memory_context(self, user_id: str) -> str:
        """组装用户画像注入段（memory_enabled 关 / 无条目 → 空串跳过注入）

        返回文本形如：
        【用户画像（个性化参考信息，回答时可结合用户背景与偏好）】
        用户画像：从事电力行业，SCA 系统调试；……
        偏好：回答使用简洁中文；……
        """
        data = self._load(user_id)
        if not data.get("memory_enabled", False):
            return ""
        items = [i for i in data.get("items", []) if i.get("content")]
        if not items:
            return ""
        profiles = [i["content"] for i in items if i.get("type") == "profile"]
        prefs = [i["content"] for i in items if i.get("type") == "preference"]
        lines = ["【用户画像（个性化参考信息，回答时可结合用户背景与偏好）】"]
        if profiles:
            lines.append("用户画像：" + "；".join(profiles))
        if prefs:
            lines.append("偏好：" + "；".join(prefs))
        return "\n".join(lines)

    # ---------- 频率控制 ----------

    def should_extract(self, user_id: str, current_round: int) -> bool:
        """是否允许提取：距上次 ≥N 轮，或距上次 ≥M 分钟，或无记录

        current_round：本次对话后的轮次数（消息对数）。
        """
        data = self._load(user_id)
        last_round = int(data.get("last_extract_round") or 0)
        if current_round - last_round >= EXTRACT_MIN_ROUNDS:
            return True
        last_at = data.get("last_extract_at") or ""
        if last_at:
            try:
                last_dt = datetime.strptime(last_at, "%Y-%m-%d %H:%M:%S")
                if datetime.now() - last_dt >= timedelta(minutes=EXTRACT_MIN_MINUTES):
                    return True
            except ValueError:
                return True  # 时间字段损坏 → 视为无记录，允许提取
        return last_round == 0  # 从未提取过 → 首次对话即提取

    # ---------- 提取 ----------

    async def extract_and_merge(self, user_id: str, messages: list,
                                current_round: Optional[int] = None) -> bool:
        """对话结束后异步提取并合并用户画像（任何失败静默，不抛异常）

        - messages：对话消息列表（ChatMessage / dict，取最近 N 条）
        - current_round：轮次数（缺省按消息数估算）；用于频率控制
        - 成功（LLM 返回合法 JSON，可为空数组）→ 合并并更新提取记录，True
        - 被频率控制/并发防护拦截或 LLM 失败 → False（不更新提取记录）
        """
        lines = self._normalize_messages(messages)
        if not lines:
            return False
        if current_round is None:
            current_round = max(1, len(messages) // 2)
        if current_round <= 0:
            current_round = 1
        if user_id in _extract_running:
            logger.info("用户画像提取跳过（已有任务进行中）: %s", user_id)
            return False
        if not self.should_extract(user_id, current_round):
            return False
        _extract_running.add(user_id)
        try:
            extracted = await self._call_extract_llm(lines)
            self._merge_extracted(user_id, extracted, current_round)
            return True
        except Exception as e:  # noqa: BLE001 —— 提取属旁路任务，任何失败静默
            logger.warning("用户画像提取失败（已忽略）: %s err=%s",
                           user_id, str(e)[:200])
            return False
        finally:
            _extract_running.discard(user_id)

    @staticmethod
    def _normalize_messages(messages: list) -> List[str]:
        """对话消息 → 最近 EXTRACT_MESSAGE_LIMIT 条文本（"用户/助手: 内容"）"""
        lines = []
        for m in messages[-EXTRACT_MESSAGE_LIMIT:]:
            if isinstance(m, dict):
                role, content = m.get("role", ""), m.get("content", "")
            else:
                role = getattr(m, "role", "")
                content = getattr(m, "content", "")
            role = "用户" if role == "user" else "助手"
            if content:
                lines.append(f"{role}: {content}")
        return lines

    async def _call_extract_llm(self, lines: List[str]) -> List[dict]:
        """调用 LLM 提取画像条目（统一工厂 + 思考关闭策略 + 超时包装）

        成功返回条目列表（可能为空数组）；LLM 失败/输出非 JSON → 抛异常，
        由 extract_and_merge 静默兜底（不更新提取记录，下次可重试）。
        """
        cfg = get_active_config()
        llm_cfg = llm_to_dict(cfg.llm)
        client = get_llm_client(llm_cfg)
        payload: dict = {
            "messages": [
                {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
                {"role": "user",
                 "content": _EXTRACT_USER_TEMPLATE.format(
                     conversation="\n".join(lines))},
            ],
        }
        # 提取属简单延迟敏感任务：关闭思考（在线 extra_body / 本地 prefill
        # 由策略统一处理，调用点无需关心服务商）
        get_thinking_strategy(llm_cfg, "disabled").apply(payload)
        resp = await llm_completion(
            client,
            model=cfg.llm.model,
            messages=payload["messages"],
            max_tokens=1024,
            temperature=0.2,
            extra_body=payload.get("extra_body"),
            timeout=60.0,
        )
        text = ""
        if resp and resp.choices and resp.choices[0].message:
            text = resp.choices[0].message.content or ""
        return self._parse_extract_json(text)

    @staticmethod
    def _parse_extract_json(text: str) -> List[dict]:
        """解析 LLM 输出 JSON 数组（容忍 ```json 围栏与前后杂文）

        非 JSON 数组 → 抛 ValueError（调用方静默，下次重试）。
        """
        if not text:
            raise ValueError("LLM 输出为空")
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            raise ValueError("LLM 输出非 JSON 数组")
        data = json.loads(text[start:end + 1])
        if not isinstance(data, list):
            raise ValueError("LLM 输出非 JSON 数组")
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            itype = item.get("type") if item.get("type") in _ITEM_TYPES else "profile"
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            try:
                confidence = float(item.get("confidence") or 0.5)
            except (TypeError, ValueError):
                confidence = 0.5
            result.append({
                "type": itype,
                "content": content,
                "confidence": max(0.0, min(1.0, confidence)),
            })
        return result

    def _merge_extracted(self, user_id: str, extracted: List[dict],
                         current_round: int) -> None:
        """合并提取结果：同内容更新（置信度加权）、未提及衰减（×0.9）、
        <0.3 移除；更新 last_extract_round/last_extract_at"""
        data = self._load(user_id)
        now = _now_str()
        existing = data.get("items", [])
        # 已有条目按归一化内容索引（同内容条目以首个为准，防御脏数据）
        existing_by_norm: dict = {}
        for old in existing:
            existing_by_norm.setdefault(
                "".join(old.get("content", "").split()), old)
        merged = []
        for e in extracted:
            key = "".join(e["content"].split())
            old = existing_by_norm.pop(key, None)
            if old is not None:
                # 同内容条目：更新（置信度加权：旧×(1-w) + 新×w）
                old["confidence"] = round(
                    min(1.0, old["confidence"]
                        * (1 - CONFIDENCE_MERGE_WEIGHT_NEW)
                        + e["confidence"] * CONFIDENCE_MERGE_WEIGHT_NEW), 3)
                old["content"] = e["content"]
                old["type"] = e["type"]
                old["updated_at"] = now
                merged.append(old)
            else:
                merged.append({
                    "id": _gen_id(),
                    "type": e["type"],
                    "content": e["content"],
                    "confidence": e["confidence"],
                    "created_at": now,
                    "updated_at": now,
                })
        # 本轮未提及的旧条目：置信度衰减（随时间/对话轮次淡出）
        for old in existing_by_norm.values():
            old["confidence"] = round(
                float(old.get("confidence") or 0.5) * CONFIDENCE_DECAY, 3)
            merged.append(old)
        data["items"] = [i for i in merged
                         if i.get("confidence", 1.0) >= CONFIDENCE_REMOVE_THRESHOLD]
        data["last_extract_round"] = int(current_round)
        data["last_extract_at"] = now
        self._save(data)
        logger.info("用户画像提取完成: %s 共 %d 条（本次提取 %d）",
                    user_id, len(data["items"]), len(extracted))


_user_memory_service: Optional[UserMemoryService] = None


def get_user_memory_service() -> UserMemoryService:
    global _user_memory_service
    if _user_memory_service is None:
        _user_memory_service = UserMemoryService()
    return _user_memory_service
