"""会话持久化：data/chat/*.json 的读写、归档裁剪、列表与删除

从 `chat/service.py` 拆出——会话存储与流式问答是两件事，混在一个类里让
"问答逻辑在哪"很难找。`_ARCHIVE_CONTEXT_ROUNDS` / `_trim_messages`（归档裁剪）
也放在此文件：经 grep 确认它们**只被** `delete_session` 使用。

以 **Mixin** 形式提供（方法原样挂在 ChatService 上，调用点零改动）。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from backend.config import CHAT_DELETED_DIR, CHAT_DIR
from backend.models.rag_models import ChatHistoryItem, ChatSession

logger = logging.getLogger(__name__)


_ARCHIVE_CONTEXT_ROUNDS = 2


def _trim_messages(messages: list, keep_idxs: set) -> tuple[list, bool]:
    """裁剪会话消息：每个被反馈的回答连同其前 N 轮上下文，其余丢弃

    返回 (裁剪后的消息, 是否真的裁掉了消息)。每条消息整条保留（含 sources
    引用快照 / prompt 提示词 / 耗时字段），所以回溯详情数据不受裁剪影响。

    起点按"从被反馈下标往前数 N+1 个用户消息"定位，而不是按固定条数减下标
    ——消息数组并非严格一问一答交替（中断生成、空回答、无命中提示都会打破
    配对），硬减下标会错位到别的轮次。多个反馈各取一段后取并集。
    """
    keep: set = set()
    need = _ARCHIVE_CONTEXT_ROUNDS + 1
    for idx in keep_idxs:
        if not isinstance(idx, int) or not (0 <= idx < len(messages)):
            continue
        start = 0
        found = 0
        for i in range(idx, -1, -1):
            if messages[i].get("role") == "user":
                found += 1
                if found >= need:
                    start = i
                    break
        keep.update(range(start, idx + 1))
    if not keep:
        # 反馈下标全部越界/非法（脏数据）→ 不裁剪，保留完整会话而非清空
        return messages, False
    return ([m for i, m in enumerate(messages) if i in keep],
            len(keep) < len(messages))




class _SessionStoreMixin:
    """会话持久化（Mixin：挂在 ChatService 上）"""

    # ---------- 会话持久化 ----------

    def _get_session_path(self, session_id: str) -> Path:
        return CHAT_DIR / f"{session_id}.json"

    def _get_archived_path(self, session_id: str) -> Path:
        """已删除会话的归档路径（软删除目录，用户侧不可见）"""
        return CHAT_DELETED_DIR / f"{session_id}.json"

    def _save_session(self, session: ChatSession):
        session.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._get_session_path(session.id).write_text(
                json.dumps(session.model_dump(mode="json"),
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _load_or_create(self, session_id: Optional[str], kb_ids: List[str],
                        message: str, user_id: Optional[str] = None) -> ChatSession:
        """加载已有会话（知识库集合不一致则新建）或创建新会话（注入 user_id）

        知识库比较用**集合**：同一组库换个选择顺序仍是同一个会话，不该新开。
        """
        if session_id:
            session = self.get_session(session_id)
            if session and set(session.kb_ids) == set(kb_ids):
                return session
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return ChatSession(
            id=uuid.uuid4().hex[:12],
            kb_ids=list(kb_ids),
            user_id=user_id,
            title=message.strip()[:20] or "新会话",
            messages=[],
            created_at=now,
            updated_at=now,
        )

    def list_sessions(self, user_id: Optional[object] = None,
                      kb_id: Optional[str] = None) -> List[ChatHistoryItem]:
        """会话历史列表（updated_at 倒序）

        - user_id: None 或 "all" → 全部（super_admin）；str → 仅本人；
          集合（list/set/tuple）→ 用户集合内（dept_admin 统计本部门用）；
          空集合也按过滤处理（部门无成员 → 返回空列表，防全系统会话泄露）
        - kb_id 可选过滤（现有实现忽略该参数，本次补上）
        - 旧会话 JSON 无 user_id → 视为归属 super_admin（普通用户不可见）
        """
        items: List[ChatHistoryItem] = []
        if not CHAT_DIR.exists():
            return items
        for f in CHAT_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                s_user_id = data.get("user_id")
                # 显式判 None：空集合（set()）也必须走过滤分支，
                # 否则 falsy 落入"不过滤全部"，部门无用户时会泄露全系统会话
                if user_id is not None and user_id != "all":
                    if isinstance(user_id, (list, set, tuple)):
                        if s_user_id not in user_id:
                            continue
                    elif s_user_id != user_id:
                        continue
                # 会话关联的库（旧文件只有 kb_id，读时按单元素列表兼容）
                s_kb_ids = (data.get("kb_ids")
                            or ([data["kb_id"]] if data.get("kb_id") else []))
                if kb_id and kb_id not in s_kb_ids:
                    continue
                items.append(ChatHistoryItem(
                    id=data["id"],
                    kb_ids=s_kb_ids,
                    user_id=s_user_id,
                    title=data.get("title", ""),
                    message_count=len(data.get("messages", [])),
                    created_at=data.get("created_at", ""),
                    updated_at=data.get("updated_at", ""),
                ))
            except Exception as e:
                logger.warning("加载会话 %s 失败: %s", f.name, e)
        return sorted(items, key=lambda x: x.updated_at, reverse=True)

    def get_session(self, session_id: str,
                    include_deleted: bool = False) -> Optional[ChatSession]:
        """读取会话

        include_deleted=True 时活会话缺失后回退读归档目录——仅供超管回溯
        反馈现场（路由层限制 super_admin）；默认 False 保证"删除"对普通
        用户与 owner 是真删除（读不到 → 路由层 404 伪装）。
        优先级"活的优先、归档兜底"：同 id 两边都有时读活的（理论上不会，
        session_id 为随机 uuid，且删除即移走不存在回写）。
        """
        path = self._get_session_path(session_id)
        if not path.exists():
            if not include_deleted:
                return None
            path = self._get_archived_path(session_id)
            if not path.exists():
                return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ChatSession(**data)
        except Exception as e:
            logger.warning("读取会话 %s 失败: %s", session_id, e)
            return None

    def delete_session(self, session_id: str,
                       keep_msg_idxs: Optional[set] = None) -> bool:
        """删除会话（对用户始终是真的删除；归档只服务超管回溯反馈现场）

        按调用方查得的反馈关联（keep_msg_idxs，见 feedback_service.
        get_feedback_msg_idxs）决定删除形态：

        - None：无法确定反馈关联（DB 查询失败）→ 归档**完整**会话。宁可多
          留，不能丢证据——当成"无反馈"直接删会毁掉回溯现场
        - 空集：确认无任何反馈 → **物理删除**。绝大多数会话没人反馈过，
          这是省空间的主要来源，归档目录只装被反馈过的会话
        - 非空：裁剪归档——每个被反馈的回答往前保留 _ARCHIVE_CONTEXT_ROUNDS
          轮上下文，其余消息丢弃（同会话多反馈各取一段后并集）

        写归档成功后才删原文件：写失败时原会话原样不动，用户可重试删除，
        不会出现"活目录归档目录两边都没有"的丢失。
        """
        path = self._get_session_path(session_id)
        if not path.exists():
            return False
        if keep_msg_idxs is not None and not keep_msg_idxs:
            try:
                path.unlink()
            except OSError as e:
                logger.warning("删除会话 %s 失败: %s", session_id, e)
                return False
            logger.info("会话已删除（无反馈关联，不归档）: %s", session_id)
            return True

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("读取会话 %s 失败: %s", session_id, e)
            return False
        messages = data.get("messages") or []
        raw_count = len(messages)
        trimmed = False
        if keep_msg_idxs:
            messages, trimmed = _trim_messages(messages, keep_msg_idxs)
        data["messages"] = messages
        # 裁剪标记：超管回溯时提示"这是裁剪版，非完整现场"，避免把 3 轮误当全程
        data["trimmed"] = trimmed
        try:
            dest = self._get_archived_path(session_id)
            dest.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            # mtime 刷为归档时刻（move 会保留原 mtime，与实际归档时间不符）
            dest.touch()
            path.unlink()
        except OSError as e:
            logger.warning("归档会话 %s 失败: %s", session_id, e)
            return False
        logger.info("会话已归档: %s（裁剪=%s，保留 %d/%d 条消息）",
                    session_id, trimmed, len(messages), raw_count)
        return True

    def is_archived(self, session_id: str) -> bool:
        """会话是否处于归档状态（活目录无、归档目录有）

        供超管回溯反馈现场时标注来源：会话是用户已删除的（内容来自归档），
        还是仍在用户的会话列表里——"用户删了会话"本身也是判断反馈严重程度
        的信号。
        """
        return (not self._get_session_path(session_id).exists()
                and self._get_archived_path(session_id).exists())

    def rename_session(self, session_id: str, title: str) -> Optional[ChatSession]:
        """重命名会话（读 JSON → 改 title → 写回）；文件不存在或读取失败返回 None"""
        path = self._get_session_path(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            session = ChatSession(**data)
        except Exception as e:
            logger.warning("读取会话 %s 失败: %s", session_id, e)
            return None
        session.title = title
        self._save_session(session)
        logger.info("会话重命名: %s -> %s", session_id, title[:30])
        return session
